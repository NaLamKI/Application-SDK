# Testing

For extension developers writing unit tests, and for anyone testing the sign-in or a target service with the SDK's fakes.

Unit tests of an extension need neither an OAuth service nor the services it calls. `appext.testing`
gives you a test environment that is the same on every machine, a signed-in test client, mocks for the
target services and – for the SDK's own tests and for anyone who wants to test the sign-in itself – an
identity provider in a box. It needs `respx` for the service mocks (`pip install "appext[test]"`).

```python
# tests/conftest.py – runs before the tests import app.main
from pathlib import Path
from appext.testing import configure_test_environment

configure_test_environment(Path(__file__).resolve().parent.parent / "extension.toml")
```

```python
# tests/test_api.py
from appext.testing import ExtensionTestClient, service_mocks, test_user
from app.main import app, ext


def test_items():
    client = ExtensionTestClient(app, user=test_user(sub="u-1", roles=["analyst"]))
    with service_mocks(ext) as mocks:
        mocks.get("data", "/items").respond(json=[{"id": "i-1", "name": "First"}])
        response = client.get("/api/items")
    assert response.status_code == 200
    assert client.token_requests == [("data", "data-api", ("data-read",), "user")]
    client.close()
```

## `configure_test_environment`

`app/main.py` reads its settings when it is imported, from the `APPEXT_*` variables of the
environment – and, in the `local` environment, from the project's `appext.toml`. A test run must not
depend on the machine it runs on: a shell that exports a deployment's settings, or a platform the
developer pointed `appext.toml` at, must not change what a test does. Put

```python
configure_test_environment(manifest="extension.toml", *, environ=None, issuer=TEST_ISSUER,
                           app_redirect_uri=TEST_APP_REDIRECT_URI)
```

in `tests/conftest.py`; `appext new` writes exactly that. It must run **before** the app is imported.
It:

1. removes **every** `APPEXT_*` variable from the environment (`environ`, default `os.environ`), so a
   deployment's settings, or `APPEXT_PLATFORM`, in your shell cannot leak in;
2. sets what a local run needs: `APPEXT_ENV=local`, `APPEXT_ISSUER` (default
   `https://idp.test/realms/test`, the address `FakeIdP` answers for), `APPEXT_APP_REDIRECT_URI`
   (default `com.example.testapp:/callback`; pass `app_redirect_uri=None` to test without a host app) and,
   for every service the manifest declares, `APPEXT_SERVICE_<NAME>_URL` – `https://<name>.test/api`
   (underscores in the name become hyphens) – so that `service_mocks` knows where the service "is";
3. returns what it set, as a dict.

The values set here win over a platform file, and nothing is looked up on the network or leaves the
machine. (A local run still reads the project's `appext.toml` – it has to be valid – but the issuer, the
service URLs and the return address are all set explicitly. The exception is `app_redirect_uri=None`:
it sets nothing, so a return address in `appext.toml` stays in effect.) Do not call it from a test of
the CLI or of anything else that needs the real environment. A test that needs a different value sets
it afterwards: `os.environ["APPEXT_…"] = …` before the import, or `monkeypatch` for code that reads the
environment later.

Without it you have to do the same by hand: the SDK has no default issuer, so
`ExtensionSettings.from_env()` fails with `APPEXT_ISSUER is required`, and a service without a URL
fails at its first call.

## `ExtensionTestClient`

A `starlette.testclient.TestClient` for an extension, signed in as the test person:

- The **session is created directly** in the extension's store – no sign-in round trip.
- **Writing requests carry the CSRF header and a matching `Origin`** by default. Pass your
  own `headers=` to test what happens without them (`403 csrf_rejected`).
- **Token exchanges are answered with a placeholder** (`test-token-<audience>`) and recorded
  in `client.token_requests` as `(service, audience, scopes, mode)` – so a test can assert
  that the extension asks for the right scopes without any issuer.
- `ExtensionTestClient(app)` (no user) is anonymous: use it to check the `401`/redirect
  behaviour. `follow_redirects` is on by default in Starlette's client; pass
  `follow_redirects=False` to look at the redirect to `/auth/login`.
- Call `close()` when done (it puts the real token source back).

`test_user(sub="…", name=…, email=…, roles=[…], client_roles=[…], scopes=[…])` describes the
person. `roles` are realm roles; `name=None` tests the "no profile scope" case.

## `service_mocks(ext)`

A `respx` router that resolves `(service name, path)` against the configured base URLs:
`mocks.get("data", "/items")`, `.post`, `.put`, `.delete`, `.route(method, …)`. Whatever is
**not** mocked fails the test instead of reaching the network. Use `.respond(json=…,
status_code=…)` as in respx.

The base URL of each service comes from `APPEXT_SERVICE_<NAME>_URL`, which
`configure_test_environment` sets for every service of the manifest. A service without a URL raises
`KeyError: no URL configured for service …` when you mock it.

## Pages, not only APIs

For `ext.pages()` routes the same client works; look at the HTML or at the redirect:

```python
def test_page_redirects_without_session():
    r = ExtensionTestClient(app).get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/auth/login")
```

## `FakeIdP` and `FakeClock`

`FakeIdP` is an in-process OpenID Connect provider that behaves like Keycloak where it matters (its
endpoints are laid out like Keycloak's, `…/protocol/openid-connect/…`): strict redirect URIs, PKCE,
consent bookkeeping (a refused consent, a revoked consent), refresh-token rotation and "session not
active", the token exchange (including "scope not consented"), client credentials, `private_key_jwt`
and secret authentication, and the back-channel logout sender. It records what the SDK sent
(`idp.requests`, `idp.token_requests("…")`) so a test can assert the **shape** of a token request.

```python
idp = FakeIdP(issuer="https://idp.test/realms/test", clock=clock)
idp.add_user("u-1", realm_roles=["analyst"])
idp.add_client("ext-demo", redirect_uris={"https://demo.apps.test/auth/callback"},
               default_scopes={"data-read"}, scope_audiences={"data-read": "data-api"})
http = idp.client()                      # an httpx.AsyncClient whose transport is the IdP
callback_url = idp.authorize(authorize_url, "u-1")      # plays the browser: login + consent
```

`FakeClock` is a clock you move by hand (`clock.advance(301)`): hand the same instance to the
extension and the IdP to test refresh and expiry without sleeping. `idp.mint_access_token(...)`
signs a token as the IdP would – handy for testing a target service built with
`appext.verify`:

```python
token = idp.mint_access_token(sub="u-1", azp="ext-demo", audience="projects-api", scope="projects-read")
response = client.get("/v1/projects", headers={"Authorization": f"Bearer {token}"})
```

The SDK's own tests (`tests/library/` in this repository) are the best examples of full sign-in flows.

## What is tested once, and what only a device proves

The sign-in flow – redirect URIs, PKCE, consent, refresh, exchange, back-channel logout – is tested
**once**, against `FakeIdP`, in the SDK's own suite (`tests/library/`), not per extension. What you test
is your extension's behaviour. What only a device proves – a WebView, the system auth sheet, a phone –
cannot be covered by unit tests at all; that belongs to the host app's own tests and to a manual run.

## Testing the CLI-created project

```sh
pytest                                   # the project's tests, no network
appext manifest check --scan-secrets     # the manifest rules and a tripwire for committed keys
```

Put both in CI. `appext manifest check` exits `1` on a broken manifest and on any finding of
the scan; the scan never prints a secret's value. To run the SDK's own tests, see the end of the
[README](../README.md#working-on-the-sdk-itself).
