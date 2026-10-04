"""Refresh under a lock, expiry, token exchange, client credentials."""
from __future__ import annotations

import asyncio

import pytest

from appext.core import ConsentRequired, ExchangeFailed, SessionInvalid, UpstreamError

from .conftest import Browser, Env


async def signed_in(env: Env, user: str = "u-1", **kw) -> str:
    b = Browser(env)
    try:
        response = await b.sign_in(user, **kw)
        assert response.status_code == 302, response.text
        return b.http.cookies[env.ext.settings.session_cookie_name]
    finally:
        await b.aclose()


# -- refresh ---------------------------------------------------------------------------------------------


async def test_a_fresh_session_is_not_refreshed(env: Env):
    session_id = await signed_in(env)
    await env.ext.core.fresh_session(session_id)
    assert env.idp.refresh_count == 0


async def test_refresh_happens_shortly_before_expiry(env: Env):
    session_id = await signed_in(env)
    before = (await env.ext.store.load_session(session_id)).access_token
    env.clock.advance(env.idp.access_lifetime - 10)  # inside the 30 s skew
    session = await env.ext.core.fresh_session(session_id)
    assert env.idp.refresh_count == 1
    assert session.access_token != before
    # The new token and rotated refresh token are what is stored now.
    stored = await env.ext.store.load_session(session_id)
    assert stored.access_token == session.access_token and stored.refresh_token == session.refresh_token


async def test_two_concurrent_callers_cause_exactly_one_refresh(tmp_path):
    """Refresh-token rotation destroys the session if the old token is presented twice; the lock prevents it."""
    from .conftest import build_env

    env = build_env(tmp_path, token_delay=0.05)
    session_id = await signed_in(env)
    env.clock.advance(env.idp.access_lifetime - 5)
    first, second, third = await asyncio.gather(*(env.ext.core.fresh_session(session_id) for _ in range(3)))
    assert env.idp.refresh_count == 1
    assert first.access_token == second.access_token == third.access_token
    # And the session is still alive afterwards (the IdP would have ended it on a reuse).
    env.clock.advance(env.idp.access_lifetime - 5)
    await env.ext.core.fresh_session(session_id)
    assert env.idp.refresh_count == 2


async def test_without_the_lock_the_idp_would_destroy_the_session(env: Env):
    """Proves the test above is meaningful: a naive double refresh does kill the session."""
    session_id = await signed_in(env)
    session = await env.ext.store.load_session(session_id)
    token = session.refresh_token
    env.clock.advance(env.idp.access_lifetime - 5)
    first = await env.ext.core._token_request({"grant_type": "refresh_token", "refresh_token": token})
    assert first["access_token"]
    from appext.core import TokenEndpointError

    with pytest.raises(TokenEndpointError) as err:
        await env.ext.core._token_request({"grant_type": "refresh_token", "refresh_token": token})
    assert err.value.error == "invalid_grant"
    assert session.sid not in env.idp.sessions  # the IdP ended the SSO session on the reuse


async def test_invalid_grant_ends_the_session(env: Env):
    session_id = await signed_in(env)
    session = await env.ext.store.load_session(session_id)
    env.idp.sessions.pop(session.sid)  # the SSO session ended at the IdP (logout elsewhere, idle timeout …)
    env.clock.advance(env.idp.access_lifetime)
    with pytest.raises(SessionInvalid):
        await env.ext.core.fresh_session(session_id)
    assert await env.ext.store.load_session(session_id) is None


async def test_other_refresh_failures_keep_the_session(env: Env):
    session_id = await signed_in(env)
    env.clock.advance(env.idp.access_lifetime)

    async def boom(*a, **k):
        raise UpstreamError("down")

    original = env.ext.core._token_request
    env.ext.core._token_request = boom
    with pytest.raises(UpstreamError):
        await env.ext.core.fresh_session(session_id)
    env.ext.core._token_request = original
    assert await env.ext.store.load_session(session_id) is not None  # a hiccup at the IdP is not a logout
    await env.ext.core.fresh_session(session_id)  # and it works again afterwards


async def test_unknown_session_is_invalid(env: Env):
    with pytest.raises(SessionInvalid):
        await env.ext.core.fresh_session("no-such-session")


async def test_roles_follow_the_refreshed_access_token(env: Env):
    session_id = await signed_in(env)
    env.idp.users["u-1"].realm_roles.append("auditor")
    env.clock.advance(env.idp.access_lifetime)
    session = await env.ext.core.fresh_session(session_id)
    assert session.realm_roles == ["analyst", "auditor"]


# -- token exchange --------------------------------------------------------------------------------------


async def test_exchange_request_has_exactly_the_shape_of_the_concept(env: Env):
    session_id = await signed_in(env)
    session = await env.ext.store.load_session(session_id)
    token = await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    (request,) = env.idp.token_requests("urn:ietf:params:oauth:grant-type:token-exchange")
    form = dict(request.form)
    assert form.pop("client_assertion") and form.pop("client_assertion_type") and form.pop("client_id") == "ext-demo"
    assert form == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": session.access_token,  # the session's OWN access token
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "projects-api",
        "scope": "svc-projects-read",
    }
    assert request.authenticated_with == "private_key_jwt"
    import jwt

    claims = jwt.decode(token.token, options={"verify_signature": False})
    assert claims["aud"] == ["projects-api"] and claims["azp"] == "ext-demo" and claims["sub"] == "u-1"
    assert claims["scope"] == "svc-projects-read"


async def test_exchanged_tokens_are_cached_per_audience_and_scope_set(env: Env):
    session_id = await signed_in(env)
    exchange = "urn:ietf:params:oauth:grant-type:token-exchange"
    a = await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    b = await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    assert a.token == b.token and len(env.idp.token_requests(exchange)) == 1
    await env.ext.core.exchange_token(session_id, "fmis-api", ["ext-data-read"])  # another audience
    assert len(env.idp.token_requests(exchange)) == 2
    await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read", "ext-data-read"])  # another scope set
    assert len(env.idp.token_requests(exchange)) == 3
    other = await signed_in(env)  # another session never shares the cache
    c = await env.ext.core.exchange_token(other, "projects-api", ["svc-projects-read"])
    assert c.token != a.token and len(env.idp.token_requests(exchange)) == 4


async def test_cache_is_used_until_shortly_before_expiry(env: Env):
    session_id = await signed_in(env)
    exchange = "urn:ietf:params:oauth:grant-type:token-exchange"
    await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    env.clock.advance(env.idp.access_lifetime - 60)
    await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    assert len(env.idp.token_requests(exchange)) == 1
    env.clock.advance(40)  # now inside the 30 s skew of the cached token
    await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    assert len(env.idp.token_requests(exchange)) == 2


async def test_exchange_refreshes_the_login_token_first_when_needed(env: Env):
    session_id = await signed_in(env)
    env.clock.advance(env.idp.access_lifetime - 5)
    await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    assert env.idp.refresh_count == 1


@pytest.mark.parametrize("consent_error", ["invalid_scope", "access_denied"])
async def test_missing_consent_raises_consent_required_with_the_scopes(tmp_path, consent_error):
    """Keycloak 26.5 answers invalid_scope ("Missing consents ..."), the concept says access_denied: both mean consent."""
    from .conftest import build_env

    env = build_env(tmp_path, consent_error=consent_error)
    session_id = await signed_in(env)
    env.idp.consents[("u-1", "ext-demo")].discard("svc-projects-read")  # e.g. revoked in the account console
    with pytest.raises(ConsentRequired) as err:
        await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    assert err.value.missing_scopes == ("svc-projects-read",)


async def test_an_invalid_scope_that_is_not_about_consent_is_a_failure_not_a_consent_prompt(env: Env):
    session_id = await signed_in(env)
    with pytest.raises(ExchangeFailed) as err:
        await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read", "no-such-scope"])
    assert err.value.error == "invalid_scope"


async def test_a_scope_the_login_never_asked_for_needs_consent(env: Env):
    session_id = await signed_in(env)
    with pytest.raises(ConsentRequired) as err:
        await env.ext.core.exchange_token(session_id, "export-api", ["svc-export-write"])
    assert err.value.missing_scopes == ("svc-export-write",)


async def test_other_exchange_errors_are_not_consent_errors(env: Env):
    session_id = await signed_in(env)
    env.idp.clients["ext-demo"].token_exchange_enabled = False
    with pytest.raises(ExchangeFailed) as err:
        await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    assert err.value.error == "unauthorized_client"


async def test_unknown_audience_is_an_exchange_failure(env: Env):
    session_id = await signed_in(env)
    with pytest.raises(ExchangeFailed):
        await env.ext.core.exchange_token(session_id, "nope-api", ["svc-projects-read"])


async def test_a_revoked_login_token_is_renewed_once_and_retried(env: Env):
    session_id = await signed_in(env)
    session = await env.ext.store.load_session(session_id)
    # The IdP stops accepting the login token as subject (it was revoked); a refresh produces a good one.
    original_exchange = env.idp._grant_exchange
    calls = {"n": 0}

    def flaky(client, form):
        calls["n"] += 1
        if calls["n"] == 1:
            return env.idp._error("invalid_token", "Invalid token")
        return original_exchange(client, form)

    env.idp._grant_exchange = flaky
    token = await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    assert token.token and calls["n"] == 2 and env.idp.refresh_count == 1
    assert (await env.ext.store.load_session(session_id)).access_token != session.access_token


# -- client credentials ----------------------------------------------------------------------------------


async def test_client_credentials_for_service_mode(env: Env):
    token = await env.ext.core.service_token("export-api", ["svc-export-write"])
    (request,) = env.idp.token_requests("client_credentials")
    assert request.form["scope"] == "svc-export-write"
    assert request.authenticated_with == "private_key_jwt"
    import jwt

    claims = jwt.decode(token.token, options={"verify_signature": False})
    assert claims["sub"] == "service-account-ext-demo" and claims["aud"] == ["export-api"]


async def test_client_credentials_are_cached_per_audience(env: Env):
    a = await env.ext.core.service_token("export-api", ["svc-export-write"])
    b = await env.ext.core.service_token("export-api", ["svc-export-write"])
    assert a.token == b.token and len(env.idp.token_requests("client_credentials")) == 1
    env.clock.advance(env.idp.access_lifetime)
    c = await env.ext.core.service_token("export-api", ["svc-export-write"])
    assert c.token != a.token and len(env.idp.token_requests("client_credentials")) == 2


async def test_client_credentials_failure_is_an_exchange_failure(env: Env):
    env.idp.clients["ext-demo"].service_account = False
    with pytest.raises(ExchangeFailed):
        await env.ext.core.service_token("export-api", ["svc-export-write"])


async def test_bypass_cache_gets_a_new_token(env: Env):
    session_id = await signed_in(env)
    a = await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"])
    env.clock.advance(1)
    b = await env.ext.core.exchange_token(session_id, "projects-api", ["svc-projects-read"], bypass_cache=True)
    assert a.token != b.token
