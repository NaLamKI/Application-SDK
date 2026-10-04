"""The manifest `extension.toml`: one reading, one set of rules.

The SDK reads the manifest at start-up, the App Store reads it on upload. Both
must reach the **same verdict**, so the rules (rules 1-6 of
`concepts/app-store.md` section 3) live here as plain checks over the parsed
data and the shared fixtures in `sdk/conformance/manifests/` run against them.
Rule 7 (does the service catalog know this audience and scope?) is the store's
business: the SDK has no catalog.

Two choices worth knowing:

* **All errors are reported, not the first.** A developer fixing a manifest
  should not have to run the check five times.
* **Unknown keys are errors** (rule 6). A typo such as `scope = [...]` would
  otherwise be accepted and silently grant nothing.
"""
from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

#: The patterns are the contract (`sdk/conformance/manifests/README.md` lists them): the store
#: copies them, and the shared cases check that both sides give the same verdict.
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
SERVICE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
ROLE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
HOST_PATTERN = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")

CLIENT_AUTH_METHODS = ("private_key_jwt", "client_secret")
SERVICE_MODES = ("user", "service")
#: Where the FMIS app shows the extension: in its own frame (the phone app's WebView, a frame under
#: the web app's header) or in the system browser.
DISPLAY_MODES = ("in_app", "external")
#: What the extension is: a web page with a server of its own that signs people in (`extension`), or
#: a **link** – an entry in the store that the app opens in the system browser, with no server of the
#: SDK behind it, no sign-in and nothing passed on (`link`).
KINDS = ("extension", "link")
#: Hosts that mean "this machine"; the only ones a link may reach over plain http.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
LINK_URL_MAX = 2048

EXTENSION_KEYS = {
    "id", "name", "description", "version", "entry", "icon", "min_app_version", "hosts",
    "audience_roles", "client_auth", "dev_port", "display", "kind", "name_localized", "description_localized",
}
REQUIRED_KEYS = ("id", "name", "version", "entry", "icon")
#: What a link has no use for: there is no server behind it, so nothing to authenticate, to run
#: locally, or to load into a WebView.
LINK_FORBIDDEN_KEYS = ("client_auth", "dev_port", "hosts")
CONSENT_KEYS = {"scopes"}
SERVICE_KEYS = {"name", "audience", "scopes", "mode"}
TOP_LEVEL_KEYS = {"extension", "consent", "services"}


class ManifestError(ValueError):
    """The manifest breaks one or more rules.

    `errors` is a list of `{"path": ..., "message": ...}` – the same shape the
    store returns with `422 invalid_manifest`. Paths look like `extension.id`,
    `consent.scopes[1]` or `services[0].mode`.
    """

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" if e["path"] else e["message"] for e in errors))

    @property
    def paths(self) -> list[str]:
        return [e["path"] for e in self.errors]


@dataclass(frozen=True)
class Consent:
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceSpec:
    """One `[[services]]` entry: a target service this extension calls."""

    name: str
    audience: str
    scopes: tuple[str, ...]
    mode: str = "user"

    @property
    def env_name(self) -> str:
        """Part of `APPEXT_SERVICE_<NAME>_URL`."""
        return self.name.upper()


def _frozen(mapping: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    version: str
    entry: str
    icon: str
    description: str = ""
    min_app_version: str | None = None
    hosts: tuple[str, ...] = ()
    audience_roles: tuple[str, ...] = ()
    client_auth: str = "private_key_jwt"
    dev_port: int | None = None
    display: str = "in_app"
    kind: str = "extension"
    name_localized: Mapping[str, str] = field(default_factory=dict)
    description_localized: Mapping[str, str] = field(default_factory=dict)
    consent: Consent = field(default_factory=Consent)
    services: tuple[ServiceSpec, ...] = ()
    #: Directory of the manifest file (where `icon` is resolved from); `None`
    #: when the manifest came from text.
    base_dir: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name_localized", _frozen(self.name_localized))
        object.__setattr__(self, "description_localized", _frozen(self.description_localized))

    @property
    def client_id(self) -> str:
        return f"ext-{self.id}"

    @property
    def external(self) -> bool:
        """The app opens it in the system browser instead of showing it itself (always true for a link)."""
        return self.display == "external"

    @property
    def is_link(self) -> bool:
        """A link: no server, no client, no sign-in – `entry` is an absolute address."""
        return self.kind == "link"

    @property
    def user_service_scopes(self) -> tuple[str, ...]:
        """Scopes the login must ask for so that the consent covers later exchanges."""
        return tuple(s for svc in self.services if svc.mode == "user" for s in svc.scopes)

    @property
    def all_scopes(self) -> tuple[str, ...]:
        """Everything the manifest asks for, in manifest order (the lock file compares this)."""
        return (*self.consent.scopes, *(s for svc in self.services for s in svc.scopes))

    def service(self, name: str) -> ServiceSpec:
        for svc in self.services:
            if svc.name == name:
                return svc
        known = ", ".join(s.name for s in self.services) or "none"
        raise KeyError(f"the manifest declares no service {name!r} (declared: {known})")


# --- reading --------------------------------------------------------------------------------


def load_manifest(source: str | os.PathLike[str] | bytes) -> Manifest:
    """Read a manifest from a file path or from TOML text.

    A `Path` is always a file. A `str` is text when it contains a newline and
    a path otherwise (a one-line manifest is not a manifest).
    """
    if isinstance(source, bytes):
        return loads_manifest(source.decode("utf-8"))
    if isinstance(source, str) and "\n" in source:
        return loads_manifest(source)
    path = Path(source)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError([{"path": "", "message": f"manifest file not found: {path}"}]) from None
    except (OSError, UnicodeDecodeError) as err:
        raise ManifestError([{"path": "", "message": f"cannot read {path}: {err}"}]) from None
    return loads_manifest(text, base_dir=path.resolve().parent)


def loads_manifest(text: str, *, base_dir: Path | None = None) -> Manifest:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise ManifestError([{"path": "", "message": f"not valid TOML: {err}"}]) from None
    return parse_manifest(data, base_dir=base_dir)


def parse_manifest(data: Mapping[str, Any], *, base_dir: Path | None = None) -> Manifest:
    """Validate parsed manifest data (TOML or JSON) and build a `Manifest`."""
    problems = _Problems()
    values = _check(data, problems)
    if problems.errors:
        raise ManifestError(problems.errors)
    return Manifest(base_dir=base_dir, **values)


class _Problems:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.errors.append({"path": path, "message": message})


def _check(data: Mapping[str, Any], problems: _Problems) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        problems.add("", "the manifest must be a table")
        return {}
    for key in data:
        if key not in TOP_LEVEL_KEYS:
            problems.add(str(key), "unknown key")

    ext = data.get("extension")
    values: dict[str, Any] = {}
    if ext is None:
        problems.add("extension", "section [extension] is required")
    elif not isinstance(ext, Mapping):
        problems.add("extension", "must be a table")
    else:
        values.update(_check_extension(ext, problems))

    seen: dict[str, str] = {}  # scope -> where it was first named (rule 2: once across the file)
    if values.get("kind") == "link":
        consent, services = (), ()
        _check_link_has_no_grants(data, problems)
    else:
        consent = _check_consent(data.get("consent"), problems, seen)
        services = _check_services(data.get("services"), problems, seen)
    values["consent"] = Consent(scopes=consent)
    values["services"] = services
    return values


# -- [extension] -------------------------------------------------------------------------------


def _text(ext: Mapping[str, Any], key: str, problems: _Problems, *, required: bool) -> str | None:
    path = f"extension.{key}"
    if key not in ext:
        if required:
            problems.add(path, "required")
        return None
    value = ext[key]
    if not isinstance(value, str) or not value.strip():
        problems.add(path, "must be a non-empty string")
        return None
    return value


def link_url_problem(url: Any) -> str | None:
    """Why `url` is no address a link may point to – or `None`.

    An absolute `https` address (`http` only for this machine), with a host name that is plain
    ASCII, no user name or password, no whitespace. Both implementations of the manifest rules
    (this one and the store's) judge exactly this; the shared cases cover it.
    """
    if not isinstance(url, str) or not url.strip():
        return "must be a non-empty string"
    if len(url) > LINK_URL_MAX:
        return f"must not be longer than {LINK_URL_MAX} characters"
    if not url.isascii() or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in url):
        return "must be plain ASCII without spaces, control characters or backslashes (percent-encode the rest, write host names as punycode)"
    try:
        parts = urlsplit(url)
        parts.port  # a port out of range raises here
    except ValueError:
        return "is not a valid address"
    if parts.scheme not in ("https", "http"):
        return "must be an absolute https:// address"
    host = parts.hostname or ""
    if not host:
        return "has no host name"
    if "@" in parts.netloc:
        return "must not contain a user name or password"
    loopback = host in LOOPBACK_HOSTS
    if parts.scheme == "http" and not loopback:
        return "must be https (http is only for this machine: 127.0.0.1 or localhost)"
    if not loopback and not HOST_PATTERN.fullmatch(host):
        return "has no valid host name"
    return None


def _check_link_has_no_grants(data: Mapping[str, Any], problems: _Problems) -> None:
    """A link asks for nothing: no consent scopes, no services. An empty table is fine."""
    consent = data.get("consent")
    if consent is not None:
        if not isinstance(consent, Mapping):
            problems.add("consent", "must be a table")
        else:
            for key in consent:
                if key not in CONSENT_KEYS:
                    problems.add(f"consent.{key}", "unknown key")
            if consent.get("scopes") not in (None, []):
                problems.add("consent.scopes", "not allowed for a link: it asks for no permissions")
    if data.get("services") not in (None, []):
        problems.add("services", "not allowed for a link: it calls no services")


def _check_extension(ext: Mapping[str, Any], problems: _Problems) -> dict[str, Any]:
    for key in ext:
        if key not in EXTENSION_KEYS:
            problems.add(f"extension.{key}", "unknown key")

    out: dict[str, Any] = {}
    out["kind"] = "extension"
    if "kind" in ext:
        if ext["kind"] in KINDS:
            out["kind"] = ext["kind"]
        else:
            problems.add("extension.kind", f"must be one of {', '.join(KINDS)}")
    is_link = out["kind"] == "link"

    for key in REQUIRED_KEYS:
        if key == "icon" and is_link:
            continue  # optional for a link, and an address instead of a file
        out[key] = _text(ext, key, problems, required=True)

    if out["id"] is not None and not ID_PATTERN.fullmatch(out["id"]):
        problems.add("extension.id", "must match ^[a-z][a-z0-9-]{1,38}[a-z0-9]$ (a DNS label, 3-40 characters)")
    if out["version"] is not None and not SEMVER_PATTERN.fullmatch(out["version"]):
        problems.add("extension.version", "must be a semantic version (MAJOR.MINOR.PATCH, optional -pre-release)")
    if is_link:
        if out["entry"] is not None and (reason := link_url_problem(out["entry"])):
            problems.add("extension.entry", f"a link's entry {reason}")
        out["icon"] = _link_icon(ext, out["entry"], problems)
    elif out["entry"] is not None and not _is_entry_path(out["entry"]):
        problems.add("extension.entry", "must be a path on the extension's own origin: start with a single '/', no scheme")

    out["description"] = ""
    if "description" in ext:
        if isinstance(ext["description"], str):
            out["description"] = ext["description"]  # may be empty: it is just not shown
        else:
            problems.add("extension.description", "must be a string")

    out["min_app_version"] = None
    if "min_app_version" in ext:
        value = ext["min_app_version"]
        if isinstance(value, str) and SEMVER_PATTERN.fullmatch(value):
            out["min_app_version"] = value
        else:
            problems.add("extension.min_app_version", "must be a semantic version")

    for key in LINK_FORBIDDEN_KEYS if is_link else ():
        if key in ext:
            problems.add(f"extension.{key}", "not allowed for a link: there is no server behind it")
    out["hosts"] = () if is_link else _string_list(ext, "hosts", problems, HOST_PATTERN, "must be a lower-case host name without scheme, port or path")
    out["audience_roles"] = _string_list(ext, "audience_roles", problems, ROLE_PATTERN, "must be a role name")

    out["client_auth"] = "private_key_jwt"
    if "client_auth" in ext and not is_link:
        if ext["client_auth"] in CLIENT_AUTH_METHODS:
            out["client_auth"] = ext["client_auth"]
        else:
            problems.add("extension.client_auth", f"must be one of {', '.join(CLIENT_AUTH_METHODS)}")

    out["dev_port"] = None
    if "dev_port" in ext and not is_link:
        port = ext["dev_port"]
        if type(port) is int and 1024 <= port <= 65535:
            out["dev_port"] = port
        else:
            problems.add("extension.dev_port", "must be an integer from 1024 to 65535")

    out["display"] = "external" if is_link else "in_app"
    if "display" in ext:
        if is_link:
            if ext["display"] != "external":
                problems.add("extension.display", 'a link always opens in the system browser: omit it or use "external"')
        elif ext["display"] in DISPLAY_MODES:
            out["display"] = ext["display"]
        else:
            problems.add("extension.display", f"must be one of {', '.join(DISPLAY_MODES)}")

    out["name_localized"] = _localized(ext, "name_localized", problems)
    out["description_localized"] = _localized(ext, "description_localized", problems)
    return out


def _link_icon(ext: Mapping[str, Any], entry: Any, problems: _Problems) -> str:
    """A link has no project to serve an icon from: it may name one, on the same host as its entry.

    The app loads icons on its own, before anyone opened anything – so only from the link's own host,
    like every other icon. Without one the app shows its fallback.
    """
    if "icon" not in ext:
        return ""
    icon = ext["icon"]
    if reason := link_url_problem(icon):
        problems.add("extension.icon", f"a link's icon {reason}")
        return ""
    if isinstance(entry, str) and not link_url_problem(entry):
        if (urlsplit(icon).hostname or "") != (urlsplit(entry).hostname or ""):
            problems.add("extension.icon", "a link's icon must be on the same host as its entry")
            return ""
    return icon


def _is_entry_path(entry: str) -> bool:
    """A path on the extension's own origin. `//host` and `/\\host` are both read by a browser as another origin."""
    return entry.startswith("/") and not entry.startswith(("//", "/\\"))


def _string_list(
    ext: Mapping[str, Any], key: str, problems: _Problems, pattern: re.Pattern[str], message: str
) -> tuple[str, ...]:
    if key not in ext:
        return ()
    value = ext[key]
    if not isinstance(value, list):
        problems.add(f"extension.{key}", "must be a list of strings")
        return ()
    good: list[str] = []
    for i, item in enumerate(value):
        if isinstance(item, str) and pattern.fullmatch(item):
            good.append(item)
        else:
            problems.add(f"extension.{key}[{i}]", message)
    return tuple(good)


def _localized(ext: Mapping[str, Any], key: str, problems: _Problems) -> dict[str, str]:
    if key not in ext:
        return {}
    table = ext[key]
    if not isinstance(table, Mapping):
        problems.add(f"extension.{key}", "must be a table of language code to text")
        return {}
    out: dict[str, str] = {}
    for lang, text in table.items():
        path = f"extension.{key}.{lang}"
        if not isinstance(lang, str) or not LANGUAGE_PATTERN.fullmatch(lang):
            problems.add(path, "language code must look like 'de' or 'pt-BR'")
        elif not isinstance(text, str) or not text.strip():
            problems.add(path, "must be a non-empty string")
        else:
            out[lang] = text
    return out


# -- [consent] and [[services]] -------------------------------------------------------------------


def _scope_list(value: Any, path: str, problems: _Problems, seen: dict[str, str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        problems.add(path, "must be a list of scope names")
        return ()
    scopes: list[str] = []
    for i, item in enumerate(value):
        where = f"{path}[{i}]"
        if not (isinstance(item, str) and SCOPE_PATTERN.fullmatch(item)):
            problems.add(where, "must be a scope name (^[a-z][a-z0-9-]*$)")
        elif item in seen:
            problems.add(where, f"duplicate scope {item!r} (already named at {seen[item]})")
        else:
            seen[item] = where
            scopes.append(item)
    return tuple(scopes)


def _check_consent(consent: Any, problems: _Problems, seen: dict[str, str]) -> tuple[str, ...]:
    if consent is None:
        return ()
    if not isinstance(consent, Mapping):
        problems.add("consent", "must be a table")
        return ()
    for key in consent:
        if key not in CONSENT_KEYS:
            problems.add(f"consent.{key}", "unknown key")
    if "scopes" not in consent:
        return ()
    return _scope_list(consent["scopes"], "consent.scopes", problems, seen)


def _check_services(services: Any, problems: _Problems, seen_scopes: dict[str, str]) -> tuple[ServiceSpec, ...]:
    if services is None:
        return ()
    if not isinstance(services, list):
        problems.add("services", "must be an array of tables ([[services]])")
        return ()
    out: list[ServiceSpec] = []
    seen: set[str] = set()
    for i, svc in enumerate(services):
        base = f"services[{i}]"
        if not isinstance(svc, Mapping):
            problems.add(base, "must be a table")
            continue
        for key in svc:
            if key not in SERVICE_KEYS:
                problems.add(f"{base}.{key}", "unknown key")

        name = svc.get("name")
        if "name" not in svc:
            problems.add(f"{base}.name", "required")
        elif not isinstance(name, str) or not SERVICE_NAME_PATTERN.fullmatch(name):
            problems.add(f"{base}.name", "must be a name usable in code (^[a-z][a-z0-9_]*$)")
        elif name in seen:
            problems.add(f"{base}.name", f"duplicate service name {name!r}")
        else:
            seen.add(name)

        audience = svc.get("audience")
        if "audience" not in svc:
            problems.add(f"{base}.audience", "required")
        elif not isinstance(audience, str) or not audience.strip():
            problems.add(f"{base}.audience", "must be a non-empty string")

        scopes: tuple[str, ...] = ()
        if "scopes" not in svc:
            problems.add(f"{base}.scopes", "required")
        else:
            scopes = _scope_list(svc["scopes"], f"{base}.scopes", problems, seen_scopes)
            if isinstance(svc["scopes"], list) and not svc["scopes"]:
                problems.add(f"{base}.scopes", "a service needs at least one scope")

        mode = svc.get("mode")
        if "mode" not in svc:
            problems.add(f"{base}.mode", "required")
        elif mode not in SERVICE_MODES:
            problems.add(f"{base}.mode", f"must be one of {', '.join(SERVICE_MODES)}")

        if (
            isinstance(name, str) and isinstance(audience, str) and audience.strip()
            and mode in SERVICE_MODES and scopes
        ):
            out.append(ServiceSpec(name=name, audience=audience, scopes=scopes, mode=mode))
    return tuple(out)
