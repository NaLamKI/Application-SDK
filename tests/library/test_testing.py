"""appext.testing: the helpers extension developers use must behave as documented."""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import Depends

from appext import Extension, ServiceClient, User
from appext.testing import ExtensionTestClient, FakeClock, FakeIdP, InvalidAuthorizeRequest, generate_client_key, service_mocks, test_user

from .conftest import MANIFEST, write_project

CALLBACK = "https://demo.apps.test/auth/callback"


def build(tmp_path):
    manifest_path = write_project(tmp_path)
    ext = Extension.from_manifest(
        manifest_path,
        environ={"APPEXT_SERVICE_PROJECTS_URL": "https://projects.test/api", "APPEXT_SERVICE_EXPORT_URL": "https://export.test"},
    )
    api = ext.router(prefix="/api")

    @api.get("/me")
    async def me(user: User = Depends(ext.current_user)):
        return {"sub": user.sub, "name": user.name, "roles": sorted(user.roles)}

    @api.get("/projects")
    async def projects(user: User = Depends(ext.current_user), client: ServiceClient = Depends(ext.service("projects"))):
        response = await client.get("/v1/projects", params={"owner": user.sub})
        response.raise_for_status()
        return response.json()

    @api.post("/export")
    async def export(user: User = Depends(ext.require_role("analyst")), client: ServiceClient = Depends(ext.service("export"))):
        return (await client.post("/v1/jobs", json={"requested_by": user.sub})).json()

    return ext, ext.asgi()


def test_the_concepts_example_test_works(tmp_path):
    ext, app = build(tmp_path)
    client = ExtensionTestClient(app, user=test_user(sub="u-1", roles=["analyst"]))
    with service_mocks(ext) as mocks:
        route = mocks.get("projects", "/v1/projects").respond(json=[{"id": 1}])
        response = client.get("/api/projects")
    assert response.status_code == 200 and response.json() == [{"id": 1}]
    assert route.calls.last.request.url.params["owner"] == "u-1"
    assert route.calls.last.request.headers["authorization"] == "Bearer test-token-projects-api"
    assert client.token_requests == [("projects", "projects-api", ("svc-projects-read",), "user")]
    client.close()


def test_the_user_is_what_the_test_says(tmp_path):
    _, app = build(tmp_path)
    with ExtensionTestClient(app, user=test_user(sub="u-7", name="Ada", roles=["analyst", "x"])) as client:
        assert client.get("/api/me").json() == {"sub": "u-7", "name": "Ada", "roles": ["analyst", "x"]}
    with ExtensionTestClient(app, user=test_user(sub="u-8", name=None)) as client:
        assert client.get("/api/me").json()["name"] is None  # as far as the scopes allow


def test_role_checks_apply(tmp_path):
    ext, app = build(tmp_path)
    with service_mocks(ext) as mocks:
        mocks.post("export", "/v1/jobs").respond(json={"job": 1})
        with ExtensionTestClient(app, user=test_user(roles=["analyst"])) as allowed:
            assert allowed.post("/api/export").status_code == 200
        with ExtensionTestClient(app, user=test_user(roles=[])) as denied:
            assert denied.post("/api/export").status_code == 403


def test_anonymous_client_gets_the_401_contract(tmp_path):
    _, app = build(tmp_path)
    with ExtensionTestClient(app, user=None) as client:
        response = client.get("/api/me")
    assert response.status_code == 401 and response.json()["error"] == "unauthenticated"


def test_csrf_is_handled_but_can_be_tested(tmp_path):
    ext, app = build(tmp_path)
    with service_mocks(ext) as mocks:
        mocks.post("export", "/v1/jobs").respond(json={})
        with ExtensionTestClient(app, user=test_user(roles=["analyst"])) as client:
            assert client.post("/api/export").status_code == 200  # header and Origin added for you
            assert client.post("/api/export", headers={"Origin": "https://evil.test"}).status_code == 403
            assert client.post("/api/export", headers={"X-Appext-CSRF": ""}).status_code != 403  # header present (empty) is still "set by a script"


def test_unmocked_service_calls_fail_instead_of_reaching_the_network(tmp_path):
    ext, app = build(tmp_path)
    import respx

    with service_mocks(ext):
        with ExtensionTestClient(app, user=test_user(), raise_server_exceptions=True) as client:
            with pytest.raises(respx.models.AllMockedAssertionError):
                client.get("/api/projects")


def test_closing_restores_the_real_token_source(tmp_path):
    ext, app = build(tmp_path)
    original = ext.token_source
    client = ExtensionTestClient(app, user=test_user())
    assert ext.token_source != original
    client.close()
    assert ext.token_source == original


async def test_works_inside_an_async_test_too(tmp_path):
    _, app = build(tmp_path)
    client = ExtensionTestClient(app, user=test_user(sub="u-async"))
    assert client.get("/api/me").json()["sub"] == "u-async"
    client.close()


def test_works_with_the_secure_host_prefixed_cookie_too(tmp_path):
    manifest_path = write_project(tmp_path)
    ext = Extension.from_manifest(manifest_path, environ={"APPEXT_PUBLIC_URL": "https://demo.apps.example.com"})
    api = ext.router(prefix="/api")

    @api.get("/me")
    async def me(user: User = Depends(ext.current_user)):
        return {"sub": user.sub}

    with ExtensionTestClient(ext.asgi(), user=test_user(sub="u-https")) as client:
        assert ext.settings.session_cookie_name == "__Host-ext_session"
        assert client.get("/api/me").json() == {"sub": "u-https"}


def test_the_client_needs_the_app_from_asgi():
    from fastapi import FastAPI

    with pytest.raises(TypeError, match="Extension.asgi"):
        ExtensionTestClient(FastAPI())


def test_service_mocks_need_a_configured_url(tmp_path):
    manifest_path = write_project(tmp_path)
    ext = Extension.from_manifest(manifest_path, environ={})
    with service_mocks(ext) as mocks:
        with pytest.raises(KeyError, match="APPEXT_SERVICE_EXPORT_URL"):
            mocks.get("export", "/x")


def test_helper_names_are_not_collected_as_tests():
    assert test_user.__test__ is False
    from appext.testing import TestUser

    assert TestUser.__test__ is False


# -- the fake identity provider behaves like Keycloak where it matters ----------------------------------------------


def pkce():
    verifier = "v" * 50
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def idp():
    idp = FakeIdP()
    pem, jwk = generate_client_key("ES256")
    idp.add_client("c1", redirect_uris={CALLBACK}, public_jwk=jwk, default_scopes={"read"}, optional_scopes={"write"}, scope_audiences={"read": "api"})
    idp.add_user("u", name="U")
    idp.pem = pem
    return idp


def authorize_url(idp, *, scope="openid read", redirect_uri=CALLBACK, **extra):
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": "c1", "redirect_uri": redirect_uri, "scope": scope, "state": "st", "nonce": "n",
              "code_challenge": challenge, "code_challenge_method": "S256", **extra}
    return f"{idp.authorization_endpoint}?{urlencode(params)}"


def test_unregistered_redirect_uris_are_refused_strictly(idp):
    for bad in ("https://evil.test/cb", CALLBACK + "/", CALLBACK + "?x=1", "https://demo.apps.test/auth/callbackX"):
        with pytest.raises(InvalidAuthorizeRequest):
            idp.authorize(authorize_url(idp, redirect_uri=bad), "u")
    with pytest.raises(InvalidAuthorizeRequest):
        idp.authorize(authorize_url(idp).replace("client_id=c1", "client_id=nope"), "u")


def test_unknown_scopes_and_missing_pkce_are_errors_in_the_redirect(idp):
    assert "error=invalid_scope" in idp.authorize(authorize_url(idp, scope="openid admin"), "u")
    url = authorize_url(idp).replace("code_challenge_method=S256", "code_challenge_method=plain")
    assert "error=invalid_request" in idp.authorize(url, "u")


def test_consent_is_asked_once_and_remembered(idp):
    idp.authorize(authorize_url(idp), "u")
    idp.authorize(authorize_url(idp), "u")
    assert [a.consent_shown for a in idp.authorizations] == [True, False]
    idp.authorize(authorize_url(idp, scope="openid read write"), "u")  # a new scope asks again
    assert idp.authorizations[-1].consent_shown


def test_refusing_consent_gives_access_denied(idp):
    assert "error=access_denied" in idp.authorize(authorize_url(idp), "u", consent=False)
    assert idp.consents[("u", "c1")] == set()


def test_prompt_login_starts_a_new_sso_session(idp):
    idp.authorize(authorize_url(idp), "u")
    first = dict(idp.sessions)
    idp.authorize(authorize_url(idp, prompt="login"), "u")
    assert len(idp.sessions) == len(first) + 1


async def test_codes_are_single_use_and_bound_to_pkce_and_redirect_uri(idp):
    from appext.config import ExtensionSettings
    from appext.core import ClientAuthenticator
    from appext.manifest import loads_manifest
    from appext.config import Secret

    redirect = idp.authorize(authorize_url(idp), "u")
    code = httpx.URL(redirect).params["code"]
    verifier, _ = pkce()
    settings = ExtensionSettings.from_env(loads_manifest(MANIFEST), {})
    settings = type(settings)(**{**settings.__dict__, "client_id": "c1", "client_key": Secret(idp.pem)})
    auth = ClientAuthenticator(settings, FakeClock())

    async def redeem(code, **override):
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier, **override}
        return await idp.client().post(idp.token_endpoint, data={**form, **auth.fields(idp.token_endpoint)})

    wrong_pkce = await redeem(code, code_verifier="x" * 50)
    assert wrong_pkce.status_code == 400  # the code is burnt by a failed attempt
    assert (await redeem(code)).status_code == 400
    fresh = httpx.URL(idp.authorize(authorize_url(idp), "u")).params["code"]
    assert (await redeem(fresh, redirect_uri="https://evil.test/cb")).status_code == 400
    fresh = httpx.URL(idp.authorize(authorize_url(idp), "u")).params["code"]
    assert (await redeem(fresh)).status_code == 200
    assert (await redeem(fresh)).status_code == 400  # replay


async def test_client_authentication_failures_are_401(idp):
    client = idp.client()
    form = {"grant_type": "client_credentials", "client_id": "c1"}
    assert (await client.post(idp.token_endpoint, data=form)).status_code == 401
    assert (await client.post(idp.token_endpoint, data={**form, "client_secret": "nope"})).status_code == 401
    assert (await client.post(idp.token_endpoint, data={**form, "client_assertion_type": "x", "client_assertion": "y"})).status_code == 401
