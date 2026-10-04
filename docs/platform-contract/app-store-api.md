# The App Store API

This document is for the developers of a platform's App Store (the control plane that registers, reviews, provisions and lists extensions): it specifies the HTTP API that `appext store …` calls and the catalog API that a host app reads, with the request and response shapes, the lifecycle rules and complete JSON examples.

The store is **not** involved at run time: an extension never calls it, and a running extension keeps working if the store is down. Everything here was checked against the SDK's `appext.cli.store`, `appext.manifest`, `appext.app` (`/_sdk/info`, `/readyz`) and `appext.lock`, and against the reference implementation of the store.

MUST, SHOULD and MAY are used as in RFC 2119. "Reference implementation" marks a choice one platform made.

## 1. Overview

| Surface | Path (below the API base) | Caller | Token | Needs |
|---|---|---|---|---|
| **Store API** | `/store/…` | developers, reviewers, administrators – through `appext store …`, CI, or the platform's own web UI | access token of the command-line client (see [oauth-service.md](oauth-service.md) §11) | a store role |
| **Catalog API** | `/catalog`, `/extensions`, `/extensions/{id}/installation` | the host app, for every signed-in person | access token of the **host app** | no store role |

**API base.** The platform file (`store_url`, see [../platform.md](../platform.md)) or `APPEXT_STORE_URL` names the API base, for example `https://api.example.com/api/v1`. The CLI appends `/store/` to it. The catalog API lives next to it: `https://api.example.com/api/v1/catalog`. The host app is configured with its own address; the SDK never calls the catalog.

**Roles.** Three roles, carried in the token (the reference implementation: realm roles in `realm_access.roles`):

| Role | May |
|---|---|
| `store-developer` | register extensions and versions, upload keys, submit, download bundles, verify, rotate secrets, delete their own; sees **only their own** extensions |
| `store-reviewer` | see every extension; approve or reject; read events |
| `store-admin` | everything a reviewer may; additionally suspend and unsuspend, maintain the service catalog, delete any extension, and give the completing approval for restricted scopes. An admin is **not** a developer: registering and submitting need `store-developer` |
| *(none)* – any signed-in person | catalog API only |

Roles combine: a person with `store-developer` and `store-reviewer` is both. A role is a claim of the verified token, **never** a header, query parameter or database row.

## 2. Conventions

- JSON, UTF-8, **camelCase** members (except the manifest, which keeps the TOML structure and its snake_case keys). Timestamps are ISO 8601 in UTC (`2026-10-03T09:12:44Z`, optionally with fractional seconds).
- Request bodies reject **unknown members** (422 `invalid_request`): a typo in `reason` must not become an empty reason.
- `Authorization: Bearer <access token>` on every request. The store MUST verify signature, `iss`, `exp`, and that `aud` contains **its own audience** (the CLI requests no audience; the issuer's configuration adds it, see [oauth-service.md](oauth-service.md) §11). It SHOULD restrict `/store/…` to tokens of the command-line client (`azp` = `appext-cli` by default) and of any other client you deliberately allow, for example your own review UI.
- **Tokens issued to an extension client MUST be refused** on every store and catalog route (`403 app_only`): a compromised extension must not be able to approve itself, list, add or remove extensions.
- A malformed roles claim means *no roles*, not an error.

### Errors

Every error answered by the store has the same shape:

```json
{
  "detail": {
    "code": "invalid_manifest",
    "message": "The manifest is not valid.",
    "errors": [
      {"path": "services[0].audience", "message": "audience 'x-api' is not in the service catalog"}
    ]
  }
}
```

`code` and `message` are always present; `errors` (a list of `{"path", "message"}`) only for 422. That includes errors of the HTTP framework (a body that is not JSON, a missing member): they MUST be rewritten into this shape, because the CLI reads `detail.code`. The CLI prints `the store answered <status> <code>: <message>` followed by one line per error; it also tolerates a plain string `detail` (what a 401 from a generic resource server looks like).

| Status | `code` | When |
|---|---|---|
| 401 | – | no or invalid token. SHOULD carry `WWW-Authenticate: Bearer`. The CLI says "Run `appext store login`" |
| 403 | `forbidden` | the role is missing: `This needs the role store-developer.` The body of such a request MUST NOT be read before the role is checked |
| 403 | `app_only` | the token was issued to an extension |
| 404 | `not_found` | no such extension – **also** for an extension the caller may not see (no oracle) |
| 409 | `invalid_state` | the step does not fit the version's status, or the thing does not exist for this kind (a link has no key) |
| 409 | `already_exists` | the id belongs to someone else or to a fixed entry; the version number exists and is frozen; a scope name is taken |
| 413 | `payload_too_large` | manifest above 256 KiB (reference implementation) |
| 415 | `unsupported_media_type` | content type other than `application/json`, `application/toml` or `text/toml` |
| 422 | `invalid_manifest` | rules of §7 broken; `errors` lists **all** violations |
| 422 | `invalid_request` | a body or query parameter does not parse, unknown member, bad `status` filter |
| 422 | `invalid_key` | the public key is unusable or contains private material |
| 422 | `invalid_service` | a service catalog entry is unusable |
| 422 | `wrong_environment`, `unknown_environment` | `env` is not the serving environment (verify) / not configured (bundle) |
| 502 | `provisioning_failed` | the OAuth service could not be set up |
| 503 | `provisioning_unconfigured` | the store has no way into the OAuth service's admin interface |

## 3. Which role may call what

"CLI" marks what `appext store …` calls; a store MUST implement at least those for the command line to work. The others belong to the platform's own review and administration tools.

| Method and path | Developer | Reviewer | Admin | CLI command |
|---|:-:|:-:|:-:|---|
| `GET /store/extensions?status=&mine=` | own | all | all | `status [--all]` |
| `POST /store/extensions` | yes | – | – | `register` |
| `GET /store/extensions/{id}` | own | all | all | `status`, `register` |
| `DELETE /store/extensions/{id}` | own, none live | – | all | – |
| `PUT /store/extensions/{id}/key` | own | – | – | `key`, `register` |
| `POST /store/extensions/{id}/submit` | own | – | – | `submit` |
| `POST /store/extensions/{id}/approve` | – | yes | yes | `approve` |
| `POST /store/extensions/{id}/reject` | – | yes | yes | `reject` |
| `GET /store/extensions/{id}/auth-bundle?env=` | own | – | – | `bundle` |
| `POST /store/extensions/{id}/rotate-secret` | own | – | – | `rotate-secret` |
| `POST /store/extensions/{id}/verify?env=` | own | – | – | `verify` |
| `POST /store/extensions/{id}/suspend`, `…/unsuspend` | – | – | yes | `suspend`, `unsuspend` |
| `GET /store/services` | yes | yes | yes | `services` |
| `PUT /store/services/{audience}`, `DELETE …` | – | – | yes | – |
| `GET /store/events?extension=&limit=&offset=` | – | yes | yes | – |

"own" = the extension the caller owns. A developer who is not the owner gets `404` – the extension does not exist for them – while reviewers and admins can read everything. A person who has only a reviewer or admin role and tries an owner's action (register, key, submit, bundle, verify, rotate) gets `403 forbidden`.

## 4. The shapes

### 4.1 `StoreExtension`

Returned by every endpoint that answers with an extension.

```json
{
  "id": "projects",
  "status": "LIVE",
  "ownerSub": "6f1d9c1e-0000-4000-8000-000000000001",
  "name": "Projects",
  "version": "1.2.0",
  "liveVersion": "1.2.0",
  "kind": "extension",
  "clientId": "ext-projects",
  "clientAuth": "private_key_jwt",
  "hasKey": true,
  "suspended": false,
  "suspendedReason": null,
  "approvedScopes": ["data-read", "export-write"],
  "versions": [
    {
      "version": "1.2.0",
      "status": "LIVE",
      "scopes": ["data-read", "export-write"],
      "restricted": true,
      "approvals": [
        {"sub": "6f1d9c1e-0000-4000-8000-000000000002", "at": "2026-10-03T09:01:12Z", "role": "store-reviewer"},
        {"sub": "6f1d9c1e-0000-4000-8000-000000000003", "at": "2026-10-03T09:05:40Z", "role": "store-admin"}
      ],
      "reviewNote": null,
      "createdAt": "2026-10-02T16:20:05.311204Z",
      "liveAt": "2026-10-03T09:12:44.870115Z",
      "manifest": null
    }
  ],
  "urls": {
    "local": {"entry": "http://127.0.0.1:8100/", "callback": "http://127.0.0.1:8100/auth/callback"},
    "prod": {"entry": "https://projects.apps.example.com/", "callback": "https://projects.apps.example.com/auth/callback"}
  },
  "createdAt": "2026-10-02T16:20:05.298772Z",
  "updatedAt": "2026-10-03T09:12:44.870115Z"
}
```

| Member | Meaning |
|---|---|
| `id` | the manifest's `extension.id`; unique across the store; the client id is `ext-<id>` |
| `status` | **derived**: `SUSPENDED` if suspended, else `LIVE` if any version is live, else the status of the *youngest* version (the one uploaded last – not the highest version number) |
| `ownerSub` | the `sub` of the developer; `null` once the owner's account is deleted (an extension that runs for other people does not disappear with its author – reference implementation) |
| `name`, `version` | of the youngest version |
| `liveVersion` | the live version's number, or `null` |
| `kind` | `extension` or `link`; **fixed for the life of the id** |
| `clientId`, `clientAuth` | `ext-<id>` and `private_key_jwt` or `client_secret`; **`null` for a link** |
| `hasKey` | a public key is registered |
| `suspended`, `suspendedReason` | see §6 |
| `approvedScopes` | the union of the scopes of every `APPROVED` or `LIVE` version – what the identity service is set up for, and what the lock file carries |
| `versions[]` | every version, oldest first. `restricted`: does it ask for a restricted scope (needs four eyes). `approvals`: `{sub, at, role}` in the order given; `role` is the store role acted as, or `system` for an automatic approval. `manifest`: the validated manifest in TOML structure – **`null` in lists**, present when a single extension is read or returned by a state change |
| `urls` | environment name → `{entry, callback}`: the entry address (`<environment URL><manifest entry>`) and the web callback (`<environment URL>/auth/callback`). For a link `entry` is the link's address in every environment and `callback` is `null` |

Version statuses: `DRAFT`, `SUBMITTED`, `APPROVED`, `LIVE`, `SUPERSEDED`, `REJECTED`. An extension's `status` can additionally be `SUSPENDED`.

### 4.2 Smaller shapes

```json
{"id": "projects", "version": "1.2.0", "status": "SUBMITTED", "pendingApprovals": 1,
 "approvals": [{"sub": "6f1d9c1e-0000-4000-8000-000000000002", "at": "2026-10-03T09:01:12Z", "role": "store-reviewer"}]}
```
`approve` answer: `pendingApprovals` is the number of approvals still missing before the identity service is set up (0 = done).

```json
{"status": "LIVE", "version": "1.2.0", "checks": [
  {"name": "info", "ok": true, "message": "/_sdk/info answers"},
  {"name": "identity", "ok": true, "message": "id and clientId match (projects, ext-projects)"},
  {"name": "version", "ok": true, "message": "version matches (1.2.0)"},
  {"name": "ready", "ok": true, "message": "/readyz answers 200"}]}
```
`verify` answer.

```json
{"clientId": "ext-projects", "secret": "<shown once>"}
```
`rotate-secret` answer. The CLI accepts `secret` or `clientSecret`.

## 5. The endpoints

### `POST /store/extensions` – register an extension or a new version

Role `store-developer`. Two body formats:

| `Content-Type` | Body |
|---|---|
| `application/toml` (what the CLI sends) | the text of `extension.toml`, UTF-8 |
| `application/json` (or none) | `{"manifest": {…}}` – the manifest in the structure of the TOML file, snake_case keys |

```json
{"manifest": {
  "extension": {"id": "projects", "name": "Projects", "description": "Project reports",
                "version": "1.2.0", "entry": "/", "icon": "icon.svg",
                "name_localized": {"de": "Projekte"}},
  "consent": {"scopes": ["data-read"]},
  "services": [{"name": "export", "audience": "export-api", "scopes": ["export-write"], "mode": "user"}]
}}
```

`201` with the `StoreExtension`; the new version is `DRAFT`.

- A **new id** creates the extension, owned by the caller. The client id is `ext-<id>`.
- A **known id the caller owns** adds a version. A version number that exists is **replaced only while it is `DRAFT` or `REJECTED`** (the replacement becomes the youngest); otherwise `409 already_exists` – use a new number.
- An id that belongs to **someone else**, or to a fixed entry of the platform, answers `409 already_exists`: the id is taken, which is all the other person needs to know.
- The **kind never changes**: `422 invalid_manifest` at path `extension.kind` ("register a new id").
- The manifest is validated as in §7 (`422 invalid_manifest`, all errors at once). Two requests registering the same id at the same time: the second gets `409 already_exists`.

### `GET /store/extensions?status=&mine=` and `GET /store/extensions/{id}`

Role: any store role. The list is ordered by `id`, versions without `manifest`. Developers see their own; reviewers and admins see everything (`mine=true` narrows). `status` is a **derived** status (`DRAFT` … `REJECTED`, `SUSPENDED`), otherwise `422 invalid_request`. A single read includes manifests.

### `DELETE /store/extensions/{id}`

The owner, **only while no version is live**, or any `store-admin`. `204`. The client at the identity service goes too (see [oauth-service.md](oauth-service.md) §12); if that fails the extension stays (`502`). Installations of the extension disappear with it.

### `PUT /store/extensions/{id}/key` – register the public key

Role `store-developer`, owner. Body `{"jwk": {…}}`; `204`.

- `kty` MUST be `RSA` (at least 2048 bits) or `EC` (`P-256`, `P-384`, `P-521`). **Any private member** (`d`, `p`, `q`, `dp`, `dq`, `qi`, `oth`, `k`) is refused with `422 invalid_key` *before anything is written*; the message names the members, never their values.
- The store keeps only `kty`, `kid`, `use`, `alg` and the public members (`n`, `e` / `crv`, `x`, `y`); anything else is dropped.
- There is **one key per extension**: a new key replaces the old one. If a version is already approved and the client exists at the identity service, the service is updated **first**; if that fails the stored key is unchanged. The swap is immediate – the gap between uploading the new key and deploying the new private key is an outage, because every sign-in, refresh and exchange authenticates with the key.
- `409 invalid_state` for a link ("a link has no client; there is no key"), for an extension that authenticates with a client secret, and for a suspended extension whose client exists.

### `POST /store/extensions/{id}/submit`

Role `store-developer`, owner. Optional body `{"version": "1.2.0"}` (default: the youngest `DRAFT`). `200` with the `StoreExtension`.

`DRAFT → SUBMITTED`. The manifest is validated again (rule 7 and policy can have changed since registration). For `client_auth = "private_key_jwt"` the public key MUST be registered first (`409 invalid_state`): a review would otherwise approve an extension whose client cannot be set up. A link needs no key. `409 invalid_state` if there is no draft. For the **automatic approval** of an update see §6.

### `POST /store/extensions/{id}/approve`

Role `store-reviewer` or `store-admin`. Optional body `{"note": "…", "version": "1.2.0"}` (default: the youngest `SUBMITTED`). `200` with the approve answer of §4.2.

- Records the caller's approval (calling twice adds nothing: **the same person counts once**) and the optional `note` as `reviewNote`.
- When the approval rule of §6 is satisfied, **the store provisions the identity service** and the version becomes `APPROVED`. If provisioning fails the answer is `502 provisioning_failed` or `503 provisioning_unconfigured`; the approval and a log line saying why are **kept**, the version stays `SUBMITTED` – nothing is half-approved – and approving again retries.
- A link needs no provisioning: it is `APPROVED` as soon as the rule is satisfied, also when the identity service is unreachable.
- `409 invalid_state` if there is no `SUBMITTED` version or the extension is suspended.

### `POST /store/extensions/{id}/reject`

Role `store-reviewer` or `store-admin`. Body `{"reason": "…"}` (**required**, non-empty; optional `version`). `200` with the `StoreExtension`. `SUBMITTED → REJECTED`; the reason is the version's `reviewNote`. A rejected version number can be replaced by registering it again.

### `GET /store/extensions/{id}/auth-bundle?env=prod`

Role `store-developer`, owner. `200`, `Content-Type: application/zip`, `Content-Disposition: attachment; filename="auth-bundle-<id>-<env>.zip"`. The contents are specified in [auth-bundle.md](auth-bundle.md).

- Available from `APPROVED` on, for the **newest** version that is `APPROVED` or `LIVE`; before that `409 invalid_state`. A link has no bundle: `409 invalid_state`.
- `env` defaults to the environment this store serves; an unknown one is `422 unknown_environment`.
- Issuing a bundle writes an event.

### `POST /store/extensions/{id}/rotate-secret`

Role `store-developer`, owner. Only for `client_auth = "client_secret"`; otherwise `409 invalid_state` (also for a link, for a client that does not exist yet, and for a suspended extension). `200` `{"clientId", "secret"}`: a new secret, **shown once**; the old one stops working. The store never stores the secret and never writes it to the log or a bundle. A reviewer who triggers the approval never sees it; **the first secret of an extension is the one `rotate-secret` returns**.

### `POST /store/extensions/{id}/verify?env=prod`

Role `store-developer`, owner. The deployment check of §9. `200` with the verify answer of §4.2 – `status` is `LIVE` if every check passed, otherwise `APPROVED` (the HTTP status is 200 either way; the CLI exits with 0 only for `LIVE`).

`409 invalid_state` if no version is `APPROVED` or the extension is suspended; `422 wrong_environment` if `env` is not the environment this store serves ("this store can only check a deployment there"). On success the version becomes `LIVE`, and **every older version that is `LIVE` or `APPROVED` becomes `SUPERSEDED`**.

### `POST /store/extensions/{id}/suspend` and `…/unsuspend`

Role `store-admin`. Body `{"reason": "…"}` (required). `200` with the `StoreExtension`.

- **Suspend** sets the flag **first** and keeps it even if the identity service cannot be reached (a suspension that waits for the identity provider is no suspension); then the client at the identity service is disabled. Repeating the call retries that step. The extension leaves the catalog at once, and tokens issued to it are refused by services that consult the store's list of approved clients.
- **Unsuspend** enables the client **first**; if that fails the extension stays suspended. `409 invalid_state` if it is not suspended.
- A suspended extension cannot be approved, verified, given a key, or have its secret rotated (`409 invalid_state`): that would set the client up again behind the admin's back.
- A link is suspended and deleted without touching the identity service.

### `GET /store/services`, `PUT /store/services/{audience}`, `DELETE /store/services/{audience}`

See §11.

### `GET /store/events?extension=&limit=&offset=`

Role reviewer or admin. Newest first; `limit` 1–500 (default 100), `offset` ≥ 0 (otherwise 422).

```json
[{"id": 412, "at": "2026-10-03T09:05:40.120301Z", "actorSub": "6f1d9c1e-0000-4000-8000-000000000003",
  "actorRole": "store-admin", "action": "approved", "extensionId": "projects", "version": "1.2.0",
  "detail": {"approvers": ["6f1d9c1e-0000-4000-8000-000000000002", "6f1d9c1e-0000-4000-8000-000000000003"], "auto": false}}]
```

The log is **append-only** and records every step **and every change made at the identity service** (time, actor `sub`, strongest role, action, extension, version, detail). It is what a review answers to a year later. The `sub` stays in the log when an account is deleted: it is an opaque identifier. Action names are the store's business; the reference implementation uses, for example, `extension_created`, `version_created`, `version_replaced`, `key_registered`, `submitted`, `approval_recorded`, `approved`, `rejected`, `verified`, `verify_failed`, `live`, `superseded`, `bundle_issued`, `suspended`, `unsuspended`, `deleted`, `keycloak_provisioned` (and a `…_failed` twin for every provisioning step), `service_saved`, `service_deleted`.

## 6. The lifecycle

```
Version:  DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED ──verify──▶ LIVE ──(a newer version goes live)──▶ SUPERSEDED
                                │
                                └──reject──▶ REJECTED
```

| Transition | Who | Rule |
|---|---|---|
| register → `DRAFT` | developer | manifest valid (§7) |
| `DRAFT → SUBMITTED` | owner | key registered (`private_key_jwt`); manifest valid |
| `SUBMITTED → APPROVED` | reviewer / admin, **or the store itself** | approval rule below; identity service provisioned |
| `SUBMITTED → REJECTED` | reviewer / admin | reason required |
| `APPROVED → LIVE` | owner (`verify`) | all deployment checks pass (§9); a link: immediate |
| `LIVE → SUPERSEDED` | the store | another version went live |

**Approval rule.** A version with **no restricted scope** needs one approval by any reviewer or admin. A version with **restricted scopes** (§11) needs **four eyes**: at least two different people, and the **completing** approval MUST come from a `store-admin` who is not among the earlier approvers. Two clicks of one person, or a second reviewer who is not an admin, are not four eyes. (`pendingApprovals` counts what is missing.) A scope the catalog does not know counts as restricted: if the catalog lost a scope that a version still asks for, the strict answer is the safe one.

**Automatic approval.** A new version of an extension that already has a **live** version, that is **not suspended**, is `APPROVED` at `submit` by the actor `system` (recorded as an event) – no review – if it asks for nothing more than the live version:

- no scope and no service the live version did not have, with the same `mode` (`user` → `service` gives the extension a service account, which counts as *more*; a service's *name* does not matter, it is only an alias in code);
- no host (`extension.hosts`) the live version did not have – a new host widens what the host app's WebView may load;
- the same `client_auth`;
- for a **link**: the same origin (scheme, host, port) as the live version; a different path is the same link.

(Note that a new host or a changed `client_auth` count as "more" just like a new scope or service.) If provisioning fails during the shortcut, the version falls back to `SUBMITTED` and waits for a reviewer like any other – the owner could not retry, so an error would only strand them. Until a newer version is approved, **the live version stays in the catalog and at the identity service unchanged**; the new scopes are attached when the new version is approved.

**Suspension.** `SUSPENDED` is a flag on the extension, not a version status. It hides the extension from the catalog, disables its client (no refresh, no exchange), blocks every step that would set the client up again, and – if the platform keeps a list of approved clients for its own services – removes it from there. Installations survive a suspension (the person may remove an app that was suspended meanwhile; removal is always allowed).

**Deletion.** Owner while nothing is live, or admin. The client at the identity service and the installations go with it.

**Account deletion** (reference implementation, optional): when a person deletes their account the store removes their installations and the extensions they own that never reached the identity service (drafts, rejected); every other one is *orphaned* (`ownerSub = null`) and stays under the admins' control.

## 7. Manifest validation

The store judges every manifest by the **same rules as the SDK** (`appext.manifest`), and the two MUST reach the same verdict. Rules 1–6 and 8 are specified in [../manifest.md](../manifest.md); the shared cases that pin them down – every `valid/*.toml` must be accepted, every `invalid/*.toml` must fail with **exactly** the listed error paths – are in [../../conformance/manifests/README.md](../../conformance/manifests/README.md). A store runs those cases against its validator; the messages are not part of the contract, the **paths** are.

| Rule | Subject |
|---|---|
| 1 | required keys, id pattern (`^[a-z][a-z0-9-]{1,38}[a-z0-9]$`), SemVer version, `entry` is a path on the extension's origin |
| 2 | scope names (`^[a-z][a-z0-9-]*$`); **no duplicate** across `consent` and `services` |
| 3 | `[[services]]`: name (`^[a-z][a-z0-9_]*$`, unique), `audience`, at least one scope, **required** `mode` (`user` or `service`) |
| 4 | `audience_roles`, `min_app_version`, `hosts`, `client_auth`, `display`, `dev_port` |
| 5 | `…_localized` tables |
| 6 | **unknown keys are an error** in every table |
| **7** | **the service catalog (the store's alone, the SDK has no catalog)** |
| 8 | `kind = "link"` rules (§8) |

**Rule 7.** Against the service catalog (§11), reporting all violations:

| Path | Message (informative) |
|---|---|
| `consent.scopes[j]` | the scope is not in the service catalog (it MUST belong to some service) |
| `services[i].audience` | the audience is not in the service catalog |
| `services[i].scopes[j]` | the scope does not belong to the service named in `audience` |

Restricted scopes are **allowed**: they cost a second approval, not a rejection.

**Policy** (a store's own decisions, reported in the same `errors` format): the reference implementation refuses `client_auth = "client_secret"` unless the operator allows it (`extension.client_auth`), and `http` addresses in a link unless the store serves a development environment (`extension.entry`, `extension.icon`).

Validation runs at registration, again at **submit** and again at **approve**: the catalog changes, and a scope removed from it must not be provisioned. A store MAY report rule 7 only after rules 1–6 and 8 pass (the reference implementation does: the catalog check needs a well-formed manifest); within each stage **all** violations are reported at once.

A manifest that does not parse as TOML is a single error at the empty path `""`.

## 8. Links

A **link** (`kind = "link"`) is an entry the host app opens in the system browser: no server of the SDK behind it, no client, no key, no bundle, nothing passed on. It has the same states with everything that belongs to a server left out:

| Step | For a link |
|---|---|
| register | rule 8; the kind is fixed for the id; the answer has `kind: "link"`, `clientId: null`, `clientAuth: null`, `hasKey: false`, `urls[env] = {"entry": "<address>", "callback": null}` |
| `PUT …/key`, `GET …/auth-bundle`, `POST …/rotate-secret` | `409 invalid_state` |
| submit | no key needed |
| approve | **the review is the only gate**: the reviewer approves *where the link leads*. Nothing is provisioned, so it also works when the identity service is down |
| verify | **no deployment is probed**: the store must not fetch an address a developer typed in, or it can be pointed at its own network (SSRF). The answer has one check, `link`, which passes: `APPROVED → LIVE` |
| new version | the same origin → automatic approval; another scheme, host or port → review |
| suspend, delete | as for an extension, without the identity service |

In the catalog a link has `kind: "link"`, `display: "external"`, `callbackUrl`, `clientId` and `callbackScheme` all `null`, `scopes: []`, and `hosts` = the host of `entryUrl`. A host app that does not know links ignores such an entry, because it requires `callbackScheme` (see [host-app.md](host-app.md)).

## 9. The deployment check (`verify`)

For an extension (not a link) the store asks the **running deployment** itself. The SDK serves what is needed, so nothing is required of the developer beyond deploying with the auth bundle:

| Request | Answer the SDK gives | What the store checks |
|---|---|---|
| `GET {url}/_sdk/info` | `200 {"sdk": "appext", "sdkVersion": "0.1.0", "id": "projects", "version": "1.2.0", "clientId": "ext-projects", "environment": "prod"}` – public, no session | JSON object; `id` equals the extension id; `clientId` equals the client id; `version` equals the approved version |
| `GET {url}/readyz` | `200 {"status": "ready", "checks": {"identity_provider": "ok", "session_store": "ok"}}` or `503 {"status": "unavailable", …}` when discovery or the session store is unreachable | status `200` |

The answer lists **four checks in a fixed order**, so a caller can show them as a list: `info` (the SDK answers), `identity` (`id` and `clientId`), `version`, `ready`. If `info` fails, `identity` and `version` fail with "not checked: /_sdk/info is not usable". **No check at all is not a pass.** `sdk`, `sdkVersion` and `environment` are informational.

The probe is careful with somebody else's code: it is a plain `GET` with no credentials, a 5 s timeout, at most **64 KiB** of response, at most 3 redirects and **only within the same host and port** (an upgrade from `http` to `https` on the default port counts as the same host, a downgrade does not). It never raises for an unreachable extension; that is a failed check. The address comes from the environment's URL template and the validated extension id, never from anything the developer typed in.

**Where the store looks.** At `{environment URL}`, or – if the environment defines a `probeUrlTemplate` – there. A store running in a container reaches a deployment on the developer's machine at `host.docker.internal`, not at `127.0.0.1`; only a development environment may define it, and the catalog keeps the public address.

## 10. Environments

An environment is a **named place an extension runs** (`local`, `prod`, …). The store knows each as:

| Property | Meaning |
|---|---|
| name | `^[a-z][a-z0-9_-]{0,30}$` |
| URL template | the extension's origin, with `{id}` and – only in a `dev` environment – `{dev_port}` (from the manifest; default 8000) |
| `dev` | a development environment: `http` is allowed for `127.0.0.1` and `localhost`, and `{dev_port}` has a meaning. Anywhere else the template MUST produce `https` |
| `probeUrlTemplate` | optional, `dev` only; see §9 |

```json
{
  "local": {"urlTemplate": "http://127.0.0.1:{dev_port}", "dev": true},
  "prod":  {"urlTemplate": "https://{id}.apps.example.com"}
}
```

- The environments are checked **when the store starts**, not when somebody approves: a template that yields `http://` for production would otherwise show up as an identity-service client that accepts a redirect over plain HTTP.
- **One client per extension**, shared by all environments: its redirect URIs are the union of `{URL}/auth/callback` of every environment, plus the platform's app redirect URI.
- **One store installation serves exactly one environment.** That one decides the addresses and `hosts` in the catalog, the single back-channel logout URL registered at the client, and which deployment `verify` can look at. Run one store (and one realm or tenant) per environment if you need back-channel logout in all of them.
- The environment the SDK is told about is the bundle's `APPEXT_ENV` ([auth-bundle.md](auth-bundle.md)). **`local` is reserved**: the SDK reads it as "development at the desk" and relaxes its production checks.
- `appext store bundle` and `verify` take `--env`. Their **default** is `local` when the store address is a loopback host and `prod` for any other. A store that names its environments differently makes developers pass `--env` every time – name yours `local` and `prod` unless you have a reason not to.

## 11. The service catalog

The target services write their scopes down here. An extension can only ask for scopes that are listed; the **owner of the service** – not each developer – decides what is worth a person's consent, and the consent text is the same for every extension.

```json
[
  {
    "audience": "data-api",
    "title": "Data service",
    "baseUrls": {"local": "http://127.0.0.1:8000/api/v1", "prod": "https://api.example.com/api/v1"},
    "managedBy": "store",
    "scopes": [
      {"name": "data-read", "consentText": "Read your data",
       "consentTextLocalized": {"de": "Ihre Daten lesen"}, "restricted": false, "isDefault": true},
      {"name": "data-write", "consentText": "Change your data",
       "consentTextLocalized": {"de": "Change your data (de)"}, "restricted": true, "isDefault": false}
    ]
  }
]
```

`GET /store/services` (any store role) returns a list of these. `PUT /store/services/{audience}` (admin) creates or replaces one, with this body (unknown members are errors):

```json
{
  "title": "Export service",
  "baseUrls": {"prod": "https://export.example.com/api"},
  "scopes": [
    {"name": "export-write", "consentText": "Create exports in your name", "restricted": true}
  ]
}
```

| Rule | Value |
|---|---|
| `audience` | `^[a-z][a-z0-9-]{0,62}$`; the identifier of the service **at the identity service** – the service must exist there (see [oauth-service.md](oauth-service.md) §10) |
| `title` | non-empty |
| `baseUrls` | environment name → base URL. `https`; `http` only for `127.0.0.1` or `localhost`. It lands in the auth bundle as `APPEXT_SERVICE_<NAME>_URL` |
| scope `name` | `^[a-z][a-z0-9-]*$`, unique **across the whole catalog** (`409 already_exists` otherwise: a scope definition is realm-wide at the identity service, and `[consent].scopes` names no service) |
| `consentText` | **required**, English; `consentTextLocalized` maps language to non-empty text |
| `restricted` | the scope needs four eyes (§6). Read **when a version is approved**, not when the scope was written |
| `isDefault` | stored and returned; the reference implementation does not use it to decide the assignment at the identity service – that follows from how a manifest uses the scope |
| removal | a scope, or the whole service, **cannot be removed** while a version in review (`SUBMITTED`), `APPROVED` or `LIVE` uses it (`409 invalid_state`): the next provisioning would find nothing to attach and the approved extension would stop working at its next sign-in |
| `DELETE` | `204`; `404 not_found` for an unknown audience |

`managedBy` says who maintains an entry: `store` (an admin, through this API) or `registry` (built into the platform and read-only; the reference implementation has one for its own data service, and adds a `permissions` list to its scopes – not part of the contract). Changing a built-in entry is `409 invalid_state`.

## 12. The catalog API (for the host app)

Everything the host app may **open** is what stood in the last catalog it loaded successfully. The catalog is the one place that decides what a person can see, and it decides on the server.

### `GET {base}/catalog`

Authentication: the host app's token. Any signed-in person; **no store role, no organisation**. A token issued to an extension: `403 app_only`. No token: `401`.

```
GET /api/v1/catalog
Authorization: Bearer <host app token>
If-None-Match: "0f3c1c8d5b6a4e2f9d1c7a8b6e5f4d3c"
```

- `200` with the document below, `ETag: "<hash>"` and `Cache-Control: private, no-cache`.
- `304` with an empty body and the same `ETag` and `Cache-Control` when `If-None-Match` matches. The comparison accepts a list, weak validators (`W/"…"`) and `*`.
- **The ETag is a hash of the answer, not of the catalog.** The answer is personal (`installed`, the role filter), so a change of what *this person* sees MUST change the tag, and two people with the same answer get the same tag. `catalogVersion` is a human-readable label (the time of the last change in the store), not a cache key.

```json
{
  "catalogVersion": "2026-10-03T12:00:00Z",
  "redirectUri": "com.example.app:/callback",
  "extensions": [
    {
      "id": "projects",
      "title": "Projects",
      "description": "Shows the signed-in person's projects",
      "titleLocalized": {"de": "Projekte"},
      "descriptionLocalized": {"de": "Zeigt die Projekte der angemeldeten Person"},
      "version": "1.2.0",
      "iconUrl": "https://projects.apps.example.com/_sdk/icon",
      "entryUrl": "https://projects.apps.example.com/",
      "callbackUrl": "https://projects.apps.example.com/auth/callback",
      "clientId": "ext-projects",
      "callbackScheme": "com.example.app",
      "hosts": ["projects.apps.example.com"],
      "scopes": [
        {"name": "data-read", "consentText": "Read your data",
         "consentTextLocalized": {"de": "Ihre Daten lesen"}, "isDefault": true, "service": "data-api"}
      ],
      "minAppVersion": "1.0.0",
      "kind": "extension",
      "display": "in_app",
      "installed": true,
      "builtin": false
    },
    {
      "id": "shop",
      "title": "Shop",
      "description": "Our shop – opens in your browser",
      "titleLocalized": {"de": "Shop"},
      "descriptionLocalized": {"de": "Our shop - opens in the browser (de)"},
      "version": "1.0.0",
      "iconUrl": "https://shop.example.com/static/icon.svg",
      "entryUrl": "https://shop.example.com/app?ref=platform",
      "callbackUrl": null,
      "clientId": null,
      "callbackScheme": null,
      "hosts": ["shop.example.com"],
      "scopes": [],
      "minAppVersion": null,
      "kind": "link",
      "display": "external",
      "installed": false,
      "builtin": false
    }
  ]
}
```

**Which entries.** Only **live**, **not suspended** extensions, described by their **live** version (a newer version in review does not show). An entry with `audience_roles` in its manifest is invisible to everyone who has none of those roles – an extension for a restricted audience must not even be *visible* to others, and the host app cannot be trusted to hide it. Roles come from the host app's token. The platform MAY add fixed entries of its own (`builtin: true`; always `installed`, always `in_app`).

| Member | Meaning |
|---|---|
| `id`, `title`, `description` (+ `…Localized`) | title and description in English with translations next to them |
| `version` | the live version (SemVer) |
| `iconUrl` | `{environment URL}/_sdk/icon` for an extension; for a link the manifest's `icon` address or `null` |
| `entryUrl` | `{environment URL}{manifest entry}` – always on the extension's own origin; for a link the link's address |
| `callbackUrl` | the extension's web callback `{environment URL}/auth/callback`; **`null` for a link** |
| `clientId` | `ext-<id>`; **`null` for a link** |
| `callbackScheme` | the scheme of the platform's app redirect URI (`com.example.app`); **`null` for a link**. The host app drops entries whose scheme it does not serve |
| `hosts` | lower-case, sorted: the host of the environment URL plus `extension.hosts` (images, fonts); for a link the host of `entryUrl` |
| `scopes[]` | **what the person will be asked to confirm**, with the texts, so the host app can say so before the first opening (the binding consent is the one at the identity service). Consent scopes and scopes of services called **on behalf of the person** (`mode = "user"`) have `isDefault: true`; scopes of the extension's own calls (`mode = "service"`) are listed with `isDefault: false` – the person is never asked about those. `service` is the audience. Empty for a link |
| `minAppVersion` | from the manifest, or `null` |
| `kind` | `extension` or `link` |
| `display` | `in_app` or `external`; from the manifest; a link is always `external` |
| `installed` | the person added it |
| `builtin` | part of the platform's fixed set |

A host app reads an unknown `kind` or `display` as `extension` / `in_app`, so new values can be added later.

### `GET {base}/extensions`

The extensions **the person added**, plus the fixed ones: a JSON **array** (no envelope) of the same entries. Same authentication as the catalog.

### `PUT {base}/extensions/{id}/installation` and `DELETE …`

`PUT` adds the extension for this person: idempotent, `204`. `404 not_found` if the extension is **not in this person's catalog** – it does not exist, it is not live, it is suspended, or their roles do not meet its `audience_roles`; these cases MUST look the same from outside. `DELETE` removes it: idempotent, `204`, **always allowed**, also for an extension that was suspended meanwhile. Installations are keyed by the person's `sub`; adding an app needs no other account at the platform.

## 13. A walk-through

Register, key, submit, approve (two people, restricted scope), bundle, verify – with `curl`. `$STORE` is `https://api.example.com/api/v1/store`; the tokens come from `appext store login` or `APPEXT_STORE_TOKEN`.

```sh
# the developer registers the manifest
curl -sS -X POST "$STORE/extensions" \
  -H "Authorization: Bearer $DEV" -H "Content-Type: application/toml" \
  --data-binary @extension.toml
# 201 → StoreExtension, status "DRAFT", versions[0].status "DRAFT", hasKey false

curl -sS -X PUT "$STORE/extensions/projects/key" \
  -H "Authorization: Bearer $DEV" -H "Content-Type: application/json" \
  -d '{"jwk": {"kty": "RSA", "kid": "4Zk83CqXb", "use": "sig", "alg": "RS256", "n": "0vx7agoe…", "e": "AQAB"}}'
# 204

curl -sS -X POST "$STORE/extensions/projects/submit" -H "Authorization: Bearer $DEV"
# 200 → versions[0].status "SUBMITTED", restricted true (export-write is restricted)

curl -sS -X POST "$STORE/extensions/projects/approve" \
  -H "Authorization: Bearer $REVIEWER" -H "Content-Type: application/json" -d '{"note": "scopes fit the use case"}'
# 200 {"id":"projects","version":"1.2.0","status":"SUBMITTED","pendingApprovals":1,"approvals":[…]}

curl -sS -X POST "$STORE/extensions/projects/approve" -H "Authorization: Bearer $ADMIN"
# 200 {"id":"projects","version":"1.2.0","status":"APPROVED","pendingApprovals":0,"approvals":[…]}
#   the identity service now has the client, scopes, mappers, redirect URIs

curl -sS -G "$STORE/extensions/projects/auth-bundle" --data-urlencode env=prod \
  -H "Authorization: Bearer $DEV" -o auth-bundle.zip
# 200 application/zip; the developer deploys the extension with it, then:

curl -sS -X POST "$STORE/extensions/projects/verify?env=prod" -H "Authorization: Bearer $DEV"
# 200 {"status":"LIVE","version":"1.2.0","checks":[…]}  → in the catalog from now on
```

Errors the same walk-through can produce:

```json
{"detail": {"code": "invalid_manifest", "message": "The manifest is not valid.",
            "errors": [{"path": "extension.id", "message": "'Projects' does not match ^[a-z][a-z0-9-]{1,38}[a-z0-9]$ (lower case DNS label, 3 to 40 characters)"},
                       {"path": "services[0].mode", "message": "required"}]}}
```
```json
{"detail": {"code": "invalid_state", "message": "projects: there is no SUBMITTED version to approve."}}
```
```json
{"detail": {"code": "forbidden", "message": "This needs the role store-developer."}}
```
```json
{"detail": {"code": "invalid_key", "message": "The key contains private key material (d, p, q). Send the public key only."}}
```

## 14. Conformance checklist

**Basics**
- [ ] Every row marked "CLI" in §3 exists, with the role rules of §1 and the error shape of §2 – also for framework-level validation errors.
- [ ] Tokens are verified (signature, `iss`, `exp`, own audience); roles come from the token only; tokens issued to extension clients get `403 app_only` on store **and** catalog routes.
- [ ] Someone who may not see an extension gets `404`, exactly as for one that does not exist.

**Lifecycle**
- [ ] The states and transitions of §6; the extension's `status` is derived as specified.
- [ ] `submit` needs the public key for `private_key_jwt`; a private JWK is refused with `invalid_key` before anything is stored.
- [ ] Restricted scopes need two different people and a completing `store-admin`; the same person counts once.
- [ ] A failed provisioning leaves the version `SUBMITTED`, keeps the approval and the reason in the log, and answers 502 / 503.
- [ ] The automatic approval shortcut applies only without new scopes, services, modes, hosts, a changed `client_auth` (link: same origin) and not for a suspended extension.
- [ ] A version going live supersedes older `LIVE` and `APPROVED` ones; a version number is replaceable only while `DRAFT` or `REJECTED`.
- [ ] Suspension takes effect immediately even if the identity service is down; unsuspension enables the client first.

**Manifests and catalog of services**
- [ ] Your validator passes every case in `conformance/manifests/` (valid accepted, invalid rejected with exactly the listed paths) and adds rule 7 with the paths of §7.
- [ ] A link: no key, no bundle, no secret, `verify` immediate, same-origin updates auto-approved, kind never changes.
- [ ] Scope names are unique across the service catalog; scopes in use by `SUBMITTED`, `APPROVED` or `LIVE` versions cannot be removed.

**Deployment**
- [ ] `verify` fetches `/_sdk/info` and `/readyz` with the limits of §9 and returns the four checks in order; no checks is not a pass.
- [ ] Environments are validated at start (`https` except loopback in a `dev` environment); the serving environment decides catalog addresses; `verify` for another one answers `422 wrong_environment`.
- [ ] The environment names are `local` and `prod`, or developers know to pass `--env`; no production environment is called `local`.

**Bundle**
- [ ] `GET …/auth-bundle` meets [auth-bundle.md](auth-bundle.md): ZIP, no secret, the newest approved version, 409 before approval and for links.

**Catalog API**
- [ ] `GET /catalog` answers 200 / 304 with an ETag that changes with the person's answer; entries have every member of §12; links have `null` for `callbackUrl`, `clientId`, `callbackScheme`.
- [ ] Only live, non-suspended extensions appear, described by the live version, filtered by `audience_roles` on the server.
- [ ] `PUT` and `DELETE …/installation` are idempotent (`204`); `PUT` of an invisible extension is `404`; `DELETE` is always allowed.
