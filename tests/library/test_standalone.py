"""The SDK stands on its own: it ships no platform and reaches for nothing outside this repository.

A platform is *configured* (a platform file, `APPEXT_*` variables, command-line options). If a real
address or a path on someone's machine crept into the repository, an extension would silently depend on
one platform or one checkout, and these tests fail.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".pytest_cache"}
TEXT = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".js", ".ts", ".tsx", ".html", ".css", ".sh", ".env", ".txt"}
SKIP_FILES = {"package-lock.json"}

URL = re.compile(r"https?://([A-Za-z0-9._-]+)")
# Hosts that name no platform: this machine, reserved example/test names, and the standards a page links to.
FINE = re.compile(
    r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|[a-z0-9-]+\.localhost"
    r"|([a-z0-9-]+\.)*example(\.com|\.org|\.net)?|([a-z0-9-]+\.)*test|([a-z0-9-]+\.)*internal"
    r"|www\.w3\.org|schemas\.openid\.net|openid\.net|htmx\.org|json-schema\.org|github\.com)$"
)
# A path of someone's machine (`/home/you/` is the placeholder the docs use).
MACHINE_PATH = re.compile(r"/Users/[A-Za-z0-9._-]+|/home/(?!you/)[a-z][a-z0-9._-]*/|[A-Za-z]:\\Users\\")


def _files():
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in names:
            path = Path(base, name)
            if name in SKIP_FILES or path.suffix not in TEXT or path == Path(__file__).resolve():
                continue
            yield path


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def test_no_real_platform_address_is_part_of_the_repository():
    found = {}
    for path in _files():
        for host in URL.findall(_text(path)):
            host = host.rstrip(".").lower()
            # a real platform has a name with a dot; `host`, `evil` and `x` are placeholders in prose and fixtures
            if "." in host and not FINE.match(host):
                found.setdefault(host, str(path.relative_to(ROOT)))
    assert not found, f"addresses that name a real host (use example.com or localhost): {found}"


def test_no_path_of_a_developer_machine_is_part_of_the_repository():
    found = [str(p.relative_to(ROOT)) for p in _files() if MACHINE_PATH.search(_text(p))]
    assert not found, found


def test_no_file_reaches_into_a_neighbouring_checkout():
    """`../something` in code or config would work on one laptop and nowhere else."""
    climbing = re.compile(r"""["'](\.\./(?!\.)[^"']*)["']""")
    found = {}
    for path in _files():
        # tests may name a path outside the repository to prove that it is refused
        if path.suffix not in {".py", ".toml", ".yml", ".yaml", ".sh"} or "tests" in path.relative_to(ROOT).parts[:1]:
            continue
        for match in climbing.findall(_text(path)):
            found.setdefault(match, str(path.relative_to(ROOT)))
    assert not found, found


@pytest.mark.parametrize("variable", ["APPEXT_ISSUER", "APPEXT_STORE_URL", "APPEXT_APP_REDIRECT_URI"])
def test_the_package_has_no_default_for_what_names_a_platform(variable):
    """The settings that point at a platform are read from the environment or a platform file, nowhere else."""
    source = "\n".join(_text(p) for p in (ROOT / "src" / "appext").rglob("*.py"))
    assert not re.search(rf"{variable}[\"']\s*,\s*[\"']https?://", source), variable
