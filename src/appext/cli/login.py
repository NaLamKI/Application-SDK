"""Signing the CLI in: the OIDC device authorization grant (RFC 8628) and the
credentials it leaves behind.

The CLI is a **public** client (`appext-cli`): it holds no secret, and the person
approves the sign-in in a browser where their Keycloak session and second factor
already live. The refresh token is the only long-lived thing on disk, in a file
only the owner can read.

The device request carries a **PKCE** challenge (S256) and the polling requests the
verifier: the realm file requires PKCE for this client (Keycloak then answers a
device request without one with `Missing parameter: code_challenge_method`), and it
ties the code the person approves to the process that asked for it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from .context import CliError, Context

DEFAULT_CLIENT_ID = "appext-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
EXPIRY_MARGIN = 30  # seconds: refresh a token that is about to expire, not one that already has
STORE_ROLES = ("store-developer", "store-reviewer", "store-admin")


def credentials_path(ctx: Context) -> Path:
    return ctx.config_dir / "credentials.json"


def _load_all(ctx: Context) -> dict:
    try:
        return json.loads(credentials_path(ctx).read_text(encoding="utf-8")).get("credentials", {})
    except (FileNotFoundError, ValueError, AttributeError):
        return {}


def _save_all(ctx: Context, credentials: dict) -> None:
    path = credentials_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Created 0600 and then renamed into place: never readable by others, never half-written.
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "credentials": credentials}, handle, indent=2)
    temporary.chmod(0o600)
    temporary.replace(path)


def load_credentials(ctx: Context, issuer: str) -> dict | None:
    return _load_all(ctx).get(issuer)


def save_credentials(ctx: Context, issuer: str, entry: dict) -> None:
    _save_all(ctx, {**_load_all(ctx), issuer: entry})


def forget_credentials(ctx: Context, issuer: str) -> bool:
    credentials = _load_all(ctx)
    if issuer not in credentials:
        return False
    del credentials[issuer]
    _save_all(ctx, credentials)
    return True


def unverified_claims(token: str) -> dict:
    """The payload of a JWT, **not verified** – only for telling the person who they are signed in as.
    Nothing is decided on it; the store verifies the token on every request."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def discover(ctx: Context, issuer: str) -> dict:
    with ctx.http() as http:
        try:
            response = http.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
            response.raise_for_status()
            return response.json()
        except Exception as error:
            raise CliError(f"cannot reach the sign-in at {issuer}: {error}", hint="Check --issuer / APPEXT_ISSUER.") from None


def _token_error(response) -> tuple[str, str]:
    try:
        body = response.json()
        return body.get("error", f"http_{response.status_code}"), body.get("error_description", "")
    except ValueError:
        return f"http_{response.status_code}", response.text[:200]


def _entry(ctx: Context, issuer: str, client_id: str, token_endpoint: str, tokens: dict) -> dict:
    return {
        "client_id": client_id,
        "token_endpoint": token_endpoint,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": ctx.now() + int(tokens.get("expires_in", 60)),
    }


def device_login(ctx: Context, issuer: str, client_id: str, *, open_browser: bool = True) -> dict:
    metadata = discover(ctx, issuer)
    device_endpoint = metadata.get("device_authorization_endpoint")
    if not device_endpoint:
        raise CliError("this sign-in does not offer the device authorization grant")
    token_endpoint = metadata["token_endpoint"]

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with ctx.http() as http:
        started = http.post(device_endpoint, data={
            "client_id": client_id, "scope": "openid",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        if started.status_code != 200:
            error, description = _token_error(started)
            raise CliError(f"the sign-in refused to start: {error} {description}".strip(),
                           hint=f"Is the public client {client_id!r} set up at the issuer? The platform's operator provides it (see docs/platform.md).")
        grant = started.json()
        address = grant.get("verification_uri_complete") or grant["verification_uri"]
        ctx.say(f"Open {address}")
        if "verification_uri_complete" not in grant:
            ctx.say(f"and enter the code {grant['user_code']}")
        else:
            ctx.say(f"and confirm the code {grant['user_code']}")
        if open_browser:
            try:
                ctx.open_url(address)
            except Exception:  # no browser (SSH, CI): the address is printed anyway
                pass
        ctx.say("Waiting for you to approve …")

        interval = int(grant.get("interval", 5))
        deadline = ctx.now() + int(grant.get("expires_in", 600))
        while True:
            ctx.sleep(interval)
            if ctx.now() > deadline:
                raise CliError("the sign-in timed out", hint="Run `appext store login` again.")
            response = http.post(token_endpoint, data={
                "grant_type": DEVICE_GRANT, "device_code": grant["device_code"],
                "client_id": client_id, "code_verifier": verifier,
            })
            if response.status_code == 200:
                return _entry(ctx, issuer, client_id, token_endpoint, response.json())
            error, description = _token_error(response)
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5  # RFC 8628 §3.5
                continue
            if error == "access_denied":
                raise CliError("the sign-in was declined")
            if error == "expired_token":
                raise CliError("the code expired before it was approved", hint="Run `appext store login` again.")
            raise CliError(f"sign-in failed: {error} {description}".strip())


def describe(entry: dict) -> str:
    claims = unverified_claims(entry["access_token"])
    who = claims.get("preferred_username") or claims.get("email") or claims.get("sub") or "unknown"
    roles = [r for r in claims.get("realm_access", {}).get("roles", []) if r in STORE_ROLES]
    return f"{who} ({', '.join(roles) or 'no store role'})"


def _refresh(ctx: Context, issuer: str, entry: dict) -> dict:
    if not entry.get("refresh_token"):
        forget_credentials(ctx, issuer)
        raise CliError("your sign-in expired", hint="Run `appext store login`.")
    with ctx.http() as http:
        response = http.post(entry["token_endpoint"], data={
            "grant_type": "refresh_token",
            "refresh_token": entry["refresh_token"],
            "client_id": entry["client_id"],
        })
    if response.status_code != 200:
        error, description = _token_error(response)
        if error == "invalid_grant":  # the refresh token is spent or the session ended
            forget_credentials(ctx, issuer)
            raise CliError("your sign-in expired", hint="Run `appext store login`.")
        raise CliError(f"could not renew the sign-in: {error} {description}".strip())
    tokens = response.json()
    tokens.setdefault("refresh_token", entry["refresh_token"])  # not every server rotates it
    renewed = _entry(ctx, issuer, entry["client_id"], entry["token_endpoint"], tokens)
    save_credentials(ctx, issuer, renewed)
    return renewed


def access_token(ctx: Context, issuer: str) -> str:
    """A valid access token for the store: `APPEXT_STORE_TOKEN` (CI), else the cached sign-in, renewed when due."""
    from_env = ctx.env.get("APPEXT_STORE_TOKEN", "").strip()
    if from_env:
        return from_env
    entry = load_credentials(ctx, issuer)
    if entry is None:
        raise CliError("you are not signed in", hint="Run `appext store login` (or set APPEXT_STORE_TOKEN in CI).")
    if entry["expires_at"] - EXPIRY_MARGIN <= ctx.now():
        entry = _refresh(ctx, issuer, entry)
    return entry["access_token"]
