"""The project in the working directory: where its manifest is and what it says.

Reading and validating is `appext.manifest` – the one implementation of the rules
that the SDK and the store both answer to. This module only finds the file and
turns a `ManifestError` into something a person can read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from appext.manifest import Manifest, ManifestError, ServiceSpec, loads_manifest

from .context import CliError, Context

MANIFEST_NAME = "extension.toml"
DEFAULT_DEV_PORT = 8000


@dataclass(frozen=True)
class Project:
    root: Path
    manifest_path: Path
    text: str
    manifest: Manifest

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def kind(self) -> str:
        return self.manifest.kind

    @property
    def is_link(self) -> bool:
        return self.manifest.is_link

    @property
    def entry(self) -> str:
        """A path on the extension's own origin – for a link, the absolute address it opens."""
        return self.manifest.entry

    @property
    def client_id(self) -> str:
        return self.manifest.client_id

    @property
    def client_auth(self) -> str:
        return self.manifest.client_auth

    @property
    def display(self) -> str:
        return self.manifest.display

    @property
    def dev_port(self) -> int:
        return self.manifest.dev_port or DEFAULT_DEV_PORT

    @property
    def icon(self) -> str:
        """A file of the project – for a link an address, or `""` when it names none."""
        return self.manifest.icon

    @property
    def consent_scopes(self) -> tuple[str, ...]:
        return self.manifest.consent.scopes

    @property
    def services(self) -> tuple[ServiceSpec, ...]:
        return self.manifest.services


def manifest_path(ctx: Context, given: str | None) -> Path:
    """`extension.toml` itself, or a directory that holds it; default: the working directory."""
    path = ctx.path(given) if given else ctx.cwd
    if path.is_dir():
        path = path / MANIFEST_NAME
    if not path.is_file():
        where = f"in {path.parent}" if given else "here"
        raise CliError(
            f"no {MANIFEST_NAME} {where}",
            hint="Run the command in a project created by `appext new`, or pass the path.",
        )
    return path


def format_errors(errors) -> str:
    lines = [f"  {e.get('path') or '(file)'}: {e['message']}" for e in errors]
    return f"the manifest breaks {len(lines)} rule(s):\n" + "\n".join(lines)


def validate(text: str) -> Manifest:
    """The parsed manifest, or a `CliError` listing every rule it breaks."""
    try:
        return loads_manifest(text)
    except ManifestError as error:
        raise CliError(format_errors(error.errors)) from error


def load_project(ctx: Context, given: str | None = None) -> Project:
    path = manifest_path(ctx, given)
    text = path.read_text(encoding="utf-8")
    return Project(path.parent, path, text, validate(text))


def refuse_link(project: Project, what: str) -> None:
    """Stop a command that needs a server, a key or a client when the project is a link.

    A link is only an entry in the store: no server to run, no Keycloak client, no key, no deployment.
    `what` finishes the sentence "there is …".
    """
    if project.is_link:
        raise CliError(
            f"{project.id} is a link, and a link has no server: there is {what}.",
            hint="A link is published with `appext store register`, `submit` and – once a reviewer approved it – `verify`.",
        )


def extension_id(ctx: Context, given_id: str | None, manifest: str | None, *, not_for_a_link: str | None = None) -> str:
    """The id on the command line, else the one in the project's manifest.

    With `not_for_a_link` (what a link lacks, as `refuse_link` words it), a command that cannot work on a
    link refuses it – when the manifest is what names the extension. An id given on the command line is
    the store's to judge: it answers 409 for a link.
    """
    if given_id:
        return given_id
    project = load_project(ctx, manifest)
    if not_for_a_link:
        refuse_link(project, not_for_a_link)
    return project.id
