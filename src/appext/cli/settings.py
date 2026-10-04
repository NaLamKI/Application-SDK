"""Which platform a command talks to: the issuer, the store, the CLI's client, the host app's address.

`appext.platform` knows the file format and where files are looked for; this module lays the
command line over it and turns "not configured" into an error the person can act on.

Order, strongest first, setting by setting: option on the command line, environment variable,
platform file (`--platform`/`APPEXT_PLATFORM`, then `appext.toml` in the project, then the person's
`~/.config/appext/platform.toml`). Nothing is guessed: a setting that is needed and nowhere is an
error that says how to provide it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from appext import platform as platforms
from appext.platform import Platform, PlatformError

from .context import CliError, Context


def platform_option(parser) -> None:
    """The option every command that needs a platform offers."""
    parser.add_argument(
        "--platform", metavar="NAME|FILE",
        help="the platform to work with: a platform file, or the name of one in ~/.config/appext/platforms/ "
             "(APPEXT_PLATFORM; default: appext.toml in the project, then ~/.config/appext/platform.toml)",
    )


def resolve(args, ctx: Context, *, project_dir: Path | None = None) -> Platform:
    """The platform as configured for this command. Unset settings stay `None`."""
    try:
        base = platforms.discover(ctx.env, explicit=getattr(args, "platform", None), project_dir=project_dir, cwd=ctx.cwd)
    except PlatformError as error:
        raise CliError(f"platform file: {error}", hint="See docs/platform.md of the SDK for the format.") from None
    layered = platforms.with_environment(base, ctx.env)
    issuer = getattr(args, "issuer", None)
    store_url = getattr(args, "store_url", None)
    client_id = getattr(args, "client_id", None)
    return replace(
        layered,
        issuer=issuer.rstrip("/") if issuer else layered.issuer,
        store_url=store_url.rstrip("/") if store_url else layered.store_url,
        cli_client_id=client_id or layered.cli_client_id,
    )


def require_issuer(platform: Platform) -> str:
    if not platform.issuer:
        raise CliError(platforms.missing("OAuth service (issuer)", option="--issuer", variable="APPEXT_ISSUER", key="issuer"))
    return platform.issuer


def require_store_url(platform: Platform) -> str:
    if not platform.store_url:
        raise CliError(platforms.missing("App Store", option="--store-url", variable="APPEXT_STORE_URL", key="store_url"))
    return platform.store_url
