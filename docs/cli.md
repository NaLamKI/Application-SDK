# The `appext` command line

For anyone who builds, runs or publishes an extension: the commands, their options and the files they write.

`appext --help` lists the commands; `appext <command> --help` the options. Exit code 0 means
success; 1 a failure the message explains (a broken manifest, an API error, a finding of
the secret scan, a missing platform setting); 2 a usage error. Errors go to stderr as `error: …` with a
hint where there is one.

| Command | Does |
|---|---|
| `appext new <id> [--template spa\|htmx\|link] [--dir .] [--name "…"] [--platform NAME\|FILE]` | Creates `<dir>/<id>/` from a template. The id is checked by the manifest rule; a target that exists and is not empty is refused; nothing is written when anything is wrong. `--name` is the display name (default: derived from the id). The platform (`--platform`, `APPEXT_PLATFORM`, `APPEXT_ISSUER`, `APPEXT_STORE_URL`, a platform file) is written into the project as `appext.toml`, hints for what is missing included, and its starter service fills the template ([platform.md](platform.md#what-appext-new-writes)). `link` is a [link](manifest.md#links): only an `extension.toml`, a README and `appext.toml` (no server, so no code, tests, Dockerfile or key), and the next steps it prints are the publishing ones. |
| `appext dev [module:app] [--manifest P] [--host 127.0.0.1] [--port N] [--env-file F]… [--no-reload] [--platform NAME\|FILE]` | Runs the extension in browser mode with reload on `http://127.0.0.1:<dev_port>`. Fills in local defaults (`APPEXT_ENV=local`, the dev key under `.appext/`, an in-memory session store, the public URL) and what the platform file knows (issuer, the host app's return address, the platform's name, service URLs); lets `--env-file`s (only `APPEXT_*` lines, e.g. a bundle's `appext.env`) and the shell's own `APPEXT_*` variables override them, validates the result and prints what it did. Refuses a [link](manifest.md#links): it has nothing to run. |
| `appext serve <module:app> [--host 0.0.0.0] [--port N] [--workers N]` | The container's entry point: uvicorn without reload, port `--port`, else `$PORT`, else 8000. Proxy headers are trusted only for `APPEXT_TRUSTED_PROXIES`. Takes the environment as it is (a platform file is read only by an app running with `APPEXT_ENV=local`). Refuses when `./extension.toml` is a link. |
| `appext health [--port N]` | Exit code 0 if `http://127.0.0.1:$PORT/healthz` answers 200 – the Docker `HEALTHCHECK`. |
| `appext manifest check [path] [--scan-secrets]` | The manifest rules 1–6 and 8, every violation listed. For a link it prints `kind link` and the `entry` address (no client, consent or service lines), and does not look for an icon file – its icon is an address. `--scan-secrets` also scans the project for private keys (PEM, private JWK members) and literal `client_secret` values; it never prints a value. A file is skipped only if both `.gitignore` and `.dockerignore` ignore it (a project without a Dockerfile: `.gitignore` alone). Exit 1 on any finding. |
| `appext keys generate [--out .appext] [--alg RS256\|ES256] [--force]` | A key pair for `private_key_jwt`: `client_key.pem` (mode 0600, created that way) and `client_key.jwk.json` (public only, with the RFC 7638 thumbprint as `kid`). Never prints the private key; refuses to overwrite without `--force`. |
| `appext keys session [--out .appext/session_key] [--force]` | A random 32-byte key for `APPEXT_SESSION_KEY_FILE`, mode 0600. |
| `appext keycloak export [path] [--out F] [--realm NAME] [--key JWK] [--dev-user] [--consent-audience A] [--backchannel-host H \| --backchannel-url U] [--platform NAME\|FILE]` | A realm import for a **local** Keycloak (only useful where the platform's OAuth service is Keycloak): the client `ext-<id>` with your public key, the scopes with audience mappers, a stand-in client per audience, `basic` and `acr`, optionally a test user. The realm is named after the issuer (`…/realms/<name>`), else `local`, unless you pass `--realm`; the `[consent]` scopes get the audience `--consent-audience`, default the platform's starter audience; the host app's return address is added to the client's redirect URIs. Creates the dev key if there is none. Refuses a link: there is no client to describe. |
| `appext store …` | The App Store: [store.md](store.md). Every command takes `--store-url`, `--issuer` and `--platform`. |

## Which platform a command talks to

`appext dev`, `appext keycloak export`, `appext new` and every `appext store …` command read the
platform's settings. There are no built-in defaults. Each setting is taken from, strongest first, an option
(`--issuer`, `--store-url`, `--client-id`), an environment variable (`APPEXT_ISSUER`,
`APPEXT_STORE_URL`, `APPEXT_CLI_CLIENT_ID`, `APPEXT_APP_REDIRECT_URI`) and the **platform file**, which
`--platform NAME|FILE` or `APPEXT_PLATFORM` names, else `appext.toml` of the project, else
`~/.config/appext/platform.toml`. A setting that is needed and nowhere is an error that lists the three
ways. The full picture, with every key of the file: [platform.md](platform.md).

## Links

A [link](manifest.md#links) has no server, no OAuth client, no key and no deployment, so the
commands that work on those stop early, before a request or a file:

```
$ appext dev
error: shop is a link, and a link has no server: there is nothing to run.
  A link is published with `appext store register`, `submit` and – once a reviewer approved it – `verify`.
```

| Command | For a link |
|---|---|
| `appext dev`, `appext serve` | Refused: nothing to run (`serve` looks at `./extension.toml`; a missing or broken one is left to the app). No dev key is created. |
| `appext keycloak export` | Refused: no client to export. |
| `appext store key`, `bundle`, `rotate-secret` | Refused when the id comes from the manifest: no key to upload, no auth bundle to download, no secret to rotate. Given an id on the command line, the store's `409` is shown as it is. |
| `appext store register` | Uploads the manifest only, says "Link registered … no server, no key and no deployment" and prints the next steps: `submit`, a reviewer approves, `verify`. `--key` is ignored with a warning. |
| `appext store submit`, `verify`, `status` | As for an extension. `verify` has nothing to probe and makes the approved link live at once; `status` shows `kind link` and the `entry` instead of the client. |
| `appext manifest check` | Rules 1–6 and 8; prints `kind` and `entry`. |
| `appext keys …` | Not tied to a manifest, so not refused – but a link has no use for the keys. |

## What `appext dev` sets

| Variable | Value |
|---|---|
| `APPEXT_ENV` | `local` |
| `APPEXT_PUBLIC_URL` | `http://<host>:<port>` |
| `APPEXT_SESSION_STORE` | `memory` |
| `APPEXT_SESSION_KEY_FILE` | `.appext/session_key` (created) |
| `APPEXT_CLIENT_KEY_FILE`, `APPEXT_CLIENT_KEY_ID` | `.appext/client_key.pem` (created) and its `kid` – for `private_key_jwt` |

and, **only where the platform file or the environment provides them**:

| Variable | From |
|---|---|
| `APPEXT_ISSUER` | `platform.issuer` |
| `APPEXT_APP_REDIRECT_URI` | `platform.app_redirect_uri` – without it there is no host-app hand-over |
| `APPEXT_APP_NAME` | `platform.name` – the name in the bar "Back to …" ([bridge.md](bridge.md)) |
| `APPEXT_SERVICE_<NAME>_URL` | `[platform.services]`: one per manifest service whose audience is listed there |

Then the layers are applied: the defaults above, then each `--env-file` in order (only `APPEXT_*` lines
are taken over; others are ignored with a warning), then the `APPEXT_*` variables of your shell – what
the shell says wins. Everything else comes from the SDK's own local defaults, which are few: no issuer,
no service URL and no return address are among them. Without an issuer `appext dev` stops with
`APPEXT_ISSUER is required (APPEXT_ENV=local)`; for a service without a URL it warns (`APPEXT_SERVICE_<NAME>_URL
is not set`) and calls to it fail. `APPEXT_APP_ORIGINS` (the host app's web origins, which also switch
the "back" bar on), `APPEXT_APP_MARKER`, `APPEXT_APP_BACK_LABELS` and `APPEXT_APP_ACCENT` are not set by
`dev`: pass them in an `--env-file` or the shell. A memory store loses its sessions on every reload.

## Environment variables the CLI reads

| Variable | Used by | Meaning |
|---|---|---|
| `APPEXT_PLATFORM` | `new`, `dev`, `keycloak export`, `store …` | the platform file: a path, or a name under `~/.config/appext/platforms/` |
| `APPEXT_ISSUER`, `APPEXT_STORE_URL`, `APPEXT_CLI_CLIENT_ID`, `APPEXT_APP_REDIRECT_URI` | as above | single settings of the platform; see [platform.md](platform.md#where-the-settings-come-from) |
| `APPEXT_STORE_TOKEN` | `store …` | a bearer token for the store (CI): replaces `appext store login`; the issuer must still be configured |
| `XDG_CONFIG_HOME` | all | moves `~/.config` (where `appext/` keeps platform files and credentials) |
| `APPEXT_TRUSTED_PROXIES`, `PORT` | `serve`, `health` | see [deployment.md](deployment.md) |

`appext dev` also reads every `APPEXT_*` variable that configures the extension itself
([deployment.md](deployment.md#configuration)).

## Files the CLI writes

| File | Mode | Content |
|---|---|---|
| `appext.toml` | – | the platform of a new project, written by `appext new` ([platform.md](platform.md)); no secret |
| `.appext/client_key.pem` | 0600 | private client key – never commit, never bake into an image |
| `.appext/client_key.jwk.json` | – | its public half – this is what the store gets |
| `.appext/session_key` | 0600 | session-encryption key |
| `auth-bundle/` | – | what `appext store bundle` unpacks: `appext.env`, the lock file, a README (the directory can be changed with `--out`) |
| `~/.config/appext/credentials.json` (`$XDG_CONFIG_HOME/appext/`) | 0600, directory 0700 | the store sign-in per issuer, renewed with the refresh token |

`~/.config/appext/platforms/<name>.toml` and `~/.config/appext/platform.toml` are platform files that
*you* put there; the CLI only reads them.
