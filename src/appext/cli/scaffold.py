"""`appext new`: render a template directory into a new project.

Placeholders are the exact tokens `{{id}}`, `{{name}}` and `{{package}}` –
without inner spaces, so that Jinja's own `{{ name }}` in the HTMX pages is never
touched. The `link` template is a manifest and a README, nothing else: a link has no server.
"""

from __future__ import annotations

import re
from pathlib import Path

from .context import CliError, Context
from .project import validate

PLACEHOLDER = re.compile(r"\{\{(id|name|package)\}\}")
#: Copied byte for byte: vendored or generated files are not ours to rewrite.
VERBATIM = ("*.min.js", "package-lock.json")
SKIP_PARTS = {"node_modules", "__pycache__", ".pytest_cache", ".DS_Store", "dist", ".appext", ".venv"}


def templates_dir() -> Path:
    """The templates of the installed wheel, else of the source tree (editable install)."""
    here = Path(__file__).resolve()
    for candidate in (here.parents[1] / "_templates", here.parents[3] / "templates"):
        if candidate.is_dir():
            return candidate
    raise CliError("the project templates are missing from this installation")


def display_name(extension_id: str) -> str:
    return extension_id.replace("-", " ").title()


def _values(extension_id: str, name: str) -> dict[str, str]:
    return {"id": extension_id, "name": name, "package": extension_id.replace("-", "_")}


def _render(text: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not SKIP_PARTS & set(path.relative_to(root).parts):
            yield path


def _check_name(name: str) -> None:
    """The name lands in TOML strings and HTML text unescaped; keep out what would break either."""
    if any(c in name for c in '"\\<>&') or any(ord(c) < 0x20 for c in name):
        raise CliError("the display name must not contain quotes, backslashes, < > &, or line breaks")


def new(args, ctx: Context) -> int:
    name = args.name or display_name(args.id)
    _check_name(name)
    values = _values(args.id, name)
    source = templates_dir() / args.template
    target = ctx.path(args.dir) / args.id
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise CliError(f"{target} exists and is not empty")

    # The manifest decides whether the id is acceptable – check it before any file is written.
    validate(_render((source / "extension.toml").read_text(encoding="utf-8"), values))

    for path in _files(source):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if any(path.match(pattern) for pattern in VERBATIM):
            destination.write_bytes(path.read_bytes())
            continue
        try:
            destination.write_text(_render(path.read_text(encoding="utf-8"), values), encoding="utf-8")
        except UnicodeDecodeError:  # an image: copy as it is
            destination.write_bytes(path.read_bytes())
        destination.chmod(path.stat().st_mode & 0o777)

    shown = target.relative_to(ctx.cwd) if target.is_relative_to(ctx.cwd) else target
    ctx.say(f"Created {shown} from the {args.template} template.")
    ctx.say("")
    ctx.say("Next:")
    ctx.say(f"  cd {shown}")
    if args.template == "link":  # no server: nothing to install, test or run
        ctx.say("  edit extension.toml: set `entry` to the address the app should open")
        ctx.say("  appext manifest check")
        ctx.say("  appext store login")
        ctx.say("  appext store register")
        ctx.say("  appext store submit")
        ctx.say("  (a reviewer approves it)")
        ctx.say("  appext store verify")
        return 0
    ctx.say('  python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[test]"')
    if args.template == "spa":
        ctx.say("  (cd frontend && npm ci && npm run build)")
    ctx.say("  pytest")
    ctx.say("  appext dev")
    return 0
