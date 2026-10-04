# Sign-in, session and logout

The SDK implements the **backend-for-frontend** pattern: tokens exist only on the server;
the browser (or the app's WebView) holds one cookie with a random session id. The flow is
the authorization-code flow with PKCE that `concepts/extensions-sso.md` describes; an
extension developer implements none of it.

## Routes the SDK adds

| Path | What it does |
|---|---|
| `GET /auth/login?return_to=/path` | Starts a sign-in: creates `state`, `nonce` and a PKCE verifier, sets the transaction cookie, redirects to Keycloak. Always a **top-level navigation**, never a `fetch`. |
| `GET /auth/callback` | Redeems the code, validates the ID token, creates the session, redirects to `return_to`. |
| `GET`/`POST /auth/logout?return_to=/&sso=false` | Ends this extension's session. `sso=true` also ends the Keycloak session. |
| `POST /auth/backchannel-logout` | Keycloak tells the extension that a session ended; see below. |
| `/_sdk/info`, `/_sdk/icon`, `/_sdk/client.js`, `/_sdk/bridge.js` | Metadata and frontend helpers. |
| `/healthz`, `/readyz` | Liveness; readiness checks the Keycloak discovery and the session store. |

`/auth` and `/_sdk` are reserved; neither your routes nor the static fallback answer there.

## Browser mode and app mode

The same routes serve a normal browser and the app's WebView. The WebView appends
`FMIS-App-WebView/…` to its user agent. If the marker is present, **this sign-in
transaction** uses the redirect URI `APPEXT_APP_REDIRECT_URI`
(`org.agrifooddata.apps.fmis.web:/callback`, the same for every extension); otherwise it
uses `<APPEXT_PUBLIC_URL>/auth/callback`. Nothing else differs. The marker is not a security
feature: whoever forges it gets a redirect URI their own browser cannot open.

In the app, the sign-in works like this: the WebView loads the extension, the extension
redirects to Keycloak, the app intercepts exactly that request (client id, redirect URI and
`response_type=code` must match the catalog entry) and runs it in the system's auth sheet,
where the Keycloak session of the app already exists. The callback URL is handed back to the
WebView, which loads it. The person typically sees only the sheet flash, and a consent
screen once. The extension sees an ordinary sign-in and needs to know nothing of this –
except:

- **`response_mode=query`.** `form_post` would never arrive over a custom scheme. The SDK
  sets it.
- **`app_sub`.** The app sets a cookie `app_sub` with the account's `sub` before loading the
  extension. If the ID token's `sub` differs (another person is signed in at the system
  browser), the SDK throws the tokens away and restarts the sign-in with `prompt=login`.
- **`error=access_denied`** (consent refused *or* sheet closed) is shown as a page that
  explains both and offers to try again.

## Scopes at sign-in

The authorization request asks for `openid`, every `[consent]` scope and the scopes of every
`mode = "user"` service. A caller of `/auth/login?scope=…` can ask for more only from what
the manifest declares – never beyond.

## The session

- **Cookie:** `__Host-ext_session` – HttpOnly, Secure, SameSite=Lax, `Path=/`, no `Domain`.
  The value is 256 bits of randomness and carries no data. On plain `http://127.0.0.1` or
  `localhost` (development) it is called `ext_session_<port>` (`ext_session_8100`) without
  `Secure`, because `__Host-` requires `Secure`; the port is in the name because cookies are
  scoped by host, not by port – two extensions on one machine would otherwise sign each other
  out. The transaction cookie is `__Host-ext_tx` / `ext_tx_<port>`.
- **Record in the store:** Keycloak `sid`, `sub`, ID-token claims, access and refresh token,
  granted scopes, roles, and the cache of exchanged tokens. Tokens are encrypted with AES-GCM
  (`APPEXT_SESSION_KEY_FILE`); a stolen Redis dump holds no token.
- **A new sign-in never inherits a session id** (no session fixation): the old record is
  deleted when the callback creates the new one.
- **Refresh:** the access token is renewed on the server shortly before it expires, **under a
  per-session lock**. With refresh-token rotation, two parallel refreshes would present an
  already used token and Keycloak would end the whole session; the lock plus a re-read after
  acquiring it makes "two callers, one refresh" the only outcome.
- **End of session:** if the refresh fails with `invalid_grant`, the session is invalid. An
  API route answers `401`, a page redirects to the sign-in; in the app the person notices
  at most the auth sheet.

## Protecting routes

```python
api = ext.router(prefix="/api")     # JSON API: no session -> 401 {"error":"unauthenticated","login_url":"/auth/login?return_to=…"}
pages = ext.pages()                 # HTML pages: no session -> 302 to /auth/login
```

An API cannot start a sign-in with `fetch`, and a redirect would lose the app's chance to
intercept; so the API answers `401` with a `login_url`, and the frontend helper navigates
there as a whole page:

```js
import { extFetch } from "/_sdk/client.js";
const response = await extFetch("/api/projects");   // 401 -> sign-in -> back to this page
```

`extFetch` is `fetch` that sends the CSRF header and `credentials: "same-origin"`, follows a
`401` with a `login_url` that points into `/auth/` by navigating the top-level window (with
`return_to` set to the current page), and stops after three such navigations within 30
seconds so a broken setup does not loop forever (it then returns the `401`). Static HTML is
served only with a session unless you pass `ext.asgi(public_pages=True)`.

In handlers:

```python
user: User = Depends(ext.current_user)        # sub, name, email, preferred_username, roles, scopes
user: User = Depends(ext.require_role("analyst"))   # 403 unless the person has the realm or client role
```

`name` and `email` are `None` unless the granted scopes include them; extensions get no
profile by default. `user.roles` merges the realm roles and the roles of the extension's own
client; `user.has_role(...)` does not care which. These are for decisions **inside the
extension**; what a person may do in the FMIS data is decided by the FMIS API from the
exchanged token's scopes ([services.md](services.md)).

## `return_to`

Only a **path on the extension's own origin** is accepted: one leading `/`, no `//`, no
backslash, no control character (browsers drop tabs and newlines and read what follows as a
host), no scheme, never a path under `/auth` (it would only start the next sign-in).
Everything else becomes `/`. The same function guards `/auth/logout`.

## Logout

- **`/auth/logout`** ends only the extension's session. The app's Keycloak session stays:
  closing one extension must not sign the person out of the app.
- **`/auth/logout?sso=true`** also redirects to Keycloak's end-session endpoint (with
  `id_token_hint`).
- **App logout** ends the Keycloak session. Keycloak then sends a **back-channel logout** to
  every extension with a session: a POST with a `logout_token` to `/auth/backchannel-logout`.
  The SDK validates it (signature, `iss`, `aud`, the `backchannel-logout` event claim, **no**
  `nonce`, `sid` or `sub`) and deletes **all sessions of that `sid`** – the store keeps an
  index from `sid` to session ids. The endpoint is exempt from the CSRF check because it is
  server to server.

## CSRF

Cookies are `SameSite=Lax`, which already keeps cross-site POSTs from carrying the session.
The second layer is aimed at what matters with one subdomain per extension – a *sibling* site
is same-site:

- Writing requests (`POST`, `PUT`, `PATCH`, `DELETE`) under your `ext.router()` prefix need
  the header **`X-Appext-CSRF`** (any value; `extFetch` sets it). A cross-origin page cannot
  set a custom header without a CORS preflight, which the SDK never grants.
- If an `Origin` header is present, it must be the extension's own origin; a cross-site
  `Sec-Fetch-Site` without `Origin` is refused.
- Routes of `ext.pages()` (plain HTML forms cannot set headers): `Origin` must be the own
  origin, or absent with `Sec-Fetch-Site` saying same-origin or none.
- Refusals are `403 {"detail": {"code": "csrf_rejected", …}}`.

## Configuration of the sign-in

`APPEXT_ISSUER`, `APPEXT_CLIENT_ID`, `APPEXT_CLIENT_AUTH`, `APPEXT_CLIENT_KEY_FILE`,
`APPEXT_PUBLIC_URL`, `APPEXT_APP_REDIRECT_URI`, `APPEXT_SESSION_*` – the full table is in
[deployment.md](deployment.md). The client authenticates with `private_key_jwt` (an RS256 or
ES256 assertion with `iss = sub = client_id`, `aud` = the token endpoint, a `jti` and a short
expiry) or, if the operator allows it, a client secret.
