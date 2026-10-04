"""Signature checking against a JWKS, shared by the login core and `appext.verify`.

Two decisions that look like detail and are not:

* **Algorithms are an allow-list of asymmetric ones.** The token's own header
  never decides: `none` and the HMAC family are refused, so nobody can sign a
  token with the public key as the secret.
* **Time is checked here, with the caller's clock**, not by the JWT library
  (which reads the wall clock). That is what makes expiry testable without
  `sleep`, and one `leeway` applies to every check.

An unknown `kid` triggers one refetch of the JWKS (the IdP rotated its keys),
but not more often than `min_refetch` seconds: otherwise anyone could make this
process hammer the IdP by sending tokens with random key ids.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt

ASYMMETRIC_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")


class TokenInvalid(Exception):
    """The token cannot be trusted. The message is safe to show to an operator (never contains the token)."""


class JWKSUnavailable(Exception):
    """The JWKS could not be fetched; says nothing about the token."""


class JWKSCache:
    def __init__(
        self,
        url: str | Callable[[], Awaitable[str]],
        http: httpx.AsyncClient | Callable[[], httpx.AsyncClient],
        *,
        ttl: float = 600.0,
        min_refetch: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = url
        self._http = http if callable(http) else (lambda: http)
        self.ttl = ttl
        self.min_refetch = min_refetch
        self._clock = clock
        self._keys: dict[str | None, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self.fetch_count = 0

    async def key_for(self, token: str) -> tuple[Any, str]:
        """The verification key and algorithm for a token, from its (untrusted) header."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenInvalid("not a well-formed JWT") from None
        alg = header.get("alg")
        if alg not in ASYMMETRIC_ALGORITHMS:
            raise TokenInvalid(f"signature algorithm {alg!r} is not accepted")
        kid = header.get("kid")

        key = self._lookup(kid, alg)
        if key is None and self._may_refetch():
            await self._refresh()
            key = self._lookup(kid, alg)
        if key is None:
            raise TokenInvalid("the token was signed with a key that is not in the JWKS")
        return key.key, alg

    def _lookup(self, kid: str | None, alg: str) -> jwt.PyJWK | None:
        if self._fetched_at is None or self._clock() - self._fetched_at > self.ttl:
            return None
        if kid is not None:
            return self._keys.get(kid)
        family = "EC" if alg.startswith("ES") else "RSA"
        candidates = [k for k in self._keys.values() if k.key_type == family]
        return candidates[0] if len(candidates) == 1 else None

    def _may_refetch(self) -> bool:
        return self._fetched_at is None or self._clock() - self._fetched_at >= min(self.min_refetch, self.ttl)

    async def _refresh(self) -> None:
        url = self._url if isinstance(self._url, str) else await self._url()
        try:
            response = await self._http().get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as err:
            raise JWKSUnavailable(f"cannot fetch the JWKS ({type(err).__name__})") from None
        self.fetch_count += 1
        keys: dict[str | None, jwt.PyJWK] = {}
        for jwk in document.get("keys", []) if isinstance(document, dict) else []:
            if jwk.get("use") == "enc":
                continue
            try:
                keys[jwk.get("kid")] = jwt.PyJWK(jwk)
            except (jwt.PyJWTError, ValueError, KeyError):
                continue  # a key type we do not support must not break the others
        self._keys = keys
        self._fetched_at = self._clock()


def decode_jwt(
    token: str,
    key: Any,
    alg: str,
    *,
    issuer: str | None,
    audience: str | None,
    now: float,
    leeway: float = 60.0,
    require: tuple[str, ...] = ("exp", "iss"),
) -> dict[str, Any]:
    """Verify signature, issuer, audience, time. Raises `TokenInvalid` for anything wrong."""
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[alg],
            issuer=issuer,
            audience=audience,
            options={
                "require": list(require),
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": audience is not None,
                "verify_iss": issuer is not None,
            },
        )
    except jwt.ExpiredSignatureError:  # pragma: no cover - time checks are ours
        raise TokenInvalid("token expired") from None
    except jwt.InvalidAudienceError:
        raise TokenInvalid("the token is not meant for this client (audience)") from None
    except jwt.InvalidIssuerError:
        raise TokenInvalid("the token comes from another issuer") from None
    except jwt.MissingRequiredClaimError as err:
        raise TokenInvalid(f"the token lacks the claim {err.claim!r}") from None
    except jwt.InvalidSignatureError:
        raise TokenInvalid("signature does not match") from None
    except jwt.PyJWTError as err:
        raise TokenInvalid(f"the token is invalid ({type(err).__name__})") from None

    _check_times(claims, now, leeway)
    return claims


def _check_times(claims: dict[str, Any], now: float, leeway: float) -> None:
    for name in ("exp", "nbf", "iat"):
        if name in claims and (isinstance(claims[name], bool) or not isinstance(claims[name], (int, float))):
            raise TokenInvalid(f"claim {name!r} is not a number")
    if "exp" in claims and claims["exp"] + leeway <= now:
        raise TokenInvalid("the token has expired")
    if "nbf" in claims and claims["nbf"] - leeway > now:
        raise TokenInvalid("the token is not valid yet")
    if "iat" in claims and claims["iat"] - leeway > now:
        raise TokenInvalid("the token was issued in the future")


def scopes_of(claims: dict[str, Any]) -> frozenset[str]:
    """The scopes of an access token: the `scope` claim (space-separated) and/or `scp` (list or string)."""
    found: set[str] = set()
    for name in ("scope", "scp"):
        value = claims.get(name)
        if isinstance(value, str):
            found.update(value.split())
        elif isinstance(value, list):
            found.update(s for s in value if isinstance(s, str))
    return frozenset(found)
