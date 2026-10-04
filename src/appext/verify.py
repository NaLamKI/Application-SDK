"""Token verification for **target services** written in Python.

An extension calls a service with a token that was exchanged for exactly that
service. The service has to check what the concept lists: signature (JWKS),
`iss`, `exp`, its **own** client id in `aud`, and the scope it needs. In the
exchanged token the calling extension is the authorized party (`azp`), so a
service can decide per extension what it may do on a person's behalf.

    verifier = TokenVerifier(issuer="https://sso.example.com/realms/example",
                             audience="projects-api",
                             jwks_url="https://sso.example.com/realms/example/protocol/openid-connect/certs")
    app = FastAPI()
    verifier.install(app)

    @app.get("/v1/projects", dependencies=[Depends(require_scope("svc-projects-read"))])
    async def projects(principal: Principal = Depends(current_principal)): ...

The same library on both sides means the rules cannot drift apart.

Statuses: no or invalid token -> 401 (`WWW-Authenticate: Bearer`), a valid token
that lacks the scope or comes from the wrong extension -> 403, an unreachable
JWKS -> 503 (the token may be fine; we just cannot tell).
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request

from .jwks import JWKSCache, JWKSUnavailable, TokenInvalid, decode_jwt, scopes_of

__all__ = ["Principal", "TokenInvalid", "TokenVerifier", "current_principal", "require_azp", "require_scope"]


@dataclass(frozen=True)
class Principal:
    """The verified caller: who (`sub`), through which extension (`azp`), allowed what (`scopes`)."""

    sub: str | None
    azp: str | None
    scopes: frozenset[str]
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class TokenVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        leeway: float = 30.0,
        jwks_ttl: float = 600.0,
        min_refetch: float = 10.0,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.clock = clock
        self.leeway = leeway
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=10.0)
        self.jwks = JWKSCache(jwks_url, self.http, ttl=jwks_ttl, min_refetch=min_refetch, clock=clock)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    def install(self, app: FastAPI) -> "TokenVerifier":
        """Make this the verifier of `app` (what `require_scope(...)` without `verifier=` uses)."""
        app.state.token_verifier = self
        return self

    async def verify(self, token: str) -> Principal:
        """Check a bearer token. Raises `TokenInvalid`, or `JWKSUnavailable` if the keys cannot be fetched."""
        key, alg = await self.jwks.key_for(token)
        claims = decode_jwt(
            token, key, alg, issuer=self.issuer, audience=self.audience, now=self.clock(), leeway=self.leeway,
            require=("exp", "iss", "aud"),
        )
        return Principal(sub=claims.get("sub"), azp=claims.get("azp"), scopes=scopes_of(claims), claims=claims)

    # -- FastAPI ------------------------------------------------------------------------------------

    async def principal(self, request: Request) -> Principal:
        """Dependency: the verified caller of this request (cached per request)."""
        cached = getattr(request.state, "appext_principal", None)
        if cached is not None:
            return cached
        token = _bearer(request)
        if token is None:
            raise _unauthorized("missing", "A bearer token is required.")
        try:
            principal = await self.verify(token)
        except TokenInvalid as err:
            raise _unauthorized("invalid_token", str(err)) from None
        except JWKSUnavailable:
            raise HTTPException(503, detail={"code": "jwks_unavailable", "message": "The signing keys cannot be fetched right now."}) from None
        request.state.appext_principal = principal
        return principal

    def require_scope(self, *scopes: str) -> Callable[..., Awaitable[Principal]]:
        return _scope_dependency(self.principal, scopes)

    def require_azp(self, *clients: str) -> Callable[..., Awaitable[Principal]]:
        return _azp_dependency(self.principal, clients)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None


def _unauthorized(reason: str, message: str) -> HTTPException:
    challenge = "Bearer" if reason == "missing" else 'Bearer error="invalid_token"'
    code = "unauthenticated" if reason == "missing" else "invalid_token"
    return HTTPException(401, detail={"code": code, "message": message}, headers={"WWW-Authenticate": challenge})


def _scope_dependency(get_principal: Callable[..., Awaitable[Principal]], scopes: tuple[str, ...]):
    async def dependency(request: Request) -> Principal:
        principal = await get_principal(request)
        missing = [s for s in scopes if s not in principal.scopes]
        if missing:
            raise HTTPException(
                403,
                detail={"code": "insufficient_scope", "message": "The token lacks the scope: " + ", ".join(missing)},
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{" ".join(scopes)}"'},
            )
        return principal

    return dependency


def _azp_dependency(get_principal: Callable[..., Awaitable[Principal]], clients: tuple[str, ...]):
    async def dependency(request: Request) -> Principal:
        principal = await get_principal(request)
        if principal.azp not in clients:
            raise HTTPException(
                403, detail={"code": "forbidden_client", "message": "This extension may not call this service."}
            )
        return principal

    return dependency


def _installed(request: Request) -> TokenVerifier:
    verifier = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise RuntimeError("no TokenVerifier is installed: call verifier.install(app) or pass verifier= to the dependency")
    return verifier


async def current_principal(request: Request) -> Principal:
    """Dependency using the verifier installed on the app (`verifier.install(app)`)."""
    return await _installed(request).principal(request)


def require_scope(*scopes: str, verifier: TokenVerifier | None = None) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: the token must carry **all** of `scopes`."""
    return _scope_dependency(verifier.principal if verifier else current_principal, scopes)


def require_azp(*clients: str, verifier: TokenVerifier | None = None) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: the calling extension (`azp`) must be one of `clients`."""
    return _azp_dependency(verifier.principal if verifier else current_principal, clients)
