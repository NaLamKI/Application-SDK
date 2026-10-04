"""`appext manifest check`: the manifest rules 1–6 and 8 with readable output."""

from __future__ import annotations

from .context import CliError, Context
from .project import Project, manifest_path, validate

RULE_7 = "Scopes and audiences are checked against the service catalog by the App Store (rule 7): `appext store register`."
LINK_NOTE = "A link has no server, key or deployment; the review is its only gate: `appext store register`, `submit`, then `verify`."


def _warnings(project: Project) -> list[str]:
    warnings = []
    if project.is_link:
        return warnings  # its icon is an address on the link's own host, not a file of the project
    if not (project.root / project.icon).is_file():
        warnings.append(f"icon {project.icon!r} does not exist yet – the catalog shows no icon until it does")
    return warnings


def _link_summary(project: Project) -> list[str]:
    lines = [
        f"id         {project.id}",
        f"name       {project.name}",
        f"version    {project.version}",
        f"kind       {project.kind}",
        f"entry      {project.entry}",
        f"display    {project.display}  (the app opens it in the system browser)",
    ]
    if project.icon:
        lines.append(f"icon       {project.icon}")
    return lines


def _summary(project: Project) -> list[str]:
    if project.is_link:
        return _link_summary(project)
    lines = [
        f"id         {project.id}  (OAuth client {project.client_id})",
        f"name       {project.name}",
        f"version    {project.version}",
        f"client     {project.client_auth}",
        f"display    {project.display}" + ("  (the app opens it in the system browser)" if project.manifest.external else ""),
        f"consent    {', '.join(project.consent_scopes) or '-'}",
    ]
    for service in project.services:
        scopes = ", ".join(service.scopes)
        lines.append(f"service    {service.name} ({service.mode}) -> {service.audience} [{scopes}]")
    return lines


def check(args, ctx: Context) -> int:
    path = manifest_path(ctx, args.path)
    text = path.read_text(encoding="utf-8")
    try:
        manifest = validate(text)
    except CliError as error:
        ctx.say(f"{path.name}: INVALID")
        ctx.say(error.message)
        return 1
    project = Project(path.parent, path, text, manifest)
    ctx.say(f"{path.name}: OK")
    for line in _summary(project):
        ctx.say(f"  {line}")
    for warning in _warnings(project):
        ctx.warn(warning)
    ctx.say(f"  note: {LINK_NOTE if project.is_link else RULE_7}")

    if not args.scan_secrets:
        return 0
    from .scan import scan

    findings = scan(project.root)
    if not findings:
        ctx.say("secret scan: nothing found")
        return 0
    ctx.say(f"secret scan: {len(findings)} finding(s) – values are never printed")
    for finding in findings:
        ctx.say(f"  {finding}")
    ctx.say("Keep keys and secrets out of the project tree and the image: list them in .gitignore and .dockerignore.")
    return 1
