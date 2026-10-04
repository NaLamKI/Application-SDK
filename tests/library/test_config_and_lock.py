"""Settings from the environment, the lock file, and the check between them."""
from __future__ import annotations

import base64
import logging

import pytest

from appext.config import ConfigError, ExtensionSettings, Secret, generate_session_key, parse_session_keys
from appext.lock import LOCK_FILENAME, LockError, check_lock, check_startup, read_lock
from appext.manifest import loads_manifest

from .conftest import MANIFEST

manifest = loads_manifest(MANIFEST)

#: What a local run needs: nothing is guessed, so the issuer has to come from somewhere.
LOCAL = {"APPEXT_ISSUER": "http://127.0.0.1:58080/realms/example"}

PLATFORM_FILE = """
[platform]
name = "Example Platform"
issuer = "http://127.0.0.1:58080/realms/example"
app_redirect_uri = "com.example.app:/callback"

[platform.services]
projects-api = "http://127.0.0.1:8000/api/v1"
"""

PROD = {
    "APPEXT_ENV": "prod",
    "APPEXT_ISSUER": "https://sso.example.com/realms/example",
    "APPEXT_PUBLIC_URL": "https://demo.apps.example.com",
    "APPEXT_CLIENT_KEY_FILE": "{key}",
    "APPEXT_SESSION_KEY_FILE": "{session}",
    "APPEXT_SESSION_STORE": "redis://redis:6379/3",
    "APPEXT_SERVICE_PROJECTS_URL": "https://projects.example.com",
    "APPEXT_SERVICE_EXPORT_URL": "https://export.example.com",
}


@pytest.fixture
def prod_env(tmp_path):
    key = tmp_path / "client_key"
    key.write_text("-----BEGIN PRIVATE KEY-----\nxxx\n-----END PRIVATE KEY-----\n")
    session = tmp_path / "session_key"
    session.write_text(generate_session_key())
    return {k: v.format(key=key, session=session) for k, v in PROD.items()}


# -- defaults for local development ---------------------------------------------------------------------


def test_local_needs_nothing_but_an_issuer(caplog):
    with caplog.at_level(logging.WARNING, logger="appext"):
        s = ExtensionSettings.from_env(manifest, LOCAL)
    assert s.env == "local" and s.is_local
    assert s.issuer == "http://127.0.0.1:58080/realms/example"
    assert s.client_id == "ext-demo" and s.client_auth == "private_key_jwt"
    assert s.public_url == "http://127.0.0.1:8000"
    assert s.app_redirect_uri is None, "no host app: the extension is a website like any other"
    assert s.session_store == "memory"
    assert s.session_key_generated and s.session_keys == ()
    assert "session key generated" in caplog.text or "generated for this process" in caplog.text
    assert s.cookie_secure is False and s.session_cookie_name == "ext_session_8000" and s.transaction_cookie_name == "ext_tx_8000"
    assert s.web_redirect_uri == "http://127.0.0.1:8000/auth/callback"


def test_nothing_is_guessed_not_even_locally():
    with pytest.raises(ConfigError, match="APPEXT_ISSUER is required"):
        ExtensionSettings.from_env(manifest, {})


def test_local_dev_port_from_the_manifest():
    m = loads_manifest(MANIFEST.replace('hosts = ["cdn.example.test"]', "dev_port = 8123"))
    assert ExtensionSettings.from_env(m, LOCAL).public_url == "http://127.0.0.1:8123"


def test_a_local_service_without_a_url_is_a_warning_not_an_error(caplog):
    with caplog.at_level(logging.WARNING, logger="appext"):
        s = ExtensionSettings.from_env(manifest, LOCAL)
    assert "projects" not in s.services and "export" not in s.services
    assert "APPEXT_SERVICE_PROJECTS_URL is not set" in caplog.text


def test_redirect_uri_depends_on_the_mode():
    s = ExtensionSettings.from_env(manifest, {**LOCAL, "APPEXT_APP_REDIRECT_URI": "com.example.app:/callback"})
    assert s.redirect_uri(True) == "com.example.app:/callback" and s.redirect_uri(False) == s.web_redirect_uri


def test_without_a_host_app_the_app_mode_has_no_address_of_its_own():
    s = ExtensionSettings.from_env(manifest, LOCAL)
    assert s.redirect_uri(True) == s.redirect_uri(False) == s.web_redirect_uri


# -- the platform file supplies the defaults of development at the desk ---------------------------------


@pytest.fixture
def project(tmp_path):
    (tmp_path / "extension.toml").write_text(MANIFEST.replace('audience = "projects-api"', 'audience = "projects-api"'))
    (tmp_path / "appext.toml").write_text(PLATFORM_FILE)
    return __import__("appext").load_manifest(tmp_path / "extension.toml")


def test_the_projects_platform_file_fills_in_what_the_environment_does_not_say(project):
    s = ExtensionSettings.from_env(project, {})
    assert s.issuer == "http://127.0.0.1:58080/realms/example"
    assert s.app_redirect_uri == "com.example.app:/callback"
    assert s.services["projects"] == "http://127.0.0.1:8000/api/v1"
    assert "export" not in s.services  # a service the file does not know stays unset


def test_the_environment_wins_over_the_platform_file(project):
    s = ExtensionSettings.from_env(project, {
        "APPEXT_ISSUER": "http://127.0.0.1:9999/realms/other",
        "APPEXT_APP_REDIRECT_URI": "com.other.app:/callback",
        "APPEXT_SERVICE_PROJECTS_URL": "http://127.0.0.1:1234",
    })
    assert s.issuer == "http://127.0.0.1:9999/realms/other"
    assert s.app_redirect_uri == "com.other.app:/callback"
    assert s.services["projects"] == "http://127.0.0.1:1234"


def test_a_deployment_does_not_read_the_platform_file(project, prod_env):
    prod_env.pop("APPEXT_ISSUER")
    with pytest.raises(ConfigError, match="APPEXT_ISSUER is required"):
        ExtensionSettings.from_env(project, prod_env)


def test_a_platform_file_that_names_a_platform_by_environment(project, tmp_path):
    named = tmp_path / "other.toml"
    named.write_text(PLATFORM_FILE.replace("realms/example", "realms/chosen"))
    s = ExtensionSettings.from_env(project, {"APPEXT_PLATFORM": str(named)})
    assert s.issuer == "http://127.0.0.1:58080/realms/chosen"


def test_a_broken_platform_file_is_reported(project, tmp_path):
    (tmp_path / "appext.toml").write_text('[platform]\nissuer = "not a url"\nsurprise = 1\n')
    with pytest.raises(ConfigError) as err:
        ExtensionSettings.from_env(project, {})
    assert "platform file" in str(err.value) and "platform.surprise" in str(err.value)


# -- what a platform may tell the page the extension draws -------------------------------------------------


def test_the_host_apps_name_marker_labels_and_accent():
    s = ExtensionSettings.from_env(manifest, LOCAL)
    assert s.app_name == "" and s.app_marker == "-App-WebView/" and dict(s.app_back_labels) == {} and s.app_accent == ""
    s = ExtensionSettings.from_env(manifest, {
        **LOCAL,
        "APPEXT_APP_NAME": "Acme",
        "APPEXT_APP_MARKER": "AcmeWebView/",
        "APPEXT_APP_BACK_LABELS": '{"en": "Back to {app}", "pt-BR": "Voltar para {app}"}',
        "APPEXT_APP_ACCENT": "#0b9f6a",
    })
    assert (s.app_name, s.app_marker, s.app_accent) == ("Acme", "AcmeWebView/", "#0b9f6a")
    assert dict(s.app_back_labels) == {"en": "Back to {app}", "pt-BR": "Voltar para {app}"}


@pytest.mark.parametrize("change, message", [
    ({"APPEXT_APP_BACK_LABELS": "not json"}, "APPEXT_APP_BACK_LABELS"),
    ({"APPEXT_APP_BACK_LABELS": '["en"]'}, "APPEXT_APP_BACK_LABELS"),
    ({"APPEXT_APP_BACK_LABELS": '{"english": "Back"}'}, "APPEXT_APP_BACK_LABELS"),
    ({"APPEXT_APP_BACK_LABELS": '{"en": ""}'}, "APPEXT_APP_BACK_LABELS"),
    ({"APPEXT_APP_ACCENT": "green"}, "APPEXT_APP_ACCENT"),
])
def test_bad_host_app_settings_are_reported(change, message):
    with pytest.raises(ConfigError, match=message):
        ExtensionSettings.from_env(manifest, {**LOCAL, **change})


# -- a deployment ----------------------------------------------------------------------------------------------


def test_a_complete_deployment_environment(prod_env):
    s = ExtensionSettings.from_env(manifest, prod_env)
    assert s.env == "prod" and not s.is_local
    assert s.public_url == "https://demo.apps.example.com" and s.origin == "https://demo.apps.example.com"
    assert s.cookie_secure and s.session_cookie_name == "__Host-ext_session" and s.transaction_cookie_name == "__Host-ext_tx"
    assert s.services == {"projects": "https://projects.example.com", "export": "https://export.example.com"}
    assert len(s.session_keys) == 1 and not s.session_key_generated
    assert s.client_key is not None


def test_a_deployment_guesses_nothing_and_reports_every_problem():
    with pytest.raises(ConfigError) as err:
        ExtensionSettings.from_env(manifest, {"APPEXT_ENV": "prod"})
    problems = " ".join(err.value.problems)
    for needed in ("APPEXT_ISSUER", "APPEXT_PUBLIC_URL", "APPEXT_CLIENT_KEY_FILE", "APPEXT_SESSION_KEY_FILE",
                   "APPEXT_SERVICE_PROJECTS_URL", "APPEXT_SERVICE_EXPORT_URL", "APPEXT_SESSION_STORE"):
        assert needed in problems
    assert len(err.value.problems) >= 7
    assert "APPEXT_ISSUER" in str(err.value)


def test_client_secret_auth_needs_the_secret_instead_of_the_key(prod_env, tmp_path):
    prod_env["APPEXT_CLIENT_AUTH"] = "client_secret"
    del prod_env["APPEXT_CLIENT_KEY_FILE"]
    with pytest.raises(ConfigError, match="APPEXT_CLIENT_SECRET_FILE"):
        ExtensionSettings.from_env(manifest, prod_env)
    secret = tmp_path / "secret"
    secret.write_text("topsecret\n")
    prod_env["APPEXT_CLIENT_SECRET_FILE"] = str(secret)
    s = ExtensionSettings.from_env(manifest, prod_env)
    assert s.client_secret.reveal() == "topsecret"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"APPEXT_ISSUER": "http://sso.example.com/realms/example"}, "https"),
        ({"APPEXT_ISSUER": "ftp://x"}, "http"),
        ({"APPEXT_PUBLIC_URL": "http://demo.apps.example.com"}, "https"),
        ({"APPEXT_PUBLIC_URL": "https://demo.apps.example.com/sub/path"}, "origin only"),
        ({"APPEXT_CLIENT_AUTH": "password"}, "APPEXT_CLIENT_AUTH"),
        ({"APPEXT_SESSION_STORE": "memory"}, "development only"),
        ({"APPEXT_SESSION_STORE": "mysql://x"}, "APPEXT_SESSION_STORE"),
        ({"APPEXT_TRUSTED_PROXIES": "not-an-ip"}, "APPEXT_TRUSTED_PROXIES"),
        ({"APPEXT_COOKIE_SECURE": "false"}, "APPEXT_COOKIE_SECURE"),
        ({"APPEXT_COOKIE_SECURE": "maybe"}, "APPEXT_COOKIE_SECURE"),
        ({"APPEXT_SERVICE_PROJECTS_URL": "projects.example.com"}, "APPEXT_SERVICE_PROJECTS_URL"),
        ({"APPEXT_APP_REDIRECT_URI": "no-scheme"}, "APPEXT_APP_REDIRECT_URI"),
        ({"APPEXT_SESSION_KEY_FILE": "/nonexistent/key"}, "cannot be read"),
        ({"APPEXT_HTTP_TIMEOUT": "fast"}, "APPEXT_HTTP_TIMEOUT"),
    ],
)
def test_invalid_values_are_named(prod_env, change, message):
    with pytest.raises(ConfigError, match=message):
        ExtensionSettings.from_env(manifest, {**prod_env, **change})


def test_loopback_may_use_http_even_in_a_deployment(prod_env):
    prod_env["APPEXT_PUBLIC_URL"] = "http://127.0.0.1:8000"
    prod_env["APPEXT_ISSUER"] = "http://127.0.0.1:58080/realms/example"
    s = ExtensionSettings.from_env(manifest, prod_env)
    assert not s.cookie_secure and s.session_cookie_name == "ext_session_8000"


def test_secure_cookies_cannot_be_forced_on_plain_http():
    with pytest.raises(ConfigError, match="https"):
        ExtensionSettings.from_env(manifest, {"APPEXT_COOKIE_SECURE": "true"})


def test_trusted_proxies_are_parsed(prod_env):
    prod_env["APPEXT_TRUSTED_PROXIES"] = "10.0.0.0/8, 192.168.1.5,::1"
    assert ExtensionSettings.from_env(manifest, prod_env).trusted_proxies == ("10.0.0.0/8", "192.168.1.5", "::1")


def test_issuer_trailing_slash_is_normalised(prod_env):
    prod_env["APPEXT_ISSUER"] += "/"
    assert ExtensionSettings.from_env(manifest, prod_env).issuer == "https://sso.example.com/realms/example"


def test_secrets_never_show_up_in_repr(prod_env, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("topsecret-value")
    prod_env["APPEXT_CLIENT_SECRET_FILE"] = str(secret)
    s = ExtensionSettings.from_env(manifest, prod_env)
    text = repr(s) + str(s)
    assert "topsecret-value" not in text and "BEGIN PRIVATE KEY" not in text
    assert repr(Secret("x")) == "Secret(***)" and str(Secret("x")) == "Secret(***)"
    keys = [k.reveal() for k in s.session_keys]
    assert all(base64.b64encode(k).decode() not in text for k in keys)


# -- session key files -------------------------------------------------------------------------------------------


def test_session_key_formats():
    raw = bytes(range(32))
    assert parse_session_keys(base64.b64encode(raw).decode() + "\n") == [raw]
    assert parse_session_keys(base64.urlsafe_b64encode(raw).decode()) == [raw]
    assert parse_session_keys(raw.hex()) == [raw]
    assert parse_session_keys(raw) == [raw]
    assert parse_session_keys(f"# active\n{raw.hex()}\n\n# previous\n{(raw[::-1]).hex()}\n") == [raw, raw[::-1]]


@pytest.mark.parametrize("content", ["", "# only a comment", "not base64 !!", base64.b64encode(b"short").decode(), "ab" * 20])
def test_bad_session_keys(content):
    with pytest.raises(ValueError):
        parse_session_keys(content)


def test_generated_session_key_is_valid_and_random():
    a, b = generate_session_key(), generate_session_key()
    assert a != b and len(parse_session_keys(a)[0]) == 32


# -- lock file -----------------------------------------------------------------------------------------------------------

LOCK = """
[lock]
extension = "demo"
version = "1.2.0"
client_id = "ext-demo"
environment = "prod"
generated_at = "2026-10-03T09:12:44Z"
approved_scopes = ["ext-data-read", "svc-projects-read", "svc-export-write"]
keycloak_client_uuid = "1234"
"""


def write_lock(tmp_path, text=LOCK):
    path = tmp_path / LOCK_FILENAME
    path.write_text(text)
    return path


def test_read_lock(tmp_path):
    lock = read_lock(write_lock(tmp_path))
    assert lock.extension == "demo" and lock.version == "1.2.0" and lock.environment == "prod"
    assert lock.approved_scopes == ("ext-data-read", "svc-projects-read", "svc-export-write")
    assert lock.keycloak_client_uuid == "1234" and lock.generated_at == "2026-10-03T09:12:44Z"


def test_read_lock_accepts_a_toml_datetime(tmp_path):
    lock = read_lock(write_lock(tmp_path, LOCK.replace('"2026-10-03T09:12:44Z"', "2026-10-03T09:12:44Z")))
    assert "2026-10-03" in lock.generated_at


@pytest.mark.parametrize("text", ["", "[lock]\nextension='x'", "[lock]\nextension='x'\nversion='1.0.0'\nclient_id='c'\napproved_scopes='a'", "not toml ["])
def test_unreadable_locks(tmp_path, text):
    with pytest.raises(LockError):
        read_lock(write_lock(tmp_path, text))


def test_missing_lock_file(tmp_path):
    with pytest.raises(LockError, match="not found"):
        read_lock(tmp_path / "nope.toml")


def lock_of(tmp_path, **replace):
    text = LOCK
    for old, new in replace.items():
        text = text.replace(old.replace("__", " "), new)
    return read_lock(write_lock(tmp_path, text))


def test_a_lock_that_covers_the_manifest_passes(tmp_path):
    check_lock(manifest, read_lock(write_lock(tmp_path)), environment="prod", client_id="ext-demo")


def test_extra_scopes_in_the_manifest_refuse_to_start(tmp_path):
    lock = lock_of(tmp_path, **{'"svc-export-write"': '"other"'})
    with pytest.raises(LockError, match="svc-export-write"):
        check_lock(manifest, lock)


def test_fewer_scopes_than_approved_are_fine(tmp_path):
    m = loads_manifest(MANIFEST.replace('scopes = ["svc-export-write"]', 'scopes = ["svc-export-write"]').replace('[consent]\nscopes = ["ext-data-read"]', "[consent]\nscopes = []"))
    check_lock(m, read_lock(write_lock(tmp_path)))


def test_every_mismatch_is_refused(tmp_path):
    base = read_lock(write_lock(tmp_path))
    with pytest.raises(LockError, match="belongs to extension"):
        check_lock(manifest, lock_of(tmp_path, **{'extension = "demo"': 'extension = "other"'}))
    with pytest.raises(LockError, match="version"):
        check_lock(manifest, lock_of(tmp_path, **{'version = "1.2.0"': 'version = "1.1.0"'}))
    with pytest.raises(LockError, match="client"):
        check_lock(manifest, base, client_id="ext-other")
    with pytest.raises(LockError, match="environment"):
        check_lock(manifest, base, environment="dev")


def test_startup_check_reads_the_lock_next_to_the_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "extension.toml").write_text(MANIFEST)
    write_lock(tmp_path, LOCK.replace('environment = "prod"', 'environment = "local"'))
    m = __import__("appext").load_manifest(tmp_path / "extension.toml")
    s = ExtensionSettings.from_env(m, {"APPEXT_ENV": "local", **LOCAL})
    assert check_startup(m, s).version == "1.2.0"
    write_lock(tmp_path, LOCK.replace('environment = "prod"', 'environment = "local"').replace('"svc-export-write"', '"x"'))
    with pytest.raises(LockError, match="svc-export-write"):
        check_startup(m, s)


def test_missing_lock_is_only_acceptable_locally(tmp_path, monkeypatch, prod_env):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "extension.toml").write_text(MANIFEST)
    m = __import__("appext").load_manifest(tmp_path / "extension.toml")
    assert check_startup(m, ExtensionSettings.from_env(m, LOCAL)) is None
    with pytest.raises(LockError, match="not found"):
        check_startup(m, ExtensionSettings.from_env(m, prod_env))


def test_lock_path_from_the_environment(tmp_path, prod_env):
    other = tmp_path / "elsewhere"
    other.mkdir()
    path = other / "my.lock.toml"
    path.write_text(LOCK)
    m = loads_manifest(MANIFEST)
    s = ExtensionSettings.from_env(m, {**prod_env, "APPEXT_LOCK_FILE": str(path)})
    assert check_startup(m, s).extension == "demo"


def test_two_extensions_on_one_machine_do_not_share_cookie_names(prod_env):
    """Cookies are scoped by host, not by port: with one name, the extension on :8100 and
    the one on :8200 would overwrite each other's session at 127.0.0.1."""
    names = []
    for port in (8100, 8200):
        env = {**prod_env, "APPEXT_PUBLIC_URL": f"http://127.0.0.1:{port}", "APPEXT_ISSUER": "http://127.0.0.1:58080/realms/example"}
        s = ExtensionSettings.from_env(manifest, env)
        names.append((s.session_cookie_name, s.transaction_cookie_name))
    assert names[0] != names[1] and names[0][0].endswith("_8100") and names[1][1].endswith("_8200")


def test_a_deployment_keeps_the_host_prefix_and_no_port_suffix(prod_env):
    s = ExtensionSettings.from_env(manifest, prod_env)
    assert s.session_cookie_name == "__Host-ext_session" and s.transaction_cookie_name == "__Host-ext_tx"


# -- the web app that may embed the extension ------------------------------------------------------------


def test_no_web_app_means_nothing_may_embed_the_extension(prod_env):
    s = ExtensionSettings.from_env(manifest, prod_env)
    assert s.app_origins == () and s.frame_ancestors == "'none'"


def test_app_origins_are_read_as_a_list_without_duplicates(prod_env):
    prod_env["APPEXT_APP_ORIGINS"] = "https://app.example.com, https://staging.example.com/ ,https://app.example.com"
    s = ExtensionSettings.from_env(manifest, prod_env)
    assert s.app_origins == ("https://app.example.com", "https://staging.example.com")
    assert s.frame_ancestors == "https://app.example.com https://staging.example.com"


@pytest.mark.parametrize(
    "value",
    [
        "https://app.example.com/path",        # an origin, not an address
        "https://*.example.com",               # a wildcard would let every subdomain embed us
        "app.example.com",                     # no scheme
        "https://app.example.com; script-src *",  # the value ends up in a Content-Security-Policy
        "http://app.example.com",              # http only on the own machine
        "https://user@app.example.com",
    ],
)
def test_an_app_origin_that_is_none_is_refused_at_start(prod_env, value):
    prod_env["APPEXT_APP_ORIGINS"] = value
    with pytest.raises(ConfigError) as caught:
        ExtensionSettings.from_env(manifest, prod_env)
    assert "APPEXT_APP_ORIGINS" in str(caught.value)


def test_the_local_web_app_may_use_http(prod_env):
    prod_env["APPEXT_APP_ORIGINS"] = "http://127.0.0.1:8088,http://localhost:8088"
    prod_env["APPEXT_PUBLIC_URL"] = "http://127.0.0.1:8100"
    prod_env["APPEXT_ISSUER"] = "http://127.0.0.1:58080/realms/example"
    s = ExtensionSettings.from_env(manifest, prod_env)
    assert s.app_origins == ("http://127.0.0.1:8088", "http://localhost:8088")
