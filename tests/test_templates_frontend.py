"""What the browser gets from a project created by `appext new`: the SPA frontend builds
(`npm ci && npm run build`) and the pages of both templates work under the SDK's
default Content-Security-Policy, which forbids inline scripts and styles."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest

from appext.cli import Context, main

NPM = shutil.which("npm")
needs_npm = pytest.mark.skipif(NPM is None or os.environ.get("APPEXT_SKIP_NPM") == "1",
                               reason="needs npm (and the npm registry or a warm cache); APPEXT_SKIP_NPM=1 skips")


def create(tmp_path: Path, template: str) -> Path:
    out = io.StringIO()
    assert main(["new", "demo-app", "--template", template, "--dir", str(tmp_path)],
                Context(env={}, cwd=tmp_path, out=out, err=out)) == 0, out.getvalue()
    return tmp_path / "demo-app"


@pytest.fixture(scope="module")
def spa(tmp_path_factory) -> Path:
    return create(tmp_path_factory.mktemp("spa"), "spa")


@pytest.fixture(scope="module")
def htmx(tmp_path_factory) -> Path:
    return create(tmp_path_factory.mktemp("htmx"), "htmx")


@pytest.fixture(scope="module")
def built(spa) -> Path:
    """The SPA project with its frontend built, once for all tests of this module."""
    frontend = spa / "frontend"
    for command in (["ci", "--no-audit", "--no-fund"], ["run", "build"]):
        result = subprocess.run([NPM, *command], cwd=frontend, capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stdout + result.stderr
    return spa


class Inline(HTMLParser):
    """Collects what a policy of `default-src 'self'` would block: inline scripts, styles, handlers."""

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []
        self._script_without_src = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            if attributes.get("src"):
                self.scripts.append(attributes["src"])
            else:
                self._script_without_src = True
                self.problems.append("inline <script>")
        if tag == "style":
            self.problems.append("<style> block")
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href", ""))
        for name, value in attrs:
            if name == "style":
                self.problems.append("style attribute")
            if name.startswith("on"):
                self.problems.append(f"event handler {name}")
            if name in ("href", "src") and (value or "").startswith("javascript:"):
                self.problems.append("javascript: URL")

    def handle_data(self, data):
        pass


def inline_problems(html: str) -> Inline:
    parser = Inline()
    parser.feed(html)
    return parser


def serve_and_fetch(project: Path, paths: list[str], *, user: bool = True) -> dict:
    """Imports the project's app in a fresh interpreter and fetches `paths` as a signed-in test user."""
    script = textwrap.dedent(f"""
        import json, os
        for name in [n for n in os.environ if n.startswith("APPEXT_")]:
            del os.environ[name]
        from appext.testing import ExtensionTestClient, service_mocks, test_user
        from app.main import app, ext
        client = ExtensionTestClient(app, user=test_user(name="Ada") if {user!r} else None)
        out = {{}}
        with service_mocks(ext) as mocks:
            mocks.get("fmis", "/fields").respond(json=[{{"id": "f1", "name": "North", "area": 1.5, "areaUnit": "ha"}}])
            for path in {paths!r}:
                r = client.get(path, follow_redirects=False)
                out[path] = {{"status": r.status_code, "csp": r.headers.get("content-security-policy"),
                             "type": r.headers.get("content-type"), "text": r.text}}
        print("RESULT" + json.dumps(out))
    """)
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPEXT_")}
    result = subprocess.run([sys.executable, "-c", script], cwd=project, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.split("RESULT", 1)[1])


# --- SPA ----------------------------------------------------------------------------------------


@needs_npm
def test_the_spa_frontend_builds_from_the_committed_lock_file(built):
    dist = built / "frontend" / "dist"
    assert (dist / "index.html").is_file()
    assert list((dist / "assets").glob("*.js")) and list((dist / "assets").glob("*.css"))


@needs_npm
def test_the_built_page_uses_no_inline_script_or_style(built):
    html = (built / "frontend" / "dist" / "index.html").read_text()
    page = inline_problems(html)
    assert page.problems == []
    assert page.scripts and all(s.startswith("/assets/") for s in page.scripts)
    assert page.stylesheets and all(s.startswith("/assets/") for s in page.stylesheets)


@needs_npm
def test_the_sdk_client_is_imported_from_the_sdk_not_bundled(built):
    bundle = next((built / "frontend" / "dist" / "assets").glob("*.js")).read_text()
    assert re.search(r'from\s*"/_sdk/client\.js"', bundle)


@needs_npm
def test_the_sdk_serves_the_built_frontend_under_its_default_policy(built):
    served = serve_and_fetch(built, ["/", "/assets/does-not-exist.js", "/_sdk/client.js", "/_sdk/icon"])
    page = served["/"]
    assert page["status"] == 200 and "text/html" in page["type"]
    assert "default-src 'self'" in page["csp"] and "frame-ancestors 'none'" in page["csp"]
    assert "script-src" not in page["csp"] and "unsafe-inline" not in page["csp"]
    assert inline_problems(page["text"]).problems == []
    assert served["/assets/does-not-exist.js"]["status"] == 404
    assert "extFetch" in served["/_sdk/client.js"]["text"]
    assert served["/_sdk/icon"]["status"] == 200


@needs_npm
def test_the_page_is_not_shown_without_a_session(built):
    served = serve_and_fetch(built, ["/"], user=False)
    assert served["/"]["status"] == 302


# --- HTMX ---------------------------------------------------------------------------------------


def test_htmx_pages_use_no_inline_script_or_style(htmx):
    served = serve_and_fetch(htmx, ["/", "/fields"])
    for path in ("/", "/fields"):
        assert served[path]["status"] == 200, path
        assert "default-src 'self'" in served[path]["csp"]
        assert inline_problems(served[path]["text"]).problems == [], path
    page = inline_problems(served["/"]["text"])
    assert page.scripts == ["/_sdk/bridge.js", "/htmx.min.js", "/app.js"] and page.stylesheets == ["/style.css"]


def test_htmx_is_told_not_to_inject_styles_or_evaluate_code(htmx):
    html = serve_and_fetch(htmx, ["/"])["/"]["text"]
    config = re.search(r"<meta name=\"htmx-config\" content='([^']+)'", html)
    assert config, "htmx-config meta tag missing"
    assert json.loads(config.group(1)) == {"includeIndicatorStyles": False, "allowEval": False, "allowScriptTags": False}


def test_htmx_assets_are_served_with_their_license_header(htmx):
    served = serve_and_fetch(htmx, ["/htmx.min.js", "/app.js", "/style.css", "/htmx.LICENSE.txt"], user=False)
    assert all(v["status"] == 200 for v in served.values())
    assert served["/htmx.min.js"]["text"].startswith("/*! htmx 2.")
    assert "Zero-Clause BSD" in served["/htmx.LICENSE.txt"]["text"]
    assert "javascript" in served["/htmx.min.js"]["type"]


def test_the_vendored_htmx_is_the_unmodified_upstream_file(htmx):
    """Everything after the three comment lines of our header is htmx's own minified file."""
    text = (htmx / "static" / "htmx.min.js").read_text()
    header, _, code = text.partition("*/\n")
    assert header.startswith("/*! htmx 2.0.") and code.startswith("var htmx=function(){")
    assert code.rstrip().endswith("}();")
