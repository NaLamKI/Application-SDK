"""Server-side sessions: what the cookie points to, and where it lives.

The browser holds one random id (>= 256 bit) and nothing else; tokens never
leave the backend. Everything below is about keeping that promise at rest:

* **Encrypted.** Each record is sealed with AES-256-GCM before it reaches the
  store, bound to its storage key (so a record cannot be swapped into another
  slot). The key comes from a file; the first key of the file encrypts, all
  decrypt, which is how a key is rotated.
* **The cookie value is never stored.** Records are keyed by `sha256(id)`. A
  copy of the Redis database therefore holds neither readable tokens nor
  session ids that could be replayed as a cookie.
* **One implementation of the semantics.** `SessionStore` implements sessions,
  login transactions, the token cache, the `sid -> sessions` index and the
  per-session lock once, on top of six small primitives. `MemoryStore` (for
  development) and `RedisStore` (for replicas) only supply the primitives,
  so they cannot drift apart.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("appext")

SESSION_ID_BYTES = 32  # 256 bit


class LockTimeout(RuntimeError):
    """Another worker held the session lock for too long."""


# --- the person behind a session ------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    """The signed-in person, as far as the granted scopes tell.

    `name` and `email` are `None` when the consent did not include them.
    `realm_roles` are realm-wide, `client_roles` are roles of *this* extension's
    client – use `has_role()` when you do not care which.
    """

    sub: str
    name: str | None = None
    email: str | None = None
    preferred_username: str | None = None
    realm_roles: tuple[str, ...] = ()
    client_roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()

    @property
    def roles(self) -> frozenset[str]:
        return frozenset((*self.realm_roles, *self.client_roles))

    def has_role(self, role: str) -> bool:
        return role in self.realm_roles or role in self.client_roles


@dataclass
class Session:
    """What is known about a signed-in browser. Persisted encrypted; `id` is not."""

    sub: str
    access_token: str
    access_expires_at: float
    refresh_token: str | None = None
    refresh_expires_at: float | None = None
    id_token: str | None = None
    sid: str | None = None
    #: Claims of the validated ID token.
    claims: dict[str, Any] = field(default_factory=dict)
    #: Scopes the token endpoint reported as granted.
    scopes: list[str] = field(default_factory=list)
    realm_roles: list[str] = field(default_factory=list)
    client_roles: list[str] = field(default_factory=list)
    app_mode: bool = False
    created_at: float = 0.0
    id: str = ""

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record.pop("id")
        return record

    @classmethod
    def from_record(cls, session_id: str, record: dict[str, Any]) -> "Session":
        known = {k: v for k, v in record.items() if k in cls.__dataclass_fields__ and k != "id"}
        return cls(id=session_id, **known)

    @property
    def user(self) -> User:
        c = self.claims
        return User(
            sub=self.sub,
            name=c.get("name") or None,
            email=c.get("email") or None,
            preferred_username=c.get("preferred_username") or None,
            realm_roles=tuple(self.realm_roles),
            client_roles=tuple(self.client_roles),
            scopes=tuple(self.scopes),
        )


# --- encryption at rest ---------------------------------------------------------------------------


class TokenCipher:
    """AES-256-GCM with a key ring. Output: `v1.<key id>.<base64url(nonce || ciphertext)>`."""

    def __init__(self, keys: list[bytes]) -> None:
        if not keys:
            raise ValueError("at least one key is required")
        if any(len(k) != 32 for k in keys):
            raise ValueError("keys must be 32 bytes (AES-256)")
        self._ring = {self.key_id(k): AESGCM(k) for k in keys}
        self._active_id = self.key_id(keys[0])

    @staticmethod
    def key_id(key: bytes) -> str:
        return hashlib.sha256(key).hexdigest()[:8]

    @classmethod
    def generate(cls) -> "TokenCipher":
        return cls([secrets.token_bytes(32)])

    def seal(self, plaintext: bytes, aad: str) -> bytes:
        nonce = secrets.token_bytes(12)
        sealed = self._ring[self._active_id].encrypt(nonce, plaintext, aad.encode())
        return b"v1." + self._active_id.encode() + b"." + base64.urlsafe_b64encode(nonce + sealed)

    def open(self, blob: bytes, aad: str) -> bytes | None:
        """The plaintext, or `None` for anything that is not ours (wrong key, tampered, swapped slot)."""
        try:
            version, key_id, body = blob.split(b".", 2)
            aes = self._ring.get(key_id.decode())
            if version != b"v1" or aes is None:
                return None
            raw = base64.urlsafe_b64decode(body)
            return aes.decrypt(raw[:12], raw[12:], aad.encode())
        except (ValueError, InvalidTag):
            return None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_session_id() -> str:
    return secrets.token_urlsafe(SESSION_ID_BYTES)


# --- the store ------------------------------------------------------------------------------------


class SessionStore(ABC):
    """Sessions, login transactions, token cache, `sid` index and per-session lock.

    Subclasses implement the primitives at the bottom; everything else is shared.
    """

    #: Cleanup bound for the `sid` / `sub` indexes; at least as long as any session lives.
    index_ttl: float = 30 * 24 * 3600

    def __init__(self, cipher: TokenCipher, *, clock: Callable[[], float] = time.time) -> None:
        self.cipher = cipher
        self.clock = clock

    # -- sessions -----------------------------------------------------------------------------

    async def create_session(self, session: Session, ttl: float) -> str:
        """Store a new session under a fresh random id and return the id (the cookie value)."""
        session.id = new_session_id()
        await self.save_session(session, ttl)
        return session.id

    async def save_session(self, session: Session, ttl: float) -> None:
        sk = _digest(session.id)
        key = f"s:{sk}"
        payload = json.dumps(session.to_record(), separators=(",", ":")).encode()
        await self._put(key, self.cipher.seal(payload, key), ttl)
        # Re-added on every save: the session may outlive the index entry made at creation.
        if session.sid:
            await self._index_add(f"i:sid:{_digest(session.sid)}", sk, self.index_ttl)
        await self._index_add(f"i:sub:{_digest(session.sub)}", sk, self.index_ttl)

    async def load_session(self, session_id: str) -> Session | None:
        if not session_id:
            return None
        key = f"s:{_digest(session_id)}"
        blob = await self._get(key)
        if blob is None:
            return None
        plain = self.cipher.open(blob, key)
        if plain is None:
            log.warning("a stored session could not be decrypted (key changed or record damaged); treating it as absent")
            return None
        return Session.from_record(session_id, json.loads(plain))

    async def delete_session(self, session_id: str) -> None:
        if session_id:
            await self._drop(_digest(session_id))

    async def delete_sessions_by_sid(self, sid: str) -> int:
        """Back-channel logout: end every session of one Keycloak session. Returns how many existed."""
        return await self._drop_index(f"i:sid:{_digest(sid)}")

    async def delete_sessions_by_sub(self, sub: str) -> int:
        """Back-channel logout without `sid`: end every session of the person."""
        return await self._drop_index(f"i:sub:{_digest(sub)}")

    async def _drop_index(self, index: str) -> int:
        members = await self._index_members(index)
        ended = 0
        for sk in members:
            if await self._get(f"s:{sk}") is not None:
                ended += 1
            await self._drop(sk)
        await self._remove(index)
        return ended

    async def _drop(self, sk: str) -> None:
        cached = await self._index_members(f"ts:{sk}")
        await self._remove(f"s:{sk}", f"ts:{sk}", *(f"t:{sk}:{c}" for c in cached))

    # -- login transactions ----------------------------------------------------------------------

    async def put_transaction(self, state: str, data: dict[str, Any], ttl: float) -> None:
        key = f"x:{_digest(state)}"
        await self._put(key, self.cipher.seal(json.dumps(data).encode(), key), ttl)

    async def pop_transaction(self, state: str) -> dict[str, Any] | None:
        """Take a transaction out. Single use: a replayed callback finds nothing."""
        key = f"x:{_digest(state)}"
        blob = await self._take(key)
        plain = self.cipher.open(blob, key) if blob is not None else None
        return json.loads(plain) if plain is not None else None

    # -- token cache -------------------------------------------------------------------------------

    async def get_cached_token(self, namespace: str, name: str) -> dict[str, Any] | None:
        """`namespace` is a session id, or any fixed string for process-wide entries (client credentials)."""
        key = f"t:{_digest(namespace)}:{_digest(name)}"
        blob = await self._get(key)
        plain = self.cipher.open(blob, key) if blob is not None else None
        return json.loads(plain) if plain is not None else None

    async def put_cached_token(self, namespace: str, name: str, token: dict[str, Any], ttl: float) -> None:
        ns = _digest(namespace)
        entry = _digest(name)
        key = f"t:{ns}:{entry}"
        await self._put(key, self.cipher.seal(json.dumps(token).encode(), key), ttl)
        await self._index_add(f"ts:{ns}", entry, self.index_ttl)

    async def drop_cached_token(self, namespace: str, name: str) -> None:
        await self._remove(f"t:{_digest(namespace)}:{_digest(name)}")

    # -- the lock -----------------------------------------------------------------------------------

    def session_lock(self, session_id: str, *, timeout: float = 20.0, ttl: float = 30.0) -> AbstractAsyncContextManager[None]:
        """Mutual exclusion per session across workers (refresh must not run twice at once).

        `ttl` bounds the damage of a worker that dies while holding it.
        """
        return self._lock(f"l:{_digest(session_id)}", timeout, ttl)

    # -- primitives ---------------------------------------------------------------------------------

    @abstractmethod
    async def _put(self, key: str, value: bytes, ttl: float) -> None: ...

    @abstractmethod
    async def _get(self, key: str) -> bytes | None: ...

    @abstractmethod
    async def _take(self, key: str) -> bytes | None:
        """Read and delete in one atomic step."""

    @abstractmethod
    async def _remove(self, *keys: str) -> None: ...

    @abstractmethod
    async def _index_add(self, index: str, member: str, ttl: float) -> None: ...

    @abstractmethod
    async def _index_members(self, index: str) -> set[str]: ...

    @abstractmethod
    def _lock(self, key: str, timeout: float, ttl: float) -> AbstractAsyncContextManager[None]: ...

    @abstractmethod
    async def ping(self) -> None:
        """Return if the store is reachable, raise otherwise (`/readyz` calls this)."""

    async def aclose(self) -> None:
        """Release connections."""


class MemoryStore(SessionStore):
    """In-process store for development and tests. Not shared between processes.

    Locks are created per contention epoch (not kept forever) so that a store
    used from several event loops, as test clients do, never holds a lock bound
    to a loop that is gone.
    """

    def __init__(self, cipher: TokenCipher | None = None, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(cipher or TokenCipher.generate(), clock=clock)
        self._values: dict[str, tuple[bytes, float]] = {}
        self._indexes: dict[str, tuple[set[str], float]] = {}
        self._locks: dict[str, list] = {}  # key -> [asyncio.Lock, users]
        self._next_purge = 0.0

    def _purge(self) -> None:
        now = self.clock()
        if now < self._next_purge:
            return
        self._next_purge = now + 60
        self._values = {k: v for k, v in self._values.items() if v[1] > now}
        self._indexes = {k: v for k, v in self._indexes.items() if v[1] > now}

    async def _put(self, key: str, value: bytes, ttl: float) -> None:
        self._purge()
        self._values[key] = (value, self.clock() + ttl)

    async def _get(self, key: str) -> bytes | None:
        entry = self._values.get(key)
        if entry is None or entry[1] <= self.clock():
            self._values.pop(key, None)
            return None
        return entry[0]

    async def _take(self, key: str) -> bytes | None:
        value = await self._get(key)
        self._values.pop(key, None)
        return value

    async def _remove(self, *keys: str) -> None:
        for key in keys:
            self._values.pop(key, None)
            self._indexes.pop(key, None)

    async def _index_add(self, index: str, member: str, ttl: float) -> None:
        members, _ = self._indexes.get(index, (set(), 0.0))
        members.add(member)
        self._indexes[index] = (members, self.clock() + ttl)

    async def _index_members(self, index: str) -> set[str]:
        entry = self._indexes.get(index)
        if entry is None or entry[1] <= self.clock():
            self._indexes.pop(index, None)
            return set()
        return set(entry[0])

    @asynccontextmanager
    async def _lock(self, key: str, timeout: float, ttl: float) -> AsyncIterator[None]:
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            try:
                await asyncio.wait_for(entry[0].acquire(), timeout)
            except TimeoutError:
                raise LockTimeout("timed out waiting for the session lock") from None
            try:
                yield
            finally:
                entry[0].release()
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(key) is entry:
                del self._locks[key]

    async def ping(self) -> None:
        return None


class RedisStore(SessionStore):
    """Redis (`redis.asyncio`): several replicas share sessions, token cache and the refresh lock."""

    def __init__(self, client: Any, cipher: TokenCipher, *, prefix: str = "appext:", clock: Callable[[], float] = time.time) -> None:
        super().__init__(cipher, clock=clock)
        self.redis = client
        self.prefix = prefix

    @classmethod
    def from_url(cls, url: str, cipher: TokenCipher, **kwargs: Any) -> "RedisStore":
        try:
            from redis.asyncio import Redis
        except ImportError:
            raise RuntimeError(
                "APPEXT_SESSION_STORE is a redis:// URL but the 'redis' package is not installed: pip install 'appext[redis]'"
            ) from None
        return cls(Redis.from_url(url, decode_responses=False), cipher, **kwargs)

    def _k(self, key: str) -> str:
        return self.prefix + key

    async def _put(self, key: str, value: bytes, ttl: float) -> None:
        await self.redis.set(self._k(key), value, px=max(1, int(ttl * 1000)))

    async def _get(self, key: str) -> bytes | None:
        return await self.redis.get(self._k(key))

    async def _take(self, key: str) -> bytes | None:
        return await self.redis.getdel(self._k(key))

    async def _remove(self, *keys: str) -> None:
        if keys:
            await self.redis.delete(*(self._k(k) for k in keys))

    async def _index_add(self, index: str, member: str, ttl: float) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.sadd(self._k(index), member)
            pipe.pexpire(self._k(index), max(1, int(ttl * 1000)))
            await pipe.execute()

    async def _index_members(self, index: str) -> set[str]:
        return {m.decode() if isinstance(m, bytes) else m for m in await self.redis.smembers(self._k(index))}

    @asynccontextmanager
    async def _lock(self, key: str, timeout: float, ttl: float) -> AsyncIterator[None]:
        name = self._k(key)
        token = secrets.token_hex(16).encode()
        deadline = time.monotonic() + timeout
        while not await self.redis.set(name, token, nx=True, px=max(1, int(ttl * 1000))):
            if time.monotonic() >= deadline:
                raise LockTimeout("timed out waiting for the session lock")
            await asyncio.sleep(0.02 + secrets.randbelow(30) / 1000)
        try:
            yield
        finally:
            await self._release(name, token)

    async def _release(self, name: str, token: bytes) -> None:
        """Delete the lock only if it is still ours (it may have expired and been taken by another worker)."""
        from redis.exceptions import WatchError

        async with self.redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(name)
                if await pipe.get(name) == token:
                    pipe.multi()
                    pipe.delete(name)
                    await pipe.execute()
            except WatchError:
                pass

    async def ping(self) -> None:
        await self.redis.ping()

    async def aclose(self) -> None:
        await self.redis.aclose()


def open_store(url: str, cipher: TokenCipher, *, clock: Callable[[], float] = time.time) -> SessionStore:
    """The store for an `APPEXT_SESSION_STORE` value: `memory` or a redis URL."""
    if url == "memory":
        return MemoryStore(cipher, clock=clock)
    return RedisStore.from_url(url, cipher, clock=clock)
