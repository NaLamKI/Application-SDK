# The manifest `extension.toml`

The SDK reads it at start-up; the App Store reads it when you upload it and, after
approval, builds the Keycloak client, the auth bundle and the catalog entry from it.
**No secret ever belongs in it** – keys and secrets arrive at run time as files or
environment variables ([deployment.md](deployment.md)). (A **link** – an entry that only opens a
web page in the browser – has no server, client or bundle at all: see [Links](#links).)

```toml
[extension]
id = "projektauswertung"
name = "Project analysis"
description = "Analyses across all projects"
version = "1.2.0"
entry = "/"
icon = "icon.svg"
min_app_version = "1.0.0"
hosts = []
audience_roles = []
client_auth = "private_key_jwt"
dev_port = 8100
display = "in_app"

[extension.name_localized]
de = "Projektauswertung"

[extension.description_localized]
de = "Auswertungen über alle Projekte"

[consent]
scopes = ["ext-data-read"]

[[services]]
name = "fmis"
audience = "fmis-api"
scopes = ["ext-stock-read"]
mode = "user"

[[services]]
name = "export"
audience = "export-api"
scopes = ["svc-export-write"]
mode = "service"
```

## `[extension]`

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | `^[a-z][a-z0-9-]{1,38}[a-z0-9]$` – a DNS label. The Keycloak client is `ext-<id>`, the production host `<id>.<apps domain>`. Never changes after registration. |
| `name` | yes | Title, **English** (the contract language); translations in `name_localized`. |
| `description` | no | Short description for the catalog, English. |
| `version` | yes | Semantic version `MAJOR.MINOR.PATCH`, optional `-pre-release`. Each registration is one version; the lock file pins it. |
| `entry` | yes | Path on the extension's own origin that the app opens: starts with one `/`, no scheme, no `//`, no backslash or control character. For a **link**: an absolute `https` address ([Links](#links)). |
| `icon` | yes | A file in the project, relative to the manifest. The extension serves it at `/_sdk/icon` (SVGs are served sandboxed: an icon cannot run script); the catalog's `iconUrl` points there. For a **link**: optional, and an address on the same host as `entry`. |
| `min_app_version` | no | Older app versions hide the extension. Semantic version. |
| `hosts` | no | Further hosts the WebView may load (images, fonts). Lower-case host names, no scheme, port or path. They also widen the default CSP for `img-src` and `font-src` (https only). Not allowed for a link. |
| `audience_roles` | no | Only people with one of these realm roles see the extension in the catalog. |
| `client_auth` | no | `private_key_jwt` (default) or `client_secret`. The store allows the latter only when the operator enabled it (`STORE_ALLOW_CLIENT_SECRET`). Not allowed for a link. |
| `dev_port` | no | Port for the `local` environment, 1024–65535. `appext dev` serves on it; the store registers `http://127.0.0.1:<dev_port>/auth/callback`. Default 8000 – which is where the FMIS backend runs on a developer machine, so the templates use 8100. Not allowed for a link. |
| `display` | no | Where the app shows the extension: `in_app` (default) or `external`. See [below](#display-in-the-app-or-in-the-browser). A link is always `external`. |
| `kind` | no | `extension` (default) – a web page with a server of its own that signs people in – or `link`: an entry the app opens in the system browser, with no server behind it. See [Links](#links). |
| `name_localized`, `description_localized` | no | Tables `language -> text`; the language code is `de` or `pt-BR`; texts are non-empty. |

## `display`: in the app or in the browser

```toml
display = "in_app"      # the default: the app shows the extension itself
display = "external"    # the app hands it to the system browser
```

`in_app` is what an extension has always been: the phone app shows it in a WebView, the web
app in a frame under its own header, with the sign-in handed over silently.

`external` makes the app a **jump-off point**: tapping the extension opens `entry` in the
system browser (a new tab on the web) and that is all the app does. Use it when your service
is a website or app of its own that is better off in the browser – or when you want people to
arrive at your own site. What changes:

* **The page is not embedded.** It may send the person anywhere (your own website, a universal
  link into your own app); the host restrictions of the WebView do not apply, and there is no
  bridge (`AppExt.close()`, `setTitle()` … are quiet no-ops, as in any browser tab).
* **No sign-in is run when the extension is added.** The page signs the person in when it
  is opened, like any other website; the consent screen comes then. Nothing about the login
  changes for the extension: it is still an OIDC client with the same scopes.
* **The SDK refuses framing.** The extension answers with `frame-ancestors 'none'`
  (and `X-Frame-Options: DENY`) even where `APPEXT_APP_ORIGINS` names the web app: it is
  never embedded, so the web app need not be allowed to.
* The catalog and the app say so *before* the tap: the detail screen shows "Opens in – Your
  browser" and the button reads "Open in browser"; the tile carries a small "leaves the app" sign.
* In the browser tab `bridge.js` still shows the bar "Back to FMIS" when a web app is
  configured; `<meta name="appext-shell" content="off">` hides it if your page has its own navigation.

`entry` stays a path on the extension's own origin either way (rule 1): an external extension
still *is* an extension – it has a service in the store, a Keycloak client and a deployment the
store checks. If you only want to point to a page elsewhere, you need no extension at all: a [link](#links)
is exactly that.

`appext manifest check` prints the value.

## Links

```toml
[extension]
id = "shop"
name = "Our shop"
description = "Seed and supplies"
version = "1.0.0"
kind = "link"
entry = "https://shop.example.com/fmis"
icon = "https://shop.example.com/static/icon.svg"   # optional: an address on the same host as entry

[extension.name_localized]
de = "Unser Shop"
```

`kind = "link"` makes the entry a **link**: an entry in the App Store that the FMIS app opens in
the system browser – and nothing more. There is **no server of the SDK behind it**: no Keycloak
client, no key, no deployment, no sign-in. The page receives **nothing from FMIS**: no token, no
person, no farm; the app only opens the address. Use a link to point people to a website or app
that already exists, run by you or by somebody else. `appext new <id> --template link` creates one.

**How it differs from `display = "external"`.** An ordinary extension with `display = "external"`
is still an extension: it has a server (its `entry` is a path on its own origin), a Keycloak
client, a key and a deployment that the store checks, and the page signs the person in itself.
A link has none of that – it is just the address.

What the manifest of a link looks like (rule 8):

* `entry` is an **absolute `https` address**: plain ASCII (write host names as punycode,
  percent-encode the rest), a host name, no user name or password, no whitespace or backslash, at
  most 2048 characters. `http` is allowed only for this machine (`127.0.0.1`, `localhost`, `::1`),
  and a store accepts even that only in a development environment.
* `icon` is **optional**, and if present an **address by the same rules, on the same host as
  `entry`** – not a file (a link has no project to serve one from). The app loads icons by itself,
  before anyone opened anything, so it does so only from the link's own host. Without an icon the
  app shows its fallback. `Manifest.icon` is `""` then.
* `display` is `external` and cannot be anything else: leave it out, or write `external`; `in_app`
  is an error. As for any `external` entry, the app and the catalog say so before the tap.
* **Refused** (one error at the key, whatever the value): `client_auth`, `dev_port`, `hosts` – they
  belong to a server – and a `[consent]` with scopes or any `[[services]]`: a link asks for no
  permissions and calls no service. An empty `[consent]` table is fine.
* Everything else works as for an extension: `id`, `name`, `description`, `version`,
  `min_app_version`, `audience_roles` (only those people see the link) and the `*_localized` tables.
* `kind` is fixed for an id: the store refuses a version of the other kind under the same id.

**The review is the only gate.** A link has no deployment the store could check, and it does not
fetch the address you typed in. A reviewer approves *the place the link leads to*. A new version
whose `entry` stays on the same scheme, host and port (another path or query is fine) needs no new
review; **another host sends it back to review**, as it is another link. How a link goes through the
store: [store.md](store.md#a-link).

`appext manifest check` prints `kind` and `entry`. An `Extension` cannot be made from a link:
`Extension.from_manifest` raises a `ManifestError` at `extension.kind` – there is nothing to run.

## `[consent]`

`scopes` – scopes the person confirms when they first open the extension (Default Client
Scopes of the Keycloak client). These are scopes that belong to the extension itself, not
to a service it calls on someone's behalf.

## `[[services]]`

One entry per service the extension calls (`ext.service("<name>")`).

| Key | Meaning |
|---|---|
| `name` | The name in code: `^[a-z][a-z0-9_]*$`, unique. Sets the variable `APPEXT_SERVICE_<NAME>_URL`. |
| `audience` | The Keycloak client of the target service, not empty. Must exist in the store's service catalog. |
| `scopes` | At least one. The scopes of *this* service that the exchanged token carries. |
| `mode` | `user` – on behalf of the signed-in person (token exchange); `service` – as the extension itself (client credentials). Default `user`. |

For `mode = "user"` the SDK asks for these scopes **at sign-in**, so that the consent screen
already covers every later exchange (Keycloak lets an exchange pass only for scopes the
person agreed to). To see which scopes a service offers: `appext store services`.

## The rules

`appext manifest check` applies rules 1–6 and 8; the store applies the same when you upload and
adds rule 7. The conformance cases in `sdk/conformance/manifests/` run against both
implementations, so the verdict is the same everywhere.

1. `id`, `name`, `version`, `entry`, `icon` are present; `id` matches its pattern; `version`
   is semantic; `entry` is a path on the own origin. (A link: rule 8 – `icon` is optional and
   `entry` an address.)
2. Scope names match `^[a-z][a-z0-9-]*$`, and **no scope appears twice** across `[consent]`
   and all `[[services]]`.
3. Service names are code-safe and unique; `mode` is `user` or `service`; `audience` is not
   empty; every service has at least one scope.
4. `audience_roles` are role names, `min_app_version` is semantic, `hosts` are host names,
   `client_auth` is one of the two, `display` is `in_app` or `external`, `dev_port` is 1024–65535.
5. `*_localized` tables map a language code to a non-empty text.
6. **Unknown keys are errors.** A typo such as `scope = [...]` would otherwise be accepted and
   grant nothing, silently.
7. *(Store only.)* Every `audience` exists in the service catalog; every scope belongs to
   its service (a `consent` scope: to any service); a *restricted* scope is allowed but
   needs a second approval ([store.md](store.md)).
8. **A link** (`kind = "link"`; `kind` is `extension` or `link`). `entry` is an absolute `https`
   address – `http` only for `127.0.0.1`, `localhost`, `::1` – in plain ASCII, with a host name, no
   user name or password, no whitespace or backslash, at most 2048 characters. `icon` is optional;
   if present, an address by the same rules on the same host as `entry`. `display` is omitted or
   `external`. `client_auth`, `dev_port`, `hosts`, non-empty `[consent] scopes` and `[[services]]`
   are errors. ([Links](#links))

`appext manifest check` lists **every** broken rule, not the first:

```
extension.toml: INVALID
the manifest breaks 2 rule(s):
  extension.id: must match ^[a-z][a-z0-9-]{1,38}[a-z0-9]$ (a DNS label, 3-40 characters)
  services[0].mode: must be one of user, service
```

## What changes when

A new version with **the same scopes and services** goes live after the deployment check,
without review. **More** scopes or services send it back to review; until a reviewer
approves, the old version stays in the catalog. At start the SDK compares the manifest with
the **lock file** of the auth bundle and refuses to run if the manifest asks for more than
was approved (`LockError`) – a forgotten review shows up before the deployment goes live,
not as a failing token exchange in production. A link has no lock file: nothing runs for it
and nothing is exchanged.
