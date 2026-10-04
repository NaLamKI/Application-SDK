"""The platform an extension is written for: which OAuth service and which App Store it talks to.

`appext` is not tied to one platform. A **platform** is any system that follows the reference
architecture: it offers an OAuth 2.0 / OpenID Connect service (the *issuer*), an App Store that
registers, reviews and lists extensions (the *store*), and optionally a host app that opens
extensions in a WebView or a frame. Everything the SDK needs to know about it fits in a handful of
settings, collected here as a `Platform`:

```toml
[platform]
name = "Acme Platform"                               # shown in messages and in the bridge's "Back to …" bar
issuer = "https://auth.acme.example/realms/acme"     # the OAuth / OpenID Connect service
store_url = "https://api.acme.example/api/v1"        # the App Store API; `/store/…` is appended
cli_client_id = "appext-cli"                         # optional: the public client of the CLI at the issuer
app_redirect_uri = "com.acme.app.ext:/callback"      # optional: where the host app takes the sign-in back

[platform.services]                                  # optional: local-development URLs of target services
data-api = "http://127.0.0.1:8000/api/v1"

[platform.starter]                                   # optional: what `appext new` puts into a fresh manifest
service = "data"
audience = "data-api"
scope = "data-read"
```

Where the settings come from, strongest first (each setting on its own):

1. a command-line option (`--issuer`, `--store-url`, `--client-id`, `--platform`);
2. an environment variable (`APPEXT_ISSUER`, `APPEXT_STORE_URL`, `APPEXT_CLI_CLIENT_ID`,
   `APPEXT_APP_REDIRECT_URI`, `APPEXT_PLATFORM`);
3. the **platform file**: `--platform`/`APPEXT_PLATFORM` names it (a path, or a name that is looked up
   as `<config dir>/platforms/<name>.toml`); without either, `appext.toml` in the project directory,
   then `<config dir>/platform.toml`.

Nothing is guessed. A setting that is needed and found nowhere is an error that names the three ways
to provide it – the SDK has no built-in platform.

A running extension does not read this file in production: the auth bundle the store issues carries
the same values as `APPEXT_*` variables. The file is for development at the desk (`appext dev`,
`appext store …`, `appext new`), where nobody has issued a bundle yet.
"""
from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

#: The project's own platform file, next to `extension.toml`.
PROJECT_FILE = "appext.toml"
DEFAULT_CLI_CLIENT_ID = "appext-cli"

_PLATFORM_KEYS = {"name", "issuer", "store_url", "cli_client_id", "app_redirect_uri", "services", "starter"}
_STARTER_KEYS = {"service", "audience", "scope"}
_TOP_LEVEL_KEYS = {"platform"}


class PlatformError(ValueError):
    """A platform file that cannot be used. `problems` lists every reason, one line each."""

    def __init__(self, problems: list[str], *, source: Path | None = None) -> None:
        self.problems = problems
        self.source = source
        where = f"{source}: " if source else ""
        super().__init__(where + "; ".join(problems))


@dataclass(frozen=True)
class Starter:
    """What `appext new` puts into a fresh manifest: one service call that works against the platform."""

    service: str = "data"
    audience: str = "data-api"
    scope: str = "data-read"


@dataclass(frozen=True)
class Platform:
    """Where an extension's sign-in and App Store are. Every field may be unknown (`None`)."""

    name: str = ""
    issuer: str | None = None
    store_url: str | None = None
    cli_client_id: str = DEFAULT_CLI_CLIENT_ID
    app_redirect_uri: str | None = None
    #: Target service (by audience) -> base URL, for development on this machine.
    services: Mapping[str, str] = field(default_factory=dict)
    starter: Starter = field(default_factory=Starter)
    #: The file these settings came from; `None` for the environment alone.
    source: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))

    @property
    def display_name(self) -> str:
        """What the person reads in messages: the configured name, else a neutral phrase."""
        return self.name or "the platform"

    def to_toml(self) -> str:
        """The `[platform]` document of these settings (what `appext new` writes into a project)."""
        lines = ["[platform]"]
        for key, value in (
            ("name", self.name),
            ("issuer", self.issuer),
            ("store_url", self.store_url),
            ("cli_client_id", self.cli_client_id if self.cli_client_id != DEFAULT_CLI_CLIENT_ID else None),
            ("app_redirect_uri", self.app_redirect_uri),
        ):
            if value:
                lines.append(f"{key} = {_quote(value)}")
        if self.services:
            lines += ["", "[platform.services]", *(f"{_quote_key(k)} = {_quote(v)}" for k, v in self.services.items())]
        lines += [
            "",
            "[platform.starter]",
            f"service = {_quote(self.starter.service)}",
            f"audience = {_quote(self.starter.audience)}",
            f"scope = {_quote(self.starter.scope)}",
        ]
        return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _quote_key(key: str) -> str:
    return key if key.replace("-", "").replace("_", "").isalnum() else _quote(key)


# --- reading a file ---------------------------------------------------------------------------------


def _http_url(value: Any, path: str, problems: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{path} must be a non-empty string")
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        problems.append(f"{path} must be an http(s) URL")
        return None
    return value.rstrip("/")


def parse_platform(data: Mapping[str, Any], *, source: Path | None = None) -> Platform:
    """Validate parsed platform data (the content of a platform file) and build a `Platform`."""
    problems: list[str] = []
    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            problems.append(f"{key}: unknown table")
    table = data.get("platform")
    if table is None:
        if problems:
            raise PlatformError(problems, source=source)
        return Platform(source=source)
    if not isinstance(table, Mapping):
        raise PlatformError(["platform must be a table"], source=source)
    for key in table:
        if key not in _PLATFORM_KEYS:
            problems.append(f"platform.{key}: unknown key")

    def text(key: str) -> str | None:
        if key not in table:
            return None
        value = table[key]
        if not isinstance(value, str) or not value.strip():
            problems.append(f"platform.{key} must be a non-empty string")
            return None
        return value

    issuer = _http_url(table["issuer"], "platform.issuer", problems) if "issuer" in table else None
    store_url = _http_url(table["store_url"], "platform.store_url", problems) if "store_url" in table else None
    name = text("name")
    if name is not None and (len(name) > 80 or any(c in name for c in '"\\<>&') or any(ord(c) < 0x20 for c in name)):
        # It lands in TOML strings and pages of generated projects, unescaped.
        problems.append('platform.name must be at most 80 characters without quotes, backslashes, < > & or line breaks')
        name = None
    client_id = text("cli_client_id")
    redirect = text("app_redirect_uri")
    if redirect is not None and (not urlsplit(redirect).scheme or "#" in redirect):
        problems.append("platform.app_redirect_uri must be a URI with a scheme and no fragment")
        redirect = None

    services: dict[str, str] = {}
    raw_services = table.get("services", {})
    if not isinstance(raw_services, Mapping):
        problems.append("platform.services must be a table of audience = base URL")
    else:
        for audience, url in raw_services.items():
            checked = _http_url(url, f"platform.services.{audience}", problems)
            if checked:
                services[str(audience)] = checked

    starter = Starter()
    raw_starter = table.get("starter")
    if raw_starter is not None:
        if not isinstance(raw_starter, Mapping):
            problems.append("platform.starter must be a table")
        else:
            values = {"service": starter.service, "audience": starter.audience, "scope": starter.scope}
            for key, value in raw_starter.items():
                if key not in _STARTER_KEYS:
                    problems.append(f"platform.starter.{key}: unknown key")
                elif not isinstance(value, str) or not value.strip():
                    problems.append(f"platform.starter.{key} must be a non-empty string")
                else:
                    values[key] = value
            starter = Starter(**values)

    if problems:
        raise PlatformError(problems, source=source)
    return Platform(
        name=name or "",
        issuer=issuer,
        store_url=store_url,
        cli_client_id=client_id or DEFAULT_CLI_CLIENT_ID,
        app_redirect_uri=redirect,
        services=services,
        starter=starter,
        source=source,
    )


def load_platform_file(path: str | os.PathLike[str]) -> Platform:
    """Read a platform file. A missing or unreadable file is a `PlatformError`, never a guess."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PlatformError(["the platform file does not exist"], source=file) from None
    except (OSError, UnicodeDecodeError) as error:
        raise PlatformError([f"cannot read it: {error}"], source=file) from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PlatformError([f"not valid TOML: {error}"], source=file) from None
    return parse_platform(data, source=file.resolve())


# --- finding it -------------------------------------------------------------------------------------


def config_dir(environ: Mapping[str, str]) -> Path:
    """`$XDG_CONFIG_HOME/appext`, else `~/.config/appext` – where the CLI keeps what belongs to the person."""
    base = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "appext"


def platform_file(
    environ: Mapping[str, str],
    *,
    explicit: str | None = None,
    project_dir: Path | None = None,
    cwd: Path | None = None,
    user_default: bool = True,
) -> Path | None:
    """Which platform file applies, or `None` if the environment alone has to do.

    `explicit` (`--platform`) beats `APPEXT_PLATFORM`; either is a path (it contains a path separator
    or ends in `.toml`) or the name of a file under `<config dir>/platforms/`. A named file that does
    not exist is an error – asking for something and silently getting something else is worse.
    """
    chosen = explicit or environ.get("APPEXT_PLATFORM") or None
    if chosen:
        looks_like_path = os.sep in chosen or "/" in chosen or chosen.endswith(".toml")
        if looks_like_path:
            path = Path(chosen).expanduser()
            return path if path.is_absolute() else (cwd or Path.cwd()) / path
        return config_dir(environ) / "platforms" / f"{chosen}.toml"
    for directory in (project_dir, cwd):
        if directory is not None and (directory / PROJECT_FILE).is_file():
            return directory / PROJECT_FILE
    if not user_default:
        return None
    default = config_dir(environ) / "platform.toml"
    return default if default.is_file() else None


def discover(
    environ: Mapping[str, str],
    *,
    explicit: str | None = None,
    project_dir: Path | None = None,
    cwd: Path | None = None,
    user_default: bool = True,
) -> Platform:
    """The platform file's settings, else an empty `Platform` (nothing configured)."""
    path = platform_file(environ, explicit=explicit, project_dir=project_dir, cwd=cwd, user_default=user_default)
    return load_platform_file(path) if path is not None else Platform()


def with_environment(platform: Platform, environ: Mapping[str, str]) -> Platform:
    """`platform` with the environment variables laid over it (they win, setting by setting)."""
    from dataclasses import replace

    def pick(variable: str, current: str | None, *, url: bool = False) -> str | None:
        """The variable's value if it says something (blank counts as unset), else `current`."""
        value = (environ.get(variable) or "").strip()
        if not value:
            return current
        return value.rstrip("/") if url else value

    return replace(
        platform,
        issuer=pick("APPEXT_ISSUER", platform.issuer, url=True),
        store_url=pick("APPEXT_STORE_URL", platform.store_url, url=True),
        cli_client_id=pick("APPEXT_CLI_CLIENT_ID", platform.cli_client_id) or DEFAULT_CLI_CLIENT_ID,
        app_redirect_uri=pick("APPEXT_APP_REDIRECT_URI", platform.app_redirect_uri),
    )


#: What the message says when a needed setting is nowhere.
HOW_TO_CONFIGURE = (
    "set it with {option}, with the environment variable {variable}, or in a platform file "
    "([platform] {key} in appext.toml next to extension.toml, or --platform NAME for "
    "~/.config/appext/platforms/NAME.toml)"
)


def missing(what: str, *, option: str, variable: str, key: str) -> str:
    """The sentence for a setting that is needed and not set anywhere."""
    return f"no {what} is configured: " + HOW_TO_CONFIGURE.format(option=option, variable=variable, key=key)
