"""Logout: the extension's own, RP-initiated, and back-channel (sessions of a `sid` die)."""
from __future__ import annotations

import pytest

from appext.core import LogoutTokenError

from .conftest import Browser, Env, query_of


async def sign_in_two(env: Env) -> tuple[Browser, Browser]:
    a, b = Browser(env), Browser(env)
    assert (await a.sign_in("u-1")).status_code == 302
    assert (await b.sign_in("u-1")).status_code == 302  # same person, same SSO session -> same sid
    return a, b


def cookie(env: Env, browser: Browser) -> str:
    return browser.http.cookies[env.ext.settings.session_cookie_name]


async def test_backchannel_logout_ends_every_session_of_the_sid(env: Env):
    a, b = await sign_in_two(env)
    c = Browser(env)
    try:
        assert (await c.sign_in("u-2")).status_code == 302  # another person, another sid
        sid = (await env.ext.store.load_session(cookie(env, a))).sid
        assert sid == (await env.ext.store.load_session(cookie(env, b))).sid

        response = await env.idp.send_backchannel_logout("ext-demo", sid=sid, sub="u-1")
        assert response.status_code == 200

        assert await env.ext.store.load_session(cookie(env, a)) is None
        assert await env.ext.store.load_session(cookie(env, b)) is None
        assert await env.ext.store.load_session(cookie(env, c)) is not None
        assert (await a.http.get("/api/me")).status_code == 401
        assert (await c.http.get("/api/me")).status_code == 200
    finally:
        for browser in (a, b, c):
            await browser.aclose()


async def test_backchannel_logout_also_drops_cached_service_tokens(env: Env):
    a = Browser(env)
    try:
        await a.sign_in("u-1")
        session_id = cookie(env, a)
        await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
        sid = (await env.ext.store.load_session(session_id)).sid
        await env.ext.core.handle_backchannel_logout(env.idp.make_logout_token("ext-demo", sid=sid))
        name = env.ext.core._cache_name("projects-api", ["svc-projects-read"])
        assert await env.ext.store.get_cached_token(session_id, name) is None
    finally:
        await a.aclose()


async def test_logout_token_with_only_a_subject_ends_that_persons_sessions(env: Env):
    a, b = await sign_in_two(env)
    try:
        ended = await env.ext.core.handle_backchannel_logout(env.idp.make_logout_token("ext-demo", sub="u-1"))
        assert ended == 2
        assert await env.ext.store.load_session(cookie(env, a)) is None
    finally:
        await a.aclose()
        await b.aclose()


async def test_idp_ending_the_sso_session_notifies_the_extension(env: Env):
    """What happens when the person signs out of the app: Keycloak calls every extension back."""
    a = Browser(env)
    try:
        await a.sign_in("u-1")
        sid = (await env.ext.store.load_session(cookie(env, a))).sid
        results = await env.idp.end_session(sid)
        assert results == [("ext-demo", 200)]
        assert (await a.http.get("/api/me")).status_code == 401
    finally:
        await a.aclose()


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ({"with_event": False}, "event"),
        ({"nonce": "n-123"}, "nonce"),
        ({"audience": "ext-other"}, "audience"),
        ({"issuer": "https://evil.test/realms/test"}, "issuer"),
        ({"iat": -10_000}, "too old"),
    ],
)
async def test_invalid_logout_tokens_are_refused_and_end_nothing(env: Env, mutation, reason):
    a = Browser(env)
    try:
        await a.sign_in("u-1")
        sid = (await env.ext.store.load_session(cookie(env, a))).sid
        mutation = dict(mutation)
        if "iat" in mutation:
            mutation["iat"] = env.clock() + mutation["iat"]
        token = env.idp.make_logout_token("ext-demo", sid=sid, sub="u-1", **mutation)
        with pytest.raises(LogoutTokenError):
            await env.ext.core.handle_backchannel_logout(token)
        response = await env.idp.send_backchannel_logout("ext-demo", token=token)
        assert response.status_code == 400
        assert await env.ext.store.load_session(cookie(env, a)) is not None, reason
    finally:
        await a.aclose()


async def test_logout_token_signed_by_a_foreign_key_is_refused(env: Env):
    from cryptography.hazmat.primitives.asymmetric import rsa

    from appext.testing import _SigningKey

    rogue = _SigningKey("fake-key-1", rsa.generate_private_key(public_exponent=65537, key_size=2048))
    token = env.idp.make_logout_token("ext-demo", sid="x", key=rogue)
    with pytest.raises(LogoutTokenError):
        await env.ext.core.handle_backchannel_logout(token)


async def test_an_id_token_is_not_a_logout_token(env: Env):
    a = Browser(env)
    try:
        await a.sign_in("u-1")
        session = await env.ext.store.load_session(cookie(env, a))
        with pytest.raises(LogoutTokenError):
            await env.ext.core.handle_backchannel_logout(session.id_token)
        assert await env.ext.store.load_session(cookie(env, a)) is not None
    finally:
        await a.aclose()


async def test_a_logout_token_without_sid_and_sub_is_refused(env: Env):
    with pytest.raises(LogoutTokenError):
        await env.ext.core.handle_backchannel_logout(env.idp.make_logout_token("ext-demo"))


async def test_backchannel_endpoint_validates_the_request(env: Env, browser: Browser):
    url = "/auth/backchannel-logout"
    assert (await browser.http.post(url, data={})).status_code == 400
    assert (await browser.http.post(url, data={"logout_token": "garbage"})).status_code == 400
    assert (await browser.http.get(url)).status_code == 405
    # No cookie, no CSRF header, no Origin needed: Keycloak is not a browser.
    ok = await browser.http.post(url, data={"logout_token": env.idp.make_logout_token("ext-demo", sid="nobody")})
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "no-store"


async def test_extension_logout_ends_only_the_extension_session(env: Env, browser: Browser):
    await browser.sign_in("u-1")
    session = await env.ext.store.load_session(cookie(env, browser))
    response = await browser.http.get("/auth/logout")
    assert response.status_code == 302 and response.headers["location"] == "/"
    assert "ext_session" in response.headers["set-cookie"] and "Max-Age=0" in response.headers["set-cookie"]
    assert await env.ext.store.load_session(session.id) is None
    assert session.sid in env.idp.sessions  # the SSO session of the app stays: closing an extension is not an app logout
    assert env.idp.logout_requests == []


async def test_extension_logout_return_to_is_checked(env: Env, browser: Browser):
    await browser.sign_in("u-1")
    response = await browser.http.get("/auth/logout", params={"return_to": "//evil.test"})
    assert response.headers["location"] == "/"


async def test_rp_initiated_logout_redirects_to_the_idp_with_the_hint(env: Env, browser: Browser):
    await browser.sign_in("u-1")
    session = await env.ext.store.load_session(cookie(env, browser))
    response = await browser.http.get("/auth/logout", params={"sso": "true"})
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(env.idp.end_session_endpoint + "?")
    q = query_of(location)
    assert q["id_token_hint"] == session.id_token
    assert q["post_logout_redirect_uri"] == "https://demo.apps.test/"
    assert q["client_id"] == "ext-demo"
    assert await env.ext.store.load_session(session.id) is None
    # Following it: the IdP ends the SSO session and sends the person back.
    followed = await env.idp.client().get(location)
    assert followed.status_code == 302 and followed.headers["location"] == "https://demo.apps.test/"
    assert session.sid not in env.idp.sessions


async def test_logout_without_a_session_is_harmless(env: Env, browser: Browser):
    response = await browser.http.get("/auth/logout", params={"sso": "true"})
    assert response.status_code == 302 and response.headers["location"] == "/"
