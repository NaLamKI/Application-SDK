"""The command line, driven through `main()` with a `Context` instead of the real world:
a temporary working directory, a dictionary for the environment, captured output."""

from __future__ import annotations

import io
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jwt
import pytest

from appext.cli import Context, main
from appext.cli import keys as cli_keys
from appext.config import parse_session_keys

MANIFEST = """\
[extension]
id = "demo"
name = "Demo"
description = "A demo"
version = "1.0.0"
entry = "/"
icon = "icon.svg"
dev_port = 8123

[consent]
scopes = ["ext-data-read"]

[[services]]
name = "data"
audience = "data-api"
scopes = ["ext-stock-read"]
mode = "user"

[[services]]
name = "export"
audience = "export-api"
scopes = ["svc-export-write"]
mode = "service"
"""


PLATFORM = """\
[platform]
name = "Example Platform"
issuer = "http://127.0.0.1:58080/realms/example"
store_url = "http://127.0.0.1:8000/api/v1"
app_redirect_uri = "com.example.app:/callback"

[platform.services]
data-api = "http://127.0.0.1:8000/api/v1"

[platform.starter]
service = "data"
audience = "data-api"
scope = "ext-data-read"
"""

LINK = """\
[extension]
id = "shop-link"
name = "Shop"
version = "1.0.0"
kind = "link"
entry = "https://shop.example.com/app"
"""


@pytest.fixture(autouse=True)
def small_keys(monkeypatch):
    """RSA key generation is the slowest thing in this file; 2048 bits is plenty for a test."""
    monkeypatch.setattr(cli_keys, "RSA_BITS", 2048)


class Run:
    """One CLI invocation: `run("keys", "generate")` -> exit code, with `.out` and `.err` captured."""

    def __init__(self, cwd: Path, env: dict | None = None) -> None:
        self.cwd = cwd
        # The person's configuration directory is in the temporary tree: a test never reads the real one.
        self.env = {"XDG_CONFIG_HOME": str(cwd / ".xdg")} if env is None else env
        self.servers: list[tuple[str, dict]] = []

    def __call__(self, *argv: str) -> int:
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        ctx = Context(
            env=self.env,
            cwd=self.cwd,
            out=self.stdout,
            err=self.stderr,
            run_server=lambda target, **options: self.servers.append((target, options)),
        )
        return main(list(argv), ctx)

    @property
    def out(self) -> str:
        return self.stdout.getvalue()

    @property
    def err(self) -> str:
        return self.stderr.getvalue()


@pytest.fixture
def run(tmp_path) -> Run:
    return Run(tmp_path)


@pytest.fixture
def project(tmp_path) -> Path:
    (tmp_path / "extension.toml").write_text(MANIFEST, encoding="utf-8")
    (tmp_path / "appext.toml").write_text(PLATFORM, encoding="utf-8")
    (tmp_path / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    return tmp_path


@pytest.fixture
def link_project(tmp_path) -> Path:
    """A link is only a manifest: no icon file, no app, no keys."""
    (tmp_path / "extension.toml").write_text(LINK, encoding="utf-8")
    return tmp_path


# --- the command tree -------------------------------------------------------------------------


def test_without_a_command_it_prints_help_and_fails(run):
    assert run() == 2
    assert "usage: appext" in run.err


def test_unknown_template_is_a_usage_error(run):
    with pytest.raises(SystemExit) as stop:
        run("new", "demo", "--template", "nope")
    assert stop.value.code == 2


def test_version(run):
    assert run("--version") == 0
    assert run.out.startswith("appext ")


def test_every_command_has_help(run):
    for argv in (["new"], ["dev"], ["serve"], ["health"], ["manifest", "check"], ["keys", "generate"], ["keys", "session"],
                 ["keycloak", "export"], ["store", "login"], ["store", "register"], ["store", "bundle"]):
        with pytest.raises(SystemExit) as stop:
            run(*argv, "--help")
        assert stop.value.code == 0, argv


# --- keys -------------------------------------------------------------------------------------


def test_keys_generate_writes_a_private_pem_with_mode_0600_and_a_public_jwk(run, tmp_path):
    assert run("keys", "generate", "--out", "k") == 0
    pem, jwk_file = tmp_path / "k" / "client_key.pem", tmp_path / "k" / "client_key.jwk.json"
    assert stat.S_IMODE(pem.stat().st_mode) == 0o600
    jwk = json.loads(jwk_file.read_text())
    assert jwk["kty"] == "RSA" and jwk["alg"] == "RS256" and jwk["use"] == "sig" and jwk["kid"]
    assert not set(jwk) & {"d", "p", "q", "dp", "dq", "qi"}, "the public file must hold no private member"
    # the private key is never printed
    assert "PRIVATE" not in run.out and pem.read_text().splitlines()[1] not in run.out


def test_the_jwk_belongs_to_the_pem(run, tmp_path):
    run("keys", "generate", "--out", "k")
    pem = (tmp_path / "k" / "client_key.pem").read_bytes()
    jwk = json.loads((tmp_path / "k" / "client_key.jwk.json").read_text())
    token = jwt.encode({"sub": "x"}, pem, algorithm="RS256")
    public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    assert jwt.decode(token, public, algorithms=["RS256"])["sub"] == "x"


def test_es256_keys(run, tmp_path):
    assert run("keys", "generate", "--out", "k", "--alg", "ES256") == 0
    pem = (tmp_path / "k" / "client_key.pem").read_bytes()
    jwk = json.loads((tmp_path / "k" / "client_key.jwk.json").read_text())
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256" and jwk["alg"] == "ES256" and "d" not in jwk
    public = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
    assert jwt.decode(jwt.encode({"sub": "x"}, pem, algorithm="ES256"), public, algorithms=["ES256"])["sub"] == "x"


def test_the_kid_is_the_rfc7638_thumbprint():
    # The example key of RFC 7638 section 3.1 and its published thumbprint.
    jwk = {"kty": "RSA", "e": "AQAB", "n": (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMs"
        "tn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajr"
        "n1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw")}
    assert cli_keys.thumbprint(jwk) == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_keys_generate_refuses_to_overwrite_without_force(run, tmp_path):
    assert run("keys", "generate", "--out", "k") == 0
    before = (tmp_path / "k" / "client_key.pem").read_bytes()
    assert run("keys", "generate", "--out", "k") == 1
    assert "already exists" in run.err and "--force" in run.err
    assert (tmp_path / "k" / "client_key.pem").read_bytes() == before
    assert run("keys", "generate", "--out", "k", "--force") == 0
    assert (tmp_path / "k" / "client_key.pem").read_bytes() != before


def test_keys_session_writes_a_key_the_sdk_can_read(run, tmp_path):
    assert run("keys", "session", "--out", "s/session_key") == 0
    path = tmp_path / "s" / "session_key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    (key,) = parse_session_keys(path.read_bytes())
    assert len(key) == 32
    assert path.read_text().strip() not in run.out


# --- manifest check ---------------------------------------------------------------------------


def test_manifest_check_accepts_a_valid_manifest(run, project):
    assert run("manifest", "check") == 0
    assert "extension.toml: OK" in run.out
    assert "ext-demo" in run.out and "export" in run.out


def test_manifest_check_says_where_the_app_shows_the_extension(run, project):
    assert run("manifest", "check") == 0
    assert "display    in_app" in run.out and "system browser" not in run.out
    (project / "extension.toml").write_text(MANIFEST.replace('dev_port = 8123', 'dev_port = 8123\ndisplay = "external"'), encoding="utf-8")
    assert run("manifest", "check") == 0
    assert "display    external" in run.out and "system browser" in run.out


def test_manifest_check_of_an_ordinary_extension_has_no_kind_or_entry_line(run, project):
    assert run("manifest", "check") == 0
    assert "kind " not in run.out and "entry " not in run.out and "OAuth client ext-demo" in run.out


def test_manifest_check_names_a_link_and_its_address(run, link_project):
    assert run("manifest", "check") == 0
    lines = run.out.splitlines()
    assert lines[0] == "extension.toml: OK"
    assert "  kind       link" in lines and "  entry      https://shop.example.com/app" in lines
    assert any(line.startswith("  display    external") and "system browser" in line for line in lines)
    assert "  id         shop-link" in lines  # no Keycloak client to name
    assert "Keycloak" not in run.out and "client " not in run.out and "consent" not in run.out and "service" not in run.out
    assert "review is its only gate" in run.out and "rule 7" not in run.out
    assert run.err == ""  # no "icon does not exist": a link has no icon file


def test_manifest_check_shows_the_icon_address_of_a_link_and_does_not_look_for_a_file(run, link_project):
    (link_project / "extension.toml").write_text(LINK + 'icon = "https://shop.example.com/icon.svg"\n', encoding="utf-8")
    assert run("manifest", "check") == 0
    assert "  icon       https://shop.example.com/icon.svg" in run.out.splitlines()
    assert run.err == ""


def test_manifest_check_lists_what_is_wrong_with_a_link(run, link_project):
    (link_project / "extension.toml").write_text(
        LINK.replace("https://", "http://") + 'dev_port = 8100\nicon = "icon.svg"\n', encoding="utf-8")
    assert run("manifest", "check") == 1
    assert "INVALID" in run.out and "extension.entry" in run.out and "extension.dev_port" in run.out and "extension.icon" in run.out


def test_manifest_check_lists_every_broken_rule(run, project):
    (project / "extension.toml").write_text(MANIFEST.replace('id = "demo"', 'id = "Demo!"').replace("1.0.0", "one"), encoding="utf-8")
    assert run("manifest", "check") == 1
    assert "INVALID" in run.out
    assert "extension.id" in run.out and "extension.version" in run.out


def test_manifest_check_accepts_a_directory_or_a_file(run, project, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert Run(other)("manifest", "check", str(project)) == 0
    assert Run(other)("manifest", "check", str(project / "extension.toml")) == 0


def test_manifest_check_without_a_manifest(run):
    assert run("manifest", "check") == 1
    assert "no extension.toml" in run.err


def test_manifest_check_warns_about_a_missing_icon_but_passes(run, project):
    (project / "icon.svg").unlink()
    assert run("manifest", "check") == 0
    assert "icon" in run.err


def test_manifest_check_reports_a_manifest_that_is_not_toml(run, project):
    (project / "extension.toml").write_text("[extension\nid=", encoding="utf-8")
    assert run("manifest", "check") == 1
    assert "not valid TOML" in run.out


# --- secret scan ------------------------------------------------------------------------------

PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n"


def scan(run) -> int:
    return run("manifest", "check", "--scan-secrets")


def test_scan_is_quiet_on_a_clean_project(run, project):
    assert scan(run) == 0
    assert "nothing found" in run.out


def test_scan_finds_a_private_key_and_never_prints_it(run, project):
    (project / "deploy").mkdir()
    (project / "deploy" / "key.pem").write_text(PEM)
    assert scan(run) == 1
    assert "deploy/key.pem:1: private key (PEM)" in run.out
    assert "MIIEvQ" not in run.out


@pytest.mark.parametrize("line", [
    'client_secret = "s3cr3t-value-123"',
    '"client_secret": "s3cr3t-value-123",',
    "APPEXT_CLIENT_SECRET=Zk3j9sLq0PwX7vB2nM5tR8aY",
    "  client_secret: 'abcdefgh1234'",
])
def test_scan_finds_literal_client_secrets(run, project, line):
    (project / "settings.txt").write_text(line + "\n")
    assert scan(run) == 1
    assert "settings.txt:1: client secret value" in run.out
    assert "s3cr3t" not in run.out and "Zk3j9" not in run.out and "abcdefgh" not in run.out


@pytest.mark.parametrize("line", [
    "APPEXT_CLIENT_SECRET_FILE=/run/secrets/client_secret",
    "client_secret = os.environ['X']",
    "client_secret = settings.client_secret",
    'client_secret: "${CLIENT_SECRET}"',
    'client_secret: "<your secret here>"',
    'client_auth = "client_secret"',
    "token_endpoint_auth_method: client_secret_basic",
    "client_secret = None",
])
def test_scan_ignores_references_and_method_names(run, project, line):
    (project / "settings.txt").write_text(line + "\n")
    assert scan(run) == 0, line


def test_scan_tells_a_public_jwk_from_a_private_one(run, project):
    run("keys", "generate", "--out", "pub")
    assert scan(run) == 1  # the PEM from keys generate: no ignore files here
    assert "pub/client_key.pem" in run.out and "client_key.jwk.json" not in run.out
    (project / "pub" / "client_key.pem").unlink()
    assert scan(run) == 0  # the public JWK alone is fine
    private = json.loads((project / "pub" / "client_key.jwk.json").read_text())
    private["d"] = "A" * 43
    (project / "pub" / "client_key.jwk.json").write_text(json.dumps(private))
    assert scan(run) == 1 and "private JWK member" in run.out


def test_scan_skips_what_git_and_docker_both_ignore(run, project):
    (project / ".appext").mkdir()
    (project / ".appext" / "client_key.pem").write_text(PEM)
    (project / "Dockerfile").write_text("FROM scratch\n")
    (project / ".gitignore").write_text(".appext/\n")
    (project / ".dockerignore").write_text(".appext\n")
    assert scan(run) == 0


def test_scan_still_flags_a_key_that_only_git_ignores_when_an_image_is_built(run, project):
    """git ignores it, but `docker build` would copy it into the image."""
    (project / ".appext").mkdir()
    (project / ".appext" / "client_key.pem").write_text(PEM)
    (project / "Dockerfile").write_text("FROM scratch\n")
    (project / ".gitignore").write_text(".appext/\n")
    assert scan(run) == 1
    (project / ".dockerignore").write_text("# forgot the key\n")
    assert scan(run) == 1


def test_scan_flags_a_key_that_only_the_image_ignores(run, project):
    """The image is safe, the repository is not: it could be committed."""
    (project / "k.pem").write_text(PEM)
    (project / "Dockerfile").write_text("FROM scratch\n")
    (project / ".dockerignore").write_text("**/*.pem\n")
    assert scan(run) == 1


def test_docker_patterns_match_from_the_root_only(run, project):
    """`*.pem` in a .dockerignore does not reach into subdirectories – so the scan must still look there."""
    (project / "sub").mkdir()
    (project / "sub" / "k.pem").write_text(PEM)
    (project / "Dockerfile").write_text("FROM scratch\n")
    (project / ".gitignore").write_text("*.pem\n")
    (project / ".dockerignore").write_text("*.pem\n")
    assert scan(run) == 1
    (project / ".dockerignore").write_text("**/*.pem\n")
    assert scan(run) == 0


def test_scan_skips_dependencies_and_binary_files(run, project):
    (project / "node_modules" / "x").mkdir(parents=True)
    (project / "node_modules" / "x" / "k.pem").write_text(PEM)
    (project / "blob.bin").write_bytes(b"\0" + PEM.encode())
    assert scan(run) == 0


# --- keycloak export --------------------------------------------------------------------------


def exported(run, *extra) -> dict:
    assert run("keycloak", "export", *extra) == 0
    return json.loads(run.out)


def test_export_describes_the_client(run, project):
    realm = exported(run)
    client = realm["clients"][0]
    assert client["clientId"] == "ext-demo"
    assert client["consentRequired"] and not client["publicClient"] and not client["fullScopeAllowed"]
    assert not client["directAccessGrantsEnabled"] and not client["implicitFlowEnabled"]
    assert client["serviceAccountsEnabled"], "one service has mode = service"
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["attributes"]["standard.token.exchange.enabled"] == "true"
    assert client["clientAuthenticatorType"] == "client-jwt"
    assert client["attributes"]["backchannel.logout.url"] == "http://host.docker.internal:8123/auth/backchannel-logout"
    assert set(client["redirectUris"]) == {
        "http://localhost:8123/auth/callback",
        "http://127.0.0.1:8123/auth/callback",
        "com.example.app:/callback",
    }


def test_export_carries_the_public_key_and_nothing_private(run, project):
    realm = exported(run)
    keys = json.loads(realm["clients"][0]["attributes"]["jwks.string"])["keys"]
    assert len(keys) == 1 and keys[0]["kty"] == "RSA" and keys[0]["use"] == "sig"
    assert not set(keys[0]) & {"d", "p", "q", "dp", "dq", "qi"}
    pem = (project / ".appext" / "client_key.pem").read_text()
    assert "PRIVATE" not in json.dumps(realm) and pem not in json.dumps(realm)
    assert realm["clients"][0]["attributes"]["token.endpoint.auth.signing.alg"] == "RS256"


def test_export_assigns_scopes_like_the_store_would(run, project):
    realm = exported(run)
    client = realm["clients"][0]
    assert client["defaultClientScopes"] == ["basic", "acr", "ext-data-read"]
    assert client["optionalClientScopes"] == ["ext-stock-read", "svc-export-write"]
    scopes = {s["name"]: s for s in realm["clientScopes"]}
    # `basic` carries `sub`: a realm file that lists clientScopes replaces the built-in set
    assert {m["protocolMapper"] for m in scopes["basic"]["protocolMappers"]} >= {"oidc-sub-mapper"}
    audience = {name: scopes[name]["protocolMappers"][0]["config"]["included.client.audience"]
                for name in ("ext-data-read", "ext-stock-read", "svc-export-write")}
    assert audience == {"ext-data-read": "data-api", "ext-stock-read": "data-api", "svc-export-write": "export-api"}
    for name in audience:
        assert scopes[name]["attributes"]["include.in.token.scope"] == "true"


def test_export_with_a_client_secret_leaves_the_secret_to_keycloak(run, project):
    (project / "extension.toml").write_text(MANIFEST.replace('dev_port = 8123', 'dev_port = 8123\nclient_auth = "client_secret"'))
    client = exported(run)["clients"][0]
    assert client["clientAuthenticatorType"] == "client-secret"
    assert "secret" not in client and "jwks.string" not in client["attributes"]


def test_export_has_no_client_to_describe_for_a_link(run, link_project):
    assert run("keycloak", "export") == 1
    assert "shop-link is a link" in run.err and "no Keycloak client" in run.err
    assert run.out == "" and not (link_project / ".appext").exists()


def test_export_options(run, project):
    realm = exported(run, "--realm", "dev", "--dev-user", "--backchannel-host", "kc-net")
    assert realm["realm"] == "dev"
    assert realm["users"][0]["username"] == "dev@localhost.invalid"
    assert realm["clients"][0]["attributes"]["backchannel.logout.url"].startswith("http://kc-net:8123/")
    custom = exported(run, "--backchannel-url", "http://extension:8000/auth/backchannel-logout")
    assert custom["clients"][0]["attributes"]["backchannel.logout.url"] == "http://extension:8000/auth/backchannel-logout"


def test_export_gives_every_audience_a_client_for_the_token_exchange(run, project):
    """Keycloak answers `Audience not found` for an audience that is not a client of the realm."""
    realm = exported(run)
    audiences = {c["clientId"]: c for c in realm["clients"] if c["clientId"] != "ext-demo"}
    assert set(audiences) == {"data-api", "export-api"}
    assert all(c["bearerOnly"] and not c["standardFlowEnabled"] for c in audiences.values())


def test_export_writes_a_file(run, project):
    assert run("keycloak", "export", "--out", ".appext/realm-ext.json") == 0
    assert json.loads((project / ".appext" / "realm-ext.json").read_text())["clients"]
    assert "realm-ext.json" in run.out


def test_export_takes_the_realm_the_consent_audience_and_the_app_address_from_the_platform(run, project):
    realm = exported(run)
    assert realm["realm"] == "example"                         # the last part of the issuer's path
    client = realm["clients"][0]
    assert "com.example.app:/callback" in client["redirectUris"]
    assert client["attributes"]["consent.screen.text"] == "Extension for Example Platform: Demo"
    scopes = {s["name"]: s for s in realm["clientScopes"]}
    assert scopes["ext-data-read"]["protocolMappers"][0]["config"]["included.client.audience"] == "data-api"


def test_export_without_a_platform_still_works_and_adds_no_host_app_address(run, project):
    (project / "appext.toml").unlink()
    realm = exported(run, "--consent-audience", "my-api")
    assert realm["realm"] == "local"
    assert set(realm["clients"][0]["redirectUris"]) == {"http://localhost:8123/auth/callback", "http://127.0.0.1:8123/auth/callback"}
    assert "Extension for the platform: Demo" == realm["clients"][0]["attributes"]["consent.screen.text"]


# --- dev, serve, health -----------------------------------------------------------------------


def test_dev_has_nothing_to_run_for_a_link(run, link_project):
    assert run("dev") == 1
    assert "shop-link is a link" in run.err and "no server" in run.err and "nothing to run" in run.err
    assert "appext store register" in run.err
    assert run.servers == [] and not (link_project / ".appext").exists()  # no key made for nothing


def test_serve_refuses_a_directory_that_holds_a_link(run, link_project):
    assert run("serve", "app.main:app") == 1
    assert "is a link" in run.err and run.servers == []


def test_serve_leaves_a_broken_manifest_to_the_app(run, tmp_path):
    (tmp_path / "extension.toml").write_text("[extension\nid=", encoding="utf-8")
    assert run("serve", "app.main:app") == 0 and len(run.servers) == 1


def test_serve_still_serves_an_ordinary_project(run, project):
    assert run("serve", "app.main:app") == 0 and len(run.servers) == 1


def test_dev_runs_the_app_in_browser_mode_with_reload(run, project):
    (project / "app").mkdir()
    assert run("dev") == 0
    (target, options), = run.servers
    assert target == "app.main:app"
    assert options["host"] == "127.0.0.1" and options["port"] == 8123  # dev_port of the manifest
    assert options["reload"] is True and options["reload_dirs"] == [str(project / "app")]
    assert options["app_dir"] == str(project)


def test_dev_prepares_the_local_environment(run, project):
    assert run("dev") == 0
    env = run.env
    assert env["APPEXT_ENV"] == "local"
    assert env["APPEXT_PUBLIC_URL"] == "http://127.0.0.1:8123"
    assert env["APPEXT_SESSION_STORE"] == "memory"
    key = Path(env["APPEXT_CLIENT_KEY_FILE"])
    assert key == project / ".appext" / "client_key.pem" and key.exists()
    assert env["APPEXT_CLIENT_KEY_ID"] == json.loads((project / ".appext" / "client_key.jwk.json").read_text())["kid"]
    assert "created now" in run.out and "appext store register" in run.out


def test_dev_takes_what_the_platform_file_knows(run, project):
    assert run("dev") == 0
    env = run.env
    assert env["APPEXT_ISSUER"] == "http://127.0.0.1:58080/realms/example"
    assert env["APPEXT_APP_REDIRECT_URI"] == "com.example.app:/callback"
    assert env["APPEXT_APP_NAME"] == "Example Platform"
    assert env["APPEXT_SERVICE_DATA_URL"] == "http://127.0.0.1:8000/api/v1"
    assert "APPEXT_SERVICE_EXPORT_URL" not in env
    assert "sign-in    http://127.0.0.1:58080/realms/example" in run.out


def test_dev_needs_a_platform_and_says_how_to_name_one(run, project):
    (project / "appext.toml").unlink()
    assert run("dev") == 1
    assert "APPEXT_ISSUER" in run.err and "appext.toml" in run.err and not run.servers


def test_dev_can_be_pointed_at_another_platform(run, project, tmp_path):
    other = tmp_path / "elsewhere.toml"
    other.write_text(PLATFORM.replace("realms/example", "realms/other").replace("Example Platform", "Other Platform"))
    assert run("dev", "--platform", str(other)) == 0
    assert run.env["APPEXT_ISSUER"] == "http://127.0.0.1:58080/realms/other" and run.env["APPEXT_APP_NAME"] == "Other Platform"


def test_dev_finds_a_named_platform_in_the_config_directory(run, project):
    named = project / ".xdg" / "appext" / "platforms"
    named.mkdir(parents=True)
    (named / "staging.toml").write_text(PLATFORM.replace("realms/example", "realms/staging"))
    assert run("dev", "--platform", "staging") == 0
    assert run.env["APPEXT_ISSUER"] == "http://127.0.0.1:58080/realms/staging"
    assert run("dev", "--platform", "missing") == 1 and "does not exist" in run.err


def test_dev_reuses_the_key_it_created(run, project):
    run("dev")
    before = (project / ".appext" / "client_key.pem").read_bytes()
    run("dev")
    assert (project / ".appext" / "client_key.pem").read_bytes() == before
    assert "created now" not in run.out


def test_dev_env_files_override_the_defaults_and_the_shell_overrides_both(run, project):
    (project / "bundle.env").write_text('# bundle\nAPPEXT_ISSUER="http://kc.test/realms/x"\nAPPEXT_SERVICE_DATA_URL=http://data.test/api\n'
                                         'APPEXT_SERVICE_EXPORT_URL=http://export.test\nexport APPEXT_PUBLIC_URL=http://127.0.0.1:9999\n')
    run.env["APPEXT_PUBLIC_URL"] = "http://localhost:8123"
    assert run("dev", "--env-file", "bundle.env", "--no-reload") == 0
    assert run.env["APPEXT_ISSUER"] == "http://kc.test/realms/x"
    assert run.env["APPEXT_PUBLIC_URL"] == "http://localhost:8123"
    assert run.servers[0][1]["reload"] is False


def test_dev_takes_only_appext_variables_from_an_env_file(run, project):
    (project / "bundle.env").write_text("APPEXT_ISSUER=http://kc.test/realms/x\nPATH=/evil\nPYTHONPATH=/evil\n")
    assert run("dev", "--env-file", "bundle.env") == 0
    assert run.env["APPEXT_ISSUER"] == "http://kc.test/realms/x"
    assert "PATH" not in run.env and "PYTHONPATH" not in run.env
    assert "ignoring PATH, PYTHONPATH" in run.err


def test_dev_warns_about_a_service_without_a_url(run, project):
    run("dev")
    assert "APPEXT_SERVICE_EXPORT_URL is not set" in run.err
    assert "APPEXT_SERVICE_DATA_URL" not in run.err  # the platform file knows where data-api is


def test_dev_explains_an_unusable_configuration(run, project):
    run.env["APPEXT_ISSUER"] = "not-a-url"
    assert run("dev") == 1
    assert "APPEXT_ISSUER" in run.err and not run.servers


def test_dev_port_and_host_can_be_chosen(run, project):
    assert run("dev", "--port", "9100", "--host", "localhost") == 0
    assert run.servers[0][1]["port"] == 9100
    assert run.env["APPEXT_PUBLIC_URL"] == "http://localhost:9100"


def test_serve_trusts_proxy_headers_only_from_the_named_proxies(run):
    run.env["APPEXT_TRUSTED_PROXIES"] = "10.0.0.0/8, 192.168.1.5"
    assert run("serve", "app.main:app") == 0
    (target, options), = run.servers
    assert target == "app.main:app"
    assert options["proxy_headers"] is True and options["forwarded_allow_ips"] == "10.0.0.0/8, 192.168.1.5"
    assert options["host"] == "0.0.0.0" and options["port"] == 8000 and "reload" not in options


def test_serve_without_trusted_proxies_ignores_forwarded_headers(run):
    run("serve", "app.main:app")
    assert run.servers[0][1]["proxy_headers"] is False
    assert "forwarded_allow_ips" not in run.servers[0][1]


def test_serve_warns_about_a_wildcard(run):
    run.env["APPEXT_TRUSTED_PROXIES"] = "*"
    run("serve", "app.main:app")
    assert "every address" in run.err


def test_serve_port_comes_from_the_environment(run):
    run.env["PORT"] = "9001"
    run("serve", "app.main:app")
    assert run.servers[0][1]["port"] == 9001
    run("serve", "app.main:app", "--port", "7")
    assert run.servers[1][1]["port"] == 7


class _Health(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status if self.path == "/healthz" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def health_server():
    server = HTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_health_exit_code_follows_healthz(run, health_server):
    run.env["PORT"] = str(health_server.server_port)
    assert run("health") == 0
    _Health.status = 503
    try:
        assert run("health") == 1
    finally:
        _Health.status = 200


def test_health_fails_when_nothing_listens(run):
    free = HTTPServer(("127.0.0.1", 0), _Health)
    port = free.server_port
    free.server_close()
    assert run("health", "--port", str(port)) == 1
