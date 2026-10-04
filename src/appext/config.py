"""Configuration of a running extension: every `APPEXT_*` variable in one place.

The auth bundle from the store provides the secret-free half
(`APPEXT_ISSUER`, `APPEXT_CLIENT_ID`, URLs …); the deployment provides the
secrets as files (`APPEXT_CLIENT_KEY_FILE`, `APPEXT_SESSION_KEY_FILE`).

Two environments, two attitudes:

* `APPEXT_ENV=local` (the default) is for development at the desk: everything
  has a default that works against the local stack, a missing session key is
  generated per process (with a warning), and a missing lock file is fine.
* Any other value is a deployment: nothing is guessed. A missing value is an
  error that names the variable, and **all** problems are reported at once.

Secrets are wrapped in `Secret`, whose `repr` is masked, so that a settings
object in a log line or a traceback never carries one.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import os
import secrets as _secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .manifest import CLIENT_AUTH_METHODS, Manifest

log = logging.getLogger("appext")

LOCAL_ENV = "local"
DEFAULT_ISSUER = "http://127.0.0.1:58080/realms/fmis"
DEFAULT_APP_REDIRECT_URI = "org.agrifooddata.apps.fmis.web:/callback"
#: What the local stack of this repository serves; only used in `local`.
LOCAL_SERVICE_URLS = {"fmis-api": "http://127.0.0.1:8000/api/v1"}
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


class ConfigError(ValueError):
    """The environment is unusable. `problems` lists every reason, one line each."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid extension configuration:\n  - " + "\n  - ".join(problems))


class Secret:
    """A value that must not end up in logs. `reveal()` is the only way out."""

    __slots__ = ("_value",)

    def __init__(self, value: str | bytes) -> None:
        self._value = value

    def reveal(self) -> str | bytes:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


def parse_session_keys(content: str | bytes) -> list[bytes]:
    """Read the session-encryption keys from a key file's content.

    One key per line, base64 or hex of 32 random bytes; the **first** key
    encrypts, all of them decrypt – that is how a key is rotated without
    logging everyone out. A file of exactly 32 raw bytes is a single key.
    """
    raw = content.encode() if isinstance(content, str) else content
    try:
        return _parse_key_lines(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        binary = not raw.isascii() or any(b < 32 or b == 127 for b in raw)
        if len(raw) == 32 and binary:
            return [raw]  # a binary key file; a 32-character typo in a text file is an error, not a key
        raise


def _parse_key_lines(text: str) -> list[bytes]:
    keys: list[bytes] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keys.append(_decode_key(line, number))
    if not keys:
        raise ValueError("no key found")
    return keys


def _decode_key(line: str, number: int) -> bytes:
    key: bytes | None = None
    if len(line) == 64:
        try:
            key = bytes.fromhex(line)
        except ValueError:
            key = None
    if key is None:
        try:
            padded = line + "=" * (-len(line) % 4)
            key = base64.urlsafe_b64decode(padded) if ("-" in line or "_" in line) else base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"line {number} is neither hex nor base64") from None
    if len(key) != 32:
        raise ValueError(f"line {number} is {len(key)} bytes, expected 32 (AES-256)")
    return key


def generate_session_key() -> str:
    """A fresh session key in the file format above (what `appext keys session` writes)."""
    return base64.b64encode(_secrets.token_bytes(32)).decode() + "\n"


@dataclass(frozen=True)
class ExtensionSettings:
    env: str
    issuer: str
    client_id: str
    client_auth: str
    public_url: str
    app_redirect_uri: str
    #: PEM or JWK (JSON) text of the private key for `private_key_jwt`.
    client_key: Secret | None = field(default=None, repr=True)
    client_key_id: str | None = None
    client_secret: Secret | None = None
    #: Service name (as in the manifest) -> base URL.
    services: Mapping[str, str] = field(default_factory=dict)
    session_store: str = "memory"
    #: First encrypts, all decrypt. Empty only when `session_key_generated`.
    session_keys: tuple[Secret, ...] = ()
    session_key_generated: bool = False
    trusted_proxies: tuple[str, ...] = ()
    cookie_secure: bool = False
    session_max_age: int = 30 * 24 * 3600
    http_timeout: float = 10.0
    lock_file: Path | None = None
    #: Where the FMIS **web** app lives (origins, `APPEXT_APP_ORIGINS`). Empty = no web
    #: app: nothing may embed the extension, and it shows no "back to FMIS" bar.
    app_origins: tuple[str, ...] = ()

    @property
    def frame_ancestors(self) -> str:
        """The `frame-ancestors` source list: the web app that embeds the extension – or nothing."""
        return " ".join(self.app_origins) if self.app_origins else "'none'"

    @property
    def is_local(self) -> bool:
        return self.env == LOCAL_ENV

    @property
    def web_redirect_uri(self) -> str:
        return f"{self.public_url}/auth/callback"

    def redirect_uri(self, app_mode: bool) -> str:
        return self.app_redirect_uri if app_mode else self.web_redirect_uri

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def cookie_prefix(self) -> str:
        """`__Host-` only where the browser accepts it: it demands `Secure`."""
        return "__Host-" if self.cookie_secure else ""

    @property
    def cookie_suffix(self) -> str:
        """The port, where the cookie has no `__Host-` prefix to keep it apart.

        Cookies are scoped by host, **not by port**: two extensions on
        `127.0.0.1:8100` and `127.0.0.1:8200` share one cookie jar, and with the same
        names each would sign the other out. In production every extension has a
        host of its own and the `__Host-` prefix pins the cookie to it; on a developer
        machine the port does that job.
        """
        if self.cookie_secure:
            return ""
        port = urlsplit(self.public_url).port
        return f"_{port}" if port else ""

    @property
    def session_cookie_name(self) -> str:
        return f"{self.cookie_prefix}ext_session{self.cookie_suffix}"

    @property
    def transaction_cookie_name(self) -> str:
        return f"{self.cookie_prefix}ext_tx{self.cookie_suffix}"

    @classmethod
    def from_env(
        cls,
        manifest: Manifest | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ExtensionSettings":
        """Read and validate the environment. Raises `ConfigError` listing every problem."""
        return _Reader(manifest, os.environ if environ is None else environ).read()


def is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


class _Reader:
    def __init__(self, manifest: Manifest | None, environ: Mapping[str, str]) -> None:
        self.manifest = manifest
        self.environ = environ
        self.problems: list[str] = []
        self.env = (environ.get("APPEXT_ENV") or LOCAL_ENV).strip()

    # -- helpers -----------------------------------------------------------------------------

    @property
    def local(self) -> bool:
        return self.env == LOCAL_ENV

    def get(self, name: str, default: str | None = None) -> str | None:
        value = self.environ.get(name)
        if value is None or not value.strip():
            return default
        return value.strip()

    def require(self, name: str, default: str | None = None) -> str | None:
        value = self.get(name, default)
        if value is None:
            self.problems.append(f"{name} is required (APPEXT_ENV={self.env})")
        return value

    def read_file(self, name: str) -> str | None:
        path = self.get(name)
        if path is None:
            return None
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            self.problems.append(f"{name}={path} cannot be read: {err.__class__.__name__}")
            return None

    def read_bytes(self, name: str) -> bytes | None:
        path = self.get(name)
        if path is None:
            return None
        try:
            return Path(path).read_bytes()
        except OSError as err:
            self.problems.append(f"{name}={path} cannot be read: {err.__class__.__name__}")
            return None

    def number(self, name: str, default: float, *, minimum: float) -> float:
        raw = self.get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.problems.append(f"{name} must be a number, got {raw!r}")
            return default
        if value < minimum:
            self.problems.append(f"{name} must be at least {minimum:g}")
            return default
        return value

    # -- the pieces --------------------------------------------------------------------------

    def read(self) -> ExtensionSettings:
        m = self.manifest
        issuer = self.read_issuer()
        client_id = self.require("APPEXT_CLIENT_ID", m.client_id if m else None)
        client_auth = self.read_client_auth()
        public_url = self.read_public_url()
        app_redirect = self.get("APPEXT_APP_REDIRECT_URI", DEFAULT_APP_REDIRECT_URI)
        if app_redirect and (not urlsplit(app_redirect).scheme or "#" in app_redirect):
            self.problems.append("APPEXT_APP_REDIRECT_URI must be a URI with a scheme and no fragment")

        key, secret = self.read_client_credentials(client_auth)
        session_keys, generated = self.read_session_keys()
        store = self.read_session_store()
        proxies = self.read_trusted_proxies()
        cookie_secure = self.read_cookie_secure(public_url)
        services = self.read_services()
        lock_file = self.get("APPEXT_LOCK_FILE")
        max_age = int(self.number("APPEXT_SESSION_MAX_AGE", 30 * 24 * 3600, minimum=60))
        timeout = self.number("APPEXT_HTTP_TIMEOUT", 10.0, minimum=0.1)
        app_origins = self.read_app_origins()

        if self.problems:
            raise ConfigError(self.problems)
        assert issuer and client_id and client_auth and public_url and app_redirect
        return ExtensionSettings(
            env=self.env,
            issuer=issuer,
            client_id=client_id,
            client_auth=client_auth,
            public_url=public_url,
            app_redirect_uri=app_redirect,
            client_key=key,
            client_key_id=self.get("APPEXT_CLIENT_KEY_ID"),
            client_secret=secret,
            services=services,
            session_store=store,
            session_keys=session_keys,
            session_key_generated=generated,
            trusted_proxies=proxies,
            cookie_secure=cookie_secure,
            session_max_age=max_age,
            http_timeout=timeout,
            lock_file=Path(lock_file) if lock_file else None,
            app_origins=app_origins,
        )

    def read_app_origins(self) -> tuple[str, ...]:
        """`APPEXT_APP_ORIGINS`: comma-separated origins of the FMIS web app.

        Each entry is an origin and nothing more – it ends up in a `Content-Security-Policy`,
        where a path, a wildcard or a stray `;` would change what the policy says.
        """
        raw = self.get("APPEXT_APP_ORIGINS", "") or ""
        origins: list[str] = []
        for entry in (e.strip() for e in raw.split(",")):
            if not entry:
                continue
            origin = entry.rstrip("/")
            parts = urlsplit(origin)
            if (
                parts.scheme not in ("http", "https")
                or not parts.hostname
                or parts.path
                or parts.query
                or parts.fragment
                or parts.username
                or "*" in origin
                or any(c in origin for c in " ;,'\"")
            ):
                self.problems.append(f"APPEXT_APP_ORIGINS: {entry!r} is not an origin (https://host[:port])")
            elif parts.scheme == "http" and not is_loopback(origin):
                self.problems.append(f"APPEXT_APP_ORIGINS: {entry!r} must use https (http only on 127.0.0.1/localhost)")
            elif origin not in origins:
                origins.append(origin)
        return tuple(origins)

    def read_issuer(self) -> str | None:
        issuer = self.require("APPEXT_ISSUER", DEFAULT_ISSUER if self.local else None)
        if issuer is None:
            return None
        issuer = issuer.rstrip("/")
        parts = urlsplit(issuer)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            self.problems.append("APPEXT_ISSUER must be an http(s) URL")
        elif parts.scheme == "http" and not (self.local or is_loopback(issuer)):
            self.problems.append("APPEXT_ISSUER must use https outside local development")
        return issuer

    def read_client_auth(self) -> str | None:
        default = self.manifest.client_auth if self.manifest else None
        method = self.require("APPEXT_CLIENT_AUTH", default or ("private_key_jwt" if self.local else None))
        if method is not None and method not in CLIENT_AUTH_METHODS:
            self.problems.append(f"APPEXT_CLIENT_AUTH must be one of {', '.join(CLIENT_AUTH_METHODS)}")
        return method

    def read_public_url(self) -> str | None:
        port = (self.manifest.dev_port if self.manifest else None) or 8000
        url = self.require("APPEXT_PUBLIC_URL", f"http://127.0.0.1:{port}" if self.local else None)
        if url is None:
            return None
        url = url.rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query or parts.fragment:
            self.problems.append("APPEXT_PUBLIC_URL must be the origin only, e.g. https://name.apps.example.com")
        elif parts.scheme == "http" and not is_loopback(url):
            self.problems.append("APPEXT_PUBLIC_URL must use https (http only on 127.0.0.1 or localhost)")
        return url

    def read_client_credentials(self, method: str | None) -> tuple[Secret | None, Secret | None]:
        key_text = self.read_file("APPEXT_CLIENT_KEY_FILE")
        secret_text = self.read_file("APPEXT_CLIENT_SECRET_FILE") or self.get("APPEXT_CLIENT_SECRET")
        key = Secret(key_text) if key_text else None
        secret = Secret(secret_text.strip()) if secret_text else None
        if not self.local:
            if method == "private_key_jwt" and key is None:
                self.problems.append("APPEXT_CLIENT_KEY_FILE is required for client_auth=private_key_jwt")
            if method == "client_secret" and secret is None:
                self.problems.append("APPEXT_CLIENT_SECRET_FILE is required for client_auth=client_secret")
        return key, secret

    def read_session_keys(self) -> tuple[tuple[Secret, ...], bool]:
        path = self.get("APPEXT_SESSION_KEY_FILE")
        content = self.read_bytes("APPEXT_SESSION_KEY_FILE") if path else self.get("APPEXT_SESSION_KEY")
        if path and content is None:
            return (), False  # unreadable: already reported
        if content is None:
            if self.local:
                log.warning(
                    "APPEXT_SESSION_KEY_FILE is not set: using a session key generated for this process; "
                    "sessions will not survive a restart (fine for development only)"
                )
                return (), True
            self.problems.append("APPEXT_SESSION_KEY_FILE is required (the key that encrypts tokens in the session store)")
            return (), False
        try:
            return tuple(Secret(k) for k in parse_session_keys(content)), False
        except ValueError as err:
            self.problems.append(f"APPEXT_SESSION_KEY_FILE is not a valid key file: {err}")
            return (), False

    def read_session_store(self) -> str:
        store = self.get("APPEXT_SESSION_STORE", "memory") or "memory"
        scheme = urlsplit(store).scheme
        if store != "memory" and scheme not in ("redis", "rediss", "unix"):
            self.problems.append("APPEXT_SESSION_STORE must be 'memory' or a redis:// / rediss:// URL")
        elif store == "memory" and not self.local:
            self.problems.append(
                "APPEXT_SESSION_STORE=memory is for development only: with several replicas or a restart "
                "every session is lost; set a redis:// URL"
            )
        return store

    def read_trusted_proxies(self) -> tuple[str, ...]:
        raw = self.get("APPEXT_TRUSTED_PROXIES", "") or ""
        proxies = tuple(p.strip() for p in raw.split(",") if p.strip())
        for p in proxies:
            if p == "*":
                continue
            try:
                ipaddress.ip_network(p, strict=False)
            except ValueError:
                self.problems.append(f"APPEXT_TRUSTED_PROXIES: {p!r} is not an IP address or network (or '*')")
        return proxies

    def read_cookie_secure(self, public_url: str | None) -> bool:
        """Secure cookies (and the `__Host-` prefix) whenever the public URL is https."""
        https = bool(public_url and public_url.startswith("https://"))
        raw = (self.get("APPEXT_COOKIE_SECURE", "auto") or "auto").lower()
        if raw not in ("auto", "true", "false"):
            self.problems.append("APPEXT_COOKIE_SECURE must be auto, true or false")
            return https
        if raw == "false" and https:
            self.problems.append("APPEXT_COOKIE_SECURE=false is not allowed with an https APPEXT_PUBLIC_URL")
        if raw == "true" and not https:
            self.problems.append("APPEXT_COOKIE_SECURE=true needs an https APPEXT_PUBLIC_URL (browsers drop Secure cookies on http)")
        return https

    def read_services(self) -> dict[str, str]:
        urls: dict[str, str] = {}
        for svc in self.manifest.services if self.manifest else ():
            variable = f"APPEXT_SERVICE_{svc.env_name}_URL"
            url = self.get(variable)
            if url is None and self.local:
                url = LOCAL_SERVICE_URLS.get(svc.audience)
            if url is None:
                if self.local:
                    log.warning("%s is not set: calls to service %r will fail", variable, svc.name)
                else:
                    self.problems.append(f"{variable} is required (service {svc.name!r} in the manifest)")
                continue
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                self.problems.append(f"{variable} must be an http(s) URL")
                continue
            if parts.scheme == "http" and not (self.local or is_loopback(url)):
                # Internal service names (http://export-api.internal) are common inside a cluster;
                # the tokens we send are short-lived and audience-bound, so this is a warning.
                log.warning("%s uses plain http: the bearer token travels unencrypted", variable)
            urls[svc.name] = url.rstrip("/")
        return urls
