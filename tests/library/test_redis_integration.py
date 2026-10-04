"""The whole extension on the Redis store (fakeredis): what replicas rely on."""
from __future__ import annotations

import asyncio

import fakeredis

from appext.session import RedisStore, TokenCipher

from .conftest import Browser, build_env

SHARED = fakeredis.FakeAsyncRedis()


def redis_store(clock):
    return RedisStore(SHARED, TokenCipher.generate(), clock=clock)


async def test_login_api_and_logout_on_redis(tmp_path):
    env = build_env(tmp_path, store_factory=redis_store)
    browser = Browser(env)
    try:
        assert (await browser.sign_in("u-1")).status_code == 302
        assert (await browser.http.get("/api/me")).json()["sub"] == "u-1"
        sid = (await env.ext.store.load_session(browser.http.cookies[env.ext.settings.session_cookie_name])).sid
        await env.idp.end_session(sid)
        assert (await browser.http.get("/api/me")).status_code == 401
    finally:
        await browser.aclose()


async def test_two_replicas_share_sessions_and_one_refresh_happens(tmp_path):
    """Two processes (two Extension objects) on one Redis: a session made by one is served by the other, and a
    refresh needed by both at once runs once - the lock lives in Redis, not in the process."""
    env = build_env(tmp_path / "a", store_factory=redis_store, token_delay=0.05)
    # The "second replica": same manifest, same IdP, same Redis and the same encryption key.
    from appext import Extension

    cipher_key = env.ext.store.cipher
    replica_store = RedisStore(env.ext.store.redis, cipher_key, clock=env.clock)
    replica = Extension.from_manifest(
        tmp_path / "a" / "project" / "extension.toml", environ=env.environ, http=env.idp.client(), clock=env.clock, store=replica_store
    )
    browser = Browser(env)
    try:
        assert (await browser.sign_in("u-1")).status_code == 302
        session_id = browser.http.cookies[env.ext.settings.session_cookie_name]
        assert (await replica.store.load_session(session_id)).sub == "u-1"
        env.clock.advance(env.idp.access_lifetime - 5)
        results = await asyncio.gather(replica.core.fresh_session(session_id), env.ext.core.fresh_session(session_id), replica.core.fresh_session(session_id))
        assert env.idp.refresh_count == 1
        assert len({r.access_token for r in results}) == 1
    finally:
        await browser.aclose()


async def test_backchannel_logout_reaches_sessions_made_by_another_replica(tmp_path):
    env = build_env(tmp_path / "a", store_factory=redis_store)
    from appext import Extension

    replica_store = RedisStore(env.ext.store.redis, env.ext.store.cipher, clock=env.clock)
    replica = Extension.from_manifest(
        tmp_path / "a" / "project" / "extension.toml", environ=env.environ, http=env.idp.client(), clock=env.clock, store=replica_store
    )
    browser = Browser(env)
    try:
        await browser.sign_in("u-1")
        session_id = browser.http.cookies[env.ext.settings.session_cookie_name]
        sid = (await env.ext.store.load_session(session_id)).sid
        # Keycloak's call lands on the *other* replica; the sid index is shared.
        ended = await replica.core.handle_backchannel_logout(env.idp.make_logout_token("ext-demo", sid=sid))
        assert ended == 1
        assert await env.ext.store.load_session(session_id) is None
    finally:
        await browser.aclose()
