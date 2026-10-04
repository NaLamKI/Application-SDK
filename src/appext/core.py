"""The framework-free heart: OIDC login, refresh, token exchange, logout.

Nothing in here knows FastAPI or cookies. It takes parameters and a
`SessionStore`, talks to the IdP with `httpx`, and returns plain results, so an
adapter for another framework only has to translate requests and responses.

What the module guarantees, in the order a login lives through it:

1. `start_login` creates `state`, `nonce` and a PKCE verifier, keeps them in the
   store under `state`, and returns the authorize URL. The redirect URI depends
   on the *transaction* (app or browser), nothing else does.
2. `handle_callback` consumes the transaction (single use), requires that the
   caller's transaction cookie equals `state`, redeems the code with client
   authentication, validates the ID token (signature, `iss`, `aud`, `nonce`,
   `exp`) and matches the account against the app's `app_sub` cookie.
3. `fresh_session` renews the access token **under a per-session lock**. With
   refresh-token rotation a second concurrent refresh would present an already
   used token and Keycloak would end the whole session; the lock plus a re-read
   after acquiring it makes "two callers, one refresh" the only possible outcome.
4. `exchange_token` swaps the session's own access token for one that is cut to a
   single service (RFC 8693), cached until shortly before it expires.
5. `handle_backchannel_logout` verifies a logout token and ends every session
   of the `sid`.

Tokens are never logged here, and error messages carry the IdP's `error` code
and description only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from .config import ConfigError, ExtensionSettings
from .jwks import JWKSCache, JWKSUnavailable, TokenInvalid, decode_jwt
from .manifest import Manifest
from .session import Session, SessionStore

log = logging.getLogger("appext")

#: The app appends this to the user agent of its WebView. Not a security feature,
#: only the switch for the redirect URI (a forged marker yields an address the
#: forger's browser cannot open).
APP_MARKER = "FMIS-App-WebView/"

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
BACKCHANNEL_EVENT = "http://schemas.openid.net/event/backchannel-logout"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
TRANSACTION_TTL = 600.0


# --- errors -----------------------------------------------------------------------------------------


class AuthError(Exception):
    """Base of everything the core raises on purpose."""


class LoginError(AuthError):
    """The callback cannot be completed. `code` is stable, `message` is for people."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class LoginDenied(LoginError):
    """The IdP answered the authorize request with an error – typically `access_denied`.

    Keycloak reports `access_denied` both for refused consent *and* for a closed
    sheet, so the wording must fit both.
    """

    def __init__(self, error: str, description: str | None = None) -> None:
        super().__init__(error, description or error, status=403 if error == "access_denied" else 400)
        self.error = error


class SessionInvalid(AuthError):
    """There is no (longer a) usable session: unknown id, ended elsewhere, refresh refused."""


class ConsentRequired(AuthError):
    """The person has not consented to these scopes (yet): sign in again, asking for them."""

    def __init__(self, missing_scopes: Sequence[str]) -> None:
        self.missing_scopes = tuple(missing_scopes)
        super().__init__("consent is required for: " + ", ".join(self.missing_scopes))


class UpstreamError(AuthError):
    """The IdP is unreachable or answered something unusable."""


class TokenEndpointError(AuthError):
    """The token endpoint refused a request. Only `error` and `error_description` are kept."""

    def __init__(self, status: int, error: str, description: str | None = None) -> None:
        self.status = status
        self.error = error
        self.description = description
        super().__init__(f"token endpoint: {error}" + (f" ({description})" if description else "") + f" [HTTP {status}]")


class ExchangeFailed(AuthError):
    """A token exchange or client-credentials request failed for a reason other than consent."""

    def __init__(self, audience: str, error: str, description: str | None = None) -> None:
        self.audience = audience
        self.error = error
        super().__init__(f"could not get a token for {audience!r}: {error}" + (f" ({description})" if description else ""))


class LogoutTokenError(AuthError):
    """A back-channel logout request that must be rejected."""


# --- small pure helpers -------------------------------------------------------------------------------


def is_app_mode(user_agent: str | None) -> bool:
    return bool(user_agent) and APP_MARKER in user_agent  # type: ignore[operator]


def safe_return_to(value: str | None, default: str = "/") -> str:
    """Accept only a path on our own origin; anything else becomes `default`.

    Open-redirect attempts all look like "a path" to a naive check: `//evil`,
    `/\\evil` and `/%09/evil` (browsers drop tabs and newlines and read the rest
    as a host), a scheme (`https://evil`, `javascript:`). One leading slash,
    no backslash, no control character – and never back into `/auth/…`, which
    would only start the next login.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    if any(ch == "\\" or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    if parts.path == "/auth" or parts.path.startswith("/auth/"):
        return default
    return value


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _split_scope(value: Any) -> list[str]:
    return value.split() if isinstance(value, str) else []


def _roles(claims: dict[str, Any], client_id: str) -> tuple[list[str], list[str]]:
    realm = (claims.get("realm_access") or {}).get("roles") or []
    client = ((claims.get("resource_access") or {}).get(client_id) or {}).get("roles") or []
    return [r for r in realm if isinstance(r, str)], [r for r in client if isinstance(r, str)]


@dataclass(frozen=True)
class AccessToken:
    """A bearer token for one service. `repr` hides the token."""

    token: str = field(repr=False)
    expires_at: float
    audience: str = ""


@dataclass(frozen=True)
class LoginRedirect:
    """Where to send the browser, and the `state` the transaction cookie must carry."""

    url: str
    state: str
    app_mode: bool


@dataclass
class LoginResult:
    """Outcome of a callback: a new session, or a restart because the account did not match."""

    return_to: str = "/"
    session_id: str | None = None
    session: Session | None = None
    ttl: float = 0.0
    restart: LoginRedirect | None = None


# --- client authentication ------------------------------------------------------------------------------


class ClientAuthenticator:
    """Authenticates the extension at the token endpoint.

    `private_key_jwt`: a signed assertion per request (`iss = sub = client id`,
    `aud = token endpoint`, unique `jti`, one minute to live), so no secret is
    ever sent. `client_secret`: the secret in the form body.
    """

    ASSERTION_LIFETIME = 60

    def __init__(self, settings: ExtensionSettings, clock: Callable[[], float]) -> None:
        self.settings = settings
        self.clock = clock
        self._key: tuple[Any, str, str | None] | None = None

    def fields(self, token_endpoint: str) -> dict[str, str]:
        s = self.settings
        if s.client_auth == "client_secret":
            if not s.client_secret:
                raise ConfigError(["APPEXT_CLIENT_SECRET_FILE is required for client_auth=client_secret"])
            return {"client_id": s.client_id, "client_secret": str(s.client_secret.reveal())}
        key, alg, kid = self._load_key()
        now = int(self.clock())
        claims = {
            "iss": s.client_id,
            "sub": s.client_id,
            "aud": token_endpoint,
            "jti": secrets.token_urlsafe(16),
            "iat": now,
            "exp": now + self.ASSERTION_LIFETIME,
        }
        headers = {"kid": kid} if kid else None
        assertion = jwt.encode(claims, key, algorithm=alg, headers=headers)
        return {"client_id": s.client_id, "client_assertion_type": ASSERTION_TYPE, "client_assertion": assertion}

    def _load_key(self) -> tuple[Any, str, str | None]:
        if self._key is None:
            self._key = load_private_key(self.settings.client_key, self.settings.client_key_id)
        return self._key


def load_private_key(secret: Any, key_id: str | None = None) -> tuple[Any, str, str | None]:
    """Parse the client key (PEM or JWK JSON) into `(key, JWT algorithm, kid)`.

    The `kid` goes into the assertion header only when it was given (in the JWK
    or `APPEXT_CLIENT_KEY_ID`): it must match what was uploaded to the store,
    and a made-up one would not.
    """
    if not secret:
        raise ConfigError(["APPEXT_CLIENT_KEY_FILE is required for client_auth=private_key_jwt"])
    text = str(secret.reveal()).strip()
    kid = key_id
    try:
        if text.startswith("{"):
            jwk = json.loads(text)
            kid = kid or jwk.get("kid")
            if jwk.get("kty") == "RSA":
                key: Any = jwt.algorithms.RSAAlgorithm.from_jwk(text)
            elif jwk.get("kty") == "EC":
                key = jwt.algorithms.ECAlgorithm.from_jwk(text)
            else:
                raise ValueError("unsupported key type")
        else:
            key = serialization.load_pem_private_key(text.encode(), password=None)
    except (ValueError, TypeError, jwt.PyJWTError) as err:
        raise ConfigError([f"APPEXT_CLIENT_KEY_FILE is not a usable private key: {err}"]) from None
    if not hasattr(key, "sign"):
        raise ConfigError(["APPEXT_CLIENT_KEY_FILE holds a public key; the private key is needed to sign"])
    if isinstance(key, rsa.RSAPrivateKey):
        return key, "RS256", kid
    if isinstance(key, ec.EllipticCurvePrivateKey):
        algs = {"secp256r1": "ES256", "secp384r1": "ES384", "secp521r1": "ES512"}
        alg = algs.get(key.curve.name)
        if alg:
            return key, alg, kid
    raise ConfigError(["APPEXT_CLIENT_KEY_FILE: only RSA and EC (P-256, P-384, P-521) keys are supported"])


# --- the core ---------------------------------------------------------------------------------------------


class AuthCore:
    def __init__(
        self,
        settings: ExtensionSettings,
        manifest: Manifest,
        store: SessionStore,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        refresh_skew: float = 30.0,
        discovery_ttl: float = 3600.0,
        leeway: float = 60.0,
        jwks_min_refetch: float = 10.0,
    ) -> None:
        self.settings = settings
        self.manifest = manifest
        self.store = store
        self.clock = clock
        self.refresh_skew = refresh_skew
        self.leeway = leeway
        self.discovery_ttl = discovery_ttl
        self._owns_http = http is None
        self._http = http
        self.client_auth = ClientAuthenticator(settings, clock)
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self.jwks = JWKSCache(self._jwks_uri, lambda: self.http, clock=clock, min_refetch=jwks_min_refetch)

    @property
    def http(self) -> httpx.AsyncClient:
        """The client for IdP calls. One we created is re-created after `aclose()` (a test client may restart the app)."""
        if self._http is None or (self._owns_http and self._http.is_closed):
            self._http = httpx.AsyncClient(timeout=self.settings.http_timeout, follow_redirects=False)
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    # -- discovery -----------------------------------------------------------------------------

    async def discovery(self, *, fresh: bool = False) -> dict[str, Any]:
        """The IdP's endpoints, cached. `fresh=True` forces a fetch (readiness check)."""
        now = self.clock()
        if not fresh and self._discovery is not None and now - self._discovery_at < self.discovery_ttl:
            return self._discovery
        url = f"{self.settings.issuer}/.well-known/openid-configuration"
        try:
            response = await self.http.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            doc = response.json()
        except (httpx.HTTPError, ValueError) as err:
            if self._discovery is not None and not fresh:
                log.warning("OIDC discovery refresh failed (%s); using the cached document", type(err).__name__)
                return self._discovery
            raise UpstreamError(f"OIDC discovery failed ({type(err).__name__})") from None
        if not isinstance(doc, dict) or doc.get("issuer", "").rstrip("/") != self.settings.issuer:
            raise UpstreamError("OIDC discovery: the issuer in the document is not the configured one")
        for needed in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not doc.get(needed):
                raise UpstreamError(f"OIDC discovery: {needed} is missing")
        self._discovery, self._discovery_at = doc, now
        return doc

    async def _jwks_uri(self) -> str:
        return (await self.discovery())["jwks_uri"]

    async def _verify(self, token: str, *, audience: str | None, require: tuple[str, ...]) -> dict[str, Any]:
        try:
            key, alg = await self.jwks.key_for(token)
        except JWKSUnavailable as err:
            raise UpstreamError(str(err)) from None
        return decode_jwt(
            token, key, alg, issuer=self.settings.issuer, audience=audience, now=self.clock(), leeway=self.leeway, require=require
        )

    # -- login -------------------------------------------------------------------------------------

    def login_scopes(self, extra: Iterable[str] = ()) -> list[str]:
        """`openid`, the consent scopes and the scopes of every user-mode service.

        Asking for the service scopes **now** is the point: Keycloak lets a token
        exchange pass only for scopes the person already consented to.
        """
        declared = set(self.manifest.all_scopes)
        wanted = ["openid", *self.manifest.consent.scopes, *self.manifest.user_service_scopes]
        wanted += [s for s in extra if s in declared]  # a caller cannot widen beyond the manifest
        return list(dict.fromkeys(wanted))

    async def start_login(
        self,
        *,
        user_agent: str | None,
        return_to: str | None = None,
        prompt: str | None = None,
        extra_scopes: Iterable[str] = (),
        app_mode: bool | None = None,
    ) -> LoginRedirect:
        endpoints = await self.discovery()
        app = is_app_mode(user_agent) if app_mode is None else app_mode
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        redirect_uri = self.settings.redirect_uri(app)
        await self.store.put_transaction(
            state,
            {
                "verifier": verifier,
                "nonce": nonce,
                "redirect_uri": redirect_uri,
                "return_to": safe_return_to(return_to),
                "app_mode": app,
            },
            TRANSACTION_TTL,
        )
        params = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.login_scopes(extra_scopes)),
            "state": state,
            "nonce": nonce,
            "code_challenge": _s256(verifier),
            "code_challenge_method": "S256",
            # Explicit although it is the default: form_post never arrives over a custom scheme.
            "response_mode": "query",
        }
        if prompt:
            params["prompt"] = prompt
        return LoginRedirect(f"{endpoints['authorization_endpoint']}?{urlencode(params)}", state, app)

    async def handle_callback(
        self,
        params: dict[str, str],
        *,
        transaction_cookie: str | None,
        app_sub: str | None = None,
    ) -> LoginResult:
        state = params.get("state") or ""
        tx = await self.store.pop_transaction(state) if state else None
        if tx is None:
            raise LoginError("unknown_transaction", "This sign-in does not belong to a request made here (it may have expired or been used already).")
        if not transaction_cookie or not hmac.compare_digest(transaction_cookie.encode(), state.encode()):
            raise LoginError("transaction_mismatch", "This sign-in was not started in this browser.")

        issuer_param = params.get("iss")  # RFC 9207: the answer names who sent it
        if issuer_param is not None and issuer_param.rstrip("/") != self.settings.issuer:
            raise LoginError("issuer_mismatch", "The answer came from an unexpected identity provider.")
        if params.get("error"):
            raise LoginDenied(params["error"], params.get("error_description"))
        code = params.get("code")
        if not code:
            raise LoginError("missing_code", "The identity provider returned no authorization code.")

        tokens = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": tx["redirect_uri"],
                "code_verifier": tx["verifier"],
            }
        )
        id_token = tokens.get("id_token")
        if not tokens.get("access_token") or not id_token:
            raise LoginError("incomplete_response", "The token response lacks an access or ID token.", status=502)
        claims = await self._validate_id_token(id_token, tx["nonce"])

        if app_sub and not hmac.compare_digest(app_sub.encode(), claims["sub"].encode()):
            # Someone else is signed in in the system browser. Discard everything, ask for credentials.
            log.info("account mismatch between the app and the browser session; restarting with prompt=login")
            restart = await self.start_login(
                user_agent=None, return_to=tx["return_to"], prompt="login", app_mode=tx["app_mode"]
            )
            return LoginResult(return_to=tx["return_to"], restart=restart)

        now = self.clock()
        realm_roles, client_roles = await self._collect_roles(tokens["access_token"], claims)
        session = Session(
            sub=claims["sub"],
            sid=claims.get("sid"),
            access_token=tokens["access_token"],
            access_expires_at=now + float(tokens.get("expires_in") or 0),
            refresh_token=tokens.get("refresh_token"),
            refresh_expires_at=_expiry(now, tokens.get("refresh_expires_in")),
            id_token=id_token,
            claims=claims,
            scopes=_split_scope(tokens.get("scope")),
            realm_roles=realm_roles,
            client_roles=client_roles,
            app_mode=bool(tx["app_mode"]),
            created_at=now,
        )
        ttl = self._session_ttl(session, now)
        session_id = await self.store.create_session(session, ttl)
        return LoginResult(return_to=tx["return_to"], session_id=session_id, session=session, ttl=ttl)

    async def _validate_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            claims = await self._verify(id_token, audience=self.settings.client_id, require=("exp", "iat", "iss", "aud", "sub"))
        except TokenInvalid as err:
            raise LoginError("invalid_id_token", f"The ID token was rejected: {err}") from None
        aud = claims["aud"]
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.settings.client_id:
            raise LoginError("invalid_id_token", "The ID token was rejected: it names several audiences and not this client as authorized party.")
        if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"].encode(), nonce.encode()):
            raise LoginError("invalid_id_token", "The ID token does not belong to this sign-in request (nonce).")
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise LoginError("invalid_id_token", "The ID token has no subject.")
        return claims

    async def _collect_roles(self, access_token: str, id_claims: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Roles come with the access token (Keycloak's default); the ID token may carry them too."""
        realm, client = _roles(id_claims, self.settings.client_id)
        try:
            at_claims = await self._verify(access_token, audience=None, require=("exp", "iss"))
        except (TokenInvalid, UpstreamError):
            at_claims = {}  # opaque or unverifiable access token: no roles from it, never a reason to fail the login
        extra_realm, extra_client = _roles(at_claims, self.settings.client_id)
        return list(dict.fromkeys([*realm, *extra_realm])), list(dict.fromkeys([*client, *extra_client]))

    def _session_ttl(self, session: Session, now: float) -> float:
        """How long the store keeps the record: bounded by `APPEXT_SESSION_MAX_AGE` and the refresh token."""
        ttl = float(self.settings.session_max_age)
        if session.refresh_token and session.refresh_expires_at:
            ttl = min(ttl, session.refresh_expires_at - now)
        elif not session.refresh_token:
            ttl = min(ttl, max(session.access_expires_at - now, 1.0))
        return max(ttl, 1.0)

    # -- token endpoint ---------------------------------------------------------------------------

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        endpoint = (await self.discovery())["token_endpoint"]
        body = {**form, **self.client_auth.fields(endpoint)}
        try:
            response = await self.http.post(endpoint, data=body, headers={"Accept": "application/json"})
        except httpx.HTTPError as err:
            raise UpstreamError(f"token endpoint unreachable ({type(err).__name__})") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code == 200 and isinstance(payload, dict):
            return payload
        if isinstance(payload, dict) and payload.get("error"):
            raise TokenEndpointError(response.status_code, str(payload["error"]), payload.get("error_description"))
        raise UpstreamError(f"token endpoint answered HTTP {response.status_code}")

    # -- refresh -----------------------------------------------------------------------------------

    def _needs_refresh(self, session: Session) -> bool:
        return session.access_expires_at - self.clock() <= self.refresh_skew

    async def fresh_session(self, session_id: str) -> Session:
        """The session with an access token valid for at least `refresh_skew` more seconds."""
        session = await self.store.load_session(session_id)
        if session is None:
            raise SessionInvalid("no such session")
        if not self._needs_refresh(session):
            return session
        return await self.refresh(session_id)

    async def refresh(self, session_id: str, *, stale_access_token: str | None = None) -> Session:
        """Renew the tokens. Concurrent callers wait and share the first caller's result.

        `stale_access_token` says "the token I just used was refused": refresh
        even though it looks fresh, unless somebody else already did while we waited.
        """
        async with self.store.session_lock(session_id):
            session = await self.store.load_session(session_id)  # re-read: another worker may have refreshed or logged out
            if session is None:
                raise SessionInvalid("no such session")
            changed = stale_access_token is not None and session.access_token != stale_access_token
            if changed or (stale_access_token is None and not self._needs_refresh(session)):
                return session
            if not session.refresh_token:
                await self.store.delete_session(session_id)
                raise SessionInvalid("the session cannot be renewed (no refresh token)")
            try:
                tokens = await self._token_request({"grant_type": "refresh_token", "refresh_token": session.refresh_token})
            except TokenEndpointError as err:
                if err.error == "invalid_grant":
                    await self.store.delete_session(session_id)
                    raise SessionInvalid("the identity provider ended this session") from None
                raise UpstreamError(f"refresh failed: {err.error}") from None
            await self._apply_refresh(session, tokens)
            return session

    async def _apply_refresh(self, session: Session, tokens: dict[str, Any]) -> None:
        if not tokens.get("access_token"):
            raise UpstreamError("refresh answered without an access token")
        now = self.clock()
        session.access_token = tokens["access_token"]
        session.access_expires_at = now + float(tokens.get("expires_in") or 0)
        if tokens.get("refresh_token"):  # rotation: the new one replaces the old one
            session.refresh_token = tokens["refresh_token"]
            session.refresh_expires_at = _expiry(now, tokens.get("refresh_expires_in"))
        if tokens.get("id_token"):
            session.id_token = tokens["id_token"]
        if tokens.get("scope"):
            session.scopes = _split_scope(tokens["scope"])
        session.realm_roles, session.client_roles = await self._collect_roles(session.access_token, session.claims)
        await self.store.save_session(session, self._session_ttl(session, now))

    # -- calling other services ------------------------------------------------------------------------

    @staticmethod
    def _cache_name(audience: str, scopes: Sequence[str]) -> str:
        return f"{audience}|{' '.join(sorted(set(scopes)))}"

    def _cached(self, entry: dict[str, Any] | None, audience: str) -> AccessToken | None:
        if entry and entry["expires_at"] - self.refresh_skew > self.clock():
            return AccessToken(entry["token"], entry["expires_at"], audience)
        return None

    async def exchange_token(
        self, session_id: str, audience: str, scopes: Sequence[str], *, bypass_cache: bool = False
    ) -> AccessToken:
        """A token for one service, in the person's name (RFC 8693 Standard Token Exchange).

        The subject is the session's **own** access token – the login token itself
        never goes to the service. Cached per (session, audience, scope set).
        """
        name = self._cache_name(audience, scopes)
        if not bypass_cache:
            hit = self._cached(await self.store.get_cached_token(session_id, name), audience)
            if hit:
                return hit
        session = await self.fresh_session(session_id)
        try:
            tokens = await self._exchange(session, audience, scopes)
        except TokenEndpointError as err:
            if err.error in ("invalid_token", "invalid_grant"):
                # The login token itself was refused (revoked, rotated away): renew once and retry.
                session = await self.refresh(session_id, stale_access_token=session.access_token)
                try:
                    tokens = await self._exchange(session, audience, scopes)
                except TokenEndpointError as again:
                    raise self._exchange_error(session, audience, scopes, again) from None
            else:
                raise self._exchange_error(session, audience, scopes, err) from None
        return await self._remember(session_id, name, audience, tokens)

    def _exchange_error(self, session: Session, audience: str, scopes: Sequence[str], err: TokenEndpointError) -> AuthError:
        if _is_missing_consent(err):
            missing = [s for s in scopes if s not in session.scopes] or list(scopes)
            return ConsentRequired(missing)
        if err.error in ("invalid_token", "invalid_grant"):
            return SessionInvalid("the identity provider no longer accepts this session")
        return ExchangeFailed(audience, err.error, err.description)

    async def _exchange(self, session: Session, audience: str, scopes: Sequence[str]) -> dict[str, Any]:
        return await self._token_request(
            {
                "grant_type": TOKEN_EXCHANGE_GRANT,
                "subject_token": session.access_token,
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "audience": audience,
                "scope": " ".join(scopes),
            }
        )

    async def service_token(self, audience: str, scopes: Sequence[str], *, bypass_cache: bool = False) -> AccessToken:
        """A token for the extension itself (client credentials), per audience, cached."""
        name = self._cache_name(audience, scopes)
        if not bypass_cache:
            hit = self._cached(await self.store.get_cached_token(_SERVICE_NAMESPACE, name), audience)
            if hit:
                return hit
        try:
            tokens = await self._token_request({"grant_type": "client_credentials", "scope": " ".join(scopes)})
        except TokenEndpointError as err:
            raise ExchangeFailed(audience, err.error, err.description) from None
        return await self._remember(_SERVICE_NAMESPACE, name, audience, tokens)

    async def _remember(self, namespace: str, name: str, audience: str, tokens: dict[str, Any]) -> AccessToken:
        token = tokens.get("access_token")
        if not token:
            raise ExchangeFailed(audience, "invalid_response", "no access token in the response")
        expires_at = self.clock() + float(tokens.get("expires_in") or 0)
        lifetime = expires_at - self.refresh_skew - self.clock()
        if lifetime > 0:
            await self.store.put_cached_token(namespace, name, {"token": token, "expires_at": expires_at}, lifetime)
        return AccessToken(token, expires_at, audience)

    # -- logout ---------------------------------------------------------------------------------------

    async def end_session_url(self, session: Session, post_logout_redirect_uri: str | None = None) -> str | None:
        """RP-initiated logout: where to send the browser to end the SSO session (None if the IdP has no such endpoint)."""
        endpoint = (await self.discovery()).get("end_session_endpoint")
        if not endpoint:
            return None
        params = {
            "client_id": self.settings.client_id,
            "post_logout_redirect_uri": post_logout_redirect_uri or f"{self.settings.public_url}/",
        }
        if session.id_token:
            params["id_token_hint"] = session.id_token
        return f"{endpoint}?{urlencode(params)}"

    async def handle_backchannel_logout(self, logout_token: str) -> int:
        """Validate a logout token and end the sessions it names. Returns how many sessions ended."""
        try:
            claims = await self._verify(logout_token, audience=self.settings.client_id, require=("iss", "aud", "iat", "jti"))
        except TokenInvalid as err:
            raise LogoutTokenError(f"invalid logout token: {err}") from None
        if BACKCHANNEL_EVENT not in (claims.get("events") if isinstance(claims.get("events"), dict) else {}):
            raise LogoutTokenError("not a logout token: the backchannel-logout event is missing")
        if "nonce" in claims:
            # OIDC Back-Channel Logout 1.0 section 2.4: a logout token never has a nonce.
            # An ID token has one, and must not be accepted as a way to log somebody out.
            raise LogoutTokenError("not a logout token: it carries a nonce")
        if abs(self.clock() - claims["iat"]) > 300:
            raise LogoutTokenError("the logout token is too old")
        sid, sub = claims.get("sid"), claims.get("sub")
        if sid:
            return await self.store.delete_sessions_by_sid(sid)
        if sub:
            return await self.store.delete_sessions_by_sub(sub)
        raise LogoutTokenError("the logout token names neither a session (sid) nor a subject (sub)")


_SERVICE_NAMESPACE = "client-credentials"


def _is_missing_consent(err: TokenEndpointError) -> bool:
    """Is this the IdP saying "the person has not consented to these scopes"?

    The concept says `access_denied`. Keycloak 26.5 (checked against the dev realm)
    answers `invalid_scope` with "Missing consents for Token Exchange in client …"
    and uses `access_denied` ("Client requires user consent") on other paths. Both
    mean the same, and a plain `invalid_scope` (a scope the client does not have)
    must stay what it is: a configuration error, not a reason to send the person
    to a consent screen that cannot help.
    """
    if err.error == "access_denied":
        return True
    return err.error == "invalid_scope" and "consent" in (err.description or "").lower()


def _expiry(now: float, expires_in: Any) -> float | None:
    """`refresh_expires_in` of 0 or missing means "no information": the caller falls back to the maximum age."""
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        return None
    return now + seconds if seconds > 0 else None
