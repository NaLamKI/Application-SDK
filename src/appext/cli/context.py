"""What a command may touch: environment, working directory, output, network, clock.

Commands receive a `Context` instead of reaching for `os.environ`, `sys.stdout`,
`time.sleep` or `httpx` directly. That is what lets the tests drive `main()` end
to end against a mocked store and identity provider – and a fake clock for the
device-flow polling – without patching globals.
"""

from __future__ import annotations

import os
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, TextIO

if TYPE_CHECKING:  # httpx is imported lazily: `appext health` runs as a Docker HEALTHCHECK
    import httpx


class CliError(Exception):
    """A failure the person can act on. `main()` prints it and exits with `code`."""

    def __init__(self, message: str, *, hint: str | None = None, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


def _run_uvicorn(target: str, **options) -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as error:  # pragma: no cover - depends on the install
        raise CliError("uvicorn is not installed", hint="Reinstall appext: pip install --force-reinstall appext") from error
    uvicorn.run(target, **options)


@dataclass
class Context:
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    cwd: Path = field(default_factory=Path.cwd)
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    #: Tests hand in an `httpx.MockTransport`; `None` means the real network.
    transport: "httpx.BaseTransport | None" = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    open_url: Callable[[str], object] = webbrowser.open
    #: `uvicorn.run` – replaced in tests so that no server starts.
    run_server: Callable[..., None] = _run_uvicorn

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def warn(self, text: str) -> None:
        print(f"warning: {text}", file=self.err)

    def path(self, given: str | Path) -> Path:
        """A path as typed on the command line, relative to the working directory."""
        p = Path(given).expanduser()
        return p if p.is_absolute() else self.cwd / p

    def show(self, path: Path) -> str:
        """A path as the person should read it: relative when it lies under the working directory."""
        try:
            return str(path.relative_to(self.cwd))
        except ValueError:
            return str(path)

    @property
    def config_dir(self) -> Path:
        from appext.platform import config_dir

        return config_dir(self.env)

    def http(self, **options) -> "httpx.Client":
        import httpx

        options.setdefault("timeout", 30.0)
        return httpx.Client(transport=self.transport, **options)
