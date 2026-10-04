# Pointing the SDK at a platform

For extension developers who set up their machine for a platform, and for platform operators who want
to hand developers a ready-made configuration.

`appext` is not tied to one platform. A **platform** is any system that follows the reference
architecture: it offers an OAuth 2.0 / OpenID Connect service (the **issuer**; Keycloak is the
reference implementation), an **App Store** that registers, reviews and lists extensions (the
**store**), and optionally a **host app** that opens extensions in a WebView or a frame. Everything
the SDK needs to know about it fits in a handful of settings, collected in a **platform file**.

**The SDK has no built-in platform.** There is no default issuer, store URL, return address or
service URL: a setting that is needed and found nowhere is an error that tells you how to provide it
([below](#the-error-when-nothing-is-configured)).

## The platform file

A platform file is TOML with one table, `[platform]`:

```toml
[platform]
name = "Acme Platform"                              # shown in messages and in the "Back to …" bar
issuer = "https://auth.acme.example/realms/acme"    # the OAuth / OpenID Connect service
store_url = "https://api.acme.example/api/v1"       # the App Store API; `/store/…` is appended
cli_client_id = "appext-cli"                        # optional: the CLI's public client at the issuer
app_redirect_uri = "com.acme.app.ext:/callback"     # optional: where the host app takes the sign-in back

[platform.services]                                 # optional: URLs of target services on your machine
data-api = "http://127.0.0.1:8000/api/v1"

[platform.starter]                                  # optional: what `appext new` puts into a manifest
service = "data"
audience = "data-api"
scope = "data-read"
```

Every key is optional in the file – a setting can come from an option or a variable instead – but
a command that needs a setting stops when it is nowhere.

| Key | Type | Meaning |
|---|---|---|
| `name` | string | A short, human name of the platform. It appears in messages, in the texts `appext new` writes into a new project, on the consent text of a realm made by `appext keycloak export`, and – through `appext dev` – as `APPEXT_APP_NAME`, the name in the bar "Back to …" ([bridge.md](bridge.md#in-a-browser-tab-the-way-back)). Without it, messages say "the platform". |
| `issuer` | http(s) URL | The OAuth / OpenID Connect service. Its discovery document `<issuer>/.well-known/openid-configuration` must be reachable. A trailing `/` is dropped. Needed by `appext dev`, `appext store …` and `appext keycloak export` (for the realm name). |
| `store_url` | http(s) URL | The base of the App Store API; the CLI appends `/store/…` to it (`https://api.acme.example/api/v1` → `https://api.acme.example/api/v1/store/extensions`). A trailing `/` is dropped. Needed by every `appext store …` command. |
| `cli_client_id` | string | The public client the CLI signs in with (device authorization grant). Default `appext-cli`. Used by `appext store login`. |
| `app_redirect_uri` | URI | The return address of the platform's host app: the URI the sign-in is handed back to when the host app runs the extension in its WebView. A URI with a scheme and no fragment (typically a custom scheme such as `com.acme.app.ext:/callback`). Leave it out if the platform has no host app that hands the sign-in over: the extension is then a website like any other ([authentication.md](authentication.md#browser-mode-and-app-mode)). |
| `[platform.services]` | table | Target services as `<audience> = "<base URL>"`, for development on this machine: the URL `appext dev` gives to a manifest service with that audience. Each URL is an http(s) URL; a trailing `/` is dropped. Quote an audience that has characters other than letters, digits, `-` and `_`. |
| `[platform.starter]` | table | The service call a *new* project starts with: `service` (the name in the manifest and in `ext.service("…")`), `audience` and `scope`. Defaults `data`, `data-api`, `data-read`. See [the starter service](#the-starter-service). |

**Validation.** `appext` reads a platform file strictly and reports **every** problem at once:

- the file must be valid TOML and readable;
- the only table at the top level is `platform`; any other is an error (`other: unknown table`);
- unknown keys are errors, in `[platform]` and in `[platform.starter]` (`platform.nme: unknown key`) – a
  typo would otherwise silently leave a setting unset;
- `issuer`, `store_url` and every `[platform.services]` entry must be http(s) URLs with a host;
- `name`, `cli_client_id`, `app_redirect_uri` and the `starter` keys must be non-empty strings;
- `app_redirect_uri` must have a scheme and no `#`.

```
$ appext store login --platform ./acme.toml
error: platform file: /home/you/acme.toml: other: unknown table; platform.nme: unknown key; platform.issuer must be an http(s) URL
  See docs/platform.md of the SDK for the format.
```

An empty file, or one without `[platform]`, is a platform with nothing set. **No secret ever belongs in
a platform file**: the file is for development at the desk and may be committed.

## Examples

### A platform with a host app

The file above: the issuer, the store, the host app's return address, one service on the developer's
machine and the starter service. It is what a platform's operator would publish for developers
([below](#for-platform-operators)).

### A Keycloak you run yourself

No platform, no App Store: you only want to run and try an extension against a Keycloak on your
laptop. Name the issuer, and give the service your extension calls a URL:

```toml
[platform]
name = "My Keycloak"
issuer = "http://localhost:8080/realms/local"

[platform.services]
data-api = "http://127.0.0.1:9000/api"      # a service you run yourself, or a stub
```

There is no `store_url`, so the commands that talk to a store (`register`, `status`, …) stop with "no
App Store is configured" – you register nothing. Instead, `appext keycloak export` writes the realm for
your Keycloak (the realm is named `local`, taken from the issuer's `/realms/local`), and `appext dev`
runs the extension ([quickstart.md](quickstart.md#b-with-a-keycloak-of-your-own)). Without
`app_redirect_uri` the extension never hands the sign-in over to an app; it is a website.

### A platform with a host app and services of its own

Most platforms look like this: a Keycloak, an App Store, a phone app that opens extensions in a
WebView, and a few services extensions may call. This is the platform file of an imaginary one, *Acme
Farm*, for the developer's machine:

```toml
[platform]
name = "Acme Farm"
issuer = "https://auth.acme.example/realms/acme"
store_url = "https://api.acme.example/api/v1"
app_redirect_uri = "acmefarm://extension-login"      # the host app's return address

[platform.services]
acme-api = "https://api.acme.example/api/v1"

[platform.starter]
service = "acme"
audience = "acme-api"
scope = "ext-data-read"
```

Nothing in the SDK knows Acme: everything above is read from the file. Swap the file and the same
extension, the same commands and the same tests work against another platform.

## Where the settings come from

Each setting is resolved **on its own**, strongest first: a command-line option, then an environment
variable, then the platform file.

| Setting | Option | Environment variable | Platform file | If it is nowhere |
|---|---|---|---|---|
| the platform file itself | `--platform NAME\|FILE` | `APPEXT_PLATFORM` | – | the lookup below |
| issuer | `--issuer` (`appext store …`) | `APPEXT_ISSUER` | `platform.issuer` | an error |
| store URL | `--store-url` (`appext store …`) | `APPEXT_STORE_URL` | `platform.store_url` | an error |
| the CLI's client | `--client-id` (`appext store login`) | `APPEXT_CLI_CLIENT_ID` | `platform.cli_client_id` | `appext-cli` |
| the host app's return address | – | `APPEXT_APP_REDIRECT_URI` | `platform.app_redirect_uri` | none: no hand-over |
| the platform's name | – | – | `platform.name` | "the platform" in messages |
| URLs of target services | – | `APPEXT_SERVICE_<NAME>_URL` | `platform.services` | none; `appext dev` warns |
| the starter service | – | – | `platform.starter` | `data`, `data-api`, `data-read` |

Environment variables win over the file, options win over variables:

```sh
# the issuer from appext.toml, the store from the option
appext store status --store-url https://staging.acme.example/api/v1
# both from the environment, whatever appext.toml says
APPEXT_ISSUER=https://auth.acme.example/realms/acme APPEXT_STORE_URL=https://api.acme.example/api/v1 appext store login
```

### Where the file is looked for

The first of these that applies is **the** platform file; files are never merged:

1. `--platform NAME|FILE`. A value that contains a `/` or ends in `.toml` is a path (relative to the
   current directory); any other value is a name, the file `~/.config/appext/platforms/NAME.toml`.
2. `APPEXT_PLATFORM`, read the same way.
3. `appext.toml` in the **project directory** – next to `extension.toml`. For `appext dev` and
   `appext keycloak export` that is the directory of the manifest; for `appext store …` it is the
   directory of `--manifest` if you pass one, else the current directory.
4. `appext.toml` in the current directory.
5. `~/.config/appext/platform.toml`, your own default.

`~/.config/appext/` is `$XDG_CONFIG_HOME/appext/` when `XDG_CONFIG_HOME` is set. A file you asked for
by `--platform` or `APPEXT_PLATFORM` that does not exist is an error – asking for one platform and
silently getting another would be worse. If no file applies, the options and variables alone have
to do.

A **running extension** (`APPEXT_ENV=local`, which is what `appext dev` and a plain local start use)
looks only at `APPEXT_PLATFORM` and at `appext.toml` next to its manifest; every `APPEXT_*` variable it
finds wins over the file. Outside `local` it reads no platform file at all ([below](#in-production-the-same-values-as-variables)).

### Who reads what

| Setting | `appext new` | `appext dev` | `appext keycloak export` | `appext store …` |
|---|---|---|---|---|
| `issuer` | written into `appext.toml` | becomes `APPEXT_ISSUER` | the realm's name (`/realms/<name>`) unless `--realm` | the sign-in (`login`, and the key of the stored credentials) |
| `store_url` | written | – | – | the API base |
| `cli_client_id` | written (when not the default) | – | – | `login` |
| `app_redirect_uri` | written | becomes `APPEXT_APP_REDIRECT_URI` | one more redirect URI of the client | – |
| `name` | written; fills `{{platform_name}}` in the template | becomes `APPEXT_APP_NAME` | the client's consent text | – |
| `services` | written | `APPEXT_SERVICE_<NAME>_URL` for each manifest service whose audience is listed | – | – |
| `starter` | written; fills the template | – | the audience of the `[consent]` scopes (`--consent-audience`) | – |

## What `appext new` writes

`appext new hello` resolves the platform like any command (`--platform`, `APPEXT_PLATFORM`,
`APPEXT_ISSUER`, `APPEXT_STORE_URL`, `APPEXT_CLI_CLIENT_ID`, `APPEXT_APP_REDIRECT_URI`, an
`appext.toml` in the current directory, your default platform file) and writes the result into the
new project as `appext.toml`. Whatever is still missing is added as a commented hint:

```toml
# The platform this project is written for: its OAuth service and its App Store. Not part of the
# store contract (extension.toml is) and no secret ever belongs here. Every key is explained in
# docs/platform.md of the SDK.

[platform]
# name = "Example Platform"
# issuer = "https://auth.example.com/realms/example"      # the OAuth / OpenID Connect service
# store_url = "https://api.example.com/api/v1"           # the App Store API

[platform.starter]
service = "data"
audience = "data-api"
scope = "data-read"
```

and the command ends with the next step: `edit appext.toml: name the platform's OAuth service (issuer)
and App Store (store_url)`. With a complete platform the project is ready at once:

```sh
appext new hello --platform acme      # writes hello/appext.toml from ~/.config/appext/platforms/acme.toml
cd hello && appext dev
```

The project's `appext.toml` pins it to that platform: from inside the project, `appext dev` and
`appext store …` use it without further options. The templates' `.dockerignore` keeps `appext.toml` out
of the image.

## The starter service

A new project should work against the platform at once, so `appext new` fills the manifest and the
sample code with one service call that the platform really offers: the **starter service**
(`[platform.starter]`).

| Key | Becomes |
|---|---|
| `service` | the `name` of the `[[services]]` entry and the argument of `ext.service("…")` |
| `audience` | the `audience` of that entry: the OAuth client of the target service |
| `scope` | its `scopes` |

The templates use the three values in exactly these places (the `{{service}}`, `{{audience}}` and
`{{scope}}` placeholders of `templates/`), and the sample route is `/items`:

```python
@api.get("/items")
async def items(data: ServiceClient = Depends(ext.service("data"))):
    return (await data.get("/items")).json()
```

`appext keycloak export` uses `starter.audience` as the audience of the extension's own `[consent]`
scopes (override with `--consent-audience`). The starter is a convenience for a first project, not a
limit: a manifest can call any service the store's catalog lists (`appext store services`).

## In production: the same values, as variables

A running extension in production does **not** read the platform file. Outside `APPEXT_ENV=local` the
SDK guesses nothing and reads no file: every value must be in the environment, and every missing one is
named at start ([deployment.md](deployment.md#configuration)). The **auth bundle** that the store issues
(`appext store bundle`) carries the platform's values as `APPEXT_*` variables in `appext.env`, so a
deployment gets the same issuer, the same return address and the same service URLs without a platform
file ever being copied there.

| Variable | Read by | Meaning | Default |
|---|---|---|---|
| `APPEXT_PLATFORM` | the CLI; a local run | the platform file (a path, or a name under `~/.config/appext/platforms/`) | none |
| `APPEXT_ISSUER` | the CLI; the extension | the OAuth / OpenID Connect service. For the extension: required, https outside `local` | none (`local`: from the project's `appext.toml`) |
| `APPEXT_STORE_URL` | the CLI | the App Store API base | none |
| `APPEXT_CLI_CLIENT_ID` | the CLI | the CLI's public client at the issuer | `appext-cli` |
| `APPEXT_STORE_TOKEN` | the CLI | a bearer token for the store, for CI: it replaces `appext store login`; the issuer must still be configured | none |
| `APPEXT_APP_REDIRECT_URI` | the CLI; the extension | the host app's return address. Set: a sign-in started from the host app's WebView returns there. Unset: there is no hand-over | none (`local`: from `appext.toml`) |
| `APPEXT_APP_ORIGINS` | the extension | origins of the host app's **web** app (comma-separated): they may embed the extension, and the "back" bar leads to the first | empty: nothing may embed it, no bar |
| `APPEXT_APP_NAME` | the extension | what the page calls the host app: "Back to *name*" | empty: "Back to the app", no name badge |
| `APPEXT_APP_MARKER` | the extension | the part of the WebView's user agent that says "the host app is showing me" | `-App-WebView/` |
| `APPEXT_APP_BACK_LABELS` | the extension | JSON object, language → label, `{app}` stands for the name: `{"en": "Back to {app}"}`; keys are language codes (`en`, `pt-BR`) | the bridge's English label |
| `APPEXT_APP_ACCENT` | the extension | the accent colour of the bar: `#` and 3 to 8 hex digits (`#rgb`, `#rrggbb`) | `#2563eb` |
| `APPEXT_SERVICE_<NAME>_URL` | the extension | base URL of the manifest service `<NAME>` (upper-cased); `local`: `appext dev` fills it from `[platform.services]` | none; required outside `local` |

The variables that belong to the extension alone (client id, keys, session store, …) are in
[deployment.md](deployment.md#configuration). Of the host-app variables only the platform's `name` has a
counterpart in the platform file (`appext dev` turns it into `APPEXT_APP_NAME`); `APPEXT_APP_ORIGINS`,
`APPEXT_APP_MARKER`, `APPEXT_APP_BACK_LABELS` and `APPEXT_APP_ACCENT` are variables only: to try them at
the desk, put them in an `--env-file` or the shell.

## The error when nothing is configured

Without any platform file, variable or option, a command that needs a setting says which one and the
three ways to provide it:

```
$ appext store login
error: no OAuth service (issuer) is configured: set it with --issuer, with the environment variable APPEXT_ISSUER, or in a platform file ([platform] issuer in appext.toml next to extension.toml, or --platform NAME for ~/.config/appext/platforms/NAME.toml)

$ appext store status
error: no App Store is configured: set it with --store-url, with the environment variable APPEXT_STORE_URL, or in a platform file ([platform] store_url in appext.toml next to extension.toml, or --platform NAME for ~/.config/appext/platforms/NAME.toml)

$ appext dev
error: the configuration is not usable:
  - APPEXT_ISSUER is required (APPEXT_ENV=local)
```

A `--platform` name without a file, and a file that breaks the rules above, are errors of their own:

```
$ appext store login --platform acme
error: platform file: /home/you/.config/appext/platforms/acme.toml: the platform file does not exist
  See docs/platform.md of the SDK for the format.
```

## Several platforms side by side

Keep one file per platform under `~/.config/appext/platforms/` and choose per command, per shell or
per project:

```sh
mkdir -p ~/.config/appext/platforms
cp acme.toml ~/.config/appext/platforms/acme.toml
cp staging.toml ~/.config/appext/platforms/staging.toml

appext new hello --platform acme                # the project is pinned to acme in hello/appext.toml
cd hello
appext store status                             # uses ./appext.toml
appext store status --platform staging          # a look at another platform, once
APPEXT_PLATFORM=staging appext store login      # the same through the environment
```

`--platform` beats the project's `appext.toml`, so a single command can address another platform without
editing anything; to move a project for good, edit its `appext.toml`. The store sign-ins do not collide:
`~/.config/appext/credentials.json` keeps one per issuer. A file at `~/.config/appext/platform.toml` is
your default for work outside any project.

## For platform operators

To make your platform easy to write for, **publish a platform file** that developers download and
save as `~/.config/appext/platforms/<name>.toml`:

```sh
mkdir -p ~/.config/appext/platforms
curl -fsSL -o ~/.config/appext/platforms/acme.toml https://developers.acme.example/appext/acme.toml
appext new hello --platform acme
```

Fill it with values that are true for a developer's machine and make sure what it points to exists:

- **`issuer`** serves the standard discovery document, the authorization, token, JWKS and
  end-session endpoints, and – for the CLI – the **device authorization grant**.
- **`cli_client_id`** names a public client at the issuer with the device authorization grant and
  PKCE (S256) enabled; the CLI asks for the scope `openid`, so the issuer must add the audience of the
  App Store API to its tokens, and the person's store roles (`store-developer`, `store-reviewer`,
  `store-admin`, read from `realm_access.roles`). Leave the key out if you call the client `appext-cli`.
- **`store_url`** is the base of your App Store API; the store itself lives at `<store_url>/store/…`.
- **`[platform.starter]`** names a service that is in your service catalog, with a scope developers
  may ask for, so that a project created by `appext new` works the moment its client is approved.
- **`[platform.services]`** (optional) gives the development URL of target services, so `appext dev`
  needs no variables.
- **`app_redirect_uri`** is the return address your host app registers for the sign-in hand-over, if
  there is a host app. A host app marks its WebView with `<Name>-App-WebView/<version>` in the user
  agent; if it uses another marker, tell developers to set `APPEXT_APP_MARKER`.
- **`name`** is how the platform shows up in messages and in the "Back to …" bar.

For deployments, the **auth bundle** your store issues is the other half: its `appext.env` should carry
`APPEXT_ENV`, `APPEXT_ISSUER`, `APPEXT_CLIENT_ID`, `APPEXT_CLIENT_AUTH`, `APPEXT_PUBLIC_URL` and one
`APPEXT_SERVICE_<NAME>_URL` per service of the manifest – and, if you have a host app,
`APPEXT_APP_REDIRECT_URI`, `APPEXT_APP_ORIGINS`, `APPEXT_APP_NAME` and, where the defaults do not fit,
`APPEXT_APP_MARKER`, `APPEXT_APP_BACK_LABELS` and `APPEXT_APP_ACCENT`. Everything a platform must
provide – endpoints, roles, the store API, the bundle, what the host app does – is described in
[platform-contract/README.md](platform-contract/README.md).
