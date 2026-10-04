"""`appext new` renders the templates into a project that is complete and healthy:
the manifest passes the check, the secret scan is clean, the project's own tests pass,
and the Dockerfile only copies what the .dockerignore lets through. The `link` template is
only a manifest and a README – it has no server, hence none of the rest.

Every project is written for a platform: `appext.toml` says which, and its `[platform.starter]`
(service, audience, scope) fills the manifest and the example code. Without a platform the
placeholders read `the platform`, `data`, `data-api` and `data-read`."""

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
from appext.platform import load_platform_file

TEMPLATES = ("spa", "htmx")  # the ones that are a project with a server
ANY_TEMPLATE = (*TEMPLATES, "link")
FILES = {
    "spa": ["extension.toml", "appext.toml", "pyproject.toml", "app/main.py", "Dockerfile", "compose.yaml", ".gitignore", ".dockerignore", "README.md",
            "icon.svg", "tests/test_api.py", "tests/conftest.py", "frontend/package.json", "frontend/package-lock.json",
            "frontend/index.html", "frontend/vite.config.js", "frontend/src/main.js", "frontend/src/style.css"],
    "htmx": ["extension.toml", "appext.toml", "pyproject.toml", "app/main.py", "app/templates/base.html", "app/templates/index.html",
             "Dockerfile", "compose.yaml", ".gitignore", ".dockerignore", "README.md", "icon.svg", "tests/test_pages.py",
             "tests/conftest.py", "static/htmx.min.js", "static/htmx.LICENSE.txt", "static/app.js", "static/style.css"],
    "link": ["extension.toml", "appext.toml", "README.md"],
}
PLACEHOLDER = re.compile(r"\{\{(?:id|name|package|platform_name|service|audience|scope)\}\}")
SDK = Path(__file__).resolve().parent.parent
BUILD_OUTPUT = {"node_modules", "__pycache__", "dist", ".appext", ".venv", ".pytest_cache"}
PRODUCT = "FM" "IS"  # the product the SDK came from; written in two parts so that this guard is no hit itself
NOT_ENGLISH = re.compile(r"[\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc\u00df]|^de = ", re.M)  # umlauts, sharp s, a "de" translation: English only

#: A platform with a starter of its own: what a project written for it must use instead of the defaults.
ACME = """\
[platform]
name = "Acme Platform"
issuer = "https://auth.acme.test/realms/acme"
store_url = "https://api.acme.test/api/v1"
app_redirect_uri = "com.acme.app:/callback"

[platform.services]
orders-api = "http://127.0.0.1:9000/api/v1"

[platform.starter]
service = "orders"
audience = "orders-api"
scope = "orders-read"
"""


class Cli:
    """The command line in a directory, with an environment that finds no platform unless the test gives one:
    `XDG_CONFIG_HOME` points at an empty directory, so the user's own `~/.config/appext` is never read."""

    def __init__(self, cwd: Path, config_home: Path | None = None) -> None:
        self.cwd = cwd
        self.env: dict[str, str] = {"XDG_CONFIG_HOME": str(config_home or cwd.parent / f"{cwd.name}-no-config")}

    def __call__(self, *argv: str) -> int:
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        return main(list(argv), Context(env=self.env, cwd=self.cwd, out=self.stdout, err=self.stderr))

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


@pytest.fixture
def acme(tmp_path_factory) -> Path:
    """A platform file outside the project directory, for `--platform` and `APPEXT_PLATFORM`."""
    file = tmp_path_factory.mktemp("platform") / "acme.toml"
    file.write_text(ACME, encoding="utf-8")
    return file


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
    return [p for p in sorted(root.rglob("*")) if p.is_file() and not BUILD_OUTPUT & set(p.relative_to(root).parts)]


def run_project_tests(project: Path, **environment: str) -> subprocess.CompletedProcess:
    """The project's own pytest run, as its author would start it – minus whatever APPEXT_* the shell exports."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPEXT_")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=project, env={**env, "PYTHONDONTWRITEBYTECODE": "1", **environment}, capture_output=True, text=True, timeout=180,
    )


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


# --- the platform: appext.toml, and what the starter of the platform fills in ----------------------------------------


def test_a_new_project_gets_a_platform_file(any_project):
    platform = load_platform_file(any_project / "appext.toml")
    assert platform.name == "" and platform.issuer is None and platform.store_url is None  # nothing is guessed
    assert (platform.starter.service, platform.starter.audience, platform.starter.scope) == ("data", "data-api", "data-read")
    text = (any_project / "appext.toml").read_text(encoding="utf-8")
    assert "# issuer = " in text and "# store_url = " in text and "no secret ever belongs here" in text  # hints for what is missing


def test_the_platform_file_carries_the_configured_platform(any_template, cli, tmp_path, acme):
    assert cli("new", "demo-app", "--template", any_template, "--platform", str(acme)) == 0, cli.err
    platform = load_platform_file(tmp_path / "demo-app" / "appext.toml")
    assert (platform.name, platform.issuer, platform.store_url) == ("Acme Platform", "https://auth.acme.test/realms/acme", "https://api.acme.test/api/v1")
    assert platform.app_redirect_uri == "com.acme.app:/callback" and dict(platform.services) == {"orders-api": "http://127.0.0.1:9000/api/v1"}
    assert (platform.starter.service, platform.starter.audience, platform.starter.scope) == ("orders", "orders-api", "orders-read")
    assert "# issuer" not in (tmp_path / "demo-app" / "appext.toml").read_text(encoding="utf-8")  # nothing is missing: no hint
    assert "edit appext.toml" not in cli.out


def test_without_a_platform_the_defaults_fill_the_placeholders(template, project, cli):
    manifest = loads_manifest((project / "extension.toml").read_text(encoding="utf-8"))
    assert manifest.description == "An extension for the platform"
    (service,) = manifest.services
    assert (service.name, service.audience, service.scopes, service.mode) == ("data", "data-api", ("data-read",), "user")
    assert 'ext.service("data")' in (project / "app" / "main.py").read_text(encoding="utf-8")
    assert "An extension for the platform, created with" in (project / "README.md").read_text(encoding="utf-8")
    assert "an extension for the platform" in (project / "pyproject.toml").read_text(encoding="utf-8")
    assert "edit appext.toml" in cli.out  # the platform's OAuth service and App Store are still to name


def assert_written_for_acme(project: Path, template: str) -> None:
    manifest = loads_manifest((project / "extension.toml").read_text(encoding="utf-8"))
    assert manifest.description == "An extension for Acme Platform"
    (service,) = manifest.services
    assert (service.name, service.audience, service.scopes, service.mode) == ("orders", "orders-api", ("orders-read",), "user")
    assert 'ext.service("orders")' in (project / "app" / "main.py").read_text(encoding="utf-8")
    tests = "".join(p.read_text(encoding="utf-8") for p in (project / "tests").glob("test_*.py"))
    assert 'mocks.get("orders", "/items")' in tests
    assert '("orders", "orders-api", ("orders-read",), "user")' in tests
    assert "An extension for Acme Platform, created with" in (project / "README.md").read_text(encoding="utf-8")
    assert "an extension for Acme Platform" in (project / "pyproject.toml").read_text(encoding="utf-8")
    for path in project_tree(project):
        if path.name in ("package-lock.json", "htmx.min.js", "appext.toml") or path.suffix == ".svg":
            continue
        text = path.read_text(encoding="utf-8")
        assert "data-api" not in text and "data-read" not in text, f"{path.relative_to(project)} still has the defaults"
        assert 'service("data")' not in text, path


def test_the_starter_of_a_platform_file_fills_manifest_code_and_tests(template, cli, tmp_path, acme):
    assert cli("new", "demo-app", "--template", template, "--platform", str(acme)) == 0, cli.err
    assert_written_for_acme(tmp_path / "demo-app", template)
    # The URL variable of the starter service is named after it, in the compose file too.
    compose = (tmp_path / "demo-app" / "compose.yaml").read_text()
    assert "APPEXT_SERVICE_ORDERS_URL: ${APPEXT_SERVICE_ORDERS_URL:-}" in compose and "APPEXT_SERVICE_DATA_URL" not in compose


def test_the_starter_of_APPEXT_PLATFORM_is_used_too(template, cli, tmp_path, acme):
    cli.env["APPEXT_PLATFORM"] = str(acme)
    assert cli("new", "demo-app", "--template", template) == 0, cli.err
    assert_written_for_acme(tmp_path / "demo-app", template)


def test_the_option_beats_the_environment(cli, tmp_path, acme):
    other = tmp_path.parent / f"{tmp_path.name}-other.toml"
    other.write_text('[platform]\nname = "Other"\n[platform.starter]\nservice = "things"\naudience = "things-api"\nscope = "things-read"\n')
    cli.env["APPEXT_PLATFORM"] = str(acme)
    assert cli("new", "demo-app", "--template", "spa", "--platform", str(other)) == 0, cli.err
    manifest = loads_manifest((tmp_path / "demo-app" / "extension.toml").read_text(encoding="utf-8"))
    assert manifest.description == "An extension for Other" and manifest.services[0].name == "things"


def test_the_projects_own_tests_pass_with_the_starter_of_a_platform(template, cli, tmp_path, acme):
    """The generated tests name the starter service, whatever it is: they have to follow the platform."""
    assert cli("new", "demo-app", "--template", template, "--platform", str(acme)) == 0, cli.err
    result = run_project_tests(tmp_path / "demo-app")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


def test_the_platform_name_comes_from_the_platform_table(cli, tmp_path, acme):
    assert cli("new", "demo-link", "--template", "link", "--platform", str(acme)) == 0, cli.err
    link = tmp_path / "demo-link"
    manifest = loads_manifest((link / "extension.toml").read_text(encoding="utf-8"))
    assert manifest.description == "A link for Acme Platform"
    assert "A **link** for Acme Platform" in (link / "README.md").read_text(encoding="utf-8")
    assert "Acme Platform" in (link / "extension.toml").read_text(encoding="utf-8").splitlines()[1]  # the header comment names it too
    assert load_platform_file(link / "appext.toml").name == "Acme Platform"


def test_a_platform_with_only_a_name_still_uses_the_default_starter(cli, tmp_path):
    named = tmp_path.parent / f"{tmp_path.name}-named.toml"
    named.write_text('[platform]\nname = "Acme Platform"\n')
    assert cli("new", "demo-app", "--template", "spa", "--platform", str(named)) == 0, cli.err
    manifest = loads_manifest((tmp_path / "demo-app" / "extension.toml").read_text(encoding="utf-8"))
    assert manifest.description == "An extension for Acme Platform"
    assert (manifest.services[0].name, manifest.services[0].audience, manifest.services[0].scopes) == ("data", "data-api", ("data-read",))


def test_templates_and_example_are_english_and_name_no_product(template):
    roots = [templates_dir() / template, SDK / "examples" / "hello"] if template == "spa" else [templates_dir() / template]
    roots.append(templates_dir() / "link")
    for root in roots:
        for path in project_tree(root):
            if path.name in ("package-lock.json", "htmx.min.js"):
                continue
            text = path.read_text(encoding="utf-8") if path.suffix != ".svg" else ""
            assert PRODUCT.lower() not in text.lower(), f"{path} names the product"
            assert not NOT_ENGLISH.search(text), f"{path} is not English"


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
    result = run_project_tests(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


def test_the_projects_own_tests_do_not_depend_on_the_machine(project, tmp_path):
    """The shell of a developer or of CI may export a deployment's settings or name a platform file: the tests ignore it."""
    result = run_project_tests(
        project, APPEXT_ENV="production", APPEXT_ISSUER="https://elsewhere.example/realms/x", APPEXT_CLIENT_ID="someone-else",
        APPEXT_PLATFORM=str(tmp_path / "does-not-exist.toml"), APPEXT_APP_REDIRECT_URI="not a uri", APPEXT_SESSION_STORE="redis://nowhere",
        XDG_CONFIG_HOME=str(tmp_path / "no-config"),
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
    assert "\nappext.toml\n" in ignored  # the platform file is for the desk; a deployment gets an auth bundle
    gitignored = (project / ".gitignore").read_text()
    for needle in (".appext/", "*.pem", "auth-bundle/"):
        assert needle in gitignored


def test_compose_has_no_built_in_platform(project):
    """The issuer is the platform's: there is no default for it. The host app's return address is optional."""
    text = (project / "compose.yaml").read_text()
    assert "APPEXT_ISSUER: ${APPEXT_ISSUER:?" in text
    assert "APPEXT_APP_REDIRECT_URI: ${APPEXT_APP_REDIRECT_URI:-}" in text
    entries = [line.strip() for line in text.splitlines() if line.strip().startswith("APPEXT_")]
    assert not any(line.startswith("APPEXT_ISSUER: ${APPEXT_ISSUER:-") for line in entries)
    # The starter service's URL has a variable named after the service, and no default URL.
    service = [line for line in entries if line.startswith("APPEXT_SERVICE_")]
    assert service == ["APPEXT_SERVICE_DATA_URL: ${APPEXT_SERVICE_DATA_URL:-}"]
    assert "A) Against the platform's own stack" in text and "B) Against a Keycloak of your own" in text
    assert ".appext/services.env" not in text and "env_file" not in text


def compose_config(project: Path, **environment: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPEXT_")}
    return subprocess.run(
        ["docker", "compose", "-f", str(project / "compose.yaml"), "--profile", "keycloak", "config", "--quiet"],
        capture_output=True, text=True, timeout=60, env={**env, **environment},
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
def test_compose_file_is_valid_once_the_issuer_is_given(project):
    result = compose_config(project, APPEXT_ISSUER="https://auth.example.test/realms/test")
    if "unknown shorthand flag" in result.stderr or "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin missing")
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
def test_compose_refuses_to_start_without_an_issuer(project):
    result = compose_config(project)
    if "unknown shorthand flag" in result.stderr or "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin missing")
    assert result.returncode != 0 and "APPEXT_ISSUER" in result.stderr


def test_the_example_is_the_spa_template_and_nothing_else(cli, tmp_path):
    """examples/hello is what `appext new hello --template spa --name Hello` generates when no platform is
    configured (the CLI of this test finds none); it must not drift away from the template."""
    example = SDK / "examples" / "hello"
    assert cli("new", "hello", "--template", "spa", "--name", "Hello") == 0
    fresh = tmp_path / "hello"
    for path in project_tree(fresh):
        relative = path.relative_to(fresh)
        assert (example / relative).read_bytes() == path.read_bytes(), f"{relative} drifted from the template"
    assert {p.relative_to(example) for p in project_tree(example)} == {p.relative_to(fresh) for p in project_tree(fresh)}
    assert (example / "appext.toml").is_file()


def test_the_example_manifest_is_valid_and_calls_the_starter_service(cli):
    example = SDK / "examples" / "hello"
    assert cli("manifest", "check", str(example)) == 0, cli.out
    assert "data (user) -> data-api [data-read]" in cli.out
    assert "An extension for the platform" in (example / "extension.toml").read_text(encoding="utf-8")


def test_templates_hold_no_build_leftovers():
    for path in templates_dir().rglob("*"):
        assert not {"node_modules", "__pycache__", "dist", ".appext", ".venv"} & set(path.parts), path
        assert path.suffix not in (".pem", ".key"), path


# --- the link template: a manifest and a README, no server ------------------------------------------------------


def test_every_template_the_command_offers_is_a_directory_and_the_other_way_round():
    assert {p.name for p in templates_dir().iterdir() if p.is_dir()} == set(OFFERED) == set(ANY_TEMPLATE)


def test_a_link_project_is_a_manifest_a_readme_and_the_platform_file_and_nothing_else(link):
    assert sorted(str(p.relative_to(link)) for p in project_tree(link)) == ["README.md", "appext.toml", "extension.toml"]


def test_the_links_manifest_is_a_valid_link(link):
    manifest = loads_manifest((link / "extension.toml").read_text(encoding="utf-8"))
    assert manifest.is_link and manifest.kind == "link" and manifest.external
    assert (manifest.id, manifest.name) == ("demo-app", "Demo App")
    assert manifest.entry == "https://example.com/" and manifest.icon == ""
    assert manifest.description == "A link for the platform"
    assert manifest.name_localized == {} and manifest.description_localized == {}  # only a commented-out example
    assert manifest.consent.scopes == () and manifest.services == () and manifest.hosts == () and manifest.dev_port is None


def test_the_links_manifest_has_nothing_of_a_server_and_explains_what_to_change(link):
    text = (link / "extension.toml").read_text(encoding="utf-8")
    keys = {line.split("=")[0].strip() for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#")}
    assert {"id", "name", "version", "kind", "entry"} <= keys
    assert not keys & {"icon", "display", "client_auth", "dev_port", "hosts", "scopes", "audience", "mode"}  # `icon` stays a comment
    assert "[consent]" not in text and "[[services]]" not in text
    assert "# icon = " in text and "same host as `entry`" in text  # the optional icon: an address, not a file
    assert "# [extension.name_localized]" in text and "# <language code> = " in text  # the shape of a translation, no language
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
    for needle in ("system browser", "no OAuth client", "no key", "no deployment", "same host", "appext store register",
                   "appext store submit", "appext store verify", "reviewer"):
        assert needle in readme, needle
