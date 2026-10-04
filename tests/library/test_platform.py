"""The platform an extension is written for: the file format, where files are looked for, the environment on top."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from appext.platform import (
    DEFAULT_CLI_CLIENT_ID,
    PROJECT_FILE,
    Platform,
    PlatformError,
    discover,
    load_platform_file,
    missing,
    parse_platform,
    platform_file,
    with_environment,
)

FULL = {
    "platform": {
        "name": "Acme Platform",
        "issuer": "https://auth.acme.example/realms/acme/",
        "store_url": "https://api.acme.example/api/v1/",
        "cli_client_id": "acme-cli",
        "app_redirect_uri": "com.acme.app.ext:/callback",
        "services": {"data-api": "http://127.0.0.1:8000/api/v1/"},
        "starter": {"service": "records", "audience": "records-api", "scope": "records-read"},
    }
}


def problems(data) -> list[str]:
    with pytest.raises(PlatformError) as error:
        parse_platform(data)
    return error.value.problems


# -- the format ---------------------------------------------------------------------------------------------


def test_a_full_platform_is_read():
    p = parse_platform(FULL)
    assert p.name == "Acme Platform" and p.display_name == "Acme Platform"
    assert p.issuer == "https://auth.acme.example/realms/acme", "no trailing slash: it is joined with paths"
    assert p.store_url == "https://api.acme.example/api/v1"
    assert p.cli_client_id == "acme-cli" and p.app_redirect_uri == "com.acme.app.ext:/callback"
    assert dict(p.services) == {"data-api": "http://127.0.0.1:8000/api/v1"}
    assert (p.starter.service, p.starter.audience, p.starter.scope) == ("records", "records-api", "records-read")


def test_everything_is_optional_and_nothing_is_invented():
    p = parse_platform({})
    assert p.issuer is None and p.store_url is None and p.app_redirect_uri is None
    assert p.cli_client_id == DEFAULT_CLI_CLIENT_ID and p.display_name == "the platform"
    assert (p.starter.service, p.starter.audience, p.starter.scope) == ("data", "data-api", "data-read")
    assert parse_platform({"platform": {"name": "Only a name"}}).issuer is None


def test_the_services_of_a_platform_cannot_be_changed_afterwards():
    p = parse_platform(FULL)
    with pytest.raises(TypeError):
        p.services["other"] = "x"  # type: ignore[index]


@pytest.mark.parametrize("data, fragment", [
    ({"platform": {"issuer": "ftp://x"}}, "platform.issuer must be an http(s) URL"),
    ({"platform": {"issuer": ""}}, "platform.issuer must be a non-empty string"),
    ({"platform": {"store_url": 5}}, "platform.store_url must be a non-empty string"),
    ({"platform": {"app_redirect_uri": "no-scheme"}}, "platform.app_redirect_uri"),
    ({"platform": {"app_redirect_uri": "app:/cb#frag"}}, "platform.app_redirect_uri"),
    ({"platform": {"name": ""}}, "platform.name must be a non-empty string"),
    ({"platform": {"name": 'Say "hi"'}}, "platform.name must be at most 80 characters"),
    ({"platform": {"name": "A<b>"}}, "platform.name must be at most 80 characters"),
    ({"platform": {"name": "x" * 81}}, "platform.name must be at most 80 characters"),
    ({"platform": {"surprise": 1}}, "platform.surprise: unknown key"),
    ({"platform": {"services": {"data-api": "nope"}}}, "platform.services.data-api must be an http(s) URL"),
    ({"platform": {"services": ["a"]}}, "platform.services must be a table"),
    ({"platform": {"starter": {"service": ""}}}, "platform.starter.service must be a non-empty string"),
    ({"platform": {"starter": {"colour": "x"}}}, "platform.starter.colour: unknown key"),
    ({"platform": "text"}, "platform must be a table"),
    ({"stranger": {}}, "stranger: unknown table"),
])
def test_what_breaks_the_format_is_named(data, fragment):
    assert any(fragment in p for p in problems(data)), problems(data)


def test_every_problem_is_reported_not_only_the_first():
    found = problems({"platform": {"issuer": "ftp://x", "store_url": "ftp://y", "surprise": 1}})
    assert len(found) == 3


# -- the file -----------------------------------------------------------------------------------------------


def test_a_platform_file_is_read_and_remembers_where_it_came_from(tmp_path):
    (tmp_path / "p.toml").write_text('[platform]\nname = "From file"\nissuer = "https://auth.example/realms/x"\n')
    p = load_platform_file(tmp_path / "p.toml")
    assert p.name == "From file" and p.source == (tmp_path / "p.toml").resolve()


def test_a_missing_or_broken_file_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(PlatformError, match="does not exist"):
        load_platform_file(tmp_path / "nope.toml")
    (tmp_path / "bad.toml").write_text("[platform\nname=")
    with pytest.raises(PlatformError, match="not valid TOML"):
        load_platform_file(tmp_path / "bad.toml")
    (tmp_path / "binary.toml").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(PlatformError, match="cannot read"):
        load_platform_file(tmp_path / "binary.toml")


def test_a_platform_survives_the_trip_through_toml():
    original = parse_platform(FULL)
    again = parse_platform(tomllib.loads(original.to_toml()))
    assert (again.name, again.issuer, again.store_url, again.cli_client_id, again.app_redirect_uri) == (
        original.name, original.issuer, original.store_url, original.cli_client_id, original.app_redirect_uri)
    assert dict(again.services) == dict(original.services) and again.starter == original.starter


def test_the_default_cli_client_is_not_written_out():
    text = Platform(name="Acme", issuer="https://auth.example/realms/x").to_toml()
    assert "cli_client_id" not in text
    assert parse_platform(tomllib.loads(text)).name == "Acme"


# -- finding the file ---------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path / "config")}


def write(path: Path, issuer: str = "https://auth.example/realms/x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[platform]\nissuer = "{issuer}"\n')
    return path


def test_nothing_configured_means_no_file(tmp_path, home):
    assert platform_file(home, project_dir=tmp_path, cwd=tmp_path) is None
    assert discover(home, project_dir=tmp_path, cwd=tmp_path) == Platform()


def test_the_projects_file_comes_before_the_users_default(tmp_path, home):
    write(tmp_path / "config" / "appext" / "platform.toml", "https://auth.example/realms/user")
    assert discover(home, cwd=tmp_path).issuer == "https://auth.example/realms/user"
    write(tmp_path / PROJECT_FILE, "https://auth.example/realms/project")
    assert discover(home, project_dir=tmp_path, cwd=tmp_path).issuer == "https://auth.example/realms/project"


def test_the_project_directory_is_looked_at_before_the_working_directory(tmp_path, home):
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    write(project / PROJECT_FILE, "https://auth.example/realms/project")
    write(elsewhere / PROJECT_FILE, "https://auth.example/realms/elsewhere")
    assert discover(home, project_dir=project, cwd=elsewhere).issuer == "https://auth.example/realms/project"


def test_a_name_is_a_file_in_the_config_directory_and_a_path_is_a_path(tmp_path, home):
    named = write(tmp_path / "config" / "appext" / "platforms" / "acme.toml", "https://auth.example/realms/acme")
    assert platform_file(home, explicit="acme", cwd=tmp_path) == named
    own = write(tmp_path / "somewhere" / "p.toml", "https://auth.example/realms/own")
    assert discover(home, explicit=str(own), cwd=tmp_path).issuer == "https://auth.example/realms/own"
    assert discover(home, explicit="somewhere/p.toml", cwd=tmp_path).issuer == "https://auth.example/realms/own", "relative to the working directory"
    assert discover({**home, "APPEXT_PLATFORM": "acme"}, cwd=tmp_path).issuer == "https://auth.example/realms/acme"


def test_asking_for_a_platform_that_is_not_there_is_an_error_not_the_next_best_file(tmp_path, home):
    write(tmp_path / PROJECT_FILE)
    with pytest.raises(PlatformError, match="does not exist"):
        discover(home, explicit="nobody", project_dir=tmp_path, cwd=tmp_path)


def test_the_option_beats_the_environment_variable(tmp_path, home):
    write(tmp_path / "config" / "appext" / "platforms" / "a.toml", "https://auth.example/realms/a")
    write(tmp_path / "config" / "appext" / "platforms" / "b.toml", "https://auth.example/realms/b")
    assert discover({**home, "APPEXT_PLATFORM": "a"}, explicit="b", cwd=tmp_path).issuer == "https://auth.example/realms/b"


def test_the_users_default_can_be_left_out(tmp_path, home):
    write(tmp_path / "config" / "appext" / "platform.toml")
    assert platform_file(home, cwd=tmp_path, user_default=False) is None


def test_none_means_no_file_at_all(tmp_path, home):
    write(tmp_path / PROJECT_FILE)
    write(tmp_path / "config" / "appext" / "platform.toml")
    assert platform_file({**home, "APPEXT_PLATFORM": "none"}, project_dir=tmp_path, cwd=tmp_path) is None
    assert platform_file(home, explicit="none", project_dir=tmp_path, cwd=tmp_path) is None
    assert discover({**home, "APPEXT_PLATFORM": "none"}, project_dir=tmp_path, cwd=tmp_path) == Platform()


# -- the environment on top ----------------------------------------------------------------------------------


def test_environment_variables_win_setting_by_setting():
    base = parse_platform(FULL)
    p = with_environment(base, {"APPEXT_ISSUER": "https://other.example/realms/o/", "APPEXT_CLI_CLIENT_ID": "env-cli"})
    assert p.issuer == "https://other.example/realms/o"
    assert p.store_url == base.store_url, "what the environment does not say stays"
    assert p.cli_client_id == "env-cli" and p.app_redirect_uri == base.app_redirect_uri
    assert with_environment(base, {"APPEXT_STORE_URL": "  "}).store_url == base.store_url, "blank is unset"


def test_the_message_for_a_missing_setting_names_every_way_to_set_it():
    text = missing("App Store", option="--store-url", variable="APPEXT_STORE_URL", key="store_url")
    assert "no App Store is configured" in text
    for way in ("--store-url", "APPEXT_STORE_URL", "store_url", "appext.toml", "--platform"):
        assert way in text
