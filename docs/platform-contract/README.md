# The platform contract

This directory is for **platform builders and operators** – the people who want extensions written with `appext` to run on *their* platform – and tells them what the platform has to provide so that the `appext` library, the `appext` command line and the bridge work against it. (If you want to *write* an extension, start with the guides in [`docs/`](../quickstart.md) instead.)

`appext` is not tied to one product. A **platform** is any system that follows the reference architecture below: it offers an OAuth 2.0 / OpenID Connect service, an App Store that registers, reviews and lists extensions, and – optionally – an app that shows the extensions. The SDK talks to those services through a small, fixed set of requests and expects fixed answers. This contract is that set.

## 1. The reference architecture in one picture

```
 developer ──① CLI────▶ ┌────────────────────────┐ ──② provisions──▶ ┌────────────────────────┐
                        │ App Store              │   clients, scopes │ OAuth service          │
 host app ──④ catalog──▶│ (control plane)        │                   │ (the issuer)           │
                        └───────────┬────────────┘                   └────────▲───────────────┘
                                    │ ③ auth bundle + lock file               │ ⑤ code, refresh,
                                    ▼                                         │   exchange, logout
 host app ──④ opens────▶┌────────────────────────┐ ───────────────────────────┘
                        │ Extension container    │
                        │ (appext library)       │
                        └───────────┬────────────┘
                                    │ ⑥ exchanged token
                                    ▼
                        ┌────────────────────────┐
                        │ Target services        │── verify with the issuer's keys (JWKS)
                        └────────────────────────┘
```

① A developer signs in to the store with the command line, registers the extension's manifest, uploads the public key, and submits it. ② When a reviewer approves, the store sets up the extension's client, scopes and consent texts at the OAuth service. ③ The store hands the developer an **auth bundle**: the secret-free settings and a **lock file** with the approved state. ④ The host app (optional) lists the store's catalog and the person's installed apps, opens an extension in a WebView (or frame, or browser), and hands the extension's sign-in to the system browser, where the person's existing session at the OAuth service is reused. ⑤ The extension's backend signs in, refreshes and logs out against the OAuth service. ⑥ To call a target service on behalf of the person it exchanges its token for one that is cut to that service.

**At run time the store is not involved.** An extension never calls it, and a running extension keeps working when the store is down.

### Words used in these documents

| Word | Meaning |
|---|---|
| **platform** | the whole system: OAuth service, App Store, host app, target services |
| **issuer** | the OAuth / OpenID Connect service, named by its issuer URL (`https://auth.example.com/realms/example`) |
| **store** | the App Store: the control plane behind `appext store …` and the catalog |
| **host app** | an app that shows extensions: on a phone (WebView), in the web (frame), on the desktop (browser) |
| **extension** | a web frontend plus a Python backend under one origin, built with `appext`; its identity at the issuer is one confidential client, `ext-<id>` |
| **link** | a store entry that the host app opens in the system browser; no server, no client (`kind = "link"`) |
| **target service** | a resource server an extension calls with an exchanged token; identified by its **audience** (`data-api`) |
| **environment** | a named place an extension runs (`local`, `prod`); one URL template each |
| **auth bundle** | the ZIP the store issues per extension and environment: `appext.env` and `extension.lock.toml` |

MUST, SHOULD and MAY are used as in RFC 2119.

## 2. What a platform provides

| | What | Required? | Read |
|---|---|---|---|
| **OAuth service** | OpenID Connect discovery; authorization code flow with PKCE; `private_key_jwt` client authentication; refresh tokens; **token exchange (RFC 8693)**; back-channel logout; the **device authorization grant** for the command line | **Yes.** Nothing works without it | [oauth-service.md](oauth-service.md) |
| **App Store API** | register / review / provision / bundle / verify extensions; service catalog; event log; the catalog for the host app | **Yes for publishing**; at run time not involved. Without it the operator has to produce bundles and lock files and configure the issuer by hand | [app-store-api.md](app-store-api.md), [auth-bundle.md](auth-bundle.md) |
| **Host app** | list the catalog, open extensions, hand the sign-in over, offer the bridge | **Optional.** Without it extensions are websites | [host-app.md](host-app.md) |

Plus the **target services** themselves: each registers its audience and scopes in the store's service catalog and verifies the exchanged tokens it receives (the library `appext.verify` does that for Python services; any language can do it with the rules in [oauth-service.md](oauth-service.md) §10).

Three levels follow from this, and a platform can grow through them:

| Level | You provide | You get |
|---|---|---|
| 0 | OAuth service; bundles and lock files made by hand | extensions run; no `appext store`, no review, no catalog |
| 1 | + App Store API | the whole developer workflow: register, review, provision, bundle, verify, catalog |
| 2 | + host app | the in-app experience: silent sign-in, the app's own navigation, the bridge |

### The host app is optional

An extension is a website that signs people in with an ordinary authorization-code flow against the platform's issuer. If the platform has **no host app** it simply leaves `APPEXT_APP_REDIRECT_URI` and `APPEXT_APP_ORIGINS` unset: the extension never starts an "app mode" sign-in, nothing may frame it, and `bridge.js` is a quiet no-op. The same extension, unchanged, runs inside a host app if one exists later. A **link** needs no host app logic beyond opening an address in a browser.

## 3. How the SDK finds the platform

The SDK has **no built-in platform**. A developer's machine learns about yours from a small **platform file**, `appext.toml`, which you publish (a developer drops it next to `extension.toml`, or into `~/.config/appext/platforms/<name>.toml` and selects it with `--platform <name>`). The full format and the lookup rules are in [../platform.md](../platform.md); this is the part that matters for the contract:

```toml
# appext.toml
[platform]
name = "Example Platform"                          # shown in messages and in the bridge's "Back to …" bar
issuer = "https://auth.example.com/realms/example" # the OAuth service
store_url = "https://api.example.com/api/v1"       # the App Store API; the CLI appends /store/…
cli_client_id = "appext-cli"                       # optional: the command line's public client (default appext-cli)
app_redirect_uri = "com.example.app:/callback"     # optional: where the host app takes a sign-in back; omit if there is no host app

[platform.services]                                # optional: base URLs of target services for development on a laptop
data-api = "http://127.0.0.1:8000/api/v1"

[platform.starter]                                 # optional: what `appext new` puts into a fresh manifest
service = "data"
audience = "data-api"
scope = "data-read"
```

| Setting | The contract it points to | Environment variable |
|---|---|---|
| `issuer` | the OAuth service, discovered at `{issuer}/.well-known/openid-configuration` ([oauth-service.md](oauth-service.md) §2) | `APPEXT_ISSUER` |
| `store_url` | the App Store API base; `appext store …` calls `{store_url}/store/…` ([app-store-api.md](app-store-api.md) §1) | `APPEXT_STORE_URL` |
| `cli_client_id` | the public client of the device grant ([oauth-service.md](oauth-service.md) §11) | `APPEXT_CLI_CLIENT_ID` |
| `app_redirect_uri` | the host app's return address ([host-app.md](host-app.md) §4) | `APPEXT_APP_REDIRECT_URI` |
| `name` | `APPEXT_APP_NAME` when `appext dev` runs the extension | – |

Each setting is looked up on its own: a command-line option wins over an environment variable, which wins over the platform file. A setting that is needed and found nowhere is an error that names the three ways to provide it.

**In production the platform file is not read.** A running extension gets the same values from the **auth bundle** the store issues ([auth-bundle.md](auth-bundle.md)): `APPEXT_ISSUER`, `APPEXT_APP_REDIRECT_URI`, and so on. The file is for development at the desk (`appext dev`, `appext store …`, `appext new`).

## 4. The version of the contract

This is **version 1 of the platform contract**. It describes what `appext` **0.1.x** sends to a platform and expects back.

- Nothing on the wire carries a contract version: the SDK does not announce one to the store or the issuer, and neither announces one to it. `/_sdk/info` reports the SDK's own version (`sdkVersion`) for information only.
- Within version 1, a new `appext` release may **add** optional things: new optional `APPEXT_*` variables (the `APPEXT_APP_*` ones were added this way), optional members in JSON bodies a reader ignores, and new values of `kind` and `display`, which older host apps read as `extension` and `in_app`. A platform that follows this contract keeps working.
- A change that breaks a platform written against version 1 – removing or renaming a variable, a route, a claim or a required member, or changing a required shape – is a **new contract version** and is announced as one.

Where these documents and the code of a released `appext` disagree, the code is what extensions do; please report the difference.

## 5. What breaks if you leave something out

| Leave out | Consequence |
|---|---|
| the **OAuth service** | everything: no sign-in, no exchange, no command-line login |
| the **discovery document** | the extension cannot find its endpoints (sign-in answers 503, `/readyz` fails); the command line refuses. There is no static endpoint configuration |
| **PKCE `S256`** | the SDK always sends it; a service that cannot handle the parameters breaks every sign-in |
| **`private_key_jwt`** with the token endpoint as `aud` | every token request of every extension fails (`invalid_client`). `client_secret` is an alternative the platform may allow, the default is the key |
| **refresh tokens** | the extension's session ends when the access token expires and the person is sent through the sign-in again (in a WebView: a hand-over every few minutes) |
| **token exchange (RFC 8693)** | extensions that declare a `mode = "user"` service fail with `502 upstream_auth_failed` at their first call to it; extensions without such a service are unaffected |
| the **client-credentials grant** | `mode = "service"` services fail the same way |
| the **consent error** of the exchange (`access_denied`, or `invalid_scope` mentioning "consent") | a missing consent becomes a generic 502 instead of a new sign-in that asks for exactly the missing scopes |
| **`sid`** in the ID token, or **back-channel logout** | signing out of the host app does not end the extensions' sessions; they live until their refresh token or the maximum session age (30 days) ends them |
| the **device grant**, or the **audience and store roles** in the command line's token | `appext store login` cannot work. CI may still use a token in `APPEXT_STORE_TOKEN`; the store API is plain HTTP |
| the **App Store API** | no `appext store …`, no review, no provisioning, no bundle, no deployment check, no catalog (level 0 above). Outside `local` an extension still needs an `extension.lock.toml` to start, so the operator must write it |
| the **service catalog** | rule 7 of the manifest cannot run, so the store cannot tell which audiences and scopes exist, and cannot provision them |
| the **auth bundle**, or the **lock file** | every deployment is configured by hand; a missing lock file stops an extension outside `local` (`LockError`) |
| the **deployment check** (`verify`) | versions cannot become `LIVE` and so never reach the catalog |
| the **catalog API** | the host app has nothing to list; extensions remain reachable at their own URLs |
| the **host app** | extensions are websites: no silent sign-in, no app navigation, no bridge (see level 0 and 1) |
| the host app's **user-agent marker** | the extension does not recognise the WebView, picks its web redirect URI, and the host app blocks the identity provider's page: the sign-in fails in the WebView |
| the **`app_sub` cookie** | no protection against a different person being signed in at the system browser than in the app: the extension could open under a foreign identity |
| the **app redirect URI at an extension's client** | `redirect_uri` is rejected at the hand-over |
| **`APPEXT_APP_ORIGINS`** (web app) | the extension answers `frame-ancestors 'none'`: the browser refuses the frame and `bridge.js` stays silent |
| a **`local` environment name** for a real deployment | the SDK reads it as development and relaxes its production checks |

## 6. The documents

| File | For | What it specifies |
|---|---|---|
| [oauth-service.md](oauth-service.md) | people who run or choose the OAuth service; store developers who provision it | discovery, the authorization code flow, client authentication, refresh, **token exchange**, claims, logout, the device grant, audiences, scopes and consent – and how the reference implementation used Keycloak |
| [app-store-api.md](app-store-api.md) | store developers | every endpoint the CLI uses, the shapes, errors, the lifecycle, manifest validation, links, the deployment check, environments, the catalog API |
| [auth-bundle.md](auth-bundle.md) | store developers and operators | the ZIP, every `APPEXT_*` variable, the lock file, secrets, how to generate and check it |
| [host-app.md](host-app.md) | host app developers | the catalog it consumes, the opening and hand-over flow, the marker, the redirect scheme, navigation rules, the bridge, the web frame, what it must not do |

Related documents of the SDK: [../platform.md](../platform.md) (the platform file), [../manifest.md](../manifest.md) (the manifest and its rules), [../../conformance/manifests/README.md](../../conformance/manifests/README.md) (the shared manifest cases every store validator has to pass).

**Where to start.** *You operate the OAuth service:* oauth-service.md. *You build the store:* app-store-api.md, then auth-bundle.md, then the provisioning part of oauth-service.md (§12). *You build the app:* host-app.md, then the catalog part of app-store-api.md (§12).

### A reference implementation exists

A complete reference implementation of this contract exists: a farm-management product, whose backend contains the App Store (package `app/store/`, on FastAPI and Keycloak) and whose Flutter app is the host app. It is not part of this repository. Where these documents write "reference implementation" they describe a decision that implementation made – a realm setting, a timeout, a product behaviour – that another platform is free to make differently; everything else is the contract.

## 7. Is my platform conformant?

Each document ends with a checklist for its part: [oauth-service.md](oauth-service.md) §15, [app-store-api.md](app-store-api.md) §14, [auth-bundle.md](auth-bundle.md) §8, [host-app.md](host-app.md) §11. Work through them, then run this smoke test of the whole chain and tick every line before you announce the platform:

- [ ] `curl {issuer}/.well-known/openid-configuration` answers with the members of [oauth-service.md](oauth-service.md) §2.
- [ ] `appext store login` completes the device grant and prints the store roles of the person.
- [ ] `appext new hello`, `appext store register` and `appext store submit` leave the version `SUBMITTED`; a reviewer runs `appext store approve` (two people, one of them an admin, if it asks for a restricted scope) – the version is `APPROVED`, and the client, scopes and audience mappings exist at the issuer.
- [ ] `appext store bundle` unpacks a bundle that the script in [auth-bundle.md](auth-bundle.md) §7 accepts.
- [ ] The extension, deployed with the bundle, answers `/_sdk/info` and `/readyz`; `appext store verify` makes the version `LIVE`.
- [ ] The host app's catalog lists it; opening it in the host app signs the person in without a password prompt; a call to a target service succeeds with an exchanged token whose `aud`, `azp` and `scope` are as specified.
- [ ] Signing out of the host app ends the extension's session (back-channel logout).
- [ ] Your manifest validator passes every case in `conformance/manifests/`.
