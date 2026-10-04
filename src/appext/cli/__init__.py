"""The `appext` command line.

    appext new <id>           project from a template
    appext dev                run it locally in browser mode
    appext manifest check     the manifest rules (+ secret scan for CI)
    appext keys generate      client key pair
    appext store …            register, submit, bundle, verify

`main()` returns the exit code instead of calling `sys.exit`, so tests (and the
console-script wrapper) decide what to do with it.
"""

from __future__ import annotations

import importlib
from typing import Sequence

from .context import CliError, Context

__all__ = ["main", "Context", "CliError"]


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("appext")
    except Exception:  # not installed (running from a checkout)
        return "unknown"


def _resolve(handler: str):
    module, _, name = handler.partition(":")
    return getattr(importlib.import_module(module), name)


def main(argv: Sequence[str] | None = None, ctx: Context | None = None) -> int:
    from .parser import build_parser

    ctx = ctx or Context()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        ctx.say(f"appext {_version()}")
        return 0
    if not getattr(args, "handler", None):
        parser.print_help(ctx.err)
        return 2
    try:
        return int(_resolve(args.handler)(args, ctx) or 0)
    except CliError as error:
        print(f"error: {error.message}", file=ctx.err)
        if error.hint:
            print(f"  {error.hint}", file=ctx.err)
        return error.code
    except KeyboardInterrupt:
        return 130
