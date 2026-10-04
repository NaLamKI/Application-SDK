"""`appext new` renders the templates into a project that is complete and healthy:
the manifest passes the check, the secret scan is clean, the project's own tests pass,
and the Dockerfile only copies what the .dockerignore lets through. The `link` template is
only a manifest and a README – it has no server, hence none of the rest."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from appext.cli import Context, main
from appext.cli import keys as cli_keys
from appext.cli.parser import TEMPLATES as OFFERED
from appext.cli.scaffold import templates_dir
from appext.cli.scan import IgnoreRules
from appext.manifest import loads_manifest

TEMPLATES = ("spa", "htmx")  # the ones that are a project with a server
ANY_TEMPLATE = (*TEMPLATES, "link")
FILES = {
    "spa": ["extension.toml", "pyproject.toml", "app/main.py", "Dockerfile", "compose.yaml", ".gitignore", ".dockerignore", "README.md",
            "icon.svg", "tests/test_api.py", "tests/conftest.py", "frontend/package.json", "frontend/package-lock.json",
            "frontend/index.html", "frontend/vite.config.js", "frontend/src/main.js", "frontend/src/style.css"],
    "htmx": ["extension.toml", "pyproject.toml", "app/main.py", "app/templates/base.html", "app/templates/index.html",
             "Dockerfile", "compose.yaml", ".gitignore", ".dockerignore", "README.md", "icon.svg", "tests/test_pages.py",
             "tests/conftest.py", "static/htmx.min.js", "static/htmx.LICENSE.txt", "static/app.js", "static/style.css"],
    "link": ["extension.toml", "README.md"],
}
PLACEHOLDER = re.compile(r"\{\{(?:id|name|package)\}\}")
SDK = Path(__file__).resolve().parent.parent


class Cli:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def __call__(self, *argv: str) -> int:
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        return main(list(argv), Context(env={}, cwd=self.cwd, out=self.stdout, err=self.stderr))

    @property
    def out(self) -> str:
        return self.stdout.getvalue()

    @property
    def err(self) -> str:
        return self.stderr.getvalue()


@pytest.fixture(autouse=True)
def small_keys(monkeypatch):
    monkeypatch.setattr(cli_keys, "RSA_BITS", 2048)


@pytest.fixture
def cli(tmp_path) -> Cli:
    return Cli(tmp_path)


@pytest.fixture(params=TEMPLATES)
def template(request) -> str:
    return request.param


@pytest.fixture(params=ANY_TEMPLATE)
def any_template(request) -> str:
    return request.param


@pytest.fixture
def project(template, cli, tmp_path) -> Path:
    assert cli("new", "demo-app", "--template", template) == 0, cli.err
    return tmp_path / "demo-app"


@pytest.fixture
def any_project(any_template, cli, tmp_path) -> Path:
    assert cli("new", "demo-app", "--template", any_template) == 0, cli.err
    return tmp_path / "demo-app"


@pytest.fixture
def link(cli, tmp_path) -> Path:
    assert cli("new", "demo-app", "--template", "link") == 0, cli.err
    return tmp_path / "demo-app"


def project_tree(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*")) if p.is_file() and "node_modules" not in p.parts and "__pycache__" not in p.parts]


# --- rendering --------------------------------------------------------------------------------


def test_the_project_has_every_expected_file(template, project):
    for name in FILES[template]:
        assert (project / name).is_file(), name


def test_no_placeholder_is_left(any_project):
    for path in project_tree(any_project):
        if path.name in ("package-lock.json", "htmx.min.js"):
            continue
        assert not PLACEHOLDER.search(path.read_text(encoding="utf-8")), path


def test_id_name_and_package_are_filled_in(project):
    manifest = (project / "extension.toml").read_text()
    assert 'id = "demo-app"' in manifest and 'name = "Demo App"' in manifest
    assert 'name = "demo_app"' in (project / "pyproject.toml").read_text()  # the package name has no hyphen
    assert "demo-app" in (project / "compose.yaml").read_text()
    assert "Demo App" in (project / "README.md").read_text()


def test_vendored_and_generated_files_are_copied_byte_for_byte(template, project):
    source = templates_dir() / template
    for name in ("frontend/package-lock.json", "static/htmx.min.js"):
        if (source / name).exists():
            assert (project / name).read_bytes() == (source / name).read_bytes()


def test_jinja_expressions_survive_rendering(cli, tmp_path):
    cli("new", "pages", "--template", "htmx")
    index = (tmp_path / "pages" / "app" / "templates" / "index.html").read_text()
    assert "{{ title }}" in index and "{{ user.name or user.email or user.sub }}" in index


def test_display_name_can_be_chosen(any_template, cli, tmp_path):
    assert cli("new", "kuh-bauer", "--template", any_template, "--name", "Cow Barn") == 0
    assert 'name = "Cow Barn"' in (tmp_path / "kuh-bauer" / "extension.toml").read_text()


@pytest.mark.parametrize("name", ['Say "hi"', "back\\slash", "a<b", "x&y", "new\nline"])
def test_a_display_name_that_would_break_the_files_is_refused(any_template, cli, tmp_path, name):
    assert cli("new", "ok-id", "--template", any_template, "--name", name) == 1
    assert not (tmp_path / "ok-id").exists()


def test_dir_is_the_parent_of_the_project(any_template, cli, tmp_path):
    assert cli("new", "inside", "--template", any_template, "--dir", "nested/here") == 0
    assert (tmp_path / "nested" / "here" / "inside" / "extension.toml").is_file()


# --- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["X", "Demo", "ab", "1abc", "has_underscore", "trail-", "a" * 41, "dot.ted"])
def test_an_id_the_manifest_rules_reject_creates_nothing(any_template, cli, tmp_path, bad):
    assert cli("new", bad, "--template", any_template) == 1
    assert "extension.id" in cli.err
    assert list(tmp_path.iterdir()) == []


def test_a_non_empty_target_is_refused_and_left_alone(any_template, cli, tmp_path):
    target = tmp_path / "taken"
    target.mkdir()
    (target / "mine.txt").write_text("precious")
    assert cli("new", "taken", "--template", any_template) == 1
    assert "not empty" in cli.err
    assert [p.name for p in target.iterdir()] == ["mine.txt"]


def test_an_empty_target_directory_is_fine(any_template, cli, tmp_path):
    (tmp_path / "empty").mkdir()
    assert cli("new", "empty", "--template", any_template) == 0
    assert (tmp_path / "empty" / "extension.toml").is_file()


def test_a_file_in_the_way_is_refused(any_template, cli, tmp_path):
    (tmp_path / "file").write_text("x")
    assert cli("new", "file", "--template", any_template) == 1


# --- the result is healthy -----------------------------------------------------------------------


def test_the_manifest_passes_the_check(project, cli):
    assert cli("manifest", "check", str(project)) == 0, cli.out + cli.err
    assert "OK" in cli.out and "ext-demo-app" in cli.out
    assert "icon" not in cli.err  # the icon is in the project from the start


def test_the_secret_scan_stays_clean_with_keys_in_place(project, cli):
    """Keys live in .appext/, which both ignore files list: they never reach git or the image."""
    assert cli("manifest", "check", str(project), "--scan-secrets") == 0
    run_in_project = Cli(project)
    assert run_in_project("keys", "generate") == 0 and run_in_project("keys", "session") == 0
    assert (project / ".appext" / "client_key.pem").is_file()
    assert cli("manifest", "check", str(project), "--scan-secrets") == 0, cli.out


def test_the_scan_catches_a_key_that_ends_up_in_the_source_tree(project, cli):
    """A key kept under another name is not covered by the ignore files – and would be committed and baked in."""
    Cli(project)("keys", "generate", "--out", ".appext")
    (project / "app" / "key_backup.txt").write_text((project / ".appext" / "client_key.pem").read_text())
    assert cli("manifest", "check", str(project), "--scan-secrets") == 1
    assert "app/key_backup.txt:1: private key (PEM)" in cli.out


def test_the_projects_own_tests_pass(project):
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPEXT_")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=project, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


def copy_sources(dockerfile: str) -> list[str]:
    sources = []
    for line in dockerfile.splitlines():
        parts = line.split()
        if parts and parts[0] == "COPY" and not any(p.startswith("--from") for p in parts):
            sources += [p for p in parts[1:-1] if not p.startswith("--")]
    return sources


def test_the_dockerfile_copies_only_what_exists_and_what_dockerignore_allows(project):
    sources = copy_sources((project / "Dockerfile").read_text())
    assert sources, "no COPY found"
    ignore = IgnoreRules.load(project / ".dockerignore", git_style=False)
    for source in sources:
        path = source.rstrip("/")
        matches = list(project.glob(source)) or [project / path]
        assert all(m.exists() for m in matches), f"COPY source {source!r} does not exist"
        assert not ignore.ignores(path, is_dir=(project / path).is_dir()), f"{source!r} is excluded by .dockerignore"


def test_the_dockerfile_follows_the_concept(template, project):
    text = (project / "Dockerfile").read_text()
    assert 'CMD ["appext", "serve", "app.main:app"]' in text
    assert 'HEALTHCHECK CMD ["appext", "health"]' in text
    assert text.rstrip().splitlines()[-4:-3] != ["USER root"] and "\nUSER appext\n" in text
    assert ("FROM node:22-alpine AS frontend" in text) == (template == "spa")  # the HTMX variant has no Node stage
    assert "COPY frontend/package*.json" in text or template == "htmx"


def test_the_image_never_receives_keys(project):
    ignored = (project / ".dockerignore").read_text()
    for needle in (".appext", "**/*.pem", "auth-bundle", "keys"):
        assert needle in ignored
    gitignored = (project / ".gitignore").read_text()
    for needle in (".appext/", "*.pem", "auth-bundle/"):
        assert needle in gitignored


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
def test_compose_file_is_valid(project):
    result = subprocess.run(["docker", "compose", "-f", str(project / "compose.yaml"), "--profile", "keycloak", "config", "--quiet"],
                            capture_output=True, text=True, timeout=60)
    if "unknown shorthand flag" in result.stderr or "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin missing")
    assert result.returncode == 0, result.stderr


def test_the_example_is_the_spa_template_and_nothing_else(cli, tmp_path):
    """sdk/examples/hello-fmis is generated from the SPA template; it must not drift away from it."""
    example = SDK / "examples" / "hello-fmis"
    assert cli("new", "hello-fmis", "--template", "spa", "--name", "Hello FMIS") == 0
    fresh = tmp_path / "hello-fmis"
    different = {"extension.toml"}  # description of the example
    for path in project_tree(fresh):
        relative = path.relative_to(fresh)
        if relative.parts[0] == "tests" or str(relative) in different:
            continue
        assert (example / relative).read_bytes() == path.read_bytes(), f"{relative} drifted from the template"
    present = {p.relative_to(example) for p in project_tree(example) if "dist" not in p.parts and ".appext" not in p.parts}
    assert {str(p) for p in present} - {str(p.relative_to(fresh)) for p in project_tree(fresh)} <= {
        "tests/conftest.py", "tests/test_api.py"}


def test_the_example_manifest_is_valid_and_reads_fields_with_ext_data_read(cli):
    example = SDK / "examples" / "hello-fmis"
    assert cli("manifest", "check", str(example)) == 0, cli.out
    assert "fmis (user) -> fmis-api [ext-data-read]" in cli.out


def test_templates_hold_no_build_leftovers():
    for path in templates_dir().rglob("*"):
        assert not {"node_modules", "__pycache__", "dist", ".appext", ".venv"} & set(path.parts), path
        assert path.suffix not in (".pem", ".key"), path


# --- the link template: a manifest and a README, no server ------------------------------------------------------


def test_every_template_the_command_offers_is_a_directory_and_the_other_way_round():
    assert {p.name for p in templates_dir().iterdir() if p.is_dir()} == set(OFFERED) == set(ANY_TEMPLATE)


def test_a_link_project_is_a_manifest_and_a_readme_and_nothing_else(link):
    assert sorted(str(p.relative_to(link)) for p in project_tree(link)) == ["README.md", "extension.toml"]


def test_the_links_manifest_is_a_valid_link(link):
    manifest = loads_manifest((link / "extension.toml").read_text(encoding="utf-8"))
    assert manifest.is_link and manifest.kind == "link" and manifest.external
    assert (manifest.id, manifest.name) == ("demo-app", "Demo App")
    assert manifest.entry == "https://example.com/" and manifest.icon == ""
    assert manifest.name_localized["de"] == "Demo App" and manifest.description_localized["de"]
    assert manifest.consent.scopes == () and manifest.services == () and manifest.hosts == () and manifest.dev_port is None


def test_the_links_manifest_has_nothing_of_a_server_and_explains_what_to_change(link):
    text = (link / "extension.toml").read_text(encoding="utf-8")
    keys = {line.split("=")[0].strip() for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#")}
    assert {"id", "name", "version", "kind", "entry"} <= keys
    assert not keys & {"icon", "display", "client_auth", "dev_port", "hosts", "scopes", "audience", "mode"}  # `icon` stays a comment
    assert "[consent]" not in text and "[[services]]" not in text
    assert "# icon = " in text and "same host as `entry`" in text  # the optional icon: an address, not a file
    assert "Replace" in text and "example.com" in text  # the entry is a placeholder to replace


def test_the_link_passes_the_check_without_a_warning_about_an_icon_file(link, cli):
    assert cli("manifest", "check", str(link)) == 0, cli.out + cli.err
    assert "kind       link" in cli.out and "entry      https://example.com/" in cli.out
    assert cli.err == ""
    assert cli("manifest", "check", str(link), "--scan-secrets") == 0, cli.out


def test_a_link_is_told_how_to_publish_it_not_how_to_run_it(cli):
    assert cli("new", "demo-app", "--template", "link") == 0
    out = cli.out
    assert "Created demo-app from the link template." in out
    steps = ["cd demo-app", "edit extension.toml", "appext manifest check", "appext store login", "appext store register",
             "appext store submit", "reviewer approves", "appext store verify"]
    positions = [out.index(step) for step in steps]
    assert positions == sorted(positions), "the steps come in the order they are taken"
    for gone in ("venv", "pip", "pytest", "npm", "appext dev"):
        assert gone not in out, gone


def test_the_other_templates_still_say_how_to_run_them(template, project, cli):
    assert "pytest" in cli.out and "appext dev" in cli.out and "venv" in cli.out
    assert "appext store register" not in cli.out


def test_the_links_readme_says_what_a_link_is_and_how_to_publish_it(link):
    readme = (link / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Demo App") and "appext new demo-app --template link" in readme
    for needle in ("system browser", "no Keycloak client", "no key", "no deployment", "same host", "appext store register",
                   "appext store submit", "appext store verify", "reviewer"):
        assert needle in readme, needle
