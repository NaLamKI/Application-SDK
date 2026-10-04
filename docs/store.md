# The App Store, from a developer's side

For extension developers who publish through their platform's App Store, and for the reviewers and administrators who use `appext store …`.

The App Store is part of the platform: its control plane. It takes your manifest, has it reviewed, sets
up the extension's client at the issuer, hands you the auth bundle, checks your deployment and
publishes the extension in the catalog that the host app reads. At run time it is not involved.
`appext store …` is the developer's interface to the store API; whatever else a platform offers (a web
page, API documentation) is its own. The API itself, for platform builders:
[platform-contract/app-store-api.md](platform-contract/app-store-api.md).

Which store a command talks to is **configured, not built in**: `store_url` in the platform file,
`--store-url` or `APPEXT_STORE_URL`; the CLI adds `/store` to it. Without one, a store command stops
with "no App Store is configured" and says how to set it ([platform.md](platform.md)).

## Roles

Store roles are carried in the access token – in the reference implementation, realm roles of the
issuer, separate from any roles inside the platform's own data. The store needs no account of the
platform's other systems: a developer needs nothing but the role.

| Role | May |
|---|---|
| `store-developer` | register extensions, upload keys, submit, download bundles, verify; sees **only their own** |
| `store-reviewer` | see everything submitted; approve or reject |
| `store-admin` | everything a reviewer may, plus suspend/unsuspend, maintain the service catalog, and give the second approval for restricted scopes. An admin is not a developer: registering and submitting need `store-developer` |
| *(none)* – any signed-in person | see the catalog, add or remove extensions, open them |

A token **issued to an extension** is refused everywhere in the store (`403 app_only`). The roles are
given by the platform's operator; `appext store login` tells you which store roles your account has.

## Lifecycle

```
DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED ──verify──▶ LIVE ──(a newer version goes live)──▶ SUPERSEDED
                      └──reject──▶ REJECTED
```

| Step | Who | CLI | What happens |
|---|---|---|---|
| register | developer | `appext store register` | The manifest is checked (rules 1–7 and 8), the extension created or – if you own the id – a **new version**, status `DRAFT`. The public key is uploaded with it (a link has none: [below](#a-link)). |
| key | developer | `appext store key` | `PUT …/key` with the public JWK. Anything with a private member is refused, locally and by the store. |
| submit | developer | `appext store submit` | `DRAFT → SUBMITTED`. If the extension already has a live version and the new one asks for **nothing more** (no further scopes, services or hosts, the same `client_auth`), the version becomes `APPROVED` at once (approved by "system", recorded in the event log) and only the deployment check remains. |
| approve | reviewer | `appext store approve <id>` | Records the approval. With **restricted scopes** a **second approval by a different `store-admin`** is required; then the version is `APPROVED` and the store sets up the issuer. |
| reject | reviewer | `appext store reject <id> --reason …` | `SUBMITTED → REJECTED`; fix, bump or re-register the same version, submit again. |
| bundle | developer | `appext store bundle --env prod` | Available from `APPROVED`. See [deployment.md](deployment.md). |
| verify | developer | `appext store verify --env prod` | The store asks `<URL>/_sdk/info` and `/readyz` of your deployment: id, version and client id must match and it must be ready. `APPROVED → LIVE`; the previous live version becomes `SUPERSEDED`. |
| suspend | admin | `appext store suspend <id> --reason …` | Out of the catalog, the client at the issuer is **disabled** (no more refresh or exchange). `unsuspend` undoes it. |

A re-registration of a version number that already exists replaces it only while it is
`DRAFT` or `REJECTED`.

### A link

A **link** (`kind = "link"`, [manifest.md](manifest.md#links)) is an entry the host app opens in the
system browser. It follows the same states, but skips everything that belongs to a server:

```
DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED ──verify──▶ LIVE
```

| Step | What is different |
|---|---|
| register | No key is uploaded, and `appext store register` does not look for one (nor warn that it is missing). The store answers with `kind: "link"`, `clientId: null`, `clientAuth: null`, `hasKey: false` and `urls[env] = {"entry": "<the address>", "callback": null}` – the same address in every environment. The kind is fixed for an id. |
| submit, approve | As for an extension. The reviewer approves *the place the link leads to*; **the review is the only gate**. Nothing is set up at the issuer. |
| `key`, `bundle`, `rotate-secret` | There is no key, no auth bundle and no secret: the store answers `409 invalid_state`. The CLI refuses earlier, without a request, when it reads the manifest of the project. |
| verify | **Immediate.** No deployment is probed (the store does not fetch an address a developer typed in): a single check `link` passes and the version goes `APPROVED → LIVE`; the previous live version becomes `SUPERSEDED`. |

A new version whose `entry` has the same scheme, host and port as the live one is approved at
`submit` without a review; another host (or scheme or port) is another link and goes to review.
In the catalog a link has `kind: "link"`, `display: "external"` and no `callbackUrl`, `clientId` or
`callbackScheme`; it asks for no scopes. `http` addresses (this machine) are accepted only by a
store in a development environment.

**New versions.** Nothing more than the live version asks for: no review. More scopes, services or hosts, or another `client_auth`: back to review; until
approval the old version stays in the catalog and at the issuer unchanged (the new
scopes are attached to the client on approval). The status shown for an extension is derived:
`SUSPENDED` if suspended, else `LIVE` if a version is live, else the newest version's status.

Every step and **every change at the issuer** is written to an append-only event log (time,
actor `sub`, role, action, extension, version, detail) that reviewers and admins can read.

## What approval sets up at the issuer

*(Not for a link: it has no client.)*

The store provisions the issuer so that the extension's sign-in and token exchanges work: a
confidential client `ext-<id>` with the authorization-code flow and PKCE S256, no password or implicit
flow, consent required, no scopes beyond those the manifest names, token exchange on, the redirect
URIs of every environment plus the host app's return address, post-logout URIs and back-channel logout
at `/auth/backchannel-logout`; your public key as the client's keys (`private_key_jwt`); the
`[consent]` scopes as default client scopes and the `user`-mode service scopes as optional ones
(service-mode scopes for client credentials, with a service account); the scope definitions with
consent texts and audience mappers. Only adding and aligning, never replacing, and nothing that would
clutter the consent screen. What exactly an issuer must end up with, and how the reference
implementation (Keycloak 26.2 or newer) does it:
[platform-contract/oauth-service.md](platform-contract/oauth-service.md).

## Environments

An environment is a named place an extension runs (`local`, `prod`, …); the store knows each with a URL
template for the extension's origin. **One client per extension** serves all of them: its redirect URIs
are the union of every environment's. **One store installation serves one environment**, and that one
decides the entry URL, the callback URL and the hosts in the catalog. A development environment may
use a `probeUrlTemplate` for the store's own deployment check – for a store in a container on your
machine, the host's address as seen from inside the container.

Pass `--env` to `bundle` and `verify`; the default is `local` for a store on this machine (a loopback
address) and `prod` for any other. The names are the store's: a store that names its environments
differently needs `--env` every time.

## The CLI

Common options of every `appext store` command: `--store-url` – the API base, to which the CLI adds
`/store` (`APPEXT_STORE_URL`, or `store_url` of the platform file) – `--issuer` (`APPEXT_ISSUER`, or
`issuer` of the platform file) and `--platform NAME|FILE` (`APPEXT_PLATFORM`). There are **no
defaults**: see [platform.md](platform.md#where-the-settings-come-from) for the order and the error you
get when nothing is configured. The extension id is optional wherever the command is run in a project –
it is taken from `./extension.toml` (or `--manifest PATH`; a platform file next to that manifest is
used). When the id comes from the manifest and the project is a link, `key`, `bundle` and
`rotate-secret` stop with "a link has no server …" before any request; with an id on the command line
the store's own `409` is shown.

| Command | |
|---|---|
| `login` | **Device authorization grant** (RFC 8628, with PKCE) and the CLI's public client (`appext-cli`, or `--client-id` / `APPEXT_CLI_CLIENT_ID` / `cli_client_id` in the platform file): prints an address and a code, opens the browser (`--no-browser` to skip), waits until you approve. The credentials are cached per issuer in `~/.config/appext/credentials.json` (mode 0600, directory 0700) and renewed with the refresh token when due. A spent refresh token means `login` again. |
| `logout` | Forget the cached credentials. |
| `register [--manifest P] [--key JWK]` | Upload `extension.toml` (as TOML) and, for `private_key_jwt`, the public key (default `.appext/client_key.jwk.json`). The manifest rules run locally first. For a **link** only the manifest goes up – no key is looked for or uploaded, and the next steps printed are `submit`, the review, `verify`. |
| `key [id] [--key JWK]` | Upload (or replace) the public key. Refused locally for a link. |
| `submit [id]` | Submit the newest draft. |
| `bundle [id] [--env E] [--out DIR]` | Download the auth bundle and unpack it (default `./auth-bundle`; any path that would leave the directory is refused). Refused locally for a link. |
| `verify [id] [--env E]` | The deployment check; exit code 0 only when the result is `LIVE`. For a link there is nothing to check and it goes live at once. |
| `status [id] [--all]` | One extension, or all of yours (`--all`: everything you may see). A link shows `kind link` and its `entry` where an extension shows its client. |
| `services` | The service catalog: audiences and scopes you may ask for, restricted ones marked. |
| `rotate-secret [id] --out FILE` | `client_secret` extensions only: new secret into a file (mode 0600), never printed; the old one stops working. Refused locally for a link. |
| `approve ID [--note]`, `reject ID --reason`, `suspend ID --reason`, `unsuspend ID --reason` | Reviewer and admin helpers. |

The CLI warns when the store or the issuer is addressed with plain `http` and not on this machine: your
sign-in token would travel unencrypted.

**In CI**, set `APPEXT_STORE_TOKEN` to a bearer token instead of signing in; nothing is cached then.
The token needs the `store-developer` role and the store's audience, like the CLI's own; the issuer must
still be configured. Which requests each command makes, with every shape, is specified in
[platform-contract/app-store-api.md](platform-contract/app-store-api.md).

**Errors** are shown as the API sends them – status, `code`, `message` – plus the manifest
errors with their paths:

```
error: the store answered 422 invalid_manifest: The manifest is invalid
  services[0].audience: unknown audience 'x-api'
```

| Status | Code | Meaning |
|---|---|---|
| 401 | – | No valid token: `appext store login`. |
| 403 | `forbidden` / `app_only` | Role missing / an extension token was used. |
| 404 | `not_found` | Unknown id (or not yours). |
| 409 | `invalid_state`, `already_exists` | Wrong step for the version's status / the id belongs to someone else. |
| 422 | `invalid_manifest` | Rules broken; `errors` lists each path. |
| 502 / 503 | `provisioning_failed` / `provisioning_unconfigured` | The issuer could not be set up; the version stays `SUBMITTED`. |

## Getting a developer account

Ask the platform's operator for the role `store-developer` and for the address of the store and the
issuer (a platform file, ideally: [platform.md](platform.md#for-platform-operators)). `appext store
login` then needs nothing but those. The operator also has to provide the CLI's public client at the
issuer; if `login` answers that the sign-in refused to start, that client is missing or has another
name (`--client-id`).

## The service catalog

Services publish their scopes in the store's service catalog: name, consent text (English, with
translations), whether the scope is **restricted**, and the base URL per environment, which lands in
the bundle as `APPEXT_SERVICE_<NAME>_URL`. An extension can only ask for scopes that are listed:
`appext store services` shows what you may ask for. How services are registered is the platform's
business ([platform-contract/app-store-api.md](platform-contract/app-store-api.md)).

## Trust model

The store is where a platform decides what an extension may do. Review, the four-eyes rule for
restricted scopes and the event log are the controls; the store's own rights at the issuer are what a
platform has to protect – see [security.md](security.md#what-the-platform-has-to-protect).
