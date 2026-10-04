"""Cross-cutting guarantees: cheap import, no secrets in logs, public API surface."""
from __future__ import annotations

import logging
import subprocess
import sys

import respx

from .conftest import Browser, Env


def test_importing_the_package_is_fast_and_pulls_in_no_framework():
    code = (
        "import sys, time; t = time.perf_counter(); import appext; dt = time.perf_counter() - t; "
        "heavy = [m for m in ('fastapi', 'starlette', 'httpx', 'jwt', 'cryptography', 'redis') if m in sys.modules]; "
        "print(dt, ','.join(heavy))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    seconds, _, heavy = done.stdout.strip().partition(" ")
    assert float(seconds) < 0.05, f"import took {seconds}s"
    assert heavy == "", f"import appext pulled in: {heavy}"


def test_the_public_names_resolve_lazily():
    import appext

    for name in appext.__all__:
        assert getattr(appext, name) is not None, name
    assert "Extension" in dir(appext)
    try:
        appext.nonexistent  # noqa: B018
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown names must raise AttributeError")


def test_version_matches_the_package_metadata():
    from importlib.metadata import version

    import appext

    assert appext.__version__ == version("appext") == "0.1.0"


async def test_no_secret_is_ever_logged(env: Env, caplog):
    """A login, a refresh, an exchange, a logout and a failure: every token and key stays out of the logs."""
    caplog.set_level(logging.DEBUG)
    browser = Browser(env)
    try:
        await browser.sign_in("u-1")
        session_id = browser.http.cookies[env.ext.settings.session_cookie_name]
        session = await env.ext.store.load_session(session_id)
        with respx.mock() as router:
            router.get("https://projects.test/api/v1/projects").respond(json=[])
            await browser.http.get("/api/projects")
        env.clock.advance(env.idp.access_lifetime)
        await env.ext.core.fresh_session(session_id)
        refreshed = await env.ext.store.load_session(session_id)
        env.idp.clients["ext-demo"].token_exchange_enabled = False
        await browser.http.get("/api/projects", headers={"Referer": "https://demo.apps.test/"})
        await browser.http.get("/auth/logout")
        secrets_seen = [
            session.access_token, session.refresh_token, session.id_token, refreshed.access_token, refreshed.refresh_token,
            session_id, env.private_key_pem.splitlines()[1],
        ]
        everything = caplog.text + "\n".join(repr(r.__dict__) for r in caplog.records)
        for secret in secrets_seen:
            assert secret and secret not in everything
        assert "BEGIN PRIVATE KEY" not in everything
    finally:
        await browser.aclose()


def test_settings_do_not_leak_secrets_into_exceptions(tmp_path):
    from appext.config import ConfigError, ExtensionSettings
    from appext.manifest import loads_manifest

    from .conftest import MANIFEST

    key = tmp_path / "key"
    key.write_text("-----BEGIN PRIVATE KEY-----\nSUPERSECRETKEYMATERIAL\n-----END PRIVATE KEY-----\n")
    try:
        ExtensionSettings.from_env(loads_manifest(MANIFEST), {"APPEXT_ENV": "prod", "APPEXT_CLIENT_KEY_FILE": str(key)})
    except ConfigError as err:
        assert "SUPERSECRETKEYMATERIAL" not in str(err)
    else:  # pragma: no cover
        raise AssertionError("a deployment without the rest of the configuration must fail")


def test_the_client_key_must_be_a_private_key_and_a_supported_one(tmp_path):
    import pytest

    from appext.config import ConfigError, Secret
    from appext.core import load_private_key
    from appext.testing import generate_client_key

    for alg, expected in (("RS256", "RS256"), ("ES256", "ES256")):
        pem, jwk = generate_client_key(alg)
        key, found, kid = load_private_key(Secret(pem))
        assert found == expected and kid is None
        assert load_private_key(Secret(pem), "my-kid")[2] == "my-kid"
    pem, jwk = generate_client_key("ES256")
    with pytest.raises(ConfigError, match="not a usable private key"):
        from cryptography.hazmat.primitives import serialization

        public_pem = serialization.load_pem_private_key(pem.encode(), None).public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        load_private_key(Secret(public_pem))
    with pytest.raises(ConfigError):
        load_private_key(Secret("garbage"))
    with pytest.raises(ConfigError):
        load_private_key(None)
    with pytest.raises(ConfigError, match="RSA and EC"):
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization

        ed = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        load_private_key(Secret(ed))


def test_a_jwk_formatted_private_key_with_kid_sets_the_header_kid():
    import json

    import jwt

    from appext.config import Secret
    from appext.core import load_private_key

    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key))
    jwk["kid"] = "key-2026-10"
    loaded, alg, kid = load_private_key(Secret(json.dumps(jwk)))
    assert alg == "RS256" and kid == "key-2026-10"
    token = jwt.encode({"a": 1}, loaded, algorithm=alg, headers={"kid": kid})
    assert jwt.get_unverified_header(token)["kid"] == "key-2026-10"
