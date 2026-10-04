"""`appext store …` against a mocked App Store and a mocked identity provider
(`httpx.MockTransport`), with a fake clock so that polling takes no time."""

from __future__ import annotations

import base64
import io
import json
import stat
import zipfile
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from appext.cli import Context, main
from appext.cli import keys as cli_keys

ISSUER = "http://127.0.0.1:58080/realms/example"
API = "http://127.0.0.1:8000/api/v1"
STORE = f"{API}/store"  # the store API lives under <store_url>/store (docs/platform-contract/app-store-api.md)
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
DEVICE_URL = f"{ISSUER}/protocol/openid-connect/auth/device"

MANIFEST = """\
[extension]
id = "demo"
name = "Demo"
version = "1.0.0"
entry = "/"
icon = "icon.svg"
"""


LINK = """\
[extension]
id = "shop-link"
name = "Shop"
version = "1.0.0"
kind = "link"
entry = "https://shop.example.com/app"
"""


def jwt_with(claims: dict) -> str:
    """Unsigned – the CLI only reads the payload to say who is signed in."""
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class World:
    """The CLI, its files, a clock and a transport whose handler each test fills in."""

    def __init__(self, tmp_path: Path) -> None:
        self.cwd = tmp_path / "project"
        self.cwd.mkdir()
        self.home = tmp_path / "config"
        self.clock = Clock()
        self.requests: list[httpx.Request] = []
        self.responses: list = []  # callables or Responses, consumed in order per route
        self.routes: dict[tuple[str, str], list] = {}
        self.opened: list[str] = []
        # The platform comes from the environment here; a file of the project is tested on its own below.
        self.env: dict[str, str] = {"XDG_CONFIG_HOME": str(self.home), "APPEXT_ISSUER": ISSUER, "APPEXT_STORE_URL": API}

    # -- mock plumbing
    def on(self, method: str, url: str, *responses) -> None:
        self.routes.setdefault((method, url), []).extend(responses)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, str(request.url).split("?")[0])
        queue = self.routes.get(key)
        if not queue:
            return httpx.Response(599, json={"unmocked": f"{key[0]} {key[1]}"})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item(request) if callable(item) else item

    def __call__(self, *argv: str) -> int:
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        ctx = Context(env=self.env, cwd=self.cwd, out=self.stdout, err=self.stderr,
                      transport=httpx.MockTransport(self._handle), sleep=self.clock.sleep, now=self.clock.now,
                      open_url=self.opened.append)
        return main(list(argv), ctx)

    @property
    def out(self) -> str:
        return self.stdout.getvalue()

    @property
    def err(self) -> str:
        return self.stderr.getvalue()

    # -- state
    @property
    def credentials_file(self) -> Path:
        return self.home / "appext" / "credentials.json"

    def sign_in(self, *, expires_in: float = 3600, refresh: str | None = "refresh-1", roles=("store-developer",)) -> None:
        """Credentials as `store login` leaves them."""
        self.credentials_file.parent.mkdir(parents=True, exist_ok=True)
        self.credentials_file.write_text(json.dumps({"version": 1, "credentials": {ISSUER: {
            "client_id": "appext-cli", "token_endpoint": TOKEN_URL, "refresh_token": refresh,
            "access_token": jwt_with({"preferred_username": "ada", "realm_access": {"roles": list(roles)}}),
            "expires_at": self.clock.t + expires_in,
        }}}))

    def bearer_of_last_request(self) -> str:
        return self.requests[-1].headers["authorization"].removeprefix("Bearer ")


@pytest.fixture(autouse=True)
def small_keys(monkeypatch):
    monkeypatch.setattr(cli_keys, "RSA_BITS", 2048)


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


@pytest.fixture
def project(world) -> Path:
    (world.cwd / "extension.toml").write_text(MANIFEST)
    return world.cwd


@pytest.fixture
def link_project(world) -> Path:
    """A link is only a manifest: no icon file, no key, no app."""
    (world.cwd / "extension.toml").write_text(LINK)
    return world.cwd


@pytest.fixture
def signed_in_link(world, link_project) -> World:
    world.sign_in()
    return world


def stored_link(**fields) -> dict:
    """What the store answers for a link: no client, no key, the same address in every environment."""
    address = {"entry": "https://shop.example.com/app", "callback": None}
    return {"id": "shop-link", "status": "DRAFT", "name": "Shop", "version": "1.0.0", "kind": "link",
            "clientId": None, "clientAuth": None, "hasKey": False,
            "versions": [{"version": "1.0.0", "status": "DRAFT"}], "urls": {"local": address, "prod": address}, **fields}


def error_body(code: str, message: str, status: int, **extra) -> httpx.Response:
    return httpx.Response(status, json={"detail": {"code": code, "message": message, **extra}})


# --- login: the device authorization grant ----------------------------------------------------

DISCOVERY = {"token_endpoint": TOKEN_URL, "device_authorization_endpoint": DEVICE_URL}
GRANT = {"device_code": "dc-1", "user_code": "ABCD-EFGH", "verification_uri": f"{ISSUER}/device",
         "verification_uri_complete": f"{ISSUER}/device?user_code=ABCD-EFGH", "expires_in": 600, "interval": 5}


def pending() -> httpx.Response:
    return httpx.Response(400, json={"error": "authorization_pending"})


def granted(**claims) -> httpx.Response:
    token = jwt_with({"preferred_username": "ada", "realm_access": {"roles": ["store-developer", "offline_access"]}, **claims})
    return httpx.Response(200, json={"access_token": token, "refresh_token": "refresh-1", "expires_in": 300})


@pytest.fixture
def idp(world) -> World:
    world.on("GET", f"{ISSUER}/.well-known/openid-configuration", httpx.Response(200, json=DISCOVERY))
    world.on("POST", DEVICE_URL, httpx.Response(200, json=GRANT))
    return world


def test_login_polls_until_the_person_approves(idp):
    idp.on("POST", TOKEN_URL, pending(), pending(), granted())
    assert idp("store", "login") == 0
    assert "Signed in as ada (store-developer)" in idp.out
    assert idp.clock.slept == [5, 5, 5]
    assert idp.opened == [GRANT["verification_uri_complete"]]
    assert "ABCD-EFGH" in idp.out and GRANT["verification_uri_complete"] in idp.out


def test_login_sends_the_device_grant_of_the_public_client_with_pkce(idp):
    import hashlib
    idp.on("POST", TOKEN_URL, granted())
    idp("store", "login")
    start, poll = [r for r in idp.requests if r.method == "POST"]
    started = {k: v[0] for k, v in parse_qs(start.content.decode()).items()}
    assert started["client_id"] == "appext-cli" and started["scope"] == "openid"
    assert started["code_challenge_method"] == "S256"  # the realm requires PKCE for this client
    polled = {k: v[0] for k, v in parse_qs(poll.content.decode()).items()}
    assert polled["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert polled["device_code"] == "dc-1" and polled["client_id"] == "appext-cli"
    # the verifier on the token request belongs to the challenge on the device request
    expected = base64.urlsafe_b64encode(hashlib.sha256(polled["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert started["code_challenge"] == expected and len(polled["code_verifier"]) >= 43


def test_login_slows_down_when_the_server_asks(idp):
    slow = httpx.Response(400, json={"error": "slow_down"})
    idp.on("POST", TOKEN_URL, pending(), slow, pending(), granted())
    assert idp("store", "login") == 0
    assert idp.clock.slept == [5, 5, 10, 10]  # +5 s from the slow_down on (RFC 8628 section 3.5)


def test_login_stores_the_credentials_privately(idp):
    idp.on("POST", TOKEN_URL, granted())
    idp("store", "login", "--no-browser")
    assert idp.opened == []
    assert stat.S_IMODE(idp.credentials_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(idp.credentials_file.parent.stat().st_mode) == 0o700
    entry = json.loads(idp.credentials_file.read_text())["credentials"][ISSUER]
    assert entry["refresh_token"] == "refresh-1" and entry["expires_at"] == idp.clock.t + 300
    assert idp.out.count("refresh-1") == 0 and entry["access_token"] not in idp.out


def test_login_warns_when_the_account_has_no_store_role(idp):
    idp.on("POST", TOKEN_URL, httpx.Response(200, json={"access_token": jwt_with({"preferred_username": "bob"}), "expires_in": 60}))
    assert idp("store", "login") == 0
    assert "no store role" in idp.out and "no store role" in idp.err


@pytest.mark.parametrize("error, text", [
    ("access_denied", "declined"),
    ("expired_token", "expired"),
])
def test_login_failures_are_explained(idp, error, text):
    idp.on("POST", TOKEN_URL, pending(), httpx.Response(400, json={"error": error}))
    assert idp("store", "login") == 1
    assert text in idp.err
    assert not idp.credentials_file.exists()


def test_login_gives_up_after_the_codes_lifetime(idp):
    idp.routes[("POST", DEVICE_URL)] = [httpx.Response(200, json={**GRANT, "expires_in": 12})]
    idp.on("POST", TOKEN_URL, pending())
    assert idp("store", "login") == 1
    assert "timed out" in idp.err


def test_login_when_the_sign_in_is_unreachable(world):
    world.on("GET", f"{ISSUER}/.well-known/openid-configuration", httpx.Response(503))
    assert world("store", "login") == 1
    assert "cannot reach the sign-in" in world.err


def test_login_needs_a_server_that_offers_the_device_grant(world):
    world.on("GET", f"{ISSUER}/.well-known/openid-configuration", httpx.Response(200, json={"token_endpoint": TOKEN_URL}))
    assert world("store", "login") == 1
    assert "device authorization" in world.err


def test_issuer_can_come_from_the_environment(world):
    world.env["APPEXT_ISSUER"] = "https://sso.example.com/realms/x"
    world.on("GET", "https://sso.example.com/realms/x/.well-known/openid-configuration", httpx.Response(503))
    world("store", "login")
    assert str(world.requests[0].url).startswith("https://sso.example.com/realms/x/")


def test_logout_forgets_the_credentials(world):
    world.sign_in()
    assert world("store", "logout") == 0 and "Signed out" in world.out
    assert json.loads(world.credentials_file.read_text())["credentials"] == {}
    assert world("store", "logout") == 0 and "not signed in" in world.out


# --- credentials: caching and refresh ---------------------------------------------------------


def test_a_fresh_token_is_used_as_it_is(world):
    world.sign_in(expires_in=3600)
    world.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[]))
    assert world("store", "status") == 0
    assert [r.url.path for r in world.requests] == ["/api/v1/store/extensions"]  # no call to the identity provider


def test_an_expiring_token_is_refreshed_and_the_new_one_saved(world):
    world.sign_in(expires_in=10, refresh="refresh-1")  # inside the 30 s margin
    world.on("POST", TOKEN_URL, httpx.Response(200, json={
        "access_token": jwt_with({"preferred_username": "ada"}), "refresh_token": "refresh-2", "expires_in": 300}))
    world.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[]))
    assert world("store", "status") == 0
    refresh = parse_qs(world.requests[0].content.decode())
    assert refresh == {"grant_type": ["refresh_token"], "refresh_token": ["refresh-1"], "client_id": ["appext-cli"]}
    saved = json.loads(world.credentials_file.read_text())["credentials"][ISSUER]
    assert saved["refresh_token"] == "refresh-2" and saved["expires_at"] == world.clock.t + 300
    assert stat.S_IMODE(world.credentials_file.stat().st_mode) == 0o600
    assert world.bearer_of_last_request() == saved["access_token"]


def test_a_refresh_without_a_rotated_token_keeps_the_old_refresh_token(world):
    world.sign_in(expires_in=0)
    world.on("POST", TOKEN_URL, httpx.Response(200, json={"access_token": jwt_with({}), "expires_in": 300}))
    world.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[]))
    world("store", "status")
    assert json.loads(world.credentials_file.read_text())["credentials"][ISSUER]["refresh_token"] == "refresh-1"


def test_a_spent_refresh_token_means_sign_in_again(world):
    world.sign_in(expires_in=0)
    world.on("POST", TOKEN_URL, httpx.Response(400, json={"error": "invalid_grant"}))
    assert world("store", "status") == 1
    assert "expired" in world.err and "appext store login" in world.err
    assert json.loads(world.credentials_file.read_text())["credentials"] == {}


def test_not_signed_in(world):
    assert world("store", "status") == 1
    assert "not signed in" in world.err and "APPEXT_STORE_TOKEN" in world.err


def test_ci_token_from_the_environment_skips_the_login(world):
    world.env["APPEXT_STORE_TOKEN"] = "ci-token"
    world.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[]))
    assert world("store", "status") == 0
    assert world.bearer_of_last_request() == "ci-token"
    assert not world.credentials_file.exists()


def test_a_token_is_not_sent_over_plain_http_to_another_machine_without_a_warning(world):
    world.env["APPEXT_STORE_TOKEN"] = "t"
    world.on("GET", "http://store.example.com/api/v1/store/extensions", httpx.Response(200, json=[]))
    assert world("store", "status", "--store-url", "http://store.example.com/api/v1") == 0
    assert "not https" in world.err
    world.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[]))
    assert world("store", "status") == 0 and world.err == ""  # loopback is fine


def test_store_url_from_flag_or_environment(world):
    world.env["APPEXT_STORE_TOKEN"] = "t"
    world.on("GET", "https://store.example.com/api/v1/store/extensions", httpx.Response(200, json=[]))
    world.env["APPEXT_STORE_URL"] = "https://store.example.com/api/v1/"
    assert world("store", "status") == 0
    world.env["APPEXT_STORE_URL"] = "https://wrong.example.com"
    assert world("store", "status", "--store-url", "https://store.example.com/api/v1") == 0


# --- which platform the commands talk to ---------------------------------------------------------

PLATFORM_FILE = """\
[platform]
name = "Example Platform"
issuer = "{issuer}"
store_url = "{store}"
cli_client_id = "{client}"
"""


def platform_file(path: Path, *, issuer=ISSUER, store=API, client="appext-cli") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLATFORM_FILE.format(issuer=issuer, store=store, client=client))
    return path


@pytest.fixture
def unconfigured(world) -> World:
    """No environment variables, no platform file: nothing says where the sign-in and the store are."""
    world.env = {"XDG_CONFIG_HOME": str(world.home)}
    return world


def test_nothing_is_guessed_and_the_error_says_how_to_configure(unconfigured):
    assert unconfigured("store", "status", "--all") == 1
    for way in ("--store-url", "APPEXT_STORE_URL", "appext.toml", "--platform"):
        assert way in unconfigured.err
    assert unconfigured.requests == []
    assert unconfigured("store", "login") == 1
    for way in ("--issuer", "APPEXT_ISSUER", "appext.toml"):
        assert way in unconfigured.err


def test_the_projects_platform_file_names_the_issuer_and_the_store(unconfigured, project):
    platform_file(project / "appext.toml")
    unconfigured.sign_in()
    unconfigured.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[]))
    assert unconfigured("store", "status", "--all") == 0
    assert str(unconfigured.requests[-1].url) == f"{STORE}/extensions", "the store of the file was asked"


def test_a_named_platform_lives_in_the_config_directory(unconfigured):
    platform_file(unconfigured.home / "appext" / "platforms" / "staging.toml", store="https://store.staging.example/api/v1",
                  issuer="https://auth.staging.example/realms/staging")
    unconfigured.env["APPEXT_STORE_TOKEN"] = "t"
    unconfigured.on("GET", "https://store.staging.example/api/v1/store/extensions", httpx.Response(200, json=[]))
    assert unconfigured("store", "status", "--all", "--platform", "staging") == 0
    unconfigured.env["APPEXT_PLATFORM"] = "staging"
    assert unconfigured("store", "status", "--all") == 0
    assert unconfigured("store", "status", "--all", "--platform", "nobody-made-this") == 1
    assert "does not exist" in unconfigured.err


def test_the_users_default_platform_file_is_the_last_resort(unconfigured):
    platform_file(unconfigured.home / "appext" / "platform.toml", store="https://store.default.example/api/v1")
    unconfigured.env["APPEXT_STORE_TOKEN"] = "t"
    unconfigured.on("GET", "https://store.default.example/api/v1/store/extensions", httpx.Response(200, json=[]))
    assert unconfigured("store", "status", "--all") == 0


def test_a_ci_token_needs_no_issuer(unconfigured):
    """The issuer is where a stored sign-in lives; a token from the environment has none to look up."""
    unconfigured.env["APPEXT_STORE_TOKEN"] = "ci-token"
    unconfigured.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[]))
    assert unconfigured("store", "status", "--all", "--store-url", API) == 0
    assert unconfigured.requests[-1].headers["authorization"] == "Bearer ci-token"


def test_flag_beats_environment_beats_file(unconfigured, project):
    platform_file(project / "appext.toml", store="https://file.example/api/v1")
    unconfigured.env["APPEXT_STORE_TOKEN"] = "t"
    unconfigured.on("GET", "https://file.example/api/v1/store/extensions", httpx.Response(200, json=[]))
    assert unconfigured("store", "status", "--all") == 0
    unconfigured.env["APPEXT_STORE_URL"] = "https://env.example/api/v1"
    unconfigured.on("GET", "https://env.example/api/v1/store/extensions", httpx.Response(200, json=[]))
    assert unconfigured("store", "status", "--all") == 0
    unconfigured.on("GET", "https://flag.example/api/v1/store/extensions", httpx.Response(200, json=[]))
    assert unconfigured("store", "status", "--all", "--store-url", "https://flag.example/api/v1") == 0
    assert [str(r.url) for r in unconfigured.requests][-3:] == [
        "https://file.example/api/v1/store/extensions",
        "https://env.example/api/v1/store/extensions",
        "https://flag.example/api/v1/store/extensions",
    ]


def test_a_broken_platform_file_is_reported_with_its_problems(unconfigured, project):
    (project / "appext.toml").write_text('[platform]\nissuer = "ftp://nope"\nsurprise = 1\n')
    assert unconfigured("store", "status", "--all") == 1
    assert "platform.issuer" in unconfigured.err and "platform.surprise" in unconfigured.err


def test_login_uses_the_cli_client_of_the_platform(unconfigured, project):
    platform_file(project / "appext.toml", client="acme-cli")
    unconfigured.on("GET", f"{ISSUER}/.well-known/openid-configuration", httpx.Response(200, json=DISCOVERY))
    unconfigured.on("POST", DEVICE_URL, httpx.Response(200, json=GRANT))
    unconfigured.on("POST", TOKEN_URL, granted())
    assert unconfigured("store", "login") == 0
    start = [r for r in unconfigured.requests if r.method == "POST"][0]
    assert parse_qs(start.content.decode())["client_id"] == ["acme-cli"]
    # the environment and the option are stronger than the file
    unconfigured.env["APPEXT_CLI_CLIENT_ID"] = "env-cli"
    unconfigured.on("POST", TOKEN_URL, granted())
    assert unconfigured("store", "login", "--client-id", "flag-cli") == 0
    assert parse_qs([r for r in unconfigured.requests if r.method == "POST"][-2].content.decode())["client_id"] == ["flag-cli"]


# --- register, key, submit --------------------------------------------------------------------


@pytest.fixture
def signed_in(world, project) -> World:
    world.sign_in()
    return world


def stored(**fields) -> dict:
    return {"id": "demo", "status": "DRAFT", "name": "Demo", "version": "1.0.0", "clientId": "ext-demo",
            "clientAuth": "private_key_jwt", "hasKey": False, "versions": [{"version": "1.0.0", "status": "DRAFT"}], **fields}


def test_register_uploads_the_manifest_text_and_the_public_key(signed_in):
    signed_in("keys", "generate")
    signed_in.on("POST", f"{STORE}/extensions", httpx.Response(201, json=stored()))
    signed_in.on("PUT", f"{STORE}/extensions/demo/key", httpx.Response(204))
    signed_in.on("GET", f"{STORE}/extensions/demo", httpx.Response(200, json=stored(hasKey=True)))
    assert signed_in("store", "register") == 0, signed_in.err
    post, put, _ = signed_in.requests[-3:]
    assert post.headers["content-type"] == "application/toml"
    assert post.content.decode() == MANIFEST
    jwk = json.loads(put.content)["jwk"]
    assert jwk["kty"] == "RSA" and "d" not in jwk
    assert "Registered demo 1.0.0" in signed_in.out and "Public key uploaded" in signed_in.out
    assert "key: yes" in signed_in.out  # the summary is fetched after the upload


def test_register_never_uploads_a_private_key(signed_in):
    private = {"kty": "RSA", "n": "AQAB", "e": "AQAB", "d": "x" * 40}
    (signed_in.cwd / "k.jwk.json").write_text(json.dumps(private))
    signed_in.on("POST", f"{STORE}/extensions", httpx.Response(201, json=stored()))
    assert signed_in("store", "register", "--key", "k.jwk.json") == 1
    assert "private key material" in signed_in.err
    assert [r.method for r in signed_in.requests] == ["POST"]  # the manifest went up, the key did not


def test_register_without_a_key_file_says_how_to_make_one(signed_in):
    signed_in.on("POST", f"{STORE}/extensions", httpx.Response(201, json=stored()))
    assert signed_in("store", "register") == 0
    assert "appext keys generate" in signed_in.err


def test_register_checks_the_rules_before_the_round_trip(signed_in):
    (signed_in.cwd / "extension.toml").write_text(MANIFEST.replace('id = "demo"', 'id = "X"'))
    assert signed_in("store", "register") == 1
    assert "extension.id" in signed_in.err
    assert signed_in.requests == []


def test_register_shows_the_stores_manifest_errors(signed_in):
    signed_in.on("POST", f"{STORE}/extensions", error_body(
        "invalid_manifest", "The manifest is invalid", 422,
        errors=[{"path": "services[0].audience", "message": "unknown audience 'x-api'"}]))
    assert signed_in("store", "register") == 1
    assert "422 invalid_manifest: The manifest is invalid" in signed_in.err
    assert "services[0].audience: unknown audience 'x-api'" in signed_in.err


def test_register_with_a_client_secret_extension_skips_the_key(signed_in):
    (signed_in.cwd / "extension.toml").write_text(MANIFEST + 'client_auth = "client_secret"\n')
    signed_in.on("POST", f"{STORE}/extensions", httpx.Response(201, json=stored(clientAuth="client_secret")))
    assert signed_in("store", "register") == 0
    assert [r.method for r in signed_in.requests] == ["POST"]
    assert "rotate-secret --out FILE" in signed_in.out


def test_a_taken_id_is_reported_with_the_stores_words(signed_in):
    signed_in.on("POST", f"{STORE}/extensions", error_body("already_exists", "Extension 'demo' belongs to someone else", 409))
    assert signed_in("store", "register") == 1
    assert "409 already_exists: Extension 'demo' belongs to someone else" in signed_in.err


def test_key_uploads_to_the_named_extension(signed_in):
    signed_in("keys", "generate")
    signed_in.on("PUT", f"{STORE}/extensions/other/key", httpx.Response(204))
    assert signed_in("store", "key", "other") == 0
    assert signed_in.requests[-1].url.path == "/api/v1/store/extensions/other/key"


def test_submit_takes_the_id_from_the_manifest(signed_in):
    signed_in.on("POST", f"{STORE}/extensions/demo/submit", httpx.Response(200, json=stored(status="SUBMITTED")))
    assert signed_in("store", "submit") == 0
    assert "Submitted demo" in signed_in.out and "[SUBMITTED]" in signed_in.out


def test_submit_with_an_explicit_id_needs_no_project(world):
    world.sign_in()
    world.on("POST", f"{STORE}/extensions/x-y/submit", httpx.Response(200, json=stored(id="x-y")))
    assert world("store", "submit", "x-y") == 0


def test_a_conflicting_state_is_explained(signed_in):
    signed_in.on("POST", f"{STORE}/extensions/demo/submit", error_body("invalid_state", "No draft version to submit", 409))
    assert signed_in("store", "submit") == 1
    assert "409 invalid_state: No draft version to submit" in signed_in.err


# --- a link: registered, submitted and verified, but there is no key, bundle or secret ----------------


def test_register_a_link_uploads_the_manifest_and_nothing_else(signed_in_link):
    signed_in_link.on("POST", f"{STORE}/extensions", httpx.Response(201, json=stored_link()))
    assert signed_in_link("store", "register") == 0, signed_in_link.err
    post, = signed_in_link.requests
    assert post.method == "POST" and post.content.decode() == LINK
    out = signed_in_link.out
    assert "Link registered: shop-link 1.0.0" in out and "no server, no key and no deployment" in out
    assert "appext store submit" in out and "reviewer approves" in out and "appext store verify" in out
    assert "  kind       link" in out and "  entry      https://shop.example.com/app" in out
    assert "client" not in out and "None" not in out and out.count("https://shop.example.com/app") == 1
    assert signed_in_link.err == ""  # not even a warning about the missing key


def test_register_a_link_ignores_a_key_that_happens_to_be_there(signed_in_link):
    signed_in_link("keys", "generate")
    signed_in_link.on("POST", f"{STORE}/extensions", httpx.Response(201, json=stored_link()))
    assert signed_in_link("store", "register") == 0
    assert [r.method for r in signed_in_link.requests] == ["POST"]  # the dev key is not looked at, let alone uploaded
    assert signed_in_link.err == ""


def test_register_a_link_says_so_when_a_key_is_asked_for_explicitly(signed_in_link):
    signed_in_link("keys", "generate")
    signed_in_link.on("POST", f"{STORE}/extensions", httpx.Response(201, json=stored_link()))
    assert signed_in_link("store", "register", "--key", ".appext/client_key.jwk.json") == 0
    assert [r.method for r in signed_in_link.requests] == ["POST"]
    assert "--key is ignored" in signed_in_link.err


def test_register_checks_the_rules_of_a_link_before_the_round_trip(signed_in_link):
    (signed_in_link.cwd / "extension.toml").write_text(LINK.replace("https://", "http://"))
    assert signed_in_link("store", "register") == 1
    assert "extension.entry" in signed_in_link.err and signed_in_link.requests == []


def test_submit_and_status_of_a_link_show_its_address(signed_in_link):
    signed_in_link.on("POST", f"{STORE}/extensions/shop-link/submit", httpx.Response(200, json=stored_link(status="SUBMITTED")))
    assert signed_in_link("store", "submit") == 0
    assert "[SUBMITTED]" in signed_in_link.out and "entry      https://shop.example.com/app" in signed_in_link.out
    signed_in_link.on("GET", f"{STORE}/extensions/shop-link", httpx.Response(200, json=stored_link(
        status="LIVE", liveVersion="1.0.0", versions=[{"version": "1.0.0", "status": "LIVE"}])))
    assert signed_in_link("store", "status") == 0
    out = signed_in_link.out
    assert "shop-link  Shop  [LIVE]" in out and "kind       link" in out and "live       1.0.0" in out and "key:" not in out


def test_status_copes_with_a_store_that_sends_no_client(signed_in):
    """`clientId` and `clientAuth` are null in a link's answer; nothing may print `None`."""
    signed_in.on("GET", f"{STORE}/extensions/demo", httpx.Response(200, json=stored(clientId=None, clientAuth=None)))
    assert signed_in("store", "status") == 0
    assert "  client     - (-, key: no)" in signed_in.out and "None" not in signed_in.out


def test_status_lists_links_and_extensions_together(world):
    world.sign_in()
    world.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[stored(), stored_link()]))
    assert world("store", "status") == 0
    assert "demo  Demo  [DRAFT]" in world.out and "client     ext-demo" in world.out
    assert "shop-link  Shop  [DRAFT]" in world.out and "kind       link" in world.out


def test_verify_of_a_link_is_the_stores_review_check_and_goes_live(signed_in_link):
    checks = [{"name": "link", "ok": True, "message": "A link has no deployment to check; the review is the check."}]
    signed_in_link.on("POST", f"{STORE}/extensions/shop-link/verify", httpx.Response(200, json={"status": "LIVE", "checks": checks}))
    assert signed_in_link("store", "verify") == 0
    assert "ok   link: A link has no deployment to check" in signed_in_link.out and "shop-link in local: LIVE" in signed_in_link.out


@pytest.mark.parametrize("command, lacks", [
    (["key"], "no key to upload"),
    (["bundle"], "no auth bundle to download"),
    (["rotate-secret", "--out", "secret.txt"], "no client secret to rotate"),
])
def test_a_link_has_no_key_bundle_or_secret_and_the_cli_says_so_before_asking_the_store(signed_in_link, command, lacks):
    signed_in_link("keys", "generate")
    assert signed_in_link("store", *command) == 1
    err = signed_in_link.err
    assert "shop-link is a link" in err and "no server" in err and lacks in err
    assert "appext store register" in err  # the hint: how a link is published
    assert signed_in_link.requests == [] and not (signed_in_link.cwd / "secret.txt").exists()


def test_a_link_can_be_pointed_at_through_its_manifest_path(world, link_project, tmp_path):
    world.sign_in()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    world.cwd = elsewhere
    assert world("store", "bundle", "--manifest", str(link_project / "extension.toml")) == 1
    assert "is a link" in world.err and world.requests == []


@pytest.mark.parametrize("command, path, method", [
    (["key"], "key", "PUT"),
    (["bundle"], "auth-bundle", "GET"),
    (["rotate-secret", "--out", "secret.txt"], "rotate-secret", "POST"),
])
def test_with_only_an_id_the_stores_own_answer_for_a_link_comes_through(world, command, path, method):
    """No manifest to read here, so the CLI does not know it is a link: the store's 409 says it."""
    world.sign_in()
    (world.cwd / ".appext").mkdir()
    (world.cwd / ".appext" / "client_key.jwk.json").write_text(json.dumps({"kty": "RSA", "n": "AQAB", "e": "AQAB"}))
    world.on(method, f"{STORE}/extensions/shop-link/{path}", error_body("invalid_state", "shop-link is a link: it has no client.", 409))
    assert world("store", command[0], "shop-link", *command[1:]) == 1
    assert "409 invalid_state: shop-link is a link" in world.err


# --- error mapping ----------------------------------------------------------------------------


def test_missing_role_is_a_hint_not_a_stack_trace(signed_in):
    signed_in.on("POST", f"{STORE}/extensions/demo/submit", error_body("forbidden", "Requires role store-developer", 403))
    assert signed_in("store", "submit") == 1
    assert "403 forbidden: Requires role store-developer" in signed_in.err and "role" in signed_in.err.split("\n")[-2]


def test_an_unauthorized_token_points_to_login(signed_in):
    signed_in.on("GET", f"{STORE}/extensions/demo", httpx.Response(401, json={"detail": "Not authenticated"}))
    assert signed_in("store", "status") == 1
    assert "401" in signed_in.err and "appext store login" in signed_in.err


def test_request_validation_errors_of_the_framework_are_readable(signed_in):
    signed_in.on("POST", f"{STORE}/extensions/demo/reject", httpx.Response(422, json={"detail": [
        {"loc": ["body", "reason"], "msg": "Field required", "type": "missing"}]}))
    assert signed_in("store", "reject", "demo", "--reason", "x") == 1
    assert "body.reason: Field required" in signed_in.err


def test_an_unreachable_store(signed_in):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    signed_in.on("GET", f"{STORE}/extensions/demo", refuse)
    assert signed_in("store", "status") == 1
    assert "cannot reach the store" in signed_in.err


# --- status, services --------------------------------------------------------------------------


def test_status_of_the_project_extension(signed_in):
    signed_in.on("GET", f"{STORE}/extensions/demo", httpx.Response(200, json=stored(
        status="LIVE", hasKey=True, liveVersion="1.0.0",
        versions=[{"version": "1.1.0", "status": "SUBMITTED", "restricted": True}, {"version": "1.0.0", "status": "LIVE"}],
        urls={"local": {"entry": "http://127.0.0.1:8100/"}})))
    assert signed_in("store", "status") == 0
    out = signed_in.out
    assert "demo  Demo  [LIVE]" in out and "key: yes" in out
    assert "1.1.0 SUBMITTED (restricted scopes) · 1.0.0 LIVE" in out and "http://127.0.0.1:8100/" in out


def test_status_outside_a_project_lists_your_extensions(world):
    world.sign_in()
    world.on("GET", f"{STORE}/extensions", httpx.Response(200, json=[stored(), stored(id="other", name="Other")]))
    assert world("store", "status") == 0
    assert "demo  Demo" in world.out and "other  Other" in world.out
    assert world.requests[-1].url.params["mine"] == "true"
    assert world("store", "status", "--all") == 0
    assert "mine" not in world.requests[-1].url.params


def test_a_suspended_extension_says_so(signed_in):
    signed_in.on("GET", f"{STORE}/extensions/demo", httpx.Response(200, json=stored(status="LIVE", suspended=True)))
    signed_in("store", "status")
    assert "[SUSPENDED]" in signed_in.out


def test_services_lists_audiences_and_scopes(world):
    world.sign_in()
    world.on("GET", f"{STORE}/services", httpx.Response(200, json=[{"audience": "data-api", "title": "Data", "scopes": [
        {"name": "ext-data-read", "consentText": "Read your data", "restricted": False},
        {"name": "ext-secret", "consentText": "More", "restricted": True}]}]))
    assert world("store", "services") == 0
    assert "data-api  Data" in world.out and "Read your data" in world.out and "[restricted" in world.out


# --- bundle ----------------------------------------------------------------------------------


def make_zip(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


BUNDLE = {"appext.env": "APPEXT_ENV=local\n", "extension.lock.toml": "[lock]\n", "README.md": "# bundle\n"}


def test_bundle_downloads_and_unpacks(signed_in):
    signed_in.on("GET", f"{STORE}/extensions/demo/auth-bundle", httpx.Response(200, content=make_zip(BUNDLE)))
    assert signed_in("store", "bundle", "--env", "prod", "--out", "out") == 0
    assert (signed_in.cwd / "out" / "appext.env").read_text() == "APPEXT_ENV=local\n"
    assert (signed_in.cwd / "out" / "extension.lock.toml").exists()
    assert signed_in.requests[-1].url.params["env"] == "prod"
    assert "appext.env" in signed_in.out


@pytest.mark.parametrize("flags, env", [([], "local"), (["--env", "prod"], "prod")])
def test_bundle_environment_defaults_to_local_for_a_local_store(signed_in, flags, env):
    signed_in.on("GET", f"{STORE}/extensions/demo/auth-bundle", httpx.Response(200, content=make_zip(BUNDLE)))
    signed_in("store", "bundle", *flags)
    assert signed_in.requests[-1].url.params["env"] == env
    assert (signed_in.cwd / "auth-bundle" / "appext.env").exists()


def test_bundle_environment_defaults_to_prod_for_a_remote_store(signed_in):
    signed_in.env["APPEXT_STORE_URL"] = "https://store.example.com/api/v1"
    signed_in.on("GET", "https://store.example.com/api/v1/store/extensions/demo/auth-bundle", httpx.Response(200, content=make_zip(BUNDLE)))
    assert signed_in("store", "bundle") == 0
    assert signed_in.requests[-1].url.params["env"] == "prod"


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/evil", "a/../../evil"])
def test_bundle_refuses_paths_that_leave_the_directory(signed_in, name):
    signed_in.on("GET", f"{STORE}/extensions/demo/auth-bundle", httpx.Response(200, content=make_zip({name: "x"})))
    assert signed_in("store", "bundle", "--out", "out") == 1
    assert "unsafe path" in signed_in.err
    assert not (signed_in.cwd.parent / "evil.txt").exists()


def test_bundle_that_is_not_a_zip(signed_in):
    signed_in.on("GET", f"{STORE}/extensions/demo/auth-bundle", httpx.Response(200, content=b"<html>"))
    assert signed_in("store", "bundle") == 1
    assert "ZIP" in signed_in.err


def test_bundle_before_approval(signed_in):
    signed_in.on("GET", f"{STORE}/extensions/demo/auth-bundle", error_body("invalid_state", "The bundle is available from APPROVED", 409))
    assert signed_in("store", "bundle") == 1
    assert "409 invalid_state" in signed_in.err


# --- verify ------------------------------------------------------------------------------------


def test_verify_exit_code_follows_the_status(signed_in):
    checks = [{"name": "info", "ok": True, "message": "id and version match"}, {"name": "ready", "ok": True, "message": "readyz 200"}]
    signed_in.on("POST", f"{STORE}/extensions/demo/verify", httpx.Response(200, json={"status": "LIVE", "checks": checks}))
    assert signed_in("store", "verify", "--env", "prod") == 0
    assert "ok   info: id and version match" in signed_in.out and "demo in prod: LIVE" in signed_in.out
    assert signed_in.requests[-1].url.params["env"] == "prod"


def test_a_failing_check_fails_the_command(signed_in):
    signed_in.on("POST", f"{STORE}/extensions/demo/verify", httpx.Response(200, json={"status": "APPROVED", "checks": [
        {"name": "ready", "ok": False, "message": "readyz answered 503"}]}))
    assert signed_in("store", "verify") == 1
    assert "FAIL ready: readyz answered 503" in signed_in.out


# --- reviewer and admin, secrets ---------------------------------------------------------------


def test_approve_shows_what_is_still_missing(world):
    world.sign_in(roles=("store-reviewer",))
    world.on("POST", f"{STORE}/extensions/demo/approve", httpx.Response(200, json={"status": "SUBMITTED", "pendingApprovals": 1}))
    assert world("store", "approve", "demo", "--note", "scopes look fine") == 0
    assert json.loads(world.requests[-1].content) == {"note": "scopes look fine"}
    assert "second store-admin" in world.out


def test_approve_without_a_note_sends_no_body(world):
    world.sign_in()
    world.on("POST", f"{STORE}/extensions/demo/approve", httpx.Response(200, json={"status": "APPROVED"}))
    assert world("store", "approve", "demo") == 0
    assert world.requests[-1].content == b""


@pytest.mark.parametrize("command, body", [
    (["reject", "--reason", "too broad"], {"reason": "too broad"}),
    (["suspend", "--reason", "incident 12"], {"reason": "incident 12"}),
    (["unsuspend", "--reason", "fixed"], {"reason": "fixed"}),
])
def test_reviewer_and_admin_commands_send_their_reason(world, command, body):
    world.sign_in()
    action = command[0]
    world.on("POST", f"{STORE}/extensions/demo/{action}", httpx.Response(200, json={}))
    assert world("store", command[0], "demo", *command[1:]) == 0
    assert json.loads(world.requests[-1].content) == body


@pytest.mark.parametrize("command", ["reject", "suspend", "unsuspend"])
def test_these_commands_require_a_reason(world, command):
    """The API refuses an empty reason (422); better to say so before the request."""
    with pytest.raises(SystemExit):
        world("store", command, "demo")


def test_rotate_secret_writes_it_to_a_file_and_never_prints_it(signed_in):
    signed_in.on("POST", f"{STORE}/extensions/demo/rotate-secret", httpx.Response(200, json={"clientSecret": "sekret-value-42"}))
    assert signed_in("store", "rotate-secret", "--out", "secret.txt") == 0
    path = signed_in.cwd / "secret.txt"
    assert path.read_text().strip() == "sekret-value-42" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "sekret" not in signed_in.out + signed_in.err
