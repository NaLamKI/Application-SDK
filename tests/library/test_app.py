"""The assembled application: cookies, 401 behaviour, CSRF, headers, health, static files."""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import Request

from appext import Extension, LockError
from appext.manifest import ManifestError, loads_manifest

from .conftest import Browser, Env, build_env, query_of


async def signed_in_browser(env: Env, user: str = "u-1", **kw) -> Browser:
    browser = Browser(env)
    response = await browser.sign_in(user, **kw)
    assert response.status_code == 302, response.text
    return browser


# -- session cookie ------------------------------------------------------------------------------------


async def test_session_cookie_attributes_over_https(env: Env, browser: Browser):
    response = await browser.sign_in()
    cookies = [c for c in response.headers.get_list("set-cookie") if c.startswith("__Host-ext_session=")]
    assert len(cookies) == 1
    cookie = cookies[0]
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age="):
        assert attribute in cookie, attribute
    assert "Domain" not in cookie  # required by the __Host- prefix
    value = cookie.split("=", 1)[1].split(";", 1)[0]
    assert len(value) >= 43  # 256 bit, URL-safe base64
    # The transaction cookie is gone afterwards.
    assert any(c.startswith("__Host-ext_tx=") and "Max-Age=0" in c for c in response.headers.get_list("set-cookie"))


async def test_session_cookie_on_plain_http_loopback(tmp_path):
    env = build_env(tmp_path, extra_environ={"APPEXT_PUBLIC_URL": "http://127.0.0.1:8000"})
    idp_redirects = {"http://127.0.0.1:8000/auth/callback"}
    env.idp.clients["ext-demo"].redirect_uris |= idp_redirects
    browser = Browser(env)
    browser.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://127.0.0.1:8000", follow_redirects=False)
    try:
        started = await browser.http.get("/auth/login")
        assert started.headers["set-cookie"].startswith("ext_tx_8000=") and "Secure" not in started.headers["set-cookie"]
        callback = env.idp.authorize(started.headers["location"], "u-1")
        response = await browser.http.get(callback)
        cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("ext_session_8000="))
        assert "HttpOnly" in cookie and "Secure" not in cookie and "SameSite=lax" in cookie
    finally:
        await browser.http.aclose()


async def test_a_new_login_replaces_the_old_session(env: Env):
    browser = await signed_in_browser(env)
    try:
        old = browser.http.cookies[env.ext.settings.session_cookie_name]
        response = await browser.sign_in("u-1")
        assert response.status_code == 302
        new = browser.http.cookies[env.ext.settings.session_cookie_name]
        assert new != old and await env.ext.store.load_session(old) is None
    finally:
        await browser.aclose()


# -- 401 for APIs, redirects for pages ----------------------------------------------------------------------


async def test_api_without_session_is_401_with_a_login_url(env: Env, browser: Browser):
    response = await browser.http.get("/api/me")
    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "unauthenticated" and body["login_url"] == "/auth/login?return_to=%2F"
    assert response.headers["cache-control"] == "no-store"


async def test_login_url_returns_to_the_page_that_made_the_call(env: Env, browser: Browser):
    response = await browser.http.get("/api/me", headers={"Referer": "https://demo.apps.test/reports?tab=2"})
    assert query_of(response.json()["login_url"])["return_to"] == "/reports?tab=2"
    foreign = await browser.http.get("/api/me", headers={"Referer": "https://evil.test/phish"})
    assert query_of(foreign.json()["login_url"])["return_to"] == "/"


async def test_api_with_session(env: Env):
    browser = await signed_in_browser(env)
    try:
        response = await browser.http.get("/api/me")
        assert response.status_code == 200
        assert response.json()["sub"] == "u-1" and response.json()["roles"] == ["analyst"]
    finally:
        await browser.aclose()


async def test_a_dead_session_gives_401_and_clears_the_cookie(env: Env):
    browser = await signed_in_browser(env)
    try:
        session = await env.ext.store.load_session(browser.http.cookies[env.ext.settings.session_cookie_name])
        env.idp.sessions.pop(session.sid)
        env.clock.advance(env.idp.access_lifetime)
        response = await browser.http.get("/api/me")
        assert response.status_code == 401 and response.json()["error"] == "unauthenticated"
        assert "Max-Age=0" in response.headers["set-cookie"]
    finally:
        await browser.aclose()


async def test_a_forged_session_cookie_is_not_a_session(env: Env, browser: Browser):
    browser.set_cookie(env.ext.settings.session_cookie_name, "A" * 43)
    assert (await browser.http.get("/api/me")).status_code == 401


async def test_pages_redirect_to_the_login(env: Env, browser: Browser):
    response = await browser.http.get("/pages/hello?x=1")
    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login?return_to=%2Fpages%2Fhello%3Fx%3D1"


async def test_pages_with_session(env: Env):
    browser = await signed_in_browser(env)
    try:
        assert (await browser.http.get("/pages/hello")).json() == {"hello": "u-1"}
    finally:
        await browser.aclose()


async def test_role_guard(env: Env):
    browser = await signed_in_browser(env)  # u-1 has "analyst", not "admin"
    try:
        denied = await browser.http.get("/api/admin")
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "forbidden"
    finally:
        await browser.aclose()


# -- services through the app ------------------------------------------------------------------------------------


@pytest.mark.parametrize("mocked", [True])
async def test_service_call_uses_an_exchanged_token(env: Env, mocked):
    browser = await signed_in_browser(env)
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.get("https://projects.test/api/v1/projects", params={"limit": "5"}).respond(json=[{"id": 1}])
            response = await browser.http.get("/api/projects")
        assert response.status_code == 200 and response.json() == [{"id": 1}]
        auth = route.calls.last.request.headers["authorization"]
        assert auth.startswith("Bearer ")
        session = await env.ext.store.load_session(browser.http.cookies[env.ext.settings.session_cookie_name])
        assert auth != f"Bearer {session.access_token}"  # never the login token itself
        import jwt

        claims = jwt.decode(auth[7:], options={"verify_signature": False})
        assert claims["aud"] == ["projects-api"] and claims["azp"] == "ext-demo"
    finally:
        await browser.aclose()


async def test_service_mode_uses_client_credentials(env: Env):
    browser = await signed_in_browser(env)
    try:
        with respx.mock() as router:
            route = router.post("https://export.test/v1/jobs").respond(json={"job": 7})
            response = await browser.http.post("/api/export", headers={"Origin": env.public_url, "X-Appext-CSRF": "1"})
        assert response.status_code == 200 and response.json() == {"job": 7}
        import jwt

        claims = jwt.decode(route.calls.last.request.headers["authorization"][7:], options={"verify_signature": False})
        assert claims["sub"] == "service-account-ext-demo"
        assert route.calls.last.request.content == b'{"requested_by":"u-1"}'
    finally:
        await browser.aclose()


async def test_missing_consent_becomes_a_401_that_asks_for_the_scopes(env: Env):
    browser = await signed_in_browser(env)
    try:
        env.idp.consents[("u-1", "ext-demo")].discard("svc-projects-read")
        response = await browser.http.get("/api/projects", headers={"Referer": "https://demo.apps.test/projects"})
        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "consent_required" and body["missing_scopes"] == ["svc-projects-read"]
        q = query_of(body["login_url"])
        assert q["scope"] == "svc-projects-read" and q["return_to"] == "/projects"
        # And the login endpoint honours it (only for scopes of the manifest).
        started = await browser.http.get(body["login_url"])
        assert "svc-projects-read" in query_of(started.headers["location"])["scope"].split()
    finally:
        await browser.aclose()


async def test_exchange_failure_is_a_502_without_details(env: Env):
    browser = await signed_in_browser(env)
    try:
        env.idp.clients["ext-demo"].token_exchange_enabled = False
        response = await browser.http.get("/api/projects")
        assert response.status_code == 502
        assert response.json()["error"] == "upstream_auth_failed"
        assert "unauthorized_client" not in response.text  # the browser cannot act on it
    finally:
        await browser.aclose()


# -- CSRF --------------------------------------------------------------------------------------------------------------


async def test_csrf_rules_for_writing_requests(env: Env):
    browser = await signed_in_browser(env)
    post = browser.http.post
    good = {"Origin": env.public_url, "X-Appext-CSRF": "1"}
    try:
        assert (await post("/api/echo")).status_code == 403  # no header, no origin
        assert (await post("/api/echo", headers={"Origin": env.public_url})).status_code == 403  # origin alone is not enough
        assert (await post("/api/echo", headers={"X-Appext-CSRF": "1", "Origin": "https://evil.test"})).status_code == 403
        assert (await post("/api/echo", headers={"X-Appext-CSRF": "1", "Origin": "null"})).status_code == 403
        sibling = {"X-Appext-CSRF": "1", "Origin": "https://other-ext.apps.test"}
        assert (await post("/api/echo", headers=sibling)).status_code == 403  # a sibling subdomain is same-site, still refused
        assert (await post("/api/echo", headers={"X-Appext-CSRF": "1", "Sec-Fetch-Site": "cross-site"})).status_code == 403
        denied = await post("/api/echo")
        assert denied.json()["detail"]["code"] == "csrf_rejected"
        assert (await post("/api/echo", headers=good)).status_code == 200
        assert (await post("/api/echo", headers={"X-Appext-CSRF": "1"})).status_code == 200  # non-browser client without Origin
        assert (await post("/api/echo", headers={"X-Appext-CSRF": "1", "Sec-Fetch-Site": "same-origin"})).status_code == 200
        for method in ("put", "patch", "delete"):
            response = await getattr(browser.http, method)("/api/echo")
            assert response.status_code == 403  # rejected by CSRF before routing (it would be 405 otherwise)
    finally:
        await browser.aclose()


async def test_reading_requests_need_no_csrf_header(env: Env):
    browser = await signed_in_browser(env)
    try:
        assert (await browser.http.get("/api/me")).status_code == 200
    finally:
        await browser.aclose()


async def test_forms_on_pages_need_a_matching_origin(env: Env):
    browser = await signed_in_browser(env)
    try:
        assert (await browser.http.post("/pages/form")).status_code == 403
        assert (await browser.http.post("/pages/form", headers={"Origin": "https://evil.test"})).status_code == 403
        assert (await browser.http.post("/pages/form", headers={"Origin": env.public_url})).status_code == 200
        assert (await browser.http.post("/pages/form", headers={"Sec-Fetch-Site": "same-origin"})).status_code == 200
    finally:
        await browser.aclose()


# -- headers ---------------------------------------------------------------------------------------------------------------


async def test_security_headers(env: Env, browser: Browser):
    for path in ("/_sdk/info", "/healthz", "/api/me", "/auth/login", "/nope"):
        response = await browser.http.get(path)
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp, path
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["referrer-policy"] == "same-origin", path
        assert response.headers["x-frame-options"] == "DENY", path
        assert response.headers["strict-transport-security"].startswith("max-age="), path
    csp = (await browser.http.get("/_sdk/info")).headers["content-security-policy"]
    assert "img-src 'self' data: https://cdn.example.test" in csp and "font-src 'self' https://cdn.example.test" in csp
    assert "unsafe-inline" not in csp and "script-src" not in csp


async def test_no_hsts_on_plain_http(tmp_path):
    env = build_env(tmp_path, extra_environ={"APPEXT_PUBLIC_URL": "http://127.0.0.1:8000"})
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://127.0.0.1:8000")
    try:
        response = await client.get("/healthz")
        assert "strict-transport-security" not in response.headers
    finally:
        await client.aclose()


async def test_the_error_page_has_its_own_policy_that_still_blocks_scripts(env: Env, browser: Browser):
    response = await browser.sign_in("u-1", consent=False)
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "script-src" not in csp


async def test_a_developers_lifespan_runs_inside_the_sdks(tmp_path):
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    events = []

    @asynccontextmanager
    async def mine(app):
        events.append("start")
        yield
        events.append("stop")

    env = build_env(tmp_path)
    app = env.ext.asgi(lifespan=mine, docs_url="/docs")
    with TestClient(app):
        assert events == ["start"]
    assert events == ["start", "stop"]
    assert app.docs_url == "/docs"


async def test_the_extension_survives_a_restart_of_the_app(tmp_path):
    """Test clients run the lifespan more than once; closing the HTTP clients must not break the next run."""
    from fastapi.testclient import TestClient

    env = build_env(tmp_path)
    for _ in range(2):
        with TestClient(env.app) as client:
            assert client.get("/healthz").status_code == 200
    assert (await env.ext.core.discovery(fresh=True))["issuer"] == env.idp.issuer


async def test_custom_csp(tmp_path):
    env = build_env(tmp_path)
    app = env.ext.asgi(csp="default-src 'self'; img-src *")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env.public_url)
    try:
        assert (await client.get("/healthz")).headers["content-security-policy"] == "default-src 'self'; img-src *"
    finally:
        await client.aclose()


async def test_responses_that_depend_on_the_person_are_never_cached(env: Env):
    browser = await signed_in_browser(env)
    try:
        for path in ("/api/me", "/pages/hello", "/auth/login", "/readyz"):
            assert browser.http is not None
            response = await browser.http.get(path)
            assert response.headers["cache-control"] == "no-store", path
    finally:
        await browser.aclose()


# -- /_sdk, health --------------------------------------------------------------------------------------------------------------


async def test_sdk_info_is_public_and_matches_the_contract(env: Env, browser: Browser):
    response = await browser.http.get("/_sdk/info")
    import appext

    assert response.status_code == 200
    assert response.json() == {
        "sdk": "appext", "sdkVersion": appext.__version__, "id": "demo", "version": "1.2.0",
        "clientId": "ext-demo", "environment": "local",
    }


async def test_icon_comes_from_the_manifest(env: Env, browser: Browser):
    response = await browser.http.get("/_sdk/icon")
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.startswith("<svg")
    assert "sandbox" in response.headers["content-security-policy"]  # an SVG never runs script as an image
    assert response.headers["access-control-allow-origin"] == "*"  # the app on the web draws it from another origin


async def test_icon_cannot_point_outside_the_project(tmp_path):
    env = build_env(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    env.ext.manifest = type(env.ext.manifest)(**{**env.ext.manifest.__dict__, "icon": "../secret.txt"})
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=env.public_url)
    try:
        assert (await client.get("/_sdk/icon")).status_code == 404
    finally:
        await client.aclose()


async def test_text_from_the_callback_url_never_reaches_the_page_unescaped(env: Env, browser: Browser):
    started = await browser.start()
    state = query_of(started.headers["location"])["state"]
    evil = "<script>alert(1)</script>"
    response = await browser.http.get("/auth/callback", params={"state": state, "error": evil, "error_description": evil})
    assert response.status_code == 400
    assert "<script>" not in response.text and "&lt;script&gt;" in response.text


async def test_a_code_the_idp_refuses_is_a_failed_sign_in_page_not_a_crash(env: Env, browser: Browser):
    started = await browser.start()
    callback = env.idp.authorize(started.headers["location"], "u-1")
    env.idp.codes.clear()  # the code is gone (used or expired at the IdP)
    response = await browser.http.get(callback)
    assert response.status_code == 400 and "invalid_grant" in response.text and "/auth/login" in response.text


async def test_a_misconfigured_client_key_is_a_500_that_reveals_nothing(tmp_path, caplog):
    env = build_env(tmp_path)
    (tmp_path / "client_key.pem").write_text("garbage")
    env.ext.settings.client_key.__init__("garbage")  # the key file content as the process read it at start
    env.ext.core.client_auth._key = None
    browser = Browser(env)
    try:
        started = await browser.start()
        callback = env.idp.authorize(started.headers["location"], "u-1")
        response = await browser.http.get(callback)
        assert response.status_code == 500
        assert response.json() == {"error": "misconfigured", "message": "The extension is not configured correctly."}
        assert "APPEXT_CLIENT_KEY_FILE" in caplog.text  # the operator sees what to fix
    finally:
        await browser.aclose()


async def test_frontend_helpers(env: Env, browser: Browser):
    for name, needle in (("client.js", "extFetch"), ("bridge.js", "AppExtBridge")):
        response = await browser.http.get(f"/_sdk/{name}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert needle in response.text


async def test_healthz(env: Env, browser: Browser):
    assert (await browser.http.get("/healthz")).json() == {"status": "ok"}
    assert (await browser.http.head("/healthz")).status_code == 200


async def test_readyz_reports_the_states(env: Env, browser: Browser):
    ready = await browser.http.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "checks": {"identity_provider": "ok", "session_store": "ok"}}

    # Identity provider unreachable.
    def refuse(request):
        raise httpx.ConnectError("down")

    good_http = env.ext.core._http
    env.ext.core._http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    env.ext.core._owns_http = False
    down = await browser.http.get("/readyz")
    assert down.status_code == 503
    assert down.json()["checks"]["identity_provider"].startswith("unavailable")
    assert down.json()["checks"]["session_store"] == "ok"
    env.ext.core._http = good_http

    # Session store unreachable.
    async def broken():
        raise ConnectionError("redis is gone")

    env.ext.store.ping = broken
    store_down = await browser.http.get("/readyz")
    assert store_down.status_code == 503
    assert store_down.json()["checks"]["session_store"].startswith("unavailable")
    assert store_down.json()["checks"]["identity_provider"] == "ok"


# -- static frontend ------------------------------------------------------------------------------------------------------------


async def test_the_entry_page_sends_visitors_without_a_session_to_the_login(env: Env, browser: Browser):
    response = await browser.http.get("/")
    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login?return_to=%2F"


async def test_the_frontend_is_served_to_signed_in_people_with_spa_fallback(env: Env):
    browser = await signed_in_browser(env)
    try:
        index = await browser.http.get("/")
        assert index.status_code == 200 and "<div id=app>" in index.text and index.headers["cache-control"] == "no-cache"
        fallback = await browser.http.get("/reports/2026/q3")  # a client-side route
        assert fallback.status_code == 200 and "<div id=app>" in fallback.text
        asset = await browser.http.get("/assets/app.js")
        assert asset.status_code == 200 and "console.log" in asset.text and "max-age" in asset.headers["cache-control"]
        assert (await browser.http.get("/assets/missing.js")).status_code == 404
        assert (await browser.http.head("/")).status_code == 200
    finally:
        await browser.aclose()


async def test_assets_do_not_need_a_session(env: Env, browser: Browser):
    assert (await browser.http.get("/assets/app.js")).status_code == 200


async def test_unknown_api_and_sdk_paths_are_404_not_the_spa(env: Env):
    browser = await signed_in_browser(env)
    try:
        for path in ("/api/unknown", "/api", "/auth/unknown", "/_sdk/unknown"):
            response = await browser.http.get(path)
            assert response.status_code == 404 and response.headers["content-type"].startswith("application/json"), path
    finally:
        await browser.aclose()


async def test_path_traversal_is_not_served(env: Env):
    browser = await signed_in_browser(env)
    try:
        for path in ("/%2e%2e/%2e%2e/extension.toml", "/..%2f..%2fextension.toml", "/assets/%2e%2e/%2e%2e/%2e%2e/extension.toml", "/%00"):
            response = await browser.http.get(path)
            assert "[extension]" not in response.text, path
    finally:
        await browser.aclose()


async def test_dotfiles_in_the_frontend_directory_are_never_served(env: Env):
    dist = env.root / "project" / "frontend" / "dist"
    (dist / ".env").write_text("SECRET=1")
    (dist / ".git").mkdir()
    (dist / ".git" / "config").write_text("[core]")
    (dist / ".well-known").mkdir()
    (dist / ".well-known" / "assetlinks.json").write_text("[]")
    browser = await signed_in_browser(env)
    try:
        for path in ("/.env", "/.git/config", "/assets/../.env"):
            response = await browser.http.get(path)
            assert response.status_code == 404 and "SECRET" not in response.text and "[core]" not in response.text, path
        assert (await browser.http.get("/.well-known/assetlinks.json")).json() == []
    finally:
        await browser.aclose()


async def test_public_pages_mode(tmp_path):
    env = build_env(tmp_path, public_pages=True)
    browser = Browser(env)
    try:
        assert (await browser.http.get("/")).status_code == 200
        assert (await browser.http.get("/api/me")).status_code == 401  # the API still needs a session
    finally:
        await browser.aclose()


async def test_routes_added_after_asgi_win_over_the_frontend(env: Env, browser: Browser):
    env.app.add_api_route("/late", lambda: {"late": True}, methods=["GET"])
    assert (await browser.http.get("/late")).json() == {"late": True}


async def test_without_a_static_dir_unknown_paths_are_404(tmp_path):
    env = build_env(tmp_path)
    app = env.ext.asgi()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env.public_url)
    try:
        assert (await client.get("/")).status_code == 404
    finally:
        await client.aclose()


# -- start-up: the lock-file check ------------------------------------------------------------------------------------------------


async def test_asgi_refuses_to_build_when_the_manifest_exceeds_the_lock(tmp_path):
    env = build_env(tmp_path)
    lock = tmp_path / "project" / "extension.lock.toml"
    lock.write_text(
        '[lock]\nextension = "demo"\nversion = "1.2.0"\nclient_id = "ext-demo"\nenvironment = "local"\n'
        'approved_scopes = ["ext-data-read"]\n'
    )
    with pytest.raises(LockError, match="svc-projects-read"):
        env.ext.asgi()
    env.ext.asgi(check_lock=False)  # the escape hatch exists, and is explicit


# -- proxies -------------------------------------------------------------------------------------------------------------------------


async def client_ip(request: Request):
    return {"ip": request.client.host, "scheme": request.url.scheme}


@pytest.mark.parametrize(
    "trusted, expected_ip, expected_scheme",
    [
        ("127.0.0.1", "10.0.0.2", "https"),  # 10.0.0.2 is the first address from the right that is not a trusted proxy
        ("127.0.0.0/8,10.0.0.0/8", "203.0.113.9", "https"),  # both hops are trusted proxies: the client is the left-most
        ("*", "203.0.113.9", "https"),
        ("", "127.0.0.1", "http"),  # nobody is trusted by default
        ("10.0.0.0/8", "127.0.0.1", "http"),  # the peer is not one of them
    ],
)
async def test_proxy_headers_only_from_trusted_proxies(tmp_path, trusted, expected_ip, expected_scheme):
    env = build_env(tmp_path, extra_environ={"APPEXT_TRUSTED_PROXIES": trusted} if trusted else {})
    env.app.add_api_route("/ip", client_ip, methods=["GET"])
    transport = httpx.ASGITransport(app=env.app, client=("127.0.0.1", 5000))
    client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000")
    try:
        response = await client.get("/ip", headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.2", "X-Forwarded-Proto": "https"})
        assert response.json() == {"ip": expected_ip, "scheme": expected_scheme}
    finally:
        await client.aclose()


# -- embedding in the FMIS web app -----------------------------------------------------------------------


WEB_APP = "https://app.fmis.test"


def web_app_env(tmp_path) -> Env:
    return build_env(tmp_path, extra_environ={"APPEXT_APP_ORIGINS": WEB_APP})


async def test_only_the_configured_web_app_may_embed_the_extension(tmp_path):
    env = web_app_env(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="https://demo.apps.test") as client:
        for path in ("/healthz", "/_sdk/info", "/auth/login"):
            response = await client.get(path)
            csp = response.headers["content-security-policy"]
            assert f"frame-ancestors {WEB_APP}" in csp and "'none'" not in csp.split("frame-ancestors", 1)[1], path
            # `X-Frame-Options` cannot name an origin: it would forbid exactly what the CSP allows.
            assert "x-frame-options" not in response.headers, path


async def test_sign_in_error_pages_show_up_inside_the_web_apps_frame(tmp_path):
    env = web_app_env(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="https://demo.apps.test") as client:
        response = await client.get("/auth/callback", params={"state": "unknown", "code": "x"})
        assert response.status_code >= 400
        assert f"frame-ancestors {WEB_APP}" in response.headers["content-security-policy"]


async def test_an_extension_the_app_opens_in_the_browser_is_never_framed(tmp_path):
    """`display = "external"`: the web app opens it in a tab, so nothing needs to frame it – and the
    operator's `APPEXT_APP_ORIGINS` (one value for every extension) must not loosen that."""
    env = build_env(tmp_path, display="external", extra_environ={"APPEXT_APP_ORIGINS": WEB_APP})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="https://demo.apps.test") as client:
        for path in ("/healthz", "/_sdk/info", "/auth/login"):
            response = await client.get(path)
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"], path
            assert WEB_APP not in response.headers["content-security-policy"], path
            assert response.headers["x-frame-options"] == "DENY", path
        # The sign-in error pages answer for themselves, too.
        error = await client.get("/auth/callback", params={"state": "unknown", "code": "x"})
        assert error.status_code >= 400
        assert "frame-ancestors 'none'" in error.headers["content-security-policy"]


async def test_an_external_extension_still_gets_the_way_back_to_the_web_app(tmp_path):
    """The bar "Back to FMIS" in a browser tab needs the web app's address – external or not."""
    env = build_env(tmp_path, display="external", extra_environ={"APPEXT_APP_ORIGINS": WEB_APP})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="https://demo.apps.test") as client:
        script = (await client.get("/_sdk/bridge.js")).text
    config = __import__("json").loads(script.splitlines()[0].removeprefix("window.__APPEXT_CONFIG__ = ").removesuffix(";"))
    assert config["appOrigins"] == [WEB_APP]


async def test_without_a_web_app_nothing_embeds_and_nothing_loosens(env: Env, browser: Browser):
    response = await browser.http.get("/healthz")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


async def test_bridge_js_knows_the_web_app_and_the_name_of_the_extension(tmp_path):
    env = web_app_env(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="https://demo.apps.test") as client:
        script = (await client.get("/_sdk/bridge.js")).text
    first = script.splitlines()[0]
    assert first.startswith("window.__APPEXT_CONFIG__ = ")
    config = __import__("json").loads(first.removeprefix("window.__APPEXT_CONFIG__ = ").removesuffix(";"))
    assert config["appOrigins"] == [WEB_APP] and config["name"] == env.ext.manifest.name


async def test_every_html_page_loads_the_bridge_without_the_author_asking(env: Env):
    browser = await signed_in_browser(env)
    try:
        page = (await browser.http.get("/")).text
        assert page.count("/_sdk/bridge.js") == 1
        # An author who loaded it themselves does not get it twice.
        assert (await browser.http.get("/")).text.count('src="/_sdk/bridge.js"') == 1
    finally:
        await browser.aclose()


def test_inject_bridge_goes_into_the_head_once():
    from appext.app import inject_bridge

    page = "<!doctype html><html><head><title>x</title></head><body>y</body></html>"
    once = inject_bridge(page)
    assert once.index("/_sdk/bridge.js") < once.index("</head>") and once.count("/_sdk/bridge.js") == 1
    assert inject_bridge(once) == once
    assert inject_bridge("<p>no head</p>").startswith('<script src="/_sdk/bridge.js">')
    assert "</HEAD >" in inject_bridge("<HEAD></HEAD ><body></body>")


# -- a link has no server -------------------------------------------------------------------------------


LINK_MANIFEST = """
[extension]
id = "shop-link"
name = "Shop"
version = "1.0.0"
kind = "link"
entry = "https://shop.example.com/fmis"
"""


def refused(error: pytest.ExceptionInfo) -> None:
    assert error.value.paths == ["extension.kind"]
    message = str(error.value)
    assert "link" in message and "no server" in message and "appext store register" in message


def test_an_extension_cannot_be_made_from_a_link(tmp_path):
    path = tmp_path / "extension.toml"
    path.write_text(LINK_MANIFEST)
    with pytest.raises(ManifestError) as error:
        Extension.from_manifest(path, environ={"APPEXT_ENV": "local"})
    refused(error)


def test_the_link_is_named_before_the_missing_settings(tmp_path):
    """A deployment's environment is checked strictly; a link must not drown in `APPEXT_ISSUER is missing …`."""
    path = tmp_path / "extension.toml"
    path.write_text(LINK_MANIFEST)
    with pytest.raises(ManifestError) as error:
        Extension.from_manifest(path, environ={"APPEXT_ENV": "prod"})
    refused(error)


def test_constructing_an_extension_from_a_link_manifest_is_refused_too(tmp_path):
    settings = build_env(tmp_path).ext.settings
    with pytest.raises(ManifestError) as error:
        Extension(loads_manifest(LINK_MANIFEST), settings)
    refused(error)
