"""appext.verify: what a target service checks on the token an extension sends."""
from __future__ import annotations

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI
from cryptography.hazmat.primitives.asymmetric import rsa

from appext.jwks import JWKSCache, TokenInvalid
from appext.testing import FakeClock, FakeIdP, _SigningKey
from appext.verify import Principal, TokenVerifier, current_principal, require_azp, require_scope

AUDIENCE = "projects-api"


@pytest.fixture
def setup():
    clock = FakeClock()
    idp = FakeIdP(clock=clock)
    verifier = TokenVerifier(idp.issuer, AUDIENCE, idp.jwks_uri, http=idp.client(), clock=clock, min_refetch=0)
    app = FastAPI()
    verifier.install(app)

    @app.get("/data", dependencies=[Depends(require_scope("svc-projects-read"))])
    async def data(principal: Principal = Depends(current_principal)):
        return {"sub": principal.sub, "azp": principal.azp, "scopes": sorted(principal.scopes)}

    @app.get("/both", dependencies=[Depends(require_scope("a", "b"))])
    async def both():
        return {"ok": True}

    @app.get("/only-demo", dependencies=[Depends(require_azp("ext-demo"))])
    async def only_demo():
        return {"ok": True}

    @app.get("/explicit", dependencies=[Depends(require_scope("svc-projects-read", verifier=verifier))])
    async def explicit():
        return {"ok": True}

    @app.get("/method", dependencies=[Depends(verifier.require_scope("svc-projects-read")), Depends(verifier.require_azp("ext-demo"))])
    async def method():
        return {"ok": True}

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc.test")
    return idp, verifier, client, clock


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_a_good_token(setup):
    idp, verifier, client, _ = setup
    token = idp.mint_access_token(sub="u-1", azp="ext-demo", audience=AUDIENCE, scope="svc-projects-read openid")
    response = await client.get("/data", headers=bearer(token))
    assert response.status_code == 200
    assert response.json() == {"sub": "u-1", "azp": "ext-demo", "scopes": ["openid", "svc-projects-read"]}


async def test_audience_may_be_a_list(setup):
    idp, _, client, _ = setup
    token = idp.mint_access_token(audience=[AUDIENCE, "account"], scope="svc-projects-read")
    assert (await client.get("/data", headers=bearer(token))).status_code == 200


@pytest.mark.parametrize("header", [None, {}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}, {"Authorization": "Bearer "}])
async def test_missing_credentials_are_401_with_a_challenge(setup, header):
    _, _, client, _ = setup
    response = await client.get("/data", headers=header)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"]["code"] == "unauthenticated"


async def test_garbage_is_401(setup):
    _, _, client, _ = setup
    response = await client.get("/data", headers=bearer("not.a.jwt"))
    assert response.status_code == 401 and response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')


async def test_wrong_audience_is_401(setup):
    idp, _, client, _ = setup
    token = idp.mint_access_token(audience="some-other-api", scope="svc-projects-read")
    response = await client.get("/data", headers=bearer(token))
    assert response.status_code == 401 and "audience" in response.json()["detail"]["message"]


async def test_missing_audience_is_401(setup):
    idp, _, client, _ = setup
    token = idp.sign({"iss": idp.issuer, "sub": "u", "exp": int(idp.clock()) + 60, "scope": "svc-projects-read"})
    assert (await client.get("/data", headers=bearer(token))).status_code == 401


async def test_wrong_issuer_is_401(setup):
    idp, _, client, _ = setup
    token = idp.mint_access_token(audience=AUDIENCE, scope="svc-projects-read", iss="https://evil.test/realms/test")
    assert (await client.get("/data", headers=bearer(token))).status_code == 401


async def test_expired_token_is_401(setup):
    idp, _, client, clock = setup
    token = idp.mint_access_token(audience=AUDIENCE, scope="svc-projects-read", lifetime=300)
    assert (await client.get("/data", headers=bearer(token))).status_code == 200
    clock.advance(300 + 31)  # past exp plus the 30 s leeway
    response = await client.get("/data", headers=bearer(token))
    assert response.status_code == 401 and "expired" in response.json()["detail"]["message"]


async def test_leeway_tolerates_small_clock_skew(setup):
    idp, _, client, clock = setup
    token = idp.mint_access_token(audience=AUDIENCE, scope="svc-projects-read", lifetime=300)
    clock.advance(300 + 10)
    assert (await client.get("/data", headers=bearer(token))).status_code == 200


async def test_a_token_without_expiry_is_refused(setup):
    idp, _, client, _ = setup
    token = idp.sign({"iss": idp.issuer, "sub": "u", "aud": AUDIENCE, "scope": "svc-projects-read"})
    assert (await client.get("/data", headers=bearer(token))).status_code == 401


async def test_a_token_from_the_future_is_refused(setup):
    idp, _, client, clock = setup
    now = int(clock())
    token = idp.sign({"iss": idp.issuer, "sub": "u", "aud": AUDIENCE, "scope": "svc-projects-read", "exp": now + 9999, "iat": now + 5000})
    assert (await client.get("/data", headers=bearer(token))).status_code == 401


async def test_foreign_signature_is_401(setup):
    idp, _, client, _ = setup
    rogue = _SigningKey("fake-key-1", rsa.generate_private_key(public_exponent=65537, key_size=2048))
    now = int(idp.clock())
    token = idp.sign({"iss": idp.issuer, "sub": "u", "aud": AUDIENCE, "exp": now + 60, "scope": "svc-projects-read"}, key=rogue)
    assert (await client.get("/data", headers=bearer(token))).status_code == 401


@pytest.mark.parametrize("alg", ["none", "HS256"])
async def test_algorithm_confusion_is_refused(setup, alg):
    idp, _, client, _ = setup
    now = int(idp.clock())
    claims = {"iss": idp.issuer, "sub": "u", "aud": AUDIENCE, "exp": now + 60, "scope": "svc-projects-read"}
    if alg == "none":
        token = jwt.encode(claims, key=None, algorithm="none", headers={"kid": "fake-key-1"})
    else:
        # The classic attack: sign with HMAC using the PUBLIC key as the secret.
        public_pem = idp._keys[0].private.public_key().public_bytes(
            __import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
            __import__("cryptography").hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        import hashlib, hmac, base64, json

        def b64(b):
            return base64.urlsafe_b64encode(b).rstrip(b"=")

        head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "fake-key-1"}).encode())
        body = b64(json.dumps(claims).encode())
        sig = b64(hmac.new(public_pem, head + b"." + body, hashlib.sha256).digest())
        token = (head + b"." + body + b"." + sig).decode()
    response = await client.get("/data", headers=bearer(token))
    assert response.status_code == 401 and "not accepted" in response.json()["detail"]["message"]


async def test_missing_scope_is_403(setup):
    idp, _, client, _ = setup
    token = idp.mint_access_token(audience=AUDIENCE, scope="openid other")
    response = await client.get("/data", headers=bearer(token))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


async def test_all_required_scopes_are_needed(setup):
    idp, _, client, _ = setup
    only_a = idp.mint_access_token(audience=AUDIENCE, scope="a")
    both = idp.mint_access_token(audience=AUDIENCE, scope="a b")
    assert (await client.get("/both", headers=bearer(only_a))).status_code == 403
    assert (await client.get("/both", headers=bearer(both))).status_code == 200


async def test_scope_claim_as_a_list_is_understood(setup):
    idp, _, client, _ = setup
    token = idp.mint_access_token(audience=AUDIENCE, scope="", scp=["svc-projects-read"])
    assert (await client.get("/data", headers=bearer(token))).status_code == 200


async def test_azp_decides_which_extension_may_call(setup):
    idp, _, client, _ = setup
    mine = idp.mint_access_token(azp="ext-demo", audience=AUDIENCE, scope="x")
    other = idp.mint_access_token(azp="ext-other", audience=AUDIENCE, scope="x")
    assert (await client.get("/only-demo", headers=bearer(mine))).status_code == 200
    denied = await client.get("/only-demo", headers=bearer(other))
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "forbidden_client"


async def test_explicit_verifier_and_method_forms(setup):
    idp, _, client, _ = setup
    token = idp.mint_access_token(azp="ext-demo", audience=AUDIENCE, scope="svc-projects-read")
    assert (await client.get("/explicit", headers=bearer(token))).status_code == 200
    assert (await client.get("/method", headers=bearer(token))).status_code == 200
    assert (await client.get("/method")).status_code == 401


async def test_no_installed_verifier_is_a_clear_programming_error():
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(require_scope("a"))])
    async def x():
        return {}

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=True), base_url="http://t")
    with pytest.raises(RuntimeError, match="install"):
        await client.get("/x", headers=bearer("t"))


async def test_jwks_unavailable_is_503_not_401(setup):
    idp, verifier, client, _ = setup

    def refuse(request):
        raise httpx.ConnectError("down")

    verifier.jwks._http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    token = idp.mint_access_token(audience=AUDIENCE, scope="svc-projects-read")
    assert (await client.get("/data", headers=bearer(token))).status_code == 503


async def test_unknown_key_id_triggers_a_refetch_for_rotated_keys(setup):
    idp, verifier, client, _ = setup
    first = idp.mint_access_token(audience=AUDIENCE, scope="svc-projects-read")
    assert (await client.get("/data", headers=bearer(first))).status_code == 200
    assert idp.jwks_requests == 1
    idp.rotate_keys()
    second = idp.mint_access_token(audience=AUDIENCE, scope="svc-projects-read")
    assert (await client.get("/data", headers=bearer(second))).status_code == 200
    assert idp.jwks_requests == 2


async def test_refetches_are_rate_limited_against_random_key_ids():
    clock = FakeClock()
    idp = FakeIdP(clock=clock)
    cache = JWKSCache(idp.jwks_uri, idp.client(), clock=clock, min_refetch=10)
    good = idp.mint_access_token()
    await cache.key_for(good)
    assert idp.jwks_requests == 1
    rogue = _SigningKey("random-kid", rsa.generate_private_key(public_exponent=65537, key_size=2048))
    for _ in range(5):
        with pytest.raises(TokenInvalid):
            await cache.key_for(idp.sign({"x": 1}, key=rogue))
    assert idp.jwks_requests == 1  # attackers cannot make us hammer the IdP
    clock.advance(11)
    with pytest.raises(TokenInvalid):
        await cache.key_for(idp.sign({"x": 1}, key=rogue))
    assert idp.jwks_requests == 2


async def test_jwks_are_cached_for_their_ttl():
    clock = FakeClock()
    idp = FakeIdP(clock=clock)
    cache = JWKSCache(idp.jwks_uri, idp.client(), ttl=100, clock=clock)
    token = idp.mint_access_token()
    for _ in range(3):
        await cache.key_for(token)
    assert idp.jwks_requests == 1
    clock.advance(101)
    await cache.key_for(token)
    assert idp.jwks_requests == 2


async def test_tokens_never_show_up_in_error_messages(setup):
    idp, _, client, _ = setup
    token = idp.mint_access_token(audience="other-api", scope="svc-projects-read")
    response = await client.get("/data", headers=bearer(token))
    assert token not in response.text and token not in str(response.headers)
