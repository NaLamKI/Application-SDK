"""appext: the SDK for extensions of platforms that follow the reference architecture.

An extension is a web frontend plus a Python backend under one origin. This
package gives the backend the sign-in (OIDC, server-side session), calls to other
services on behalf of the person (token exchange), and the checks that keep all
of it safe by default. Start with `Extension`:

    from appext import Extension, User

Importing the package is cheap on purpose (the CLI imports it for `--help`):
names load their module on first use.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

#: public name -> module that defines it
_EXPORTS = {
    "Extension": "app",
    "ExtensionSettings": "config",
    "ConfigError": "config",
    "Manifest": "manifest",
    "ManifestError": "manifest",
    "ServiceSpec": "manifest",
    "load_manifest": "manifest",
    "LockError": "lock",
    "check_lock": "lock",
    "read_lock": "lock",
    "ServiceClient": "services",
    "User": "session",
    "SessionStore": "session",
    "MemoryStore": "session",
    "RedisStore": "session",
    "AuthCore": "core",
    "ConsentRequired": "core",
    "SessionInvalid": "core",
    "LoginError": "core",
    "Principal": "verify",
    "TokenVerifier": "verify",
    "require_scope": "verify",
    "require_azp": "verify",
}

__all__ = ["__version__", *_EXPORTS]

if TYPE_CHECKING:  # pragma: no cover  # names for type checkers only; they load lazily at run time
    # ruff: noqa: F401
    from .app import Extension
    from .config import ConfigError, ExtensionSettings
    from .core import AuthCore, ConsentRequired, LoginError, SessionInvalid
    from .lock import LockError, check_lock, read_lock
    from .manifest import Manifest, ManifestError, ServiceSpec, load_manifest
    from .services import ServiceClient
    from .session import MemoryStore, RedisStore, SessionStore, User
    from .verify import Principal, TokenVerifier, require_azp, require_scope


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'appext' has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
