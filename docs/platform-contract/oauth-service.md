# The OAuth service

This document is for the people who run, choose or configure the OAuth 2.0 / OpenID Connect service of a platform (the *issuer*), and for the developers of an App Store who have to provision it: it lists exactly what `appext` – the library inside an extension, the command line and the bridge – sends to that service and what it expects back.

Everything below was checked against the SDK source (`appext.core`, `appext.jwks`, `appext.verify`, `appext.config`, `appext.cli.login`) and its in-process stand-in `appext.testing.FakeIdP`. `FakeIdP` is an executable form of this document: if your service behaves differently from it in a way listed here, extension tests pass and production fails.

The words MUST, SHOULD and MAY are used as in RFC 2119. Statements about "the reference implementation" describe a choice that one concrete platform made; they are not part of the contract.

## 1. Who talks to the service

| Party | Client type | Grants it uses | Section |
|---|---|---|---|
| **Extension backend** (the library) | confidential client, **one per extension**; id `ext-<extension id>` by convention | authorization code with PKCE, refresh token, token exchange, client credentials (only for `mode = "service"` services) | 3 – 9 |
| **Command line** (`appext store …`) | public client, default id `appext-cli` | device authorization with PKCE, refresh token | 11 |
| **Host app** | its own client; not specified by the SDK | its own sign-in. The silent sign-in of extensions relies on the session this creates at the service | [host-app.md](host-app.md) |
| **Target services** | resource servers, one *audience* each | verify access tokens | 10 |
| **App Store API** | resource server | verifies tokens of the command line (people) and of the host app (catalog) | [app-store-api.md](app-store-api.md) |

An extension never holds a token of the host app, and the host app never holds a token of an extension.

## 2. Discovery, issuer, keys

**Discovery.** The library reads `GET {issuer}/.well-known/openid-configuration` (JSON, `Accept: application/json`, no redirects followed, 10 s timeout by default). It is cached for one hour; if a refresh fails the cached document is used. `/readyz` of an extension forces a fresh fetch, so an unreachable discovery document makes the extension *not ready* (503).

| Member | Needed for | Required |
|---|---|---|
| `issuer` | must equal the configured issuer (a trailing `/` is ignored) | yes |
| `authorization_endpoint` | sign-in of an extension | yes |
| `token_endpoint` | every token request | yes |
| `jwks_uri` | verifying ID tokens, logout tokens, access tokens | yes |
| `end_session_endpoint` | RP-initiated logout (`/auth/logout?sso=true`) | no; without it the logout only ends the extension's own session |
| `device_authorization_endpoint` | `appext store login` | only for the command line |

Other members (`grant_types_supported`, `code_challenge_methods_supported`, `backchannel_logout_supported`, …) are not read. There is no way to configure endpoints without discovery.

**The issuer string.** The issuer is configured as an `http(s)` URL (`APPEXT_ISSUER` in a deployment, `issuer` in the platform file) and normalised by removing trailing slashes. The `iss` claim of every token is compared with that string **exactly**. Consequently:

- `iss` MUST equal the issuer URL without a trailing slash, byte for byte, and the same string must be the `issuer` of the discovery document (modulo a trailing slash there).
- The address the extension uses to reach the service and the address in `iss` are the same string. A service that is reached by an internal name but signs tokens with its public name needs a split-horizon setup, not a different configured issuer.
- `https` is required outside local development (`APPEXT_ENV=local`); `http` is accepted there, or for a loopback host (`127.0.0.1`, `localhost`).

**Signing keys.** Tokens MUST be signed asymmetrically with one of `RS256/384/512`, `PS256/384/512`, `ES256/384/512`. `none` and the HMAC family are refused whatever the token header says. `RS256` is the safe default (see §13 for one component of the reference implementation that accepts nothing else). The library:

- fetches `jwks_uri` on first use and caches it for 10 minutes; a token with an unknown `kid` triggers one refetch, but not more than one per 10 seconds;
- skips keys with `"use": "enc"`;
- needs the `kid` header in tokens, unless the key set holds exactly one key of the right family (RSA or EC);
- checks `exp`, `nbf`, `iat` with its own clock and a 60 s leeway (30 s in `appext.verify`).

## 3. Authorization code flow with PKCE (extension client)

### 3.1 The request

An extension starts every sign-in with a top-level navigation to the `authorization_endpoint`:

```
GET {authorization_endpoint}
  ?response_type=code
  &client_id=ext-projects
  &redirect_uri=https%3A%2F%2Fprojects.apps.example.com%2Fauth%2Fcallback
  &scope=openid%20data-read%20export-write
  &state=<24 random bytes, base64url>
  &nonce=<24 random bytes, base64url>
  &code_challenge=<BASE64URL(SHA256(verifier))>
  &code_challenge_method=S256
  &response_mode=query
  [&prompt=login]            # or prompt=consent
```

| Parameter | Contract |
|---|---|
| `response_type` | always `code` |
| `scope` | `openid`, then every scope of `[consent]`, then every scope of every `[[services]]` entry with `mode = "user"`. A sign-in can add scopes the manifest declares (after a failed exchange, see §6), never others. No `offline_access`, `profile`, `email` or `roles` is requested: an extension gets exactly the claims its scopes carry. |
| `redirect_uri` | the extension's own `{public_url}/auth/callback`, **or** – if the host app's WebView started the sign-in (see [host-app.md](host-app.md)) – the platform's app redirect URI (`com.example.app:/callback`). It is chosen per sign-in transaction; both MUST be registered at the client. |
| `code_challenge(_method)` | PKCE with `S256` on **every** sign-in, although the client is confidential. The verifier is 48 random bytes, base64url. `plain` is never sent. |
| `response_mode` | `query`, set explicitly: `form_post` can never arrive over a custom URI scheme. The service MUST support `query`. |
| `prompt` | only `login` (account mismatch, see [host-app.md](host-app.md)) or `consent`; absent otherwise. |
| `state`, `nonce` | single use; the library keeps them server-side for 10 minutes and binds `state` to a cookie of the browser that started the sign-in. |

The library does not send `ui_locales`, `max_age`, `acr_values`, `login_hint`, `resource` or `claims`.

### 3.2 The response

The browser comes back to the redirect URI with `code` and `state`, and, if the service implements RFC 9207, `iss`. If `iss` is present it MUST equal the issuer, otherwise the sign-in is refused (`issuer_mismatch`).

On failure the service answers with `error` (and `state`). `access_denied` is shown as "you declined, or the sign-in window was closed" – the library cannot tell the two apart and the host app reports a closed authentication sheet as `access_denied` too. Any other error is shown as "the identity provider reported an error (<code>)".

### 3.3 Redeeming the code

```
POST {token_endpoint}
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=authorization_code
&code=<code>
&redirect_uri=<the same redirect_uri as in the request>
&code_verifier=<verifier>
&client_id=ext-projects
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion=<assertion>          # or client_secret=<secret>, see §4
```

The response is a JSON object with HTTP 200:

| Member | Required | Used for |
|---|---|---|
| `access_token` | yes | the *login token*: the subject of later exchanges; roles are read from it |
| `id_token` | yes | identity of the person; `sid`; profile claims |
| `expires_in` | recommended | lifetime of the access token in seconds (absent counts as 0: the token is renewed before every use) |
| `refresh_token` | recommended | without it the session ends when the access token expires |
| `refresh_expires_in` | recommended | bounds the stored session; absent or 0 falls back to the maximum session age (`APPEXT_SESSION_MAX_AGE`, 30 days) |
| `scope` | recommended | the granted scopes; used to name the missing ones when an exchange needs consent |

Errors: HTTP 4xx with `{"error": "...", "error_description": "..."}`. A non-JSON answer is treated as "the identity provider is unavailable". The library never logs tokens, only the `error` code and description.

### 3.4 ID token

Verified: signature (§2), `iss`, `aud` contains the client id, `exp`, `iat`, `sub` present and non-empty, `nonce` equals the sent value. If `aud` is a list with more than one entry, `azp` MUST be the client id.

`sub` MUST be stable for a person and **identical for every client of the service**, including the host app's client: the host app tells the extension who is signed in by its `sub` (cookie `app_sub`), and the extension compares it with the `sub` of its own ID token. Pairwise subject identifiers would make the two differ and send every sign-in into the `prompt=login` loop.

### 3.5 Redirect URIs of an extension client

| Kind | Value | Why |
|---|---|---|
| Web callback | `{public_url}/auth/callback` for **every environment** the extension may run in | browser sign-in |
| App callback | the platform's app redirect URI, e.g. `com.example.app:/callback` | sign-in handed over by the host app |
| Post-logout | `{public_url}/` | RP-initiated logout |
| Back-channel logout | `{public_url}/auth/backchannel-logout` | see §9 |

The service MUST match redirect URIs exactly (see RFC 9700) and MUST NOT let wildcards in. The app callback is the same string for every extension of a platform.

### 3.6 Consent

Extension clients MUST require consent. The consent screen lists the extension's name and the *consent text* of each requested scope (§10). Consent is also what makes later token exchanges possible (§6). Consent given at sign-in covers exchanges for the same scopes; the person can revoke it in the service's own account console, after which the next exchange fails with the consent error of §6 and the library starts a sign-in that asks for the missing scopes again.

## 4. Client authentication

The extension authenticates at the token endpoint on **every** request (authorization code, refresh, exchange, client credentials). Credentials are sent **in the form body**, never in an `Authorization` header:

**`private_key_jwt`** (RFC 7523, the default for extensions):

```
client_id=ext-projects
client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion=<JWT>
```

| Assertion | Value |
|---|---|
| header `alg` | `RS256` for RSA keys; `ES256`, `ES384`, `ES512` for P-256, P-384, P-521 keys |
| header `typ` | `JWT` |
| header `kid` | **only** if the JWK carries one or `APPEXT_CLIENT_KEY_ID` is set; never invented |
| `iss`, `sub` | the client id |
| `aud` | the **token endpoint URL** from discovery (not the issuer). Services that accept only the issuer as `aud` MUST be configured to accept the token endpoint too |
| `iat`, `exp` | now, and now + 60 s |
| `jti` | random, unique per request. The service SHOULD reject a replayed `jti` |

The service holds the **public** key (JWK) of the client. The store registers it through the provisioning step of §12; the private key never leaves the developer's side. One key per client: replacing the key invalidates the old one at once.

**`client_secret`** (only if the platform allows it): `client_id=...&client_secret=...` in the body (`client_secret_post`). The secret is shown once to the developer and never stored by the store.

A service that supports only HTTP Basic authentication for secrets is not compatible; the library does not send it.

## 5. Refresh tokens

```
grant_type=refresh_token&refresh_token=<token>&client_id=...&<client authentication>
```

- The library refreshes when the access token has 30 seconds or less to live, **under a per-session lock**, and once more when the token endpoint refuses the login token during an exchange (§6.3).
- **Rotation is supported, not required.** If the response carries a new `refresh_token` it replaces the old one (and `refresh_expires_in` is re-read). If it carries none, the old one is kept. With rotation, presenting an already used token typically ends the whole session at the service; the lock plus a re-read after acquiring it make that impossible within one extension, across replicas that share the session store.
- `invalid_grant` means *the session at the service has ended*: the extension deletes its session and the person signs in again. Any other error is treated as a temporary upstream failure (HTTP 502 to the browser) and the session is kept.
- The library does **not** request `offline_access`. The lifetime of a session is the lifetime of the person's single sign-on session at the service and of the refresh token; the extension never outlives it.
- Reference implementation: rotation is switched **off** in its realm, because a lost response on a mobile network would strand the sign-in. That is an operator choice; both settings work.

## 6. Token exchange (RFC 8693)

Used for every service with `mode = "user"`: a token for exactly one audience, in the name of the signed-in person.

### 6.1 What the library sends

```
POST {token_endpoint}
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&subject_token=<the session's own access token>
&subject_token_type=urn:ietf:params:oauth:token-type:access_token
&audience=data-api
&scope=data-read%20data-write
&client_id=ext-projects
&<client authentication, §4>
```

| Parameter | Contract |
|---|---|
| `subject_token` | the access token the **same client** got at sign-in or refresh. The service MUST accept a token that was issued to the calling client (`azp`) as subject |
| `subject_token_type` | always `urn:ietf:params:oauth:token-type:access_token` |
| `audience` | the `audience` of the `[[services]]` entry – the identifier of the target service at the issuer. A single string |
| `scope` | all scopes of that entry, space-separated |
| not sent | `requested_token_type`, `resource`, `actor_token`, `actor_token_type` |

The extension's client must have **token exchange enabled**; the service MUST allow it only for clients where it was enabled (the store enables it on every extension client it provisions).

### 6.2 What the library expects back

HTTP 200 with JSON: `access_token` (required) and `expires_in` (recommended; absent counts as 0, so the token is used once and never cached). `token_type`, `issued_token_type`, `scope` and `refresh_token` are ignored. The token is cached per session, audience and scope set until 30 s before it expires.

The returned access token MUST have these properties, because the target service verifies them (§10):

| Claim | Value |
|---|---|
| `iss` | the issuer |
| `aud` | contains the requested `audience` |
| `sub` | the person (the same `sub` as in the login token) |
| `azp` | the extension's client id – target services decide per extension with it |
| `scope` | the granted scopes, space-separated (`scp` as list or string is read as well) |
| `exp` | required |

### 6.3 What the library does with errors

| Answer | Interpretation |
|---|---|
| `access_denied` | **consent missing** → `ConsentRequired` |
| `invalid_scope` whose description contains the word "consent" (any case) | **consent missing** → `ConsentRequired` |
| `invalid_token` or `invalid_grant` | the login token was refused: refresh the session once and retry; if it fails again the session counts as ended (the person signs in again) |
| anything else (plain `invalid_scope`, `invalid_client`, `unauthorized_client`, `invalid_request`, …) | a configuration error → HTTP 502 `upstream_auth_failed` to the browser, details only in the log |

`ConsentRequired` is turned into `401 {"error": "consent_required", "missing_scopes": [...], "login_url": ...}` and the frontend starts a new sign-in that asks for the missing scopes, so the consent screen is shown for exactly those. Two consequences for the service:

- Answer `access_denied` **only** for missing consent. Using it for "client not allowed to exchange" would send the person around a consent loop that cannot help. Use `unauthorized_client` or `invalid_client` for that.
- A scope the client simply does not have is a plain `invalid_scope` without "consent" in its description: a configuration error, not a reason to show a consent screen.

(Keycloak 26.5 answers `invalid_scope` with "Missing consents for Token Exchange in client …" and uses `access_denied` ("Client requires user consent") on other paths. The library accepts both.)

## 7. Client credentials

For services with `mode = "service"` the extension acts as itself:

```
grant_type=client_credentials&scope=export-write&client_id=...&<client authentication>
```

There is **no** `audience` parameter: the audience of the token comes from the scopes (§10). The client needs a service account; the answer needs `access_token` and `expires_in`; the token is cached process-wide per audience and scope set. Failures become HTTP 502 `upstream_auth_failed`.

## 8. Claims the SDK reads

| Claim | In | Used for | Required |
|---|---|---|---|
| `sub` | ID token | identity of the person; account matching with the host app | yes |
| `sid` | ID token | key of the back-channel logout index. Without it only a logout token that carries `sub` can end sessions (all sessions of the person) | recommended |
| `aud`, `azp` | ID token | audience check (§3.4) | yes / if several audiences |
| `nonce` | ID token | replay protection | yes |
| `name`, `email`, `preferred_username` | ID token | `User.name`, `User.email`, `User.preferred_username`. Present only if the granted scopes include them | no |
| `realm_access.roles` | access or ID token | `User.realm_roles` | no |
| `resource_access.<client id>.roles` | access or ID token | `User.client_roles` – only the entry of **the extension's own client id** | no |
| `scope` (or `scp`) | access token of a target service | `appext.verify`: the scopes a token carries | yes, at services |
| `azp` | access token of a target service | `appext.verify`: which extension is calling | at services that use `require_azp` |

The access token of the login is verified only to read the roles; if it is opaque or unverifiable, the person simply has no roles – the sign-in does not fail. `User.roles` merges both role lists.

**Mapping another provider to this shape.** Roles are optional for the SDK. A provider that expresses roles differently (a `roles` or `groups` claim, an `app_metadata` object, …) SHOULD be configured to emit them as

```json
{"realm_access": {"roles": ["analyst"]}, "resource_access": {"ext-projects": {"roles": ["reviewer"]}}}
```

and to include them in the **access token or ID token** of the extension client. `sid` is the session identifier that also appears in the back-channel logout token (§9); if your provider names it differently, map it to `sid` in both tokens.

## 9. Logout

**RP-initiated** (`/auth/logout?sso=true`): the browser is sent to `end_session_endpoint` with `client_id`, `post_logout_redirect_uri` (default `{public_url}/`, MUST be registered) and `id_token_hint`. Plain `/auth/logout` ends only the extension's own session and does not contact the service: closing one extension must not sign the person out of the host app.

**Back-channel logout** (OpenID Connect Back-Channel Logout 1.0): when the person's session at the service ends – typically because they sign out in the host app – the service MUST POST a logout token to the registered URL `{public_url}/auth/backchannel-logout`:

```
POST /auth/backchannel-logout
Content-Type: application/x-www-form-urlencoded

logout_token=<JWT>
```

The client MUST be registered with the back-channel URL and with *session required* (so the token carries `sid`). The extension verifies:

| Check | Requirement |
|---|---|
| signature | from the issuer's JWKS (§2) |
| `iss`, `aud` | the issuer; `aud` contains the client id |
| `iat`, `jti` | present; `iat` within ±300 s of the extension's clock |
| `events` | an object with the member `http://schemas.openid.net/event/backchannel-logout` |
| `nonce` | **absent** (an ID token must not work as a logout token) |
| `sid` or `sub` | at least one. With `sid`, all sessions of that `sid` end; with only `sub`, all sessions of the person end |

`exp` is not required but is checked if present. Answers: `200` with an empty body on success; `400 {"error": "invalid_request", "error_description": ...}` for an invalid token, `413` for a body over 64 KiB. The service MAY retry on failures other than 400. The endpoint is exempt from the CSRF check because it is server to server – it must be reachable from the service's network (a service in a container reaches a developer machine as `host.docker.internal`, for example).

## 10. Audiences, scopes, consent – the model

- Every **target service** is a *resource server* with an **audience** string, normally its client id at the issuer (`data-api`). The audience is what an extension writes in `[[services]].audience` and what the service finds in `aud` of incoming tokens. **The service MUST exist at the issuer before an exchange can name it**: services that resolve `audience` to a registered client answer `invalid_client` ("Audience not found") otherwise. Creating it is the service owner's task; the store only records it in the service catalog ([app-store-api.md](app-store-api.md)).
- Every **scope** belongs to exactly one service and carries an **audience mapping**: a token issued with that scope has the service's audience in `aud`. Without the mapping the token has the issuer's default audience and every service rejects it. The mapping is part of the scope definition at the issuer, not of the request.
- Scope names are unique across the whole platform (`^[a-z][a-z0-9-]*$`): in most services a scope definition is realm-wide, and `[consent].scopes` names a scope without saying which service it belongs to.
- Every scope has a **consent text** (English, with translations) that the consent screen shows. The text is the service owner's, identical for every extension that asks for the scope. A scope without a text makes the screen show the technical name – and a person confirming `data-write` has confirmed nothing.
- Scope assignments of an extension client:

| Manifest | Assignment at the client | Requested at sign-in | Used for |
|---|---|---|---|
| `[consent].scopes` | **default** scopes | yes | carried by the login token; the person confirms them at the first sign-in |
| `[[services]]`, `mode = "user"` | **optional** scopes | yes, so that consent covers later exchanges | selected per exchange (§6) |
| `[[services]]`, `mode = "service"` | **optional** scopes, plus a service account | no – the person is never asked | selected per client-credentials request (§7) |

**What a target service verifies** (this is what `appext.verify.TokenVerifier` does; any service can do the same in any language): signature (JWKS), `iss`, `exp`, its own audience in `aud`, then the scopes it needs in `scope`/`scp`; optionally `azp` against the extensions allowed to call it. Wrong or missing token → 401, valid token lacking the scope or from the wrong extension → 403, JWKS unreachable → 503.

**Suspension.** Disabling a client at the service stops refreshes and exchanges at once; access tokens that were issued earlier stay valid until they expire. Keep access-token lifetimes short (the reference implementation uses 15 minutes) and let services that must react immediately consult the store's list of approved clients.

## 11. The command line: device authorization grant

`appext store login` signs a developer in with the **device authorization grant** (RFC 8628) **with PKCE**, as a **public** client. The default client id is `appext-cli`; the platform file or `--client-id` / `APPEXT_CLI_CLIENT_ID` can name another.

```
POST {device_authorization_endpoint}
client_id=appext-cli&scope=openid&code_challenge=<S256>&code_challenge_method=S256
```
→ `device_code`, `user_code`, `verification_uri`, optionally `verification_uri_complete`, `interval` (default 5 s), `expires_in` (default 600 s). The CLI prints `verification_uri_complete` (or the URI and the code), opens the browser, then polls:

```
POST {token_endpoint}
grant_type=urn:ietf:params:oauth:grant-type:device_code&device_code=...&client_id=appext-cli&code_verifier=<verifier>
```

Handled errors: `authorization_pending` (keep waiting), `slow_down` (interval + 5 s), `access_denied`, `expired_token`; anything else aborts. A device request without PKCE parameters is refused by services that require PKCE for the client; the CLI always sends them.

| Requirement on the client at the issuer | Why |
|---|---|
| public client, no secret | the CLI runs on developer machines and in CI |
| device authorization grant enabled | the only flow the CLI uses |
| PKCE `S256` required | the CLI always sends it; requiring it ties the code a person approves to the process that asked |
| tokens carry the **audience of the App Store API** | the store verifies `aud` (the CLI requests no `audience` or resource parameter, so this is a mapping at the issuer) |
| tokens carry the person's **store roles** | `store-developer`, `store-reviewer`, `store-admin` – as `realm_access.roles`, or in any claim your store reads |
| refresh tokens issued | the CLI keeps the refresh token (the only long-lived secret on disk, file mode `0600`) and renews with `grant_type=refresh_token&client_id=...` when the access token has under 30 s left; `invalid_grant` means "sign in again" |

The CLI reads the claims of its own access token **without verifying them**, only to tell the person who they are signed in as (`preferred_username`, else `email`, else `sub`; and the roles from `realm_access.roles`) and to warn when no store role is present. Authorization is decided by the store from the verified token on every request. In CI, `APPEXT_STORE_TOKEN` can hold a bearer token instead; nothing is cached then.

## 12. What a store must make the service do at approval

When a version is approved (and the review, if any, is complete) the store provisions the issuer. The reference implementation does this through Keycloak's Admin REST API (§13); another platform does it however its service is managed, but the result MUST be:

1. A **confidential client** `ext-<id>` with the authorization code flow and **PKCE S256**, no implicit flow, no resource-owner-password grant, **consent required**, and **no role or other claim beyond what its scopes carry** ("full scope" off, the equivalent in other services).
2. **Client authentication** matching the manifest: the public JWK from `PUT …/key` for `private_key_jwt`, or a generated secret (shown once to the developer) for `client_secret`.
3. **Redirect URIs** = `{url}/auth/callback` of every environment + the platform's app redirect URI; **post-logout URIs** = `{url}/`; **back-channel logout URL** = `{url}/auth/backchannel-logout` (session required).
4. **Token exchange enabled** for the client.
5. **Scope definitions** for every scope the version uses, with consent texts (translated) and the audience mapping of the service the scope belongs to.
6. **Scope assignments** per §10: `[consent].scopes` default; service scopes optional; a service account if any service has `mode = "service"`.
7. Nothing else assigned that would show on the consent screen without the extension needing it.

Provisioning MUST be **additive and idempotent**: a second identical run changes nothing, and it never replaces a realm-wide setting. A new version's scopes are *added*; scopes of other versions that are still approved or live stay assigned as long as those versions are. Suspending an extension disables the client; lifting the suspension enables it again; deleting the extension removes the client.

A store should only ever touch clients that match its own naming (`ext-…`) and scopes it created; the reference implementation refuses everything else, because a store with broad rights at the issuer must not be talkable into changing the host app's client.

## 13. Keycloak: how the reference implementation did it

For operators who use Keycloak. **Version 26.2 or newer** (Standard Token Exchange V2); the reference implementation runs on 26.5. Everything here follows from §3 – §12; the names are Keycloak's.

### The client of an extension (`ext-<id>`)

Created or aligned at approval, `enabled` only on creation (so a new version never lifts a suspension):

```json
{
  "clientId": "ext-projects",
  "name": "Extension: Projects",
  "protocol": "openid-connect",
  "publicClient": false,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": false,
  "implicitFlowEnabled": false,
  "serviceAccountsEnabled": false,
  "frontchannelLogout": false,
  "fullScopeAllowed": false,
  "consentRequired": true,
  "clientAuthenticatorType": "client-jwt",
  "redirectUris": [
    "com.example.app:/callback",
    "http://127.0.0.1:8100/auth/callback",
    "https://projects.apps.example.com/auth/callback"
  ],
  "attributes": {
    "pkce.code.challenge.method": "S256",
    "standard.token.exchange.enabled": "true",
    "display.on.consent.screen": "true",
    "consent.screen.text": "${extClient_ext_projects}",
    "post.logout.redirect.uris": "http://127.0.0.1:8100/##https://projects.apps.example.com/",
    "backchannel.logout.url": "https://projects.apps.example.com/auth/backchannel-logout",
    "backchannel.logout.session.required": "true",
    "frontchannel.logout.enabled": "false",
    "use.jwks.string": "true",
    "jwks.string": "{\"keys\":[{\"use\":\"sig\",\"kty\":\"RSA\",\"n\":\"…\",\"e\":\"AQAB\",\"kid\":\"…\"}]}",
    "token.endpoint.auth.signing.alg": "RS256"
  }
}
```

| Setting | Why |
|---|---|
| `fullScopeAllowed: false` | otherwise every realm role of the person ends up in the extension's tokens |
| `consentRequired: true` | §3.6; also what makes exchanges consent-checked |
| `standard.token.exchange.enabled` | §6 |
| `clientAuthenticatorType`: `client-jwt` (key) or `client-secret` | §4. The signing algorithm is chosen to fit the key: `RS256` for RSA, `ES256/384/512` for P-256/384/521 |
| `serviceAccountsEnabled` | true only if some service has `mode = "service"` (§7) |
| `backchannel.logout.url` | the **serving environment's** URL. A client is shared by all environments, so with several environments against one realm only one of them receives back-channel logouts – run one store per environment |

For a `client_secret` client Keycloak generates the secret. The store keeps nothing: the developer obtains the secret from `rotate-secret`, whose response shows it once (the reviewer who triggers the approval is not the owner and never sees it).

### Scopes, mappers and consent texts

Per scope of the service catalog, a client scope:

```json
{
  "name": "data-read",
  "protocol": "openid-connect",
  "description": "Extension scope: Read your data",
  "attributes": {
    "include.in.token.scope": "true",
    "display.on.consent.screen": "true",
    "consent.screen.text": "${extScope_data_read}"
  }
}
```

with one **audience mapper** (`oidc-audience-mapper`, name `data-api-audience`, config `included.client.audience = data-api`, `access.token.claim = true`, `id.token.claim = false`, `introspection.token.claim = true`). Without the mapper the token says `aud: account`. `include.in.token.scope` puts the scope into the `scope` claim, where services read it.

The consent text is stored as a **message key** (`${extScope_data_read}`) with the actual texts as *realm localization* per supported language (a language without a translation gets the English text). The client's own consent line works the same way (`${extClient_ext_projects}`). Writing localization needs `manage-realm`; without it provisioning continues and the consent screen shows keys.

Assignments: `basic` and `acr` stay (`basic` puts `sub` into the tokens – without it no token has a subject); `[consent].scopes` are **default** client scopes; scopes of `[[services]]` are **optional** client scopes; every other scope the realm pre-assigns (profile, email, roles, …) is **removed** from the client so it does not appear on the consent screen. Consequence worth knowing: **extension tokens carry no roles and no profile claims** in the reference implementation, so `User.roles`, `User.name` and `User.email` are empty there unless the operator maps them in on purpose.

### Target services

Keycloak resolves `audience` of a token exchange to a **client of the realm** and answers `invalid_client` ("Audience not found") for any name that is not one. Each target service therefore needs a client (a bearer-only or otherwise unused one is enough) before an extension can exchange for it. `appext keycloak export` creates such stand-ins for a local realm.

### The command line's client (`appext-cli`)

```json
{
  "clientId": "appext-cli",
  "publicClient": true,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": false,
  "implicitFlowEnabled": false,
  "serviceAccountsEnabled": false,
  "fullScopeAllowed": true,
  "attributes": {
    "oauth2.device.authorization.grant.enabled": "true",
    "pkce.code.challenge.method": "S256"
  },
  "protocolMappers": [{
    "name": "platform-api-audience",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {"included.client.audience": "platform-api", "access.token.claim": "true", "id.token.claim": "false", "introspection.token.claim": "true"}
  }]
}
```

`fullScopeAllowed: true` is what puts the store roles (realm roles `store-developer`, `store-reviewer`, `store-admin`) into `realm_access.roles`. The host app's own client, by contrast, has `fullScopeAllowed: false` and carries no store role: that is how the same realm keeps the store's people-only routes closed to every app user.

### What the reference store verifies on its own routes

The store is a resource server like any other. In the reference implementation it accepts only **`RS256`** access tokens (a fixed list – never the `alg` of the token itself, which is how `alg: none` slips through), and requires `iss`, `exp`, `iat`, `sub` and an `aud` that contains the API's own audience. `typ` must be `Bearer` or absent: an ID token must not pass as an access token. `azp` must be on an allow-list – the host app's clients, and on the store's routes the command line's client as well – or be an extension client, and an extension client is then refused on the store and catalog routes (`app_only`). A suspended client is refused before anything else, even where the allow-list is empty (otherwise an unrecognised client would count as the app). Roles are read from `realm_access.roles`; a malformed claim means no roles, never an error. A platform that builds its store on another stack should do the equivalent.

### What the store's admin account needs

`manage-clients` (clients, client scopes) and `manage-realm` (localization). That is realm-wide power. The reference implementation limits the damage in code: it touches only clients named `ext-…`, refuses a fixed list of the realm's own clients, and changes an existing scope only if it created it (its consent text is an `${extScope_…}` key). It writes everything additively and never uses a `clientScopes` list in a realm import: such a list **replaces** the realm's built-in scope set, and in the reference implementation's history that once removed `basic` – and with it `sub` – from every token, without an error message. If you manage the realm file yourself, list the built-in scopes you need (`basic`, `acr`, …) or do not declare `clientScopes` at all.

### Realm settings the reference implementation chose (product decisions)

Access-token lifespan 15 minutes; refresh-token rotation off; "remember me" on with a 30-day idle and 180-day maximum SSO session so that the host app's sign-in carries the silent sign-in of extensions; a login theme that pre-ticks "remember me"; `ui_locales` sent by the host app. None of this is required by the SDK.

## 14. Another provider: a mapping

| The SDK needs | Keycloak | Another provider must offer |
|---|---|---|
| Discovery, JWKS | built in | OIDC discovery with the three required members; asymmetric signing keys |
| Code flow, PKCE S256 | built in | the same, with PKCE enforceable per client |
| `private_key_jwt` with `aud` = token endpoint | `client-jwt` | the same; accept the token endpoint URL as assertion audience |
| Refresh tokens, optional rotation | built in | refresh tokens for confidential clients without `offline_access` |
| RFC 8693 exchange, same-client subject, `audience` + `scope` | Standard Token Exchange V2 | an RFC 8693 endpoint with that request/response and the error codes of §6.3 – or a thin adapter in front of the provider's own on-behalf-of mechanism |
| Audience from scope | audience mapper on the client scope | a way to attach an audience to a scope, so that client credentials and exchanges yield the right `aud` |
| Consent covering exchanges | `consentRequired` + exchange consent check | a consent store that the exchange consults, and the error of §6.3 |
| Back-channel logout with `sid` | per-client `backchannel.logout.*` | OIDC Back-Channel Logout 1.0 with `sid`; `sid` also in the ID token |
| Device grant with PKCE | per-client attribute | RFC 8628 with PKCE (S256) for a public client |
| Roles in tokens | realm roles → `realm_access.roles` | a claim mapping to that shape (§8) |
| Programmable provisioning | Admin REST API | an API (or infrastructure-as-code pipeline) behind §12 |

## 15. Conformance checklist

Tick every line for your service before you announce the platform.

**Discovery and keys**
- [ ] `GET {issuer}/.well-known/openid-configuration` answers 200 JSON without redirects; `issuer` equals the configured issuer.
- [ ] `iss` in all tokens equals the issuer without a trailing slash, byte for byte.
- [ ] Tokens are signed with `RS256` (or another asymmetric algorithm of §2) and carry a `kid`; `jwks_uri` lists the keys.

**Extension clients**
- [ ] Authorization code flow with PKCE `S256`, `response_mode=query`, `prompt=login` and `prompt=consent` work; redirect URIs match exactly.
- [ ] A token request authenticates with `private_key_jwt` (assertion `aud` = token endpoint URL, replayed `jti` rejected) and, if you allow it, `client_secret` in the body.
- [ ] The authorization-code response contains `access_token`, `id_token`, `expires_in`, `refresh_token`, `refresh_expires_in`, `scope`; the ID token has `sub`, `aud`, `nonce`, `exp`, `iat`, and `sid`.
- [ ] `sub` is the same for the host app's client and every extension client.
- [ ] Refresh works for confidential clients without `offline_access`; a dead session answers `invalid_grant`.
- [ ] Token exchange (§6): same-client subject token, `audience`, `scope`; result with `aud`, `sub`, `azp`, `scope`, `exp`; `access_denied` / consent-`invalid_scope` only for missing consent.
- [ ] Client credentials (§7) yield a token whose `aud` comes from the scopes.
- [ ] Consent is required and covers later exchanges for the same scopes; revoking it produces the consent error on the next exchange.
- [ ] Back-channel logout posts a valid logout token (`events`, `sid`, no `nonce`) to `{url}/auth/backchannel-logout`; RP-initiated logout honours registered post-logout URIs.

**Audiences and scopes**
- [ ] Every target service exists at the issuer as an audience; every scope has a consent text (translated where you offer languages) and an audience mapping.
- [ ] Extension tokens contain nothing beyond what their scopes carry (no roles unless you mapped them on purpose).

**Command line**
- [ ] The public client `appext-cli` (or the name in your platform file) supports the device grant with PKCE `S256` and refresh tokens, without a client secret.
- [ ] Its access tokens carry the store API's audience and the store roles.

**Provisioning**
- [ ] The store's approval step creates and aligns clients exactly as in §12, idempotently, additively, and only for clients and scopes it owns.
