"""Key material for the extension: the client key pair and the session key.

The private key never leaves the developer's machine or the deployment's secret
store; only the public JWK is uploaded (`appext store key`). Nothing here prints
a private key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from .context import CliError, Context

KEY_FILE = "client_key.pem"
JWK_FILE = "client_key.jwk.json"
SESSION_KEY_FILE = "session_key"
RSA_BITS = 3072


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64u(value: int, length: int | None = None) -> str:
    """Big-endian, without leading zeros (RFC 7518) – except EC coordinates, which have a fixed length."""
    return _b64u(value.to_bytes(length or (value.bit_length() + 7) // 8, "big"))


def thumbprint(jwk: dict) -> str:
    """RFC 7638 – the same value the SDK puts into the `kid` of its client assertions."""
    members = {"RSA": ("e", "kty", "n"), "EC": ("crv", "kty", "x", "y")}[jwk["kty"]]
    canonical = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"))
    return _b64u(hashlib.sha256(canonical.encode()).digest())


def public_jwk(private_key, alg: str) -> dict:
    """The public half only – built member by member, so no private member can slip in."""
    public = private_key.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        numbers = public.public_numbers()
        jwk = {"kty": "RSA", "n": _int_b64u(numbers.n), "e": _int_b64u(numbers.e)}
    else:
        numbers = public.public_numbers()
        jwk = {"kty": "EC", "crv": "P-256", "x": _int_b64u(numbers.x, 32), "y": _int_b64u(numbers.y, 32)}
    return {**jwk, "use": "sig", "alg": alg, "kid": thumbprint(jwk)}


def _generate(alg: str):
    if alg == "RS256":
        return rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
    return ec.generate_private_key(ec.SECP256R1())


def write_secret(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    """Creates the file with mode 0600 from the start – there is no moment it is world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise CliError(f"{path} already exists", hint="Pass --force to replace it.") from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    path.chmod(0o600)  # an overwritten file keeps its old mode otherwise


def create_client_key(directory: Path, alg: str = "RS256", *, overwrite: bool = False) -> tuple[Path, Path]:
    key_path, jwk_path = directory / KEY_FILE, directory / JWK_FILE
    private = _generate(alg)
    write_secret(
        key_path,
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        overwrite=overwrite,
    )
    jwk_path.write_text(json.dumps(public_jwk(private, alg), indent=2) + "\n", encoding="utf-8")
    return key_path, jwk_path


def ensure_dev_key(directory: Path) -> tuple[Path, Path, bool]:
    """The dev key pair in `directory`, created on first use. Returns (key, jwk, created)."""
    key_path, jwk_path = directory / KEY_FILE, directory / JWK_FILE
    if key_path.exists() and jwk_path.exists():
        return key_path, jwk_path, False
    if key_path.exists():  # a key without its public half: derive it, do not replace the key
        private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        alg = "RS256" if isinstance(private, rsa.RSAPrivateKey) else "ES256"
        jwk_path.write_text(json.dumps(public_jwk(private, alg), indent=2) + "\n", encoding="utf-8")
        return key_path, jwk_path, False
    key_path, jwk_path = create_client_key(directory)
    return key_path, jwk_path, True


def ensure_dev_session_key(directory: Path) -> Path:
    """The dev session key, created on first use (so a restart does not warn about a throw-away key)."""
    from appext.config import generate_session_key

    path = directory / SESSION_KEY_FILE
    if not path.exists():
        write_secret(path, generate_session_key().encode())
    return path


def load_public_jwk(path: Path) -> dict:
    try:
        jwk = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"{path} not found", hint="Create one with `appext keys generate`.") from None
    except ValueError as error:
        raise CliError(f"{path} is not valid JSON: {error}") from None
    if not isinstance(jwk, dict) or jwk.get("kty") not in ("RSA", "EC"):
        raise CliError(f"{path} is not an RSA or EC JWK")
    if any(member in jwk for member in ("d", "p", "q", "dp", "dq", "qi")):
        raise CliError(f"{path} contains private key material", hint="Upload only the public JWK written by `appext keys generate`.")
    return jwk


def generate(args, ctx: Context) -> int:
    directory = ctx.path(args.out)
    key_path, jwk_path = create_client_key(directory, args.alg, overwrite=args.force)
    jwk = json.loads(jwk_path.read_text(encoding="utf-8"))
    ctx.say(f"private key  {ctx.show(key_path)}  (mode 0600 – keep it secret, never commit it)")
    ctx.say(f"public JWK   {ctx.show(jwk_path)}  (kid {jwk['kid']}, {args.alg})")
    ctx.say("Upload the public key with `appext store register` or `appext store key`.")
    return 0


def session(args, ctx: Context) -> int:
    from appext.config import generate_session_key  # the SDK owns the key file format

    path = ctx.path(args.out)
    write_secret(path, generate_session_key().encode(), overwrite=args.force)
    ctx.say(f"session key  {ctx.show(path)}  (mode 0600)")
    ctx.say("Point APPEXT_SESSION_KEY_FILE at it; every replica must use the same file.")
    ctx.say("To rotate: put the new key on the first line and keep the old ones below it.")
    return 0
