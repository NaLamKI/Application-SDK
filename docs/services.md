# Calling other services

An extension calls other services **on behalf of the signed-in person**, with a token that
was exchanged for exactly that service and exactly the scopes the manifest names. The
login token itself never leaves the extension's backend.

```toml
[[services]]
name = "fmis"                 # ext.service("fmis")
audience = "fmis-api"
scopes = ["ext-data-read"]
mode = "user"
```

```python
@api.get("/fields")
async def fields(fmis: ServiceClient = Depends(ext.service("fmis"))):
    response = await fmis.get("/fields")        # path below APPEXT_SERVICE_FMIS_URL
    response.raise_for_status()
    return response.json()
```

`ServiceClient` is a thin `httpx.AsyncClient`: `get`, `post`, `put`, `patch`, `delete`,
`head`, `request`. It differs from a plain client in three ways:

- The **base URL** comes from `APPEXT_SERVICE_<NAME>_URL` (the auth bundle sets it), and a
  request can only go *below* it. An absolute URL to another host is refused: this client
  attaches a bearer token and must not be a way to send one somewhere the manifest never
  named.
- The **token** is fetched per request from a cache or by exchange.
- After a **`401`** it retries **once** with a fresh token (a cached token can expire or be
  revoked between the cache check and the service's check). A second `401` is an answer.

Bodies must be re-sendable (`json=`, `data=`, `content=` as bytes or text) – a one-shot
stream could not be repeated after a `401`.

## The two modes

| Mode | Token | Use for |
|---|---|---|
| `user` | **Token exchange** (RFC 8693): the session's own access token is the subject token; the request carries `audience` and `scope`; the extension authenticates as a client. The result is cached per session, audience and scope set until shortly before it expires. | Everything on behalf of a person. Needs a session. |
| `service` | **Client credentials** of the extension's service account, cached per audience. No person in the token. | Background work and exports. The target service does not see the person – pass them explicitly if it matters (`{"requested_by": user.sub}`). |

Outside a request (a background task) use `ext.service_client("export")` – for a `user`
service you must pass the session: `ext.service_client("fmis", session)`.

## What Keycloak requires – and what the store does for you

1. The **subject token** must be the extension's own. It is.
2. **Audiences come from client scopes.** Each scope of a service carries an *audience
   mapper* for that service; without it the token says `aud: account` and the service rejects
   it. The store (or `appext keycloak export` locally) creates the scopes with their mappers.
3. The `scope` parameter selects **optional** scopes of the extension's client.
4. **Consent applies to exchanges.** An exchange passes only for scopes the person already
   agreed to. That is why the SDK asks for all `user` service scopes at sign-in: the consent
   screen shows exactly which data in which service the extension may reach, and later
   exchanges cannot fail for lack of consent.

## `ConsentRequired`

If an exchange is refused with `access_denied` because a scope was never consented to (the
manifest got a new scope, or the person revoked the consent in the Keycloak account console),
the SDK raises `appext.ConsentRequired(missing_scopes)`. In a request the SDK turns it into

```json
HTTP 401
{"error": "consent_required", "missing_scopes": ["ext-stock-read"], "login_url": "/auth/login?return_to=/stock&scope=ext-stock-read"}
```

and `extFetch` follows the `login_url`: a new sign-in that asks for the missing scopes, so
Keycloak shows the consent screen for exactly those. Other failures of the exchange become
`502 {"error": "upstream_auth_failed"}` (details are logged, not shown: the browser cannot
act on them). Handle `ConsentRequired` yourself only in background code.

## Writing a target service

A service in Python checks incoming tokens with the same library, so the rules cannot drift
apart. The exchanged token has the service's client id in `aud`, the granted scopes in
`scope` and the calling extension in `azp`.

```python
from fastapi import Depends, FastAPI
from appext.verify import Principal, TokenVerifier, current_principal, require_azp, require_scope

verifier = TokenVerifier(
    issuer="https://sso.example.com/realms/fmis",
    audience="projekt-api",                                    # this service's own client id
    jwks_url="https://sso.example.com/realms/fmis/protocol/openid-connect/certs",
)
app = FastAPI()
verifier.install(app)

@app.get("/v1/projekte", dependencies=[Depends(require_scope("svc-projekte-lesen"))])
async def projekte(principal: Principal = Depends(current_principal)):
    return load_projects(owner=principal.sub)

@app.delete("/v1/projekte/{id}", dependencies=[Depends(require_scope("svc-projekte-schreiben")),
                                              Depends(require_azp("ext-projektauswertung"))])
async def remove(id: str): ...                                  # only this extension, only with this scope
```

Checked: signature (JWKS, refetched on an unknown `kid`), `iss`, `exp`, the own client id in
`aud`, and then the scopes you require (all of them). Answers: no or bad token → `401` with
`WWW-Authenticate: Bearer`; a valid token without the scope → `403 insufficient_scope`; the
wrong extension → `403 forbidden_client`; unreachable JWKS → `503` (the token may be fine,
we cannot tell). `Principal` carries `sub`, `azp`, `scopes` and the raw `claims`.

Pass `verifier=` to `require_scope` / `require_azp` instead of installing one on the app if
you run several verifiers. The service must be registered in the store's **service
catalog** (`PUT /store/services/{audience}`, role `store-admin`) with its scopes, consent
texts and base URLs – that is what makes a scope something an extension can ask for.

The FMIS API is such a service, built in: its scopes (`ext-data-read`, `ext-stock-read`) and
the FMIS permissions behind them live in `backend/app/auth/extensions.json`. Its decision is
the **intersection** of the person's role and the scope's permissions – an extension can
never do more than the person, and never more than its scope says. The actions `manage`, `submit` and
`delete` and the whole access-rights area can never be granted to an extension scope
(deleting sets a tombstone that syncs to every device – the last thing a foreign web page
should be able to do casually); and an extension token is refused on `/me`, onboarding and
account deletion.
