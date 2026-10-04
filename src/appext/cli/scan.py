"""`appext manifest check --scan-secrets`: find what must never be built into an image.

Looks for private keys (PEM blocks, private JWKs) and literal `client_secret`
values in the project. A file is skipped only when it is ignored by **both**
`.gitignore` and `.dockerignore` (when present): a file that git ignores can
still be copied into the image, and a file the image ignores can still be
committed. The template's two ignore files list the same secret locations, so a
healthy project scans clean.

This is a tripwire for CI, not a proof: it catches the usual mistakes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ALWAYS_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
MAX_BYTES = 1_000_000

PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
PRIVATE_JWK_MEMBER = re.compile(rb'"d"\s*:\s*"[A-Za-z0-9_-]{20,}"')
#: `client_secret = "…"`, `"client_secret": "…"`, `APPEXT_CLIENT_SECRET=…` – but not `…_FILE`
#: (a path) and not `client_secret_basic` (a method name).
_SECRET_KEY = rb"client[_-]?secret(?![\w-]*file)[\"']?[ \t]*[:=][ \t]*"
QUOTED_SECRET = re.compile(_SECRET_KEY + rb"[\"'](?P<v>[^\"'\s]{8,})[\"']", re.IGNORECASE)
BARE_SECRET = re.compile(_SECRET_KEY + rb"(?P<v>[A-Za-z0-9+/_=~-]{16,})[ \t]*(?:#.*)?$", re.IGNORECASE | re.MULTILINE)
#: Values that are references or placeholders, not secrets.
PLACEHOLDER_VALUE = re.compile(rb"^(?:[$%{<]|/|https?:|changeme|change-me|your[-_]|example|xxx|\*+$|\.+$)", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


class IgnoreRules:
    """The common subset of `.gitignore` / `.dockerignore`: globs, `**`, trailing `/`, `!` re-includes."""

    def __init__(self, lines: list[str], *, git_style: bool) -> None:
        self.rules: list[tuple[bool, re.Pattern]] = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            self.rules.append((negate, self._compile(line.lstrip("!"), git_style)))

    @staticmethod
    def _compile(pattern: str, git_style: bool) -> re.Pattern:
        directory_only = pattern.endswith("/")
        pattern = pattern.strip("/")
        # gitignore: a pattern without a slash matches at any depth; dockerignore: from the root only.
        anywhere = git_style and "/" not in pattern
        out, i = "", 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out, i = out + "(?:.*/)?", i + 3
            elif pattern.startswith("**", i):
                out, i = out + ".*", i + 2
            elif pattern[i] == "*":
                out, i = out + "[^/]*", i + 1
            elif pattern[i] == "?":
                out, i = out + "[^/]", i + 1
            else:
                out, i = out + re.escape(pattern[i]), i + 1
        prefix = "(?:.*/)?" if anywhere else ""
        # Matching a directory ignores everything below it.
        return re.compile(f"^{prefix}{out}(?:/.*)?$" if not directory_only else f"^{prefix}{out}/.*$")

    @classmethod
    def load(cls, path: Path, *, git_style: bool) -> "IgnoreRules | None":
        if not path.is_file():
            return None
        return cls(path.read_text(encoding="utf-8", errors="replace").splitlines(), git_style=git_style)

    def ignores(self, relative: str, *, is_dir: bool = False) -> bool:
        target = relative + "/" if is_dir else relative
        ignored = False
        for negate, pattern in self.rules:
            if pattern.match(relative) or (is_dir and pattern.match(target + "x")):
                ignored = not negate
        return ignored


def _lineno(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def scan_file(path: Path, shown: str) -> list[Finding]:
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data[:8192]:
        return []
    findings: list[Finding] = []
    for match in PRIVATE_KEY.finditer(data):
        findings.append(Finding(shown, _lineno(data, match.start()), "private key (PEM)"))
    if b'"kty"' in data:
        for match in PRIVATE_JWK_MEMBER.finditer(data):
            findings.append(Finding(shown, _lineno(data, match.start()), "private JWK member"))
    for pattern in (QUOTED_SECRET, BARE_SECRET):
        for match in pattern.finditer(data):
            if not PLACEHOLDER_VALUE.match(match.group("v")):
                findings.append(Finding(shown, _lineno(data, match.start()), "client secret value"))
    return findings


def scan(root: Path) -> list[Finding]:
    git = IgnoreRules.load(root / ".gitignore", git_style=True)
    docker = IgnoreRules.load(root / ".dockerignore", git_style=False)
    builds_image = (root / "Dockerfile").is_file()  # without one, the build context is no concern

    def skipped(relative: str, is_dir: bool) -> bool:
        out_of_git = git is not None and git.ignores(relative, is_dir=is_dir)
        out_of_image = not builds_image or (docker is not None and docker.ignores(relative, is_dir=is_dir))
        return out_of_git and out_of_image

    findings: list[Finding] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in sorted(directory.iterdir()):
            relative = entry.relative_to(root).as_posix()
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in ALWAYS_SKIP and not skipped(relative, True):
                    stack.append(entry)
            elif not skipped(relative, False):
                findings += scan_file(entry, relative)
    return sorted(findings, key=lambda f: (f.path, f.line))
