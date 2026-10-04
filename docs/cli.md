# The `appext` command line

`appext --help` lists the commands; `appext <command> --help` the options. Exit code 0 means
success; 1 a failure the message explains (a broken manifest, an API error, a finding of
the secret scan); 2 a usage error. Errors go to stderr as `error: …` with a hint where there
is one.

| Command | Does |
|---|---|
| `appext new <id> [--template spa\|htmx\|link] [--dir .] [--name "…"]` | Creates `<dir>/<id>/` from a template. The id is checked by the manifest rule; a target that exists and is not empty is refused; nothing is written when anything is wrong. `--name` is the display name (default: derived from the id). `link` is a [link](manifest.md#links): only an `extension.toml` and a README (no server, so no code, tests, Dockerfile or key), and the next steps it prints are the publishing ones. |
| `appext dev [module:app] [--manifest P] [--host 127.0.0.1] [--port N] [--env-file F]… [--no-reload]` | Runs the extension in browser mode with reload on `http://127.0.0.1:<dev_port>`. Fills in local defaults (`APPEXT_ENV=local`, the dev key under `.appext/`, an in-memory session store, the public URL), lets `--env-file`s (only `APPEXT_*` lines, e.g. a bundle's `appext.env`) and the shell's own `APPEXT_*` variables override them, validates the result and prints what it did. Refuses a [link](manifest.md#links): it has nothing to run. |
| `appext serve <module:app> [--host 0.0.0.0] [--port N] [--workers N]` | The container's entry point: uvicorn without reload, port `--port`, else `$PORT`, else 8000. Proxy headers are trusted only for `APPEXT_TRUSTED_PROXIES`. Refuses when `./extension.toml` is a link. |
| `appext health [--port N]` | Exit code 0 if `http://127.0.0.1:$PORT/healthz` answers 200 – the Docker `HEALTHCHECK`. |
| `appext manifest check [path] [--scan-secrets]` | The manifest rules 1–6 and 8, every violation listed. For a link it prints `kind link` and the `entry` address (no client, consent or service lines), and does not look for an icon file – its icon is an address. `--scan-secrets` also scans the project for private keys (PEM, private JWK members) and literal `client_secret` values; it never prints a value. A file is skipped only if both `.gitignore` and `.dockerignore` ignore it (a project without a Dockerfile: `.gitignore` alone). Exit 1 on any finding. |
| `appext keys generate [--out .appext] [--alg RS256\|ES256] [--force]` | A key pair for `private_key_jwt`: `client_key.pem` (mode 0600, created that way) and `client_key.jwk.json` (public only, with the RFC 7638 thumbprint as `kid`). Never prints the private key; refuses to overwrite without `--force`. |
| `appext keys session [--out .appext/session_key] [--force]` | A random 32-byte key for `APPEXT_SESSION_KEY_FILE`, mode 0600. |
| `appext keycloak export [path] [--out F] [--realm fmis] [--key JWK] [--dev-user] [--consent-audience fmis-api] [--backchannel-host H \| --backchannel-url U]` | A realm import for a **local** Keycloak: the client `ext-<id>` with your public key, the scopes with audience mappers, a stand-in client per audience, `basic` and `acr`, optionally a test user. Creates the dev key if there is none. Refuses a link: there is no client to describe. |
| `appext store …` | The App Store: [store.md](store.md). |

## Links

A [link](manifest.md#links) has no server, no Keycloak client, no key and no deployment, so the
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

Everything else comes from the SDK's own local defaults: the issuer
`http://127.0.0.1:58080/realms/fmis`, the app redirect URI, and `http://127.0.0.1:8000/api/v1` for a
service whose audience is `fmis-api`. Other services need `APPEXT_SERVICE_<NAME>_URL`; `dev`
warns when it is missing. A memory store loses its sessions on every reload.

## Files the CLI writes

| File | Mode | Content |
|---|---|---|
| `.appext/client_key.pem` | 0600 | private client key – never commit, never bake into an image |
| `.appext/client_key.jwk.json` | 0644 | its public half – this is what the store gets |
| `.appext/session_key` | 0600 | session-encryption key |
| `~/.config/appext/credentials.json` (`$XDG_CONFIG_HOME/appext/`) | 0600, directory 0700 | the store sign-in per issuer, renewed with the refresh token |
