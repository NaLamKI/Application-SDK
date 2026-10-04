"""MemoryStore and RedisStore (on fakeredis) must behave the same; and tokens must not rest in the clear."""
from __future__ import annotations

import asyncio
import base64

import fakeredis
import pytest

from appext.session import LockTimeout, MemoryStore, RedisStore, Session, TokenCipher, new_session_id

SECRET_ACCESS = "access-token-SECRET-aaaaaaaa"
SECRET_REFRESH = "refresh-token-SECRET-bbbbbbbb"
SECRET_ID = "id-token-SECRET-cccccccc"


def make_session(**over) -> Session:
    data = dict(
        sub="u-1", sid="kc-session-1", access_token=SECRET_ACCESS, access_expires_at=2e9, refresh_token=SECRET_REFRESH,
        refresh_expires_at=2e9 + 100, id_token=SECRET_ID, claims={"sub": "u-1", "name": "Una"}, scopes=["openid"],
        realm_roles=["analyst"], created_at=1e9,
    )
    data.update(over)
    return Session(**data)


@pytest.fixture(params=["memory", "redis"])
async def store(request):
    cipher = TokenCipher.generate()
    if request.param == "memory":
        yield MemoryStore(cipher)
    else:
        redis = fakeredis.FakeAsyncRedis()
        yield RedisStore(redis, cipher)
        await redis.aclose()


async def raw_contents(store) -> list[bytes]:
    """Everything the store holds, keys and values, as bytes."""
    if isinstance(store, MemoryStore):
        out = [k.encode() for k in store._values] + [v for v, _ in store._values.values()]
        for key, (members, _) in store._indexes.items():
            out.append(key.encode())
            out.extend(m.encode() for m in members)
        return out
    out = []
    for key in await store.redis.keys("*"):
        out.append(key)
        kind = await store.redis.type(key)
        if kind == b"string":
            out.append(await store.redis.get(key))
        elif kind == b"set":
            out.extend(await store.redis.smembers(key))
    return out


async def test_session_roundtrip(store):
    session = make_session()
    session_id = await store.create_session(session, 60)
    loaded = await store.load_session(session_id)
    assert loaded == session and loaded.id == session_id
    assert loaded.user.sub == "u-1" and loaded.user.name == "Una" and loaded.user.roles == {"analyst"}


async def test_session_ids_have_at_least_256_bits_and_are_unique(store):
    ids = {await store.create_session(make_session(), 60) for _ in range(20)}
    assert len(ids) == 20
    for session_id in ids:
        assert len(base64.urlsafe_b64decode(session_id + "=" * (-len(session_id) % 4))) >= 32
    assert len(new_session_id()) >= 43


async def test_unknown_or_empty_ids_load_nothing(store):
    assert await store.load_session("nope") is None
    assert await store.load_session("") is None


async def test_delete_session(store):
    session_id = await store.create_session(make_session(), 60)
    await store.delete_session(session_id)
    assert await store.load_session(session_id) is None
    await store.delete_session(session_id)  # idempotent
    await store.delete_session("")


async def test_sessions_expire(store):
    session_id = await store.create_session(make_session(), 0.05)
    await asyncio.sleep(0.12)
    assert await store.load_session(session_id) is None


async def test_nothing_in_the_store_is_a_readable_token_or_a_usable_session_id(store):
    session_id = await store.create_session(make_session(), 60)
    await store.put_cached_token(session_id, "projects-api|svc", {"token": "exchanged-SECRET-dddd", "expires_at": 9e9}, 60)
    await store.put_transaction("state-SECRET-eeee", {"verifier": "verifier-SECRET-ffff", "nonce": "n"}, 60)
    contents = b"\n".join(await raw_contents(store))
    assert contents  # we are really looking at something
    for secret in (SECRET_ACCESS, SECRET_REFRESH, SECRET_ID, "exchanged-SECRET-dddd", "verifier-SECRET-ffff", "state-SECRET-eeee"):
        assert secret.encode() not in contents, secret
    # Neither the cookie value nor the identifiers of the person / SSO session are stored in the clear.
    for identifier in (session_id, "kc-session-1", "u-1", "Una", "analyst"):
        assert identifier.encode() not in contents, identifier


async def test_records_are_bound_to_their_slot(store):
    """A record copied into another session's slot does not decrypt."""
    a = await store.create_session(make_session(sub="u-a"), 60)
    b = await store.create_session(make_session(sub="u-b"), 60)
    from appext.session import _digest

    blob_a = await store._get(f"s:{_digest(a)}")
    await store._put(f"s:{_digest(b)}", blob_a, 60)
    assert await store.load_session(b) is None


async def test_tampered_records_are_treated_as_absent(store):
    from appext.session import _digest

    session_id = await store.create_session(make_session(), 60)
    key = f"s:{_digest(session_id)}"
    blob = await store._get(key)
    await store._put(key, blob[:-3] + b"AAA", 60)
    assert await store.load_session(session_id) is None


async def test_key_rotation_new_key_encrypts_old_keys_still_decrypt():
    k_old, k_new = b"o" * 32, b"n" * 32
    store_old = MemoryStore(TokenCipher([k_old]))
    session_id = await store_old.create_session(make_session(), 60)
    rotated = MemoryStore(TokenCipher([k_new, k_old]))
    rotated._values = store_old._values
    assert (await rotated.load_session(session_id)).sub == "u-1"
    only_new = MemoryStore(TokenCipher([k_new]))
    only_new._values = store_old._values
    assert await only_new.load_session(session_id) is None
    # A record written after the rotation uses the new key.
    fresh_id = await rotated.create_session(make_session(), 60)
    only_new._values = rotated._values
    assert (await only_new.load_session(fresh_id)).sub == "u-1"


def test_cipher_rejects_bad_keys():
    with pytest.raises(ValueError):
        TokenCipher([])
    with pytest.raises(ValueError):
        TokenCipher([b"short"])


async def test_sid_index_ends_all_sessions_of_one_sid(store):
    a = await store.create_session(make_session(sid="s1"), 60)
    b = await store.create_session(make_session(sid="s1"), 60)
    c = await store.create_session(make_session(sid="s2"), 60)
    assert await store.delete_sessions_by_sid("s1") == 2
    assert await store.load_session(a) is None and await store.load_session(b) is None
    assert await store.load_session(c) is not None
    assert await store.delete_sessions_by_sid("s1") == 0
    assert await store.delete_sessions_by_sid("unknown") == 0


async def test_sub_index_ends_all_sessions_of_a_person(store):
    a = await store.create_session(make_session(sub="p1", sid="s1"), 60)
    b = await store.create_session(make_session(sub="p1", sid="s2"), 60)
    c = await store.create_session(make_session(sub="p2", sid="s3"), 60)
    assert await store.delete_sessions_by_sub("p1") == 2
    assert await store.load_session(a) is None and await store.load_session(b) is None
    assert await store.load_session(c) is not None


async def test_saving_a_session_keeps_it_in_the_index(store):
    session_id = await store.create_session(make_session(sid="s1"), 60)
    session = await store.load_session(session_id)
    session.access_token = "renewed"
    await store.save_session(session, 60)
    assert (await store.load_session(session_id)).access_token == "renewed"
    assert await store.delete_sessions_by_sid("s1") == 1


async def test_transactions_are_single_use(store):
    await store.put_transaction("st", {"nonce": "n"}, 60)
    assert await store.pop_transaction("st") == {"nonce": "n"}
    assert await store.pop_transaction("st") is None
    assert await store.pop_transaction("never") is None


async def test_transactions_expire(store):
    await store.put_transaction("st", {"nonce": "n"}, 0.05)
    await asyncio.sleep(0.12)
    assert await store.pop_transaction("st") is None


async def test_concurrent_pops_of_a_transaction_yield_it_once(store):
    await store.put_transaction("st", {"nonce": "n"}, 60)
    results = await asyncio.gather(*(store.pop_transaction("st") for _ in range(8)))
    assert sum(r is not None for r in results) == 1


async def test_token_cache_roundtrip_and_removal_with_the_session(store):
    session_id = await store.create_session(make_session(), 60)
    await store.put_cached_token(session_id, "a", {"token": "t1", "expires_at": 1.0}, 60)
    await store.put_cached_token(session_id, "b", {"token": "t2", "expires_at": 2.0}, 60)
    await store.put_cached_token("shared", "a", {"token": "t3", "expires_at": 3.0}, 60)
    assert await store.get_cached_token(session_id, "a") == {"token": "t1", "expires_at": 1.0}
    await store.drop_cached_token(session_id, "a")
    assert await store.get_cached_token(session_id, "a") is None
    await store.delete_session(session_id)
    assert await store.get_cached_token(session_id, "b") is None
    assert await store.get_cached_token("shared", "a") is not None  # other namespaces stay


async def test_lock_excludes_concurrent_holders(store):
    inside = 0
    peak = 0

    async def worker():
        nonlocal inside, peak
        async with store.session_lock("s1", timeout=5):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak == 1


async def test_locks_of_different_sessions_do_not_block_each_other(store):
    async with store.session_lock("s1", timeout=1):
        async with store.session_lock("s2", timeout=1):
            pass


async def test_lock_wait_times_out(store):
    async with store.session_lock("s1", timeout=5):
        with pytest.raises(LockTimeout):
            async with store.session_lock("s1", timeout=0.1):
                pass
    async with store.session_lock("s1", timeout=1):  # and it is free again
        pass


async def test_lock_is_released_after_an_error(store):
    with pytest.raises(RuntimeError):
        async with store.session_lock("s1", timeout=1):
            raise RuntimeError("boom")
    async with store.session_lock("s1", timeout=1):
        pass


async def test_redis_lock_expires_so_a_crashed_worker_cannot_block_forever():
    store = RedisStore(fakeredis.FakeAsyncRedis(), TokenCipher.generate())
    # A worker that took the lock and died: the key is there, nobody will release it.
    await store.redis.set(store._k("l:" + __import__("hashlib").sha256(b"s1").hexdigest()), b"dead-worker", px=100)
    async with store.session_lock("s1", timeout=2, ttl=1):
        pass


async def test_redis_lock_release_does_not_delete_another_workers_lock():
    store = RedisStore(fakeredis.FakeAsyncRedis(), TokenCipher.generate())
    import hashlib

    name = store._k("l:" + hashlib.sha256(b"s1").hexdigest())
    async with store.session_lock("s1", timeout=1, ttl=60):
        await store.redis.set(name, b"someone-else")  # our lock "expired" and another worker took it
    assert await store.redis.get(name) == b"someone-else"


async def test_ping(store):
    await store.ping()


async def test_redis_store_from_url_needs_the_package_hint(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "redis.asyncio", None)
    with pytest.raises(RuntimeError, match=r"appext\[redis\]"):
        RedisStore.from_url("redis://localhost:6379/0", TokenCipher.generate())
