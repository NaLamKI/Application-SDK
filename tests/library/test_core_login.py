"""The login: transaction, callback, and everything that must be refused."""
from __future__ import annotations

import jwt
import pytest

from appext.core import APP_MARKER, LoginDenied, LoginError, is_app_mode, safe_return_to

from .conftest import APP_REDIRECT, PUBLIC_URL, Browser, Env, query_of


async def test_authorize_url_carries_everything_the_flow_needs(env: Env):
    start = await env.ext.core.start_login(user_agent="Mozilla/5.0", return_to="/reports")
    q = query_of(start.url)
    assert start.url.startswith(env.idp.authorization_endpoint + "?")
    assert q["response_type"] == "code"
    assert q["client_id"] == "ext-demo"
    assert q["redirect_uri"] == f"{PUBLIC_URL}/auth/callback"
    assert q["response_mode"] == "query"
    assert q["code_challenge_method"] == "S256"
    assert len(q["code_challenge"]) == 43
    assert q["state"] == start.state and len(q["state"]) >= 32 and q["nonce"]
    # openid + consent scopes + every user-mode service scope; not the service-mode one.
    assert q["scope"].split() == ["openid", "ext-data-read", "svc-projects-read"]
    assert "prompt" not in q


async def test_app_marker_selects_the_app_redirect_uri(env: Env):
    app = await env.ext.core.start_login(user_agent=f"Mozilla/5.0 {APP_MARKER}1")
    web = await env.ext.core.start_login(user_agent="Mozilla/5.0")
    assert query_of(app.url)["redirect_uri"] == APP_REDIRECT and app.app_mode
    assert query_of(web.url)["redirect_uri"] == f"{PUBLIC_URL}/auth/callback" and not web.app_mode
    assert is_app_mode(f"x {APP_MARKER}2") and not is_app_mode(None) and not is_app_mode("")


async def test_each_login_gets_fresh_secrets(env: Env):
    a = await env.ext.core.start_login(user_agent=None)
    b = await env.ext.core.start_login(user_agent=None)
    qa, qb = query_of(a.url), query_of(b.url)
    assert len({qa["state"], qb["state"]}) == 2
    assert len({qa["nonce"], qb["nonce"]}) == 2
    assert qa["code_challenge"] != qb["code_challenge"]


async def test_extra_scopes_cannot_widen_beyond_the_manifest(env: Env):
    start = await env.ext.core.start_login(user_agent=None, extra_scopes=["svc-export-write", "admin", "ext-data-read"])
    scopes = query_of(start.url)["scope"].split()
    assert scopes == ["openid", "ext-data-read", "svc-projects-read", "svc-export-write"]


async def test_full_login_in_the_browser(env: Env, browser: Browser):
    response = await browser.sign_in("u-1", return_to="/reports?x=1")
    assert response.status_code == 302
    assert response.headers["location"] == "/reports?x=1"
    assert env.ext.settings.session_cookie_name in browser.http.cookies
    # The ID token and the session: sub, sid, roles from the access token.
    session_id = browser.http.cookies[env.ext.settings.session_cookie_name]
    session = await env.ext.store.load_session(session_id)
    assert session.sub == "u-1" and session.sid
    assert session.realm_roles == ["analyst"]
    assert "svc-projects-read" in session.scopes
    assert session.app_mode is False


async def test_login_in_app_mode_returns_through_the_custom_scheme(env: Env):
    b = Browser(env, user_agent=f"Mozilla/5.0 {APP_MARKER}1")
    try:
        started = await b.start()
        assert query_of(started.headers["location"])["redirect_uri"] == APP_REDIRECT
        callback = env.idp.authorize(started.headers["location"], "u-1")
        assert callback.startswith(APP_REDIRECT + "?")
        # The app loads the parameters unchanged into the WebView, which holds the transaction cookie.
        done = await b.http.get(b.web_url(callback))
        assert done.status_code == 302
        session = await env.ext.store.load_session(b.http.cookies[env.ext.settings.session_cookie_name])
        assert session.app_mode is True
    finally:
        await b.aclose()


async def test_code_redemption_authenticates_with_a_signed_assertion(env: Env, browser: Browser):
    await browser.sign_in()
    (request,) = env.idp.token_requests("authorization_code")
    assert request.authenticated_with == "private_key_jwt"
    form = request.form
    assert form["redirect_uri"] == f"{PUBLIC_URL}/auth/callback"
    assert len(form["code_verifier"]) >= 43
    assert "client_secret" not in form
    claims = request.assertion_claims
    assert claims["iss"] == claims["sub"] == "ext-demo"
    assert claims["aud"] == env.idp.token_endpoint
    assert claims["exp"] - claims["iat"] <= 120 and claims["jti"]


async def test_client_secret_authentication_is_supported(tmp_path):
    from .conftest import build_env

    env = build_env(tmp_path, client_auth="client_secret")
    b = Browser(env)
    try:
        response = await b.sign_in()
        assert response.status_code == 302
        (request,) = env.idp.token_requests("authorization_code")
        assert request.authenticated_with == "client_secret_post"
    finally:
        await b.aclose()


async def test_the_assertion_has_a_fresh_jti_each_time(env: Env, browser: Browser):
    await browser.sign_in()
    await browser.sign_in()
    jtis = [r.assertion_claims["jti"] for r in env.idp.token_requests("authorization_code")]
    assert len(set(jtis)) == 2


async def test_state_the_server_never_issued_is_refused(env: Env, browser: Browser):
    started = await browser.start()
    good = env.idp.authorize(started.headers["location"], "u-1")
    forged = good.replace(query_of(good)["state"], "not-a-real-state")
    response = await browser.http.get(forged)
    assert response.status_code == 400
    assert env.ext.settings.session_cookie_name not in browser.http.cookies


async def test_a_callback_replayed_is_refused(env: Env, browser: Browser):
    started = await browser.start()
    callback = env.idp.authorize(started.headers["location"], "u-1")
    first = await browser.http.get(callback)
    assert first.status_code == 302
    second = await browser.http.get(callback)
    assert second.status_code == 400


async def test_the_core_consumes_a_transaction_on_first_use(env: Env):
    start = await env.ext.core.start_login(user_agent=None)
    params = query_of(env.idp.authorize(start.url, "u-1"))
    first = await env.ext.core.handle_callback(params, transaction_cookie=start.state)
    assert first.session is not None
    with pytest.raises(LoginError) as err:
        await env.ext.core.handle_callback(params, transaction_cookie=start.state)
    assert err.value.code == "unknown_transaction"


async def test_the_transaction_cookie_must_match_the_state(env: Env):
    """Login CSRF: the attacker's callback URL loaded in the victim's browser must not sign the victim in."""
    attacker, victim = Browser(env), Browser(env)
    try:
        started = await attacker.start()
        callback = env.idp.authorize(started.headers["location"], "u-2")
        await victim.start()  # the victim has a transaction cookie, but for another state
        response = await victim.http.get(callback)
        assert response.status_code == 400
        assert env.ext.settings.session_cookie_name not in victim.http.cookies
        # No cookie at all is refused too.
        bare = Browser(env)
        try:
            started = await attacker.start()
            callback = env.idp.authorize(started.headers["location"], "u-2")
            assert (await bare.http.get(callback)).status_code == 400
        finally:
            await bare.aclose()
    finally:
        await attacker.aclose()
        await victim.aclose()


async def test_access_denied_is_reported_and_creates_no_session(env: Env, browser: Browser):
    response = await browser.sign_in("u-1", consent=False)
    assert response.status_code == 403
    assert "not given access" in response.text
    assert env.ext.settings.session_cookie_name not in browser.http.cookies
    assert "/auth/login" in response.text  # a way to try again


async def test_core_raises_login_denied_for_idp_errors(env: Env):
    start = await env.ext.core.start_login(user_agent=None)
    with pytest.raises(LoginDenied) as err:
        await env.ext.core.handle_callback({"state": start.state, "error": "access_denied"}, transaction_cookie=start.state)
    assert err.value.error == "access_denied"


async def test_a_wrong_issuer_in_the_response_is_refused(env: Env):
    start = await env.ext.core.start_login(user_agent=None)
    with pytest.raises(LoginError) as err:
        await env.ext.core.handle_callback(
            {"state": start.state, "code": "x", "iss": "https://evil.test/realms/test"}, transaction_cookie=start.state
        )
    assert err.value.code == "issuer_mismatch"


async def test_account_mismatch_discards_tokens_and_restarts_with_prompt_login(env: Env):
    """The app's user (cookie app_sub) differs from whoever is signed in in the system browser."""
    b = Browser(env, user_agent=f"x {APP_MARKER}1")
    try:
        b.set_cookie("app_sub", "u-1")
        started = await b.start(return_to="/dashboard")
        callback = env.idp.authorize(started.headers["location"], "u-2")  # the wrong person is signed in
        response = await b.http.get(b.web_url(callback))
        assert response.status_code == 302
        restart = response.headers["location"]
        assert restart.startswith(env.idp.authorization_endpoint)
        q = query_of(restart)
        assert q["prompt"] == "login"
        assert q["redirect_uri"] == APP_REDIRECT  # still the app transaction
        assert env.ext.settings.session_cookie_name not in b.http.cookies
        # The restarted transaction completes for the right person and returns to the original page.
        done = await b.http.get(b.web_url(env.idp.authorize(restart, "u-1")))
        assert done.status_code == 302 and done.headers["location"] == "/dashboard"
        assert env.ext.settings.session_cookie_name in b.http.cookies
    finally:
        await b.aclose()


async def test_matching_account_cookie_is_accepted(env: Env, browser: Browser):
    browser.set_cookie("app_sub", "u-1")
    response = await browser.sign_in("u-1")
    assert response.status_code == 302 and env.ext.settings.session_cookie_name in browser.http.cookies


async def test_id_token_with_wrong_nonce_is_refused(env: Env, browser: Browser):
    started = await browser.start()
    callback = env.idp.authorize(started.headers["location"], "u-1")
    code = query_of(callback)["code"]
    env.idp.codes[code]["nonce"] = "someone-elses-nonce"
    response = await browser.http.get(callback)
    assert response.status_code == 400 and "nonce" in response.text


async def test_id_token_for_another_client_is_refused(env: Env, browser: Browser):
    started = await browser.start()
    callback = env.idp.authorize(started.headers["location"], "u-1")
    # The IdP signs an ID token whose audience is some other client.
    original = env.idp._issue

    def other_audience(client, user, sid, scopes, *, nonce=None):
        tokens = original(client, user, sid, scopes, nonce=nonce)
        claims = jwt.decode(tokens["id_token"], options={"verify_signature": False})
        claims["aud"] = "ext-other"
        tokens["id_token"] = env.idp.sign(claims)
        return tokens

    env.idp._issue = other_audience
    response = await browser.http.get(callback)
    assert response.status_code == 400 and "ID token was rejected" in response.text


async def test_id_token_signed_by_a_foreign_key_is_refused(env: Env, browser: Browser):
    started = await browser.start()
    callback = env.idp.authorize(started.headers["location"], "u-1")
    original = env.idp._issue
    from cryptography.hazmat.primitives.asymmetric import rsa
    from appext.testing import _SigningKey

    rogue = _SigningKey("fake-key-1", rsa.generate_private_key(public_exponent=65537, key_size=2048))  # same kid, other key

    def forged(client, user, sid, scopes, *, nonce=None):
        tokens = original(client, user, sid, scopes, nonce=nonce)
        claims = jwt.decode(tokens["id_token"], options={"verify_signature": False})
        tokens["id_token"] = env.idp.sign(claims, key=rogue)
        return tokens

    env.idp._issue = forged
    response = await browser.http.get(callback)
    assert response.status_code == 400


async def test_expired_id_token_is_refused(env: Env, browser: Browser):
    started = await browser.start()
    callback = env.idp.authorize(started.headers["location"], "u-1")
    env.idp.access_lifetime = -3600  # the IdP issues tokens that are already an hour old
    response = await browser.http.get(callback)
    assert response.status_code == 400


async def test_idp_down_at_login_start_is_a_503_page(tmp_path):
    import httpx

    from .conftest import build_env

    env = build_env(tmp_path)

    def refuse(request):
        raise httpx.ConnectError("down")

    env.ext.core._http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    env.ext.core._owns_http = False
    b = Browser(env)
    try:
        response = await b.start()
        assert response.status_code == 503
        assert "identity provider" in response.text
    finally:
        await b.aclose()


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/reports", "/reports"),
        ("/reports?a=1&b=2#frag", "/reports?a=1&b=2#frag"),
        ("/", "/"),
        (None, "/"),
        ("", "/"),
        ("reports", "/"),
        ("//evil.test", "/"),
        ("//evil.test/path", "/"),
        ("https://evil.test/", "/"),
        ("http://evil.test", "/"),
        ("javascript:alert(1)", "/"),
        ("/\\evil.test", "/"),
        ("/%09/evil.test", "/%09/evil.test"),  # percent-encoded stays a path: browsers do not decode it into a host
        ("/\t/evil.test", "/"),
        ("/\n/evil.test", "/"),
        ("/\r\nSet-Cookie: x=1", "/"),
        ("/auth/login?return_to=/x", "/"),
        ("/auth", "/"),
        ("/authors", "/authors"),
    ],
)
def test_return_to_accepts_only_paths_on_this_origin(value, expected):
    assert safe_return_to(value) == expected


async def test_open_redirect_attempts_end_on_the_start_page(env: Env, browser: Browser):
    for evil in ("//evil.test/x", "https://evil.test", "/\\evil.test"):
        browser2 = Browser(env)
        try:
            response = await browser2.sign_in("u-1", return_to=evil)
            assert response.headers["location"] == "/"
        finally:
            await browser2.aclose()


async def test_login_endpoint_ignores_unknown_prompt_values(env: Env, browser: Browser):
    started = await browser.start(prompt="none")
    assert "prompt" not in query_of(started.headers["location"])
    started = await browser.start(prompt="login")
    assert query_of(started.headers["location"])["prompt"] == "login"


async def test_transaction_cookie_attributes(env: Env, browser: Browser):
    started = await browser.start()
    cookie = started.headers["set-cookie"]
    assert cookie.startswith("__Host-ext_tx=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attribute in cookie
    assert "Domain" not in cookie
