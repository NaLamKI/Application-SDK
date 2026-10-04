"""`appext dev`, `appext serve`, `appext health`.

`dev` is the extension in **browser mode** on the developer's machine; `serve`
is what the container runs. They share the module:attribute loading of uvicorn
and nothing else – `dev` fills in local defaults, `serve` takes the environment
as given and fails loudly (in the SDK's settings) when it is incomplete.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING

from .context import CliError, Context

if TYPE_CHECKING:  # `appext health` runs every few seconds in a container: keep its imports to the minimum
    from .project import Project

DEFAULT_PORT = 8000


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` lines, `#` comments, optional quotes – what a bundle's `appext.env` contains."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CliError(f"cannot read {path}: {error.strerror or error}") from None
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def appext_only(ctx: Context, values: dict[str, str], source: Path) -> dict[str, str]:
    """An env file configures the extension, nothing else: `PATH` or `PYTHONPATH` in a downloaded
    bundle must not change how the developer's machine runs code."""
    foreign = sorted(k for k in values if not k.startswith("APPEXT_"))
    if foreign:
        ctx.warn(f"{source.name}: ignoring {', '.join(foreign)} (only APPEXT_* variables are taken over)")
    return {k: v for k, v in values.items() if k.startswith("APPEXT_")}


def local_defaults(project: Project, *, host: str, port: int, key_file: Path, key_id: str, session_key_file: Path) -> dict[str, str]:
    """What `appext dev` adds to the SDK's own local defaults (issuer, redirect URI, service URLs):
    the address it serves on, the dev key and an in-memory session store."""
    defaults = {
        "APPEXT_ENV": "local",
        "APPEXT_PUBLIC_URL": f"http://{host}:{port}",
        "APPEXT_SESSION_STORE": "memory",
        "APPEXT_SESSION_KEY_FILE": str(session_key_file),
    }
    if project.client_auth == "private_key_jwt":
        defaults["APPEXT_CLIENT_KEY_FILE"] = str(key_file)
        defaults["APPEXT_CLIENT_KEY_ID"] = key_id
    return defaults


def dev(args, ctx: Context) -> int:
    from appext.config import ConfigError, ExtensionSettings

    from .keys import ensure_dev_key, ensure_dev_session_key
    from .project import load_project, refuse_link

    project = load_project(ctx, args.manifest)
    refuse_link(project, "nothing to run")  # before a key is made or a port is picked
    port = args.port or project.dev_port
    key_file, jwk_file, created = ensure_dev_key(project.root / ".appext")
    session_key_file = ensure_dev_session_key(project.root / ".appext")
    key_id = json.loads(jwk_file.read_text(encoding="utf-8"))["kid"]

    layered = local_defaults(project, host=args.host, port=port, key_file=key_file, key_id=key_id,
                             session_key_file=session_key_file)
    for env_file in args.env_file:
        source = ctx.path(env_file)
        layered.update(appext_only(ctx, read_env_file(source), source))
    layered.update({k: v for k, v in ctx.env.items() if k.startswith("APPEXT_")})  # what the shell says wins
    try:
        settings = ExtensionSettings.from_env(project.manifest, layered)
    except ConfigError as error:
        raise CliError("the configuration is not usable:\n" + "\n".join(f"  - {p}" for p in error.problems)) from None
    # The server (and uvicorn's reloader child) reads the real environment.
    (ctx.env if isinstance(ctx.env, MutableMapping) else os.environ).update(layered)

    ctx.say(f"{project.name} ({settings.client_id}) on {settings.public_url}")
    ctx.say(f"  sign-in    {settings.issuer}")
    ctx.say(f"  client key {ctx.show(key_file)}" + ("  (created now)" if created else ""))
    for service in project.services:
        if service.name not in settings.services:
            ctx.warn(f"APPEXT_SERVICE_{service.env_name}_URL is not set – calls to {service.name!r} will fail "
                     "(set it in the environment or an --env-file)")
    ctx.say("")
    ctx.say("Not known to the local store yet? Register it, then have a reviewer approve it:")
    ctx.say("  appext store login && appext store register && appext store submit")
    ctx.say("or, for a Keycloak of your own: appext keycloak export --out .appext/realm-ext.json --dev-user")
    ctx.say("")

    package = project.root / args.app.partition(":")[0].split(".")[0]
    watched = package if package.is_dir() else project.root
    ctx.run_server(
        args.app,
        host=args.host,
        port=port,
        app_dir=str(project.root),
        reload=not args.no_reload,
        reload_dirs=None if args.no_reload else [str(watched)],
        log_level="info",
    )
    return 0


def _port(args, ctx: Context) -> int:
    if args.port:
        return args.port
    try:
        return int(ctx.env.get("PORT") or DEFAULT_PORT)
    except ValueError:
        raise CliError(f"PORT is not a number: {ctx.env.get('PORT')!r}") from None


def _refuse_a_link_here(ctx: Context) -> None:
    """The manifest in the working directory, if there is a usable one, must not be a link's.

    A manifest that is missing or broken is none of `serve`'s business: the app the person names reads it
    (or not) and complains in its own words.
    """
    from .project import load_project, refuse_link

    try:
        project = load_project(ctx)
    except CliError:
        return
    refuse_link(project, "nothing to serve")


def serve(args, ctx: Context) -> int:
    _refuse_a_link_here(ctx)
    # Proxy headers (X-Forwarded-For/-Proto) decide the client address and the
    # scheme the SDK sees. Believe them only from the ingress the operator names;
    # from anyone else they are just a way to lie.
    trusted = ctx.env.get("APPEXT_TRUSTED_PROXIES", "").strip()
    if trusted == "*":
        ctx.warn("APPEXT_TRUSTED_PROXIES=* trusts proxy headers from every address; name the ingress instead")
    options = {"proxy_headers": bool(trusted)}
    if trusted:
        options["forwarded_allow_ips"] = trusted
    ctx.run_server(
        args.app,
        host=args.host,
        port=_port(args, ctx),
        app_dir=str(ctx.cwd),
        workers=args.workers if args.workers > 1 else None,
        server_header=False,
        **options,
    )
    return 0


def health(args, ctx: Context) -> int:
    url = f"http://127.0.0.1:{_port(args, ctx)}/healthz"
    try:
        # urllib, not httpx: this runs every few seconds inside the container.
        with urllib.request.urlopen(url, timeout=3) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError):
        return 1
