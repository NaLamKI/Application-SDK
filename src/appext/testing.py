"""Test helpers: an identity provider in a box, a test client with a session, service mocks.

Unit tests of an extension should not need Keycloak:

    from appext.testing import ExtensionTestClient, service_mocks, test_user
    from app.main import app, ext

    def test_projects():
        client = ExtensionTestClient(app, user=test_user(sub="u-1", roles=["analyst"]))
        with service_mocks(ext) as mocks:
            mocks.get("projects", "/v1/projects").respond(json=[])
            assert client.get("/api/projects").status_code == 200

A test run must not depend on the machine it runs on (the platform an operator configured, the
`APPEXT_*` variables of the shell): put `configure_test_environment()` in `tests/conftest.py`.

`ExtensionTestClient` creates the session directly in the extension's store (no
login), handles the CSRF header and `Origin`, and answers token exchanges with
a placeholder token, so the only thing a test has to mock is the target
service. `FakeIdP` is for testing the sign-in itself, and is what the SDK's own
tests use: an in-process OIDC provider that behaves like Keycloak where it
matters (strict redirect URIs, PKCE, consent bookkeeping, refresh-token
rotation, token exchange, back-channel logout) – including the ways Keycloak
refuses.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import os
import secrets
import threading
import time
import warnings
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

with warnings.catch_warnings():
    # Recent Starlette versions nag about `httpx` being used for the test client; httpx is what this
    # package depends on, so the nag is not the user's business.
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    from starlette.testclient import TestClient

from .manifest import load_manifest
from .core import ACCESS_TOKEN_TYPE, ASSERTION_TYPE, BACKCHANNEL_EVENT, TOKEN_EXCHANGE_GRANT
from .session import Session

if TYPE_CHECKING:
    from .app import Extension
    from .manifest import ServiceSpec

__all__ = [
    "ExtensionTestClient",
    "FakeClient",
    "FakeClock",
    "FakeIdP",
    "FakeUser",
    "ServiceMocks",
    "TestUser",
    "generate_client_key",
    "service_mocks",
    "test_user",
]

# Not a test module, although the name starts with "test_": keep pytest from collecting the helper.
__test__ = False


# --- time -----------------------------------------------------------------------------------------


class FakeClock:
    """A clock you move by hand. Hand the same instance to the extension and to `FakeIdP`."""

    def __init__(self, start: float = 1_900_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- keys ---------------------------------------------------------------------------------------------


def generate_client_key(alg: str = "RS256") -> tuple[str, dict[str, Any]]:
    """A key pair for `private_key_jwt`: `(private key PEM, public JWK)`."""
    if alg == "RS256":
        key: Any = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif alg == "ES256":
        key = ec.generate_private_key(ec.SECP256R1())
    else:
        raise ValueError("alg must be RS256 or ES256")
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    algorithm = jwt.algorithms.RSAAlgorithm if alg == "RS256" else jwt.algorithms.ECAlgorithm
    jwk = json.loads(algorithm.to_jwk(key.public_key()))
    jwk.update({"use": "sig", "alg": alg})
    return pem, jwk


#: The IdP checks expiry with its own (fakeable) clock, never the wall clock.
_NO_TIME_CHECKS = {"verify_aud": False, "verify_exp": False, "verify_iat": False, "verify_nbf": False}


@functools.cache
def _shared_rsa_key() -> rsa.RSAPrivateKey:
    # Generating an RSA key takes a noticeable fraction of a second; tests create many IdPs.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class _SigningKey:
    kid: str
    private: Any

    def jwk(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return jwk


# --- the fake identity provider ----------------------------------------------------------------------------


@dataclass
class FakeUser:
    sub: str
    username: str = ""
    name: str | None = None
    email: str | None = None
    realm_roles: list[str] = field(default_factory=list)
    client_roles: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class FakeClient:
    client_id: str
    redirect_uris: set[str] = field(default_factory=set)
    secret: str | None = None
    public_jwk: dict[str, Any] | None = None
    #: Default client scopes (always part of a login) and optional ones (only when requested).
    default_scopes: set[str] = field(default_factory=set)
    optional_scopes: set[str] = field(default_factory=set)
    #: scope -> audience the scope's audience mapper adds
    scope_audiences: dict[str, str] = field(default_factory=dict)
    consent_required: bool = True
    service_account: bool = False
    token_exchange_enabled: bool = True
    backchannel_logout_url: str | None = None
    post_logout_redirect_uris: set[str] = field(default_factory=set)

    @property
    def all_scopes(self) -> set[str]:
        return self.default_scopes | self.optional_scopes


@dataclass
class IdPRequest:
    """One call to the token endpoint, as the SDK made it (for assertions about request shape)."""

    grant_type: str
    form: dict[str, str]
    client_id: str | None
    authenticated_with: str | None
    assertion_claims: dict[str, Any] | None = None
    assertion_header: dict[str, Any] | None = None


@dataclass
class AuthorizeRecord:
    query: dict[str, str]
    user: str
    consent_shown: bool
    prompt: str | None


class InvalidAuthorizeRequest(Exception):
    """Keycloak shows an error page (and does not redirect) for these: unknown client, bad redirect URI."""


class FakeIdP:
    """An in-process OIDC provider with Keycloak's relevant behaviour.

    Wire it to the code under test through `idp.client()` (an `httpx.AsyncClient`
    whose transport is this app) and simulate the browser with `authorize()`.
    """

    def __init__(
        self,
        issuer: str = "https://idp.test/realms/test",
        *,
        clock: Callable[[], float] | None = None,
        access_lifetime: int = 300,
        refresh_lifetime: int = 1800,
        rotate_refresh_tokens: bool = True,
        token_delay: float = 0.0,
        consent_error: str = "invalid_scope",
    ) -> None:
        self.issuer = issuer.rstrip("/")
        parts = urlsplit(self.issuer)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.clock = clock or time.time
        self.access_lifetime = access_lifetime
        self.refresh_lifetime = refresh_lifetime
        self.rotate_refresh_tokens = rotate_refresh_tokens
        #: Seconds each token request takes: makes concurrent callers really overlap.
        self.token_delay = token_delay
        #: How a token exchange without consent is refused: Keycloak 26.5 says `invalid_scope` ("Missing
        #: consents for Token Exchange"), the concept (and older paths) say `access_denied`. Both must work.
        self.consent_error = consent_error
        self.backchannel_http: httpx.AsyncClient | None = None

        self.clients: dict[str, FakeClient] = {}
        self.users: dict[str, FakeUser] = {}
        self.consents: dict[tuple[str, str], set[str]] = {}
        #: sid -> {"sub", "clients"}; a refresh for a sid that is not here fails like Keycloak's "session not active".
        self.sessions: dict[str, dict[str, Any]] = {}
        self._sso_sid: dict[str, str] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self._seen_jti: set[str] = set()

        self.requests: list[IdPRequest] = []
        self.authorizations: list[AuthorizeRecord] = []
        self.discovery_requests = 0
        self.jwks_requests = 0
        self.logout_requests: list[dict[str, str]] = []
        self.backchannel_results: list[tuple[str, int]] = []
        self.refresh_count = 0

        self._keys = [_SigningKey("fake-key-1", _shared_rsa_key())]
        base = parts.path
        self.authorization_endpoint = f"{self.issuer}/protocol/openid-connect/auth"
        self.token_endpoint = f"{self.issuer}/protocol/openid-connect/token"
        self.jwks_uri = f"{self.issuer}/protocol/openid-connect/certs"
        self.end_session_endpoint = f"{self.issuer}/protocol/openid-connect/logout"
        self.app = Starlette(
            routes=[
                Route(f"{base}/.well-known/openid-configuration", self._discovery),
                Route(f"{base}/protocol/openid-connect/certs", self._certs),
                Route(f"{base}/protocol/openid-connect/token", self._token, methods=["POST"]),
                Route(f"{base}/protocol/openid-connect/logout", self._logout),
            ]
        )

    # -- setup -------------------------------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        """An HTTP client that talks to this IdP in-process."""
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.origin)

    def add_user(
        self,
        sub: str,
        *,
        name: str | None = None,
        email: str | None = None,
        realm_roles: Iterable[str] = (),
        client_roles: dict[str, list[str]] | None = None,
    ) -> FakeUser:
        user = FakeUser(sub, username=sub, name=name or sub, email=email or f"{sub}@example.test",
                        realm_roles=list(realm_roles), client_roles=client_roles or {})
        self.users[sub] = user
        return user

    def add_client(
        self,
        client_id: str,
        *,
        redirect_uris: Iterable[str],
        secret: str | None = None,
        public_jwk: dict[str, Any] | None = None,
        default_scopes: Iterable[str] = (),
        optional_scopes: Iterable[str] = (),
        scope_audiences: dict[str, str] | None = None,
        consent_required: bool = True,
        service_account: bool = False,
        token_exchange: bool = True,
        backchannel_logout_url: str | None = None,
        post_logout_redirect_uris: Iterable[str] = (),
    ) -> FakeClient:
        client = FakeClient(
            client_id=client_id,
            redirect_uris=set(redirect_uris),
            secret=secret,
            public_jwk=public_jwk,
            default_scopes=set(default_scopes),
            optional_scopes=set(optional_scopes),
            scope_audiences=dict(scope_audiences or {}),
            consent_required=consent_required,
            service_account=service_account,
            token_exchange_enabled=token_exchange,
            backchannel_logout_url=backchannel_logout_url,
            post_logout_redirect_uris=set(post_logout_redirect_uris),
        )
        self.clients[client_id] = client
        return client

    def rotate_keys(self) -> str:
        """Replace the signing key (JWKS then lists only the new one). Returns the new `kid`."""
        key = _SigningKey(f"fake-key-{len(self._keys) + 1}", rsa.generate_private_key(public_exponent=65537, key_size=2048))
        self._keys = [key]
        return key.kid

    # -- signing ------------------------------------------------------------------------------

    def sign(self, claims: dict[str, Any], *, key: _SigningKey | None = None, headers: dict[str, Any] | None = None) -> str:
        signer = key or self._keys[0]
        return jwt.encode(claims, signer.private, algorithm="RS256", headers={"kid": signer.kid, **(headers or {})})

    def mint_access_token(
        self,
        *,
        sub: str = "u-1",
        azp: str = "ext-demo",
        audience: str | list[str] = "demo-api",
        scope: str = "",
        lifetime: int | None = None,
        **extra: Any,
    ) -> str:
        """An access token as a target service would receive it (for `appext.verify` tests)."""
        now = int(self.clock())
        claims = {
            "iss": self.issuer, "sub": sub, "azp": azp, "aud": audience, "scope": scope,
            "iat": now, "exp": now + (self.access_lifetime if lifetime is None else lifetime), "jti": secrets.token_hex(8),
            **extra,
        }
        return self.sign(claims)

    def make_logout_token(
        self,
        client_id: str,
        *,
        sid: str | None = None,
        sub: str | None = None,
        with_event: bool = True,
        nonce: str | None = None,
        audience: str | None = None,
        issuer: str | None = None,
        iat: float | None = None,
        key: _SigningKey | None = None,
    ) -> str:
        claims: dict[str, Any] = {
            "iss": issuer or self.issuer,
            "aud": audience or client_id,
            "iat": int(self.clock() if iat is None else iat),
            "jti": secrets.token_hex(8),
        }
        if sub:
            claims["sub"] = sub
        if sid:
            claims["sid"] = sid
        if with_event:
            claims["events"] = {BACKCHANNEL_EVENT: {}}
        if nonce:
            claims["nonce"] = nonce
        return self.sign(claims, key=key)

    # -- the browser's part ----------------------------------------------------------------------------

    def authorize(self, url: str, user: str | FakeUser, *, consent: bool = True) -> str:
        """What the browser does at the authorization endpoint: returns the URL it is redirected to.

        `consent=False` answers the consent screen with "no" (Keycloak: `error=access_denied`).
        """
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        client = self.clients.get(query.get("client_id", ""))
        if client is None:
            raise InvalidAuthorizeRequest("unknown client")
        redirect_uri = query.get("redirect_uri", "")
        if redirect_uri not in client.redirect_uris:
            raise InvalidAuthorizeRequest(f"invalid redirect_uri {redirect_uri!r}")
        state = query.get("state", "")

        def back(**params: str) -> str:
            params = {**params, "state": state, "iss": self.issuer}
            separator = "&" if "?" in redirect_uri else "?"
            return redirect_uri + separator + urlencode(params)

        person = user if isinstance(user, FakeUser) else self.users[user]
        if query.get("response_type") != "code":
            return back(error="unsupported_response_type")
        if query.get("code_challenge_method") != "S256" or not query.get("code_challenge"):
            return back(error="invalid_request", error_description="Missing parameter: code_challenge_method")
        requested = query.get("scope", "").split()
        unknown = [s for s in requested if s != "openid" and s not in client.all_scopes]
        if unknown:
            return back(error="invalid_scope", error_description="Invalid scopes: " + " ".join(unknown))

        granted = {*requested, *client.default_scopes}
        asked = {s for s in granted if s != "openid"}
        have = self.consents.setdefault((person.sub, client.client_id), set())
        needs_consent = client.consent_required and not asked <= have
        self.authorizations.append(AuthorizeRecord(query, person.sub, needs_consent, query.get("prompt")))
        if needs_consent:
            if not consent:
                return back(error="access_denied", error_description="User denied consent")
            have |= asked

        if query.get("prompt") == "login" or person.sub not in self._sso_sid or self._sso_sid[person.sub] not in self.sessions:
            sid = secrets.token_hex(12)
            self._sso_sid[person.sub] = sid
            self.sessions[sid] = {"sub": person.sub, "clients": set()}
        sid = self._sso_sid[person.sub]
        self.sessions[sid]["clients"].add(client.client_id)

        code = secrets.token_urlsafe(24)
        self.codes[code] = {
            "client_id": client.client_id, "sub": person.sub, "sid": sid, "scopes": sorted(granted),
            "nonce": query.get("nonce"), "challenge": query["code_challenge"], "redirect_uri": redirect_uri,
            "exp": self.clock() + 60,
        }
        return back(code=code)

    # -- logout ----------------------------------------------------------------------------------------

    async def end_session(self, sid: str) -> list[tuple[str, int]]:
        """End an SSO session; tell every client with a back-channel URL (needs `backchannel_http`)."""
        session = self.sessions.pop(sid, None)
        for sso, current in list(self._sso_sid.items()):
            if current == sid:
                del self._sso_sid[sso]
        for record in self.refresh_tokens.values():
            if record["sid"] == sid:
                record["used"] = True
        results: list[tuple[str, int]] = []
        if session and self.backchannel_http is not None:
            for client_id in sorted(session["clients"]):
                client = self.clients[client_id]
                if client.backchannel_logout_url:
                    response = await self.send_backchannel_logout(client_id, sid=sid, sub=session["sub"])
                    results.append((client_id, response.status_code))
        self.backchannel_results.extend(results)
        return results

    async def send_backchannel_logout(
        self, client_id: str, *, sid: str | None = None, sub: str | None = None, token: str | None = None
    ) -> httpx.Response:
        """POST a logout token to the client's back-channel URL (Keycloak's form encoding)."""
        assert self.backchannel_http is not None, "set idp.backchannel_http to an AsyncClient that reaches the extension"
        client = self.clients[client_id]
        assert client.backchannel_logout_url, "the client has no backchannel_logout_url"
        token = token or self.make_logout_token(client_id, sid=sid, sub=sub)
        return await self.backchannel_http.post(client.backchannel_logout_url, data={"logout_token": token})

    async def _logout(self, request: Request) -> Response:
        params = dict(request.query_params)
        self.logout_requests.append(params)
        sid = None
        hint = params.get("id_token_hint")
        if hint:
            try:
                sid = jwt.decode(hint, self._keys[0].private.public_key(), algorithms=["RS256"], options=_NO_TIME_CHECKS).get("sid")
            except jwt.PyJWTError:
                return JSONResponse({"error": "invalid_request"}, status_code=400)
        if sid:
            await self.end_session(sid)
        target = params.get("post_logout_redirect_uri")
        client = self.clients.get(params.get("client_id", ""))
        if target and client and target in client.post_logout_redirect_uris:
            return RedirectResponse(target, status_code=302)
        return JSONResponse({"status": "logged out"})

    # -- endpoints ----------------------------------------------------------------------------------------

    async def _discovery(self, request: Request) -> Response:
        self.discovery_requests += 1
        return JSONResponse(
            {
                "issuer": self.issuer,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "jwks_uri": self.jwks_uri,
                "end_session_endpoint": self.end_session_endpoint,
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials", TOKEN_EXCHANGE_GRANT],
                "code_challenge_methods_supported": ["S256"],
                "backchannel_logout_supported": True,
                "backchannel_logout_session_supported": True,
            }
        )

    async def _certs(self, request: Request) -> Response:
        self.jwks_requests += 1
        return JSONResponse({"keys": [k.jwk() for k in self._keys]})

    def _error(self, error: str, description: str, status: int = 400) -> Response:
        return JSONResponse({"error": error, "error_description": description}, status_code=status)

    def _authenticate(self, request: Request, form: dict[str, str]) -> tuple[FakeClient | None, str | None, Response | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Client authentication as Keycloak does it: `(client, method, error, assertion claims, assertion header)`."""
        client_id = form.get("client_id")
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            raw = base64.b64decode(header[6:]).decode()
            client_id, _, secret = raw.partition(":")
            client = self.clients.get(client_id)
            if client is None or client.secret is None or not secrets.compare_digest(secret, client.secret):
                return None, None, self._error("invalid_client", "Invalid client or Invalid client credentials", 401), None, None
            return client, "client_secret_basic", None, None, None
        if "client_assertion" in form:
            if form.get("client_assertion_type") != ASSERTION_TYPE:
                return None, None, self._error("invalid_client", "Invalid client assertion type", 401), None, None
            assertion = form["client_assertion"]
            try:
                unverified = jwt.decode(assertion, options={"verify_signature": False})
                client = self.clients.get(unverified.get("iss", ""))
                if client is None or client.public_jwk is None:
                    raise jwt.InvalidTokenError("unknown client")
                if client_id and client_id != client.client_id:
                    raise jwt.InvalidTokenError("client_id does not match the assertion")
                alg = jwt.get_unverified_header(assertion).get("alg")
                claims = jwt.decode(
                    assertion, jwt.PyJWK(client.public_jwk).key, algorithms=[alg],
                    options={**_NO_TIME_CHECKS, "require": ["iss", "sub", "exp", "jti", "aud"]},
                )
                if claims["iss"] != client.client_id or claims["sub"] != client.client_id:
                    raise jwt.InvalidTokenError("iss and sub must be the client id")
                audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
                if self.token_endpoint not in audiences and self.issuer not in audiences:
                    raise jwt.InvalidTokenError("aud must be the token endpoint")
                if claims["exp"] <= self.clock():
                    raise jwt.InvalidTokenError("assertion expired")
                if claims["jti"] in self._seen_jti:
                    raise jwt.InvalidTokenError("assertion replayed")
                self._seen_jti.add(claims["jti"])
            except (jwt.PyJWTError, KeyError, ValueError) as err:
                return None, None, self._error("invalid_client", f"Client authentication failed: {err}", 401), None, None
            return client, "private_key_jwt", None, claims, jwt.get_unverified_header(assertion)
        if "client_secret" in form:
            client = self.clients.get(client_id or "")
            if client is None or client.secret is None or not secrets.compare_digest(form["client_secret"], client.secret):
                return None, None, self._error("invalid_client", "Invalid client or Invalid client credentials", 401), None, None
            return client, "client_secret_post", None, None, None
        return None, None, self._error("invalid_client", "Invalid client or Invalid client credentials", 401), None, None

    async def _token(self, request: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        client, method, error, claims, header = self._authenticate(request, form)
        self.requests.append(
            IdPRequest(form.get("grant_type", ""), form, client.client_id if client else form.get("client_id"), method, claims, header)
        )
        if error is not None or client is None:
            return error or self._error("invalid_client", "no client")
        if self.token_delay:
            await asyncio.sleep(self.token_delay)

        grant = form.get("grant_type")
        if grant == "authorization_code":
            return self._grant_code(client, form)
        if grant == "refresh_token":
            return self._grant_refresh(client, form)
        if grant == TOKEN_EXCHANGE_GRANT:
            return self._grant_exchange(client, form)
        if grant == "client_credentials":
            return self._grant_credentials(client, form)
        return self._error("unsupported_grant_type", f"Unsupported grant type: {grant}")

    # -- grants --------------------------------------------------------------------------------------------

    def _grant_code(self, client: FakeClient, form: dict[str, str]) -> Response:
        record = self.codes.pop(form.get("code", ""), None)  # single use
        if record is None or record["exp"] < self.clock():
            return self._error("invalid_grant", "Code not valid")
        if record["client_id"] != client.client_id or record["redirect_uri"] != form.get("redirect_uri"):
            return self._error("invalid_grant", "Incorrect redirect_uri or client")
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not secrets.compare_digest(challenge, record["challenge"]):
            return self._error("invalid_grant", "PKCE verification failed")
        if record["sid"] not in self.sessions:
            return self._error("invalid_grant", "Session not active")
        user = self.users[record["sub"]]
        return JSONResponse(self._issue(client, user, record["sid"], set(record["scopes"]), nonce=record["nonce"]))

    def _grant_refresh(self, client: FakeClient, form: dict[str, str]) -> Response:
        token = form.get("refresh_token", "")
        record = self.refresh_tokens.get(token)
        if record is None or record["client_id"] != client.client_id:
            return self._error("invalid_grant", "Invalid refresh token")
        if record["sid"] not in self.sessions:
            return self._error("invalid_grant", "Session not active")
        if record["used"] and self.rotate_refresh_tokens:
            # Keycloak: a rotated-away refresh token shown again means it leaked; the whole session goes.
            self.sessions.pop(record["sid"], None)
            return self._error("invalid_grant", "Maximum allowed refresh token reuse exceeded")
        if record["exp"] <= self.clock():
            return self._error("invalid_grant", "Token is not active")
        record["used"] = True
        self.refresh_count += 1
        user = self.users[record["sub"]]
        return JSONResponse(self._issue(client, user, record["sid"], set(record["scopes"])))

    def _grant_exchange(self, client: FakeClient, form: dict[str, str]) -> Response:
        if not client.token_exchange_enabled:
            return self._error("unauthorized_client", "Client not allowed to exchange", 403)
        if form.get("subject_token_type") != ACCESS_TOKEN_TYPE:
            return self._error("invalid_request", "subject_token_type must be an access token")
        try:
            subject = jwt.decode(
                form.get("subject_token", ""), self._keys[0].private.public_key(), algorithms=["RS256"],
                options=_NO_TIME_CHECKS,
            )
        except jwt.PyJWTError:
            return self._error("invalid_token", "Invalid token")
        if subject["exp"] <= self.clock() or subject.get("sid") not in self.sessions:
            return self._error("invalid_token", "Invalid token")
        audiences = subject["aud"] if isinstance(subject["aud"], list) else [subject["aud"]]
        if subject.get("azp") != client.client_id and client.client_id not in audiences:
            return self._error("access_denied", "Client is not within the token audience", 403)

        scopes = form.get("scope", "").split()
        audience = form.get("audience", "")
        unknown = [s for s in scopes if s not in client.all_scopes]
        if unknown:
            return self._error("invalid_scope", "Invalid scopes: " + " ".join(unknown))
        if client.consent_required:
            granted = self.consents.get((subject["sub"], client.client_id), set())
            if not set(scopes) <= granted:
                if self.consent_error == "access_denied":
                    return self._error("access_denied", "Client requires user consent")
                return self._error("invalid_scope", f"Missing consents for Token Exchange in client {client.client_id}")
        available = {client.scope_audiences[s] for s in scopes if s in client.scope_audiences}
        if audience and audience not in available:
            return self._error("invalid_request", f"Requested audience not available: {audience}")
        user = self.users[subject["sub"]]
        token = self._access_token(client, user, subject["sid"], set(scopes), audience=[audience] if audience else sorted(available))
        return JSONResponse(
            {"access_token": token, "token_type": "Bearer", "expires_in": self.access_lifetime,
             "issued_token_type": ACCESS_TOKEN_TYPE, "scope": " ".join(scopes)}
        )

    def _grant_credentials(self, client: FakeClient, form: dict[str, str]) -> Response:
        if not client.service_account:
            return self._error("unauthorized_client", "Client not enabled to retrieve service account", 401)
        scopes = form.get("scope", "").split()
        unknown = [s for s in scopes if s not in client.all_scopes]
        if unknown:
            return self._error("invalid_scope", "Invalid scopes: " + " ".join(unknown))
        audiences = sorted({client.scope_audiences[s] for s in scopes if s in client.scope_audiences})
        now = int(self.clock())
        claims = {
            "iss": self.issuer, "sub": f"service-account-{client.client_id}", "azp": client.client_id, "aud": audiences,
            "scope": " ".join(scopes), "iat": now, "exp": now + self.access_lifetime, "jti": secrets.token_hex(8),
        }
        return JSONResponse(
            {"access_token": self.sign(claims), "token_type": "Bearer", "expires_in": self.access_lifetime, "scope": " ".join(scopes)}
        )

    # -- token building -------------------------------------------------------------------------------------

    def _access_token(self, client: FakeClient, user: FakeUser, sid: str, scopes: set[str], *, audience: list[str] | None = None) -> str:
        now = int(self.clock())
        claims: dict[str, Any] = {
            "iss": self.issuer, "sub": user.sub, "azp": client.client_id, "sid": sid,
            "aud": audience if audience is not None else sorted({"account", *(client.scope_audiences[s] for s in scopes if s in client.scope_audiences)}),
            "scope": " ".join(sorted(scopes)), "typ": "Bearer", "iat": now, "exp": now + self.access_lifetime, "jti": secrets.token_hex(8),
        }
        if user.realm_roles:
            claims["realm_access"] = {"roles": user.realm_roles}
        if user.client_roles:
            claims["resource_access"] = {c: {"roles": r} for c, r in user.client_roles.items()}
        return self.sign(claims)

    def _issue(self, client: FakeClient, user: FakeUser, sid: str, scopes: set[str], *, nonce: str | None = None) -> dict[str, Any]:
        now = int(self.clock())
        id_claims: dict[str, Any] = {
            "iss": self.issuer, "sub": user.sub, "aud": client.client_id, "azp": client.client_id, "sid": sid,
            "iat": now, "exp": now + self.access_lifetime, "auth_time": now,
        }
        if nonce:
            id_claims["nonce"] = nonce
        if "profile" in scopes:
            id_claims.update(name=user.name, preferred_username=user.username)
        if "email" in scopes:
            id_claims["email"] = user.email
        refresh = "rt-" + secrets.token_urlsafe(24)
        self.refresh_tokens[refresh] = {
            "client_id": client.client_id, "sub": user.sub, "sid": sid, "scopes": sorted(scopes),
            "exp": self.clock() + self.refresh_lifetime, "used": False,
        }
        return {
            "access_token": self._access_token(client, user, sid, scopes),
            "expires_in": self.access_lifetime,
            "refresh_token": refresh,
            "refresh_expires_in": self.refresh_lifetime,
            "token_type": "Bearer",
            "id_token": self.sign(id_claims),
            "scope": " ".join(sorted(scopes)),
        }

    # -- convenience for assertions -----------------------------------------------------------------------------

    def token_requests(self, grant_type: str) -> list[IdPRequest]:
        return [r for r in self.requests if r.grant_type == grant_type]


# --- test users, test client ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TestUser:
    __test__ = False  # not a pytest class

    sub: str = "test-user"
    name: str | None = "Test User"
    email: str | None = "test.user@example.test"
    preferred_username: str | None = "test-user"
    roles: tuple[str, ...] = ()
    client_roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ("openid",)


#: The issuer a test run uses unless it is told otherwise: the address `FakeIdP` answers for.
TEST_ISSUER = "https://idp.test/realms/test"
#: The host app's return address in tests (any URI with a scheme will do; nothing opens it).
TEST_APP_REDIRECT_URI = "com.example.testapp:/callback"


def configure_test_environment(
    manifest: str | os.PathLike[str] = "extension.toml",
    *,
    environ: dict[str, str] | None = None,
    issuer: str = TEST_ISSUER,
    app_redirect_uri: str | None = TEST_APP_REDIRECT_URI,
) -> dict[str, str]:
    """Make the environment of a test run the same everywhere. Call it before the app is imported.

    Removes every `APPEXT_*` variable (a shell that exports a deployment's settings must not change
    a test), then sets what a local run needs: `APPEXT_ENV=local`, an issuer, the host app's return
    address and, for every service the manifest declares, `APPEXT_SERVICE_<NAME>_URL` – so that
    `service_mocks` knows where the service "is". No platform file is read and nothing leaves the machine.
    Returns what it set.

        # tests/conftest.py
        from appext.testing import configure_test_environment
        configure_test_environment("extension.toml")
    """
    target = os.environ if environ is None else environ
    for name in [n for n in target if n.startswith("APPEXT_")]:
        del target[name]
    values = {"APPEXT_ENV": "local", "APPEXT_ISSUER": issuer}
    if app_redirect_uri:
        values["APPEXT_APP_REDIRECT_URI"] = app_redirect_uri
    for service in load_manifest(manifest).services:
        values[f"APPEXT_SERVICE_{service.env_name}_URL"] = f"https://{service.name.replace('_', '-')}.test/api"
    target.update(values)
    return values


def test_user(
    sub: str = "test-user",
    *,
    name: str | None = "Test User",
    email: str | None = "test.user@example.test",
    roles: Iterable[str] = (),
    client_roles: Iterable[str] = (),
    scopes: Iterable[str] = ("openid",),
    preferred_username: str | None = None,
) -> TestUser:
    """A person for `ExtensionTestClient`. `roles` are realm roles. Pass `name=None` to test the "no profile scope" case."""
    return TestUser(
        sub=sub, name=name, email=email, preferred_username=preferred_username or sub, roles=tuple(roles),
        client_roles=tuple(client_roles), scopes=tuple(scopes),
    )


test_user.__test__ = False  # type: ignore[attr-defined]


def _run(coro: Awaitable[Any]) -> Any:
    """Run a coroutine to completion from synchronous code, also when a loop is already running (async tests)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as err:  # noqa: BLE001
            box["error"] = err

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


class ExtensionTestClient(TestClient):
    """`starlette.testclient.TestClient` for an extension, signed in as `user`.

    * The session is created straight in the extension's store: no login round trip.
    * Writing requests carry the CSRF header and a matching `Origin` by default;
      pass your own `headers=` to test what happens without them.
    * Token exchanges are answered with a placeholder token (`test-token-<audience>`),
      recorded in `token_requests`; mock the *service* with `service_mocks`.
    * `user=None` gives an anonymous client (to test the 401 behaviour).

    Call `close()` (or use `with`) to restore the extension's real token source.
    """

    __test__ = False

    def __init__(self, app: Any, user: TestUser | None = None, **kwargs: Any) -> None:
        extension = getattr(getattr(app, "state", None), "appext", None)
        if extension is None:
            raise TypeError("ExtensionTestClient needs the application returned by Extension.asgi()")
        self.extension: Extension = extension
        settings = self.extension.settings
        kwargs.setdefault("base_url", settings.public_url)
        super().__init__(app, **kwargs)
        self.user = user
        self.token_requests: list[tuple[str, str, tuple[str, ...], str]] = []
        self.headers.update({"Origin": settings.origin, "X-Appext-CSRF": "1"})
        self._original_token_source = self.extension.token_source
        self.extension.token_source = self._placeholder_token
        self.session_id: str | None = None
        if user is not None:
            self.session_id = self._create_session(user)
            self.cookies.set(settings.session_cookie_name, self.session_id, domain=self.base_url.host)

    def _create_session(self, user: TestUser) -> str:
        now = self.extension.core.clock()
        claims: dict[str, Any] = {"sub": user.sub}
        if user.name is not None:
            claims["name"] = user.name
        if user.email is not None:
            claims["email"] = user.email
        if user.preferred_username is not None:
            claims["preferred_username"] = user.preferred_username
        session = Session(
            sub=user.sub, access_token="test-login-token", access_expires_at=now + 3600, claims=claims,
            scopes=list(user.scopes), realm_roles=list(user.roles), client_roles=list(user.client_roles), created_at=now,
        )
        return _run(self.extension.store.create_session(session, 3600))

    async def _placeholder_token(self, spec: ServiceSpec, session: Session | None, bypass: bool) -> str:
        self.token_requests.append((spec.name, spec.audience, spec.scopes, spec.mode))
        return f"test-token-{spec.audience}"

    def close(self) -> None:
        self.extension.token_source = self._original_token_source
        super().close()


# --- service mocks ---------------------------------------------------------------------------------------------


class ServiceMocks:
    """`respx` routes addressed by service name and path, resolved against the configured base URLs."""

    def __init__(self, extension: Extension, router: Any) -> None:
        self.extension = extension
        self.router = router

    def url(self, service: str, path: str) -> str:
        base = self.extension.settings.services.get(service)
        if not base:
            raise KeyError(f"no URL configured for service {service!r}: set APPEXT_SERVICE_{service.upper()}_URL")
        return f"{base}/{path.lstrip('/')}"

    def route(self, method: str, service: str, path: str, **kwargs: Any) -> Any:
        return self.router.route(method=method, url=self.url(service, path), **kwargs)

    def get(self, service: str, path: str, **kwargs: Any) -> Any:
        return self.route("GET", service, path, **kwargs)

    def post(self, service: str, path: str, **kwargs: Any) -> Any:
        return self.route("POST", service, path, **kwargs)

    def put(self, service: str, path: str, **kwargs: Any) -> Any:
        return self.route("PUT", service, path, **kwargs)

    def delete(self, service: str, path: str, **kwargs: Any) -> Any:
        return self.route("DELETE", service, path, **kwargs)


@contextmanager
def service_mocks(extension: Extension, *, assert_all_called: bool = False) -> Iterator[ServiceMocks]:
    """Mock the extension's target services (needs `respx`, part of `appext[test]`).

    Anything not mocked fails the test instead of reaching the network.
    """
    try:
        import respx
    except ImportError:
        raise RuntimeError("service_mocks needs respx: pip install 'appext[test]'") from None
    with respx.mock(assert_all_called=assert_all_called, assert_all_mocked=True) as router:
        yield ServiceMocks(extension, router)
