# Testing

Unit tests of an extension need neither Keycloak nor the services it calls. `appext.testing`
gives you a signed-in test client, mocks for the target services and – for the SDK's own
tests and for anyone who wants to test the sign-in itself – an identity provider in a box.
It needs `respx` for the service mocks (`pip install "appext[test]"`).

```python
from appext.testing import ExtensionTestClient, service_mocks, test_user
from app.main import app, ext


def test_fields():
    client = ExtensionTestClient(app, user=test_user(sub="u-1", roles=["analyst"]))
    with service_mocks(ext) as mocks:
        mocks.get("fmis", "/fields").respond(json=[{"id": "f1", "name": "North"}])
        response = client.get("/api/fields")
    assert response.status_code == 200
    assert client.token_requests == [("fmis", "fmis-api", ("ext-data-read",), "user")]
    client.close()
```

## `ExtensionTestClient`

A `starlette.testclient.TestClient` for an extension, signed in as the test person:

- The **session is created directly** in the extension's store – no sign-in round trip.
- **Writing requests carry the CSRF header and a matching `Origin`** by default. Pass your
  own `headers=` to test what happens without them (`403 csrf_rejected`).
- **Token exchanges are answered with a placeholder** (`test-token-<audience>`) and recorded
  in `client.token_requests` as `(service, audience, scopes, mode)` – so a test can assert
  that the extension asks for the right scopes without any Keycloak.
- `ExtensionTestClient(app)` (no user) is anonymous: use it to check the `401`/redirect
  behaviour. `follow_redirects` is on by default in Starlette's client; pass
  `follow_redirects=False` to look at the redirect to `/auth/login`.
- Call `close()` when done (it puts the real token source back).

`test_user(sub="…", name=…, email=…, roles=[…], client_roles=[…], scopes=[…])` describes the
person. `roles` are realm roles; `name=None` tests the "no profile scope" case.

## `service_mocks(ext)`

A `respx` router that resolves `(service name, path)` against the configured base URLs:
`mocks.get("fmis", "/fields")`, `.post`, `.put`, `.delete`, `.route(method, …)`. Whatever is
**not** mocked fails the test instead of reaching the network. Use `.respond(json=…,
status_code=…)` as in respx.

In the `local` environment the FMIS API's base URL has a default; for other services the
test needs `APPEXT_SERVICE_<NAME>_URL` – set it in `tests/conftest.py` **before** the app is
imported (the template's `conftest.py` also clears every `APPEXT_*` variable of the shell,
so tests do not depend on your deployment settings).

## Pages, not only APIs

For `ext.pages()` routes the same client works; look at the HTML or at the redirect:

```python
def test_page_redirects_without_session():
    r = ExtensionTestClient(app).get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/auth/login")
```

## `FakeIdP` and `FakeClock`

`FakeIdP` is an in-process OIDC provider that behaves like Keycloak where it matters: strict
redirect URIs, PKCE, consent bookkeeping (a refused consent, a revoked consent), refresh-token
rotation and "session not active", the token exchange (including "scope not consented"),
client credentials, `private_key_jwt` and secret authentication, and the back-channel logout
sender. It records what the SDK sent (`idp.requests`, `idp.token_requests("…")`) so a test can
assert the **shape** of a token request.

```python
idp = FakeIdP(issuer="https://idp.test/realms/test", clock=clock)
idp.add_user("u-1", realm_roles=["analyst"])
idp.add_client("ext-demo", redirect_uris={"https://demo.apps.test/auth/callback"},
               default_scopes={"ext-data-read"}, scope_audiences={"ext-data-read": "fmis-api"})
http = idp.client()                      # an httpx.AsyncClient whose transport is the IdP
callback_url = idp.authorize(authorize_url, "u-1")      # plays the browser: login + consent
```

`FakeClock` is a clock you move by hand (`clock.advance(301)`): hand the same instance to the
extension and the IdP to test refresh and expiry without sleeping. `idp.mint_access_token(...)`
signs a token as the IdP would – handy for testing a target service built with
`appext.verify`:

```python
token = idp.mint_access_token(sub="u-1", azp="ext-demo", audience="projekt-api", scope="svc-projekte-lesen")
response = client.get("/v1/projekte", headers={"Authorization": f"Bearer {token}"})
```

The SDK's own tests (`sdk/tests/lib/`) are the best examples of full sign-in flows.

## Conformance, not per extension

The 14-case test matrix of `concepts/extensions-sso.md` runs **once** against the SDK as a
conformance test, not per extension. What you test is your extension's behaviour. What only a
device proves – the WebView, the system auth sheet, Android – is listed there as "Gerät".

## Testing the CLI-created project

```sh
pytest                                   # the project's tests, no network
appext manifest check --scan-secrets     # the manifest rules and a tripwire for committed keys
```

Put both in CI. `appext manifest check` exits `1` on a broken manifest and on any finding of
the scan; the scan never prints a secret's value.
