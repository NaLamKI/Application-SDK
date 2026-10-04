"""The lock file `extension.lock.toml`: the approved state, checked at start.

The auth bundle from the store carries the lock file. It records what the
review approved (version, client, scopes). At start the SDK compares it with
the manifest and **refuses to run** when the manifest asks for more. A forgotten
review then shows up before the deployment goes live and not as a failing
token exchange in production (`concepts/app-store.md` section 9).

A missing file is only acceptable in `local`: without the store there is
nothing to approve against. Anywhere else it is an error, because "no lock
file" must not become the way around the check.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .manifest import Manifest

if TYPE_CHECKING:
    from .config import ExtensionSettings

LOCK_FILENAME = "extension.lock.toml"


class LockError(RuntimeError):
    """The manifest is not covered by the approved lock file."""


@dataclass(frozen=True)
class Lock:
    extension: str
    version: str
    client_id: str
    approved_scopes: tuple[str, ...]
    environment: str | None = None
    generated_at: str | None = None
    keycloak_client_uuid: str | None = None


def read_lock(path: str | Path) -> Lock:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LockError(f"lock file not found: {path}") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise LockError(f"cannot read lock file {path}: {err}") from None

    section = data.get("lock")
    if not isinstance(section, dict):
        raise LockError(f"{path}: section [lock] is missing")
    missing = [k for k in ("extension", "version", "client_id", "approved_scopes") if k not in section]
    if missing:
        raise LockError(f"{path}: missing {', '.join(missing)} in [lock]")
    scopes = section["approved_scopes"]
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise LockError(f"{path}: approved_scopes must be a list of strings")
    for key in ("extension", "version", "client_id"):
        if not isinstance(section[key], str):
            raise LockError(f"{path}: {key} must be a string")
    return Lock(
        extension=section["extension"],
        version=section["version"],
        client_id=section["client_id"],
        approved_scopes=tuple(scopes),
        environment=_optional_str(section.get("environment")),
        generated_at=_optional_str(section.get("generated_at")),
        keycloak_client_uuid=_optional_str(section.get("keycloak_client_uuid")),
    )


def _optional_str(value: object) -> str | None:
    # TOML dates are real dates; the lock only needs them as text.
    return None if value is None else str(value)


def check_lock(
    manifest: Manifest,
    lock: Lock,
    *,
    environment: str | None = None,
    client_id: str | None = None,
) -> None:
    """Raise `LockError` unless the lock covers the manifest.

    Scopes are the substance: everything the manifest names (consent and all
    services) must be in `approved_scopes`. Identity, version and environment
    must match as well – a lock from another extension, another version or
    another environment says nothing about this deployment.
    """
    if lock.extension != manifest.id:
        raise LockError(f"the lock file belongs to extension {lock.extension!r}, the manifest to {manifest.id!r}")
    if lock.version != manifest.version:
        raise LockError(
            f"the lock file approves version {lock.version}, the manifest says {manifest.version}: "
            "fetch the auth bundle of this version (or submit it for review first)"
        )
    expected_client = client_id or manifest.client_id
    if lock.client_id != expected_client:
        raise LockError(f"the lock file is for client {lock.client_id!r}, this deployment uses {expected_client!r}")
    if environment is not None and lock.environment is not None and lock.environment != environment:
        raise LockError(
            f"the lock file was generated for environment {lock.environment!r}, this deployment runs as {environment!r}"
        )
    extra = [s for s in dict.fromkeys(manifest.all_scopes) if s not in lock.approved_scopes]
    if extra:
        raise LockError(
            "the manifest asks for scopes the store has not approved: "
            + ", ".join(extra)
            + " – submit this version for review and deploy the new auth bundle"
        )


def find_lock_path(manifest: Manifest, settings: "ExtensionSettings") -> Path | None:
    """Where the lock file is expected: `APPEXT_LOCK_FILE`, next to the manifest, or the working directory."""
    if settings.lock_file is not None:
        return settings.lock_file
    candidates = []
    if manifest.base_dir is not None:
        candidates.append(manifest.base_dir / LOCK_FILENAME)
    candidates.append(Path.cwd() / LOCK_FILENAME)
    for path in candidates:
        if path.is_file():
            return path
    return None


def check_startup(manifest: Manifest, settings: "ExtensionSettings") -> Lock | None:
    """The check `Extension.asgi()` runs: read the lock if there is one and compare."""
    path = find_lock_path(manifest, settings)
    if path is None or (settings.lock_file is None and not path.is_file()):
        if settings.is_local:
            return None
        raise LockError(
            f"{LOCK_FILENAME} not found (looked next to the manifest and in the working directory; "
            "APPEXT_LOCK_FILE sets the path). It comes with the auth bundle from the store"
        )
    lock = read_lock(path)
    check_lock(manifest, lock, environment=settings.env, client_id=settings.client_id)
    return lock
