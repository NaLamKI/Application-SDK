# The App Store, from a developer's side

The App Store is part of the FMIS backend (`/api/v1/store`). It is the control plane: it
takes your manifest, has it reviewed, sets up the Keycloak client, hands you the auth bundle,
checks your deployment and publishes the extension in the catalog the app reads
(`/api/v1/catalog`). At run time it is not involved. There is no store web page: the API
(OpenAPI docs of the backend) and `appext store …` are the interface.

## Roles

Platform roles are Keycloak **realm roles**, separate from the roles inside the FMIS data.
The store needs no FMIS account: a developer does not need a farm.

| Role | May |
|---|---|
| `store-developer` | register extensions, upload keys, submit, download bundles, verify; sees **only their own** |
| `store-reviewer` | see everything submitted; approve or reject |
| `store-admin` | everything a reviewer may, plus suspend/unsuspend, maintain the service catalog, and give the second approval for restricted scopes |
| *(none)* – any signed-in person | see the catalog, add or remove extensions, open them |

A token **issued to an extension** is refused everywhere in the store (`403 app_only`).

## Lifecycle

```
DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED ──verify──▶ LIVE ──(a newer version goes live)──▶ SUPERSEDED
                      └──reject──▶ REJECTED
```

| Step | Who | CLI | What happens |
|---|---|---|---|
| register | developer | `appext store register` | The manifest is checked (rules 1–7 and 8), the extension created or – if you own the id – a **new version**, status `DRAFT`. The public key is uploaded with it (a link has none: [below](#a-link)). |
| key | developer | `appext store key` | `PUT …/key` with the public JWK. Anything with a private member is refused, locally and by the store. |
| submit | developer | `appext store submit` | `DRAFT → SUBMITTED`. If the extension already has a live version and the new one asks for **no additional scopes or services**, the version becomes `APPROVED` at once (approved by "system", recorded in the event log) and only the deployment check remains. |
| approve | reviewer | `appext store approve <id>` | Records the approval. With **restricted scopes** a **second approval by a different `store-admin`** is required; then the version is `APPROVED` and the store sets up Keycloak. |
| reject | reviewer | `appext store reject <id> --reason …` | `SUBMITTED → REJECTED`; fix, bump or re-register the same version, submit again. |
| bundle | developer | `appext store bundle --env prod` | Available from `APPROVED`. See [deployment.md](deployment.md). |
| verify | developer | `appext store verify --env prod` | The store asks `<URL>/_sdk/info` and `/readyz` of your deployment: id, version and client id must match and it must be ready. `APPROVED → LIVE`; the previous live version becomes `SUPERSEDED`. |
| suspend | admin | `appext store suspend <id> --reason …` | Out of the catalog, the Keycloak client is **disabled** (no more refresh or exchange), and the FMIS API rejects its tokens. `unsuspend` undoes it. |

A re-registration of a version number that already exists replaces it only while it is
`DRAFT` or `REJECTED`.

### A link

A **link** (`kind = "link"`, [manifest.md](manifest.md#links)) is an entry the app opens in the
system browser. It follows the same states, but skips everything that belongs to a server:

```
DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED ──verify──▶ LIVE
```

| Step | What is different |
|---|---|
| register | No key is uploaded, and `appext store register` does not look for one (nor warn that it is missing). The store answers with `kind: "link"`, `clientId: null`, `clientAuth: null`, `hasKey: false` and `urls[env] = {"entry": "<the address>", "callback": null}` – the same address in every environment. The kind is fixed for an id. |
| submit, approve | As for an extension. The reviewer approves *the place the link leads to*; **the review is the only gate**. Nothing is set up in Keycloak. |
| `key`, `bundle`, `rotate-secret` | There is no key, no auth bundle and no secret: the store answers `409 invalid_state`. The CLI refuses earlier, without a request, when it reads the manifest of the project. |
| verify | **Immediate.** No deployment is probed (the store does not fetch an address a developer typed in): a single check `link` passes and the version goes `APPROVED → LIVE`; the previous live version becomes `SUPERSEDED`. |

A new version whose `entry` has the same scheme, host and port as the live one is approved at
`submit` without a review; another host (or scheme or port) is another link and goes to review.
In the catalog a link has `kind: "link"`, `display: "external"` and no `callbackUrl`, `clientId` or
`callbackScheme`; it asks for no scopes. `http` addresses (this machine) are accepted only by a
store in a development environment.

**New versions.** Unchanged scopes: no review. More scopes or services: back to review; until
approval the old version stays in the catalog and in the Keycloak client unchanged (the new
scopes are attached to the client on approval). The status shown for an extension is derived:
`SUSPENDED` if suspended, else `LIVE` if a version is live, else the newest version's status.

Every step and **every change to Keycloak** is written to an append-only event log (time,
actor `sub`, role, action, extension, version, detail) that reviewers and admins can read.

## What approval sets up in Keycloak

*(Not for a link: it has no client.)*

Only adding and aligning, never replacing: a confidential client `ext-<id>` with the
authorization-code flow and PKCE S256, no password or implicit flow, consent required, full
scope off, **standard token exchange on**, redirect URIs of every environment plus the app's
scheme, post-logout URIs, back-channel logout at `/auth/backchannel-logout`; your public key
as the client's JWKS (`private_key_jwt`); `consent.scopes` as default client scopes and the
`user`-mode service scopes as optional ones (service-mode scopes for client credentials, with
a service account); the scope definitions with consent texts and audience mappers. What the
realm pre-assigns and the extension does not need (profile, e-mail, roles …) is removed from
the client so it does not clutter the consent screen. Keycloak 26.2 or newer is required (the
compose stack uses 26.5.7).

## Environments

One store, environments as an attribute (`STORE_ENVIRONMENTS`). Each has a URL template with
`{id}` and `{dev_port}`; `https` is required except for `127.0.0.1`/`localhost` in a `dev`
environment. The default configuration knows only `local`: `http://127.0.0.1:{dev_port}`.
The Keycloak client is one per extension; its redirect URIs are the union of all
environments. **This installation serves one environment** (`STORE_ENVIRONMENT`), which
decides the entry URL, callback URL and hosts in the catalog. A `dev` environment may add a
`probeUrlTemplate` for the store's own deployment check, for a store in a container
(`http://host.docker.internal:{dev_port}` – the compose file does this). Pass `--env` to `bundle` and
`verify`; the default is `local` for a store on this machine and `prod` for any other.

## The CLI

Common options of every `appext store` command: `--store-url` – the API base, to which the CLI
adds `/store` (`APPEXT_STORE_URL`, default `http://127.0.0.1:8000/api/v1`) – and `--issuer` (`APPEXT_ISSUER`, default
`http://127.0.0.1:58080/realms/fmis`). The extension id is optional wherever the command is
run in a project – it is taken from `./extension.toml` (or `--manifest PATH`). When the id comes from
the manifest and the project is a link, `key`, `bundle` and `rotate-secret` stop with "a link has no
server …" before any request; with an id on the command line the store's own `409` is shown.

| Command | |
|---|---|
| `login` | **Device authorization grant** (RFC 8628, with PKCE) and the public client `appext-cli`: prints an address and a code, opens the browser (`--no-browser` to skip), waits until you approve. The credentials are cached in `~/.config/appext/credentials.json` (mode 0600, directory 0700) and renewed with the refresh token when due. A spent refresh token means `login` again. |
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

**In CI**, set `APPEXT_STORE_TOKEN` to a bearer token instead of signing in; nothing is
cached then. The token needs the `store-developer` role (its audience is the FMIS API).

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
| 502 / 503 | `provisioning_failed` / `provisioning_unconfigured` | Keycloak could not be set up; the version stays `SUBMITTED`. |

## Setting up a local store

The developer role and the CLI's public client come with the realm file
(`backend/keycloak/realm-fmis.json`); for an already running realm,
`python3 tool/keycloak_store.py --dev-user` creates the three roles, the `appext-cli` client,
the store's service-account rights, and a developer `store-dev@localhost.invalid` with all
three roles. With `STORE_PROVISIONING=off` the store skips the Keycloak step (dry run, tests).

## The service catalog

Services publish their scopes there (`PUT /store/services/{audience}`, `store-admin`): name,
consent text (English, with translations), whether the scope is **restricted**, and the base
URL per environment, which lands in the bundle as `APPEXT_SERVICE_<NAME>_URL`. An extension
can only ask for scopes that are listed. The built-in `fmis-api` is read-only in the store:
its scopes and what they permit come from `backend/app/auth/extensions.json`.

## Trust model

For now the store is for **internal teams**. Review, the four-eyes rule for restricted scopes
and the event log are built; signed images and a policy for which scopes third parties may be
offered are not (phase 3). The store has realm-wide Keycloak rights (`manage-clients`,
`manage-realm`) – see [security.md](security.md).
