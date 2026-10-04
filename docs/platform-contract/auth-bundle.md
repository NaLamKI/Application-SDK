# The auth bundle

This document is for the developers of a platform's App Store, who must generate the bundle an approved extension is deployed with, and for operators who want to know exactly what is in it, which variable means what, and why the SDK refuses to start when the bundle and the manifest disagree.

The bundle is the contract between the store and `appext` at deployment time. Everything below was checked against `appext.config` (the `APPEXT_*` variables), `appext.lock` (the lock file), `appext.cli.store` (how the bundle is downloaded and unpacked) and `appext.cli.run` (how an env file is read), and a bundle built with the code in §6 was loaded by the SDK.

MUST, SHOULD and MAY are used as in RFC 2119. "Reference implementation" marks a choice one platform made.

## 1. What it is

- **One ZIP per extension and environment**, issued by `GET /store/extensions/{id}/auth-bundle?env=` ([app-store-api.md](app-store-api.md)) from `APPROVED` on, for the **newest** version that is `APPROVED` or `LIVE`.
- It carries **the settings that differ per environment and the approved state** – and **no secret**. Anyone allowed to read a ConfigMap or a deployment repository may read it.
- It is **mounted at run time, never baked into the image**: the same image runs in every environment; the bundle and the secrets are what change.
- Links (`kind = "link"`) have no bundle: there is no deployment.

## 2. The files

```
auth-bundle-projects-prod.zip
├── appext.env              settings: issuer, client, URLs – no secret
├── extension.lock.toml     the approved state; checked by the SDK at start
└── README.md               how to mount it (Compose, Kubernetes); what is not in it
```

| File | Required | Used by |
|---|---|---|
| `appext.env` | yes | the deployment (`env_file`, `envFrom`, `appext dev --env-file`) |
| `extension.lock.toml` | yes | the SDK at start (`APPEXT_LOCK_FILE`, or next to `extension.toml`) |
| `README.md` | SHOULD | people. It names the approved scopes, says what is **not** in the bundle, and shows the mounts for Docker Compose and Kubernetes |

`appext store bundle` extracts **every** member of the ZIP into the output directory (default `./auth-bundle`) and **refuses** any member with an absolute path or a `..` segment. A platform MAY add further files; they are extracted too. Names are plain relative paths. The command prints the member names and reminds the developer that the bundle holds no secret.

The reference implementation builds the ZIP **deterministically** – the same input gives the same bytes (stored, not compressed; every entry dated 1980-01-01; fixed file order; Unix attributes `0644`) – so a bundle fetched twice can be compared, signed or cached by whoever deploys it. That is a recommendation, not a requirement.

## 3. `appext.env`

`KEY=VALUE` lines, one per variable, optional `#` comments. The SDK's own reader (`appext dev --env-file`) takes `APPEXT_*` lines only (a downloaded file must not be able to set `PATH` or `PYTHONPATH`; other keys are ignored with a warning), removes one pair of matching single or double quotes, and accepts an `export ` prefix. Docker Compose reads the file directly (`env_file`); for Kubernetes it becomes a ConfigMap (`kubectl create configmap --from-env-file`).

The reference implementation **refuses to write** a plain value that is empty or contains whitespace, a quote, a backtick, `$`, a backslash or a control character: any of those can break out of a `KEY=value` line or be expanded by `docker compose` (`$`). Refusing is better than mangling. Free text (§3.2) is the one exception: it is written **single-quoted**, and refused only if it contains a single quote, a backslash, `$` or a control character.

### 3.1 Variables the bundle sets

| Variable | The bundle sets it | Example | Meaning and what the SDK checks |
|---|:-:|---|---|
| `APPEXT_ENV` | MUST | `prod` | the **environment name**. It MUST equal `environment` in the lock file. **`local` is reserved**: the SDK reads it as "development at the desk" and relaxes its production checks (a generated session key, a missing lock file, the in-memory session store, `http` issuer, missing service URLs as warnings, defaults from the platform file). Never name a real deployment environment `local` |
| `APPEXT_ISSUER` | MUST | `https://auth.example.com/realms/example` | the OAuth service. `https` outside `local`; trailing slashes are removed; it MUST be the exact string of the `iss` claim ([oauth-service.md](oauth-service.md) §2) |
| `APPEXT_CLIENT_ID` | MUST | `ext-projects` | the client id at the identity service; the convention is `ext-<id>`. It MUST equal `client_id` in the lock file, and `/_sdk/info` reports it – the store's deployment check compares it with its own record |
| `APPEXT_CLIENT_AUTH` | MUST | `private_key_jwt` | `private_key_jwt` or `client_secret`; selects which secret the deployment must mount |
| `APPEXT_PUBLIC_URL` | MUST | `https://projects.apps.example.com` | the extension's **origin only** – no path, query or fragment; `https` (`http` only for `127.0.0.1` or `localhost`). It gives the web redirect URI (`/auth/callback`), the CSRF origin, the default post-logout address, and the cookie flags: an `https` URL means `Secure` and the `__Host-` cookie prefix |
| `APPEXT_APP_REDIRECT_URI` | MAY | `com.example.app:/callback` | where the **host app** takes a sign-in back. A URI with a scheme and no fragment. **Absent: the extension is a website like any other** – app mode never starts. Present, it applies only when the user agent carries the marker below |
| `APPEXT_APP_ORIGINS` | MAY | `https://app.example.com` | origins of the host's **web** app, comma-separated. Each is `http(s)://host[:port]` and nothing else (no path, query, fragment, user info, `*`, space, `;`, `,` or quote); `http` only for loopback; duplicates and trailing slashes are removed. Effect: the extension's CSP says `frame-ancestors <origins>` instead of `'none'` (and `X-Frame-Options` is left out, because it cannot name an origin); `bridge.js` talks to and listens to exactly these origins; the browser-tab bar leads back to the first. An extension with `display = "external"` stays unframeable whatever is set |
| `APPEXT_SERVICE_<NAME>_URL` | MUST, per service | `https://export.example.com/api` | the **base URL** of a service named in the manifest's `[[services]]`. `<NAME>` is the service's `name` in upper case (`export` → `APPEXT_SERVICE_EXPORT_URL`, `project_data` → `APPEXT_SERVICE_PROJECT_DATA_URL`). An `http(s)` URL, trailing slash removed; plain `http` outside loopback only warns. **Outside `local` every declared service needs one**, otherwise the extension does not start. A service client can only send requests *below* this URL |

**What the SDK does when one is missing.** `APPEXT_ENV` absent means `local` – a bundle that forgets it deploys with the development relaxations. `APPEXT_ISSUER` and `APPEXT_PUBLIC_URL` absent stop the extension outside `local`. `APPEXT_CLIENT_ID` and `APPEXT_CLIENT_AUTH` default to the manifest's values (`ext-<id>`, `private_key_jwt`), which are right only if the store follows that naming; a bundle MUST set them, because the lock check compares the effective client id.

`APPEXT_SERVICE_*` comes from the service catalog's `baseUrls` for the bundle's environment. If a service has **no** URL for that environment the reference implementation silently omits the variable – and the developer finds out at start. A platform SHOULD fail the bundle request instead, or at least say so in `README.md`.

### 3.2 Optional variables a platform may add

These four are **identical for every extension of a platform**: they describe the host app, not the extension. A platform MAY put them into every bundle (the reference implementation does), or its deployment templates MAY set them.

| Variable | Default | Meaning |
|---|---|---|
| `APPEXT_APP_NAME` | empty | what the extension calls the host app in the pages it draws: the label of the way back in a browser tab ("Back to Example") and a badge in that bar. Free text, trimmed. Empty: the label says "the app" and there is no badge |
| `APPEXT_APP_MARKER` | `-App-WebView/` | the part of the WebView's user agent that says "the host app is showing me". The host app appends `<Name>-App-WebView/<version>` to the user agent of its WebView; the SDK looks for this substring and then uses the app redirect URI for **that sign-in**. Set it only if your host app's marker does not contain the default. Not a security feature: a forged marker only yields an address the forger's browser cannot open. `bridge.js` uses it too, to tell the phone app's WebView from a browser tab |
| `APPEXT_APP_BACK_LABELS` | the bridge's English `Back to {app}` | a **JSON object** mapping language codes to the label of the way back; `{app}` stands for `APPEXT_APP_NAME`. Keys match `^[a-z]{2}(-[A-Z]{2})?$`, values are non-empty strings; anything else stops the extension at start. Example: `{"en": "Back to {app}", "de": "Back to {app} (de)"}`. The bridge looks the label up by the page's language tag as it is (`pt-BR`), then by its language (`pt`), then `en`, then the default |
| `APPEXT_APP_ACCENT` | `#2563eb` in the bridge | the colour of the bar's back arrow and badge: `#` and 3 to 8 hex digits (use `#rgb`, `#rrggbb` or `#rrggbbaa`). Anything else stops the extension at start |

These values contain spaces, quotes, JSON or a `#`, so the reference implementation writes them **single-quoted**, after the plain lines:

```
APPEXT_APP_NAME='Example Platform'
APPEXT_APP_BACK_LABELS='{"en":"Back to {app}","de":"Back to {app} (de)"}'
APPEXT_APP_ACCENT='#0b9f6a'
```

(The accent is quoted because a `#` may start a comment in some env-file dialects.) The SDK's own reader removes the quotes, and Docker Compose takes what is between single quotes literally. `kubectl create configmap --from-env-file` takes values literally, quotes included – check with your tooling, or set these four in the Deployment instead of the bundle.

### 3.3 Variables the bundle does **not** set

They belong to the deployment, and three of them are secrets (marked):

| Variable | Provided by | Purpose |
|---|---|---|
| `APPEXT_CLIENT_KEY_FILE` | **secret**, the developer | the private key (PEM or private JWK) for `private_key_jwt` |
| `APPEXT_CLIENT_SECRET_FILE` (or `APPEXT_CLIENT_SECRET`) | **secret**, from `rotate-secret` | the client secret, for `client_secret` |
| `APPEXT_SESSION_KEY_FILE` | **secret**, the deployment | the key that encrypts tokens in the session store: one key per line, base64 or hex of 32 bytes; the **first** encrypts, **all** decrypt (rotation); the same file for every replica |
| `APPEXT_SESSION_STORE` | deployment | `redis://…` or `rediss://…`. `memory` is for development: outside `local` it stops the extension at start |
| `APPEXT_CLIENT_KEY_ID` | deployment, optional | the `kid` of the key in the client assertion; needed only to pick a key during a rotation |
| `APPEXT_LOCK_FILE` | deployment | path of the lock file (default: next to `extension.toml`, then the working directory) |
| `APPEXT_TRUSTED_PROXIES` | deployment | the addresses that may set `X-Forwarded-For` / `-Proto`; empty = nobody |
| `APPEXT_COOKIE_SECURE` | deployment | `auto` (follows the public URL), `true`, `false` |
| `APPEXT_SESSION_MAX_AGE`, `APPEXT_HTTP_TIMEOUT` | deployment | seconds; defaults 30 days and 10 s |
| `PORT` | deployment | port of `appext serve` and `appext health` |

## 4. The lock file

`extension.lock.toml` records **what the review approved**, so a forgotten review is caught before the deployment goes live – at start, with a message that names the problem – instead of as a failing token exchange in production.

```toml
[lock]
extension = "projects"
version = "1.2.0"
client_id = "ext-projects"
environment = "prod"
generated_at = "2026-10-03T09:12:44Z"
approved_scopes = ["data-read", "export-write"]
keycloak_client_uuid = "6f1d9c1e-0000-4000-8000-000000000001"
```

| Key | Required | Meaning |
|---|:-:|---|
| `extension` | yes | the extension id; a string |
| `version` | yes | the version the bundle was issued for; a string |
| `client_id` | yes | the client id at the identity service; a string |
| `approved_scopes` | yes | a list of strings: **every scope any `APPROVED` or `LIVE` version asks for** (consent scopes and the scopes of every service), sorted and without duplicates |
| `environment` | no | the environment name |
| `generated_at` | no | a timestamp, written as a string |
| `keycloak_client_uuid` | no | the id of the client at the identity service. The name is the reference implementation's; the SDK reads it as an optional string and decides nothing on it. A platform on another identity service MAY leave it out or put its own client identifier here |

**The check.** `Extension.asgi()` runs it before the application is built. The lock file is looked for at `APPEXT_LOCK_FILE`, else `extension.lock.toml` next to `extension.toml`, else in the working directory. A missing file is allowed **only** in `local`; everywhere else it is an error – "no lock file" must not become the way around the check. (An *explicit* `APPEXT_LOCK_FILE` that does not exist is an error in `local` too.) Then, in this order:

| Condition | Error (the SDK raises `LockError` and the process does not start) |
|---|---|
| `extension` differs from the manifest's `id` | `the lock file belongs to extension 'a', the manifest to 'b'` |
| `version` differs from the manifest's `version` | `the lock file approves version 1.1.0, the manifest says 1.2.0: fetch the auth bundle of this version (or submit it for review first)` |
| `client_id` differs from `APPEXT_CLIENT_ID` | `the lock file is for client 'ext-a', this deployment uses 'ext-b'` |
| `environment` is present and differs from `APPEXT_ENV` | `the lock file was generated for environment 'prod', this deployment runs as 'staging'` |
| a scope of the manifest is not in `approved_scopes` | `the manifest asks for scopes the store has not approved: data-write – submit this version for review and deploy the new auth bundle` |

Two consequences for the store:

- The `version` of the bundle is the version of the **manifest in the deployed image**. A deployment of an older image with a newer bundle fails; the developer fetches the bundle again for the version they deploy.
- The lock file is a **safety net against forgetting a review, not enforcement against a malicious developer**: a developer controls the code and can switch the check off (`asgi(check_lock=False)`). The real enforcement is at the identity service: the client has only the approved scopes, so an exchange or a sign-in for any other scope fails there.

## 5. What is never in the bundle

| Secret | Where it lives | How it reaches the deployment |
|---|---|---|
| **private client key** (`private_key_jwt`) | created by the developer (`appext keys generate`); the store receives only the public JWK ([app-store-api.md](app-store-api.md): a private member is refused before anything is stored) | a mounted secret file, `APPEXT_CLIENT_KEY_FILE` |
| **client secret** (`client_secret`) | generated by the identity service, returned **once** by `rotate-secret`, never stored by the store, never logged | a mounted secret file, `APPEXT_CLIENT_SECRET_FILE` |
| **session key** | the deployment (`appext keys session`) | a mounted secret file, `APPEXT_SESSION_KEY_FILE` |

The `README.md` of the bundle SHOULD say which of these applies to this extension (`APPEXT_CLIENT_AUTH`) and show the mounts. The bundle of a `client_secret` extension must not promise a secret: it comes from `rotate-secret`, not from the bundle.

## 6. How a platform generates it

**Inputs**, all known to the store when a version is approved:

| Input | Source |
|---|---|
| extension id, version | the newest `APPROVED` or `LIVE` version |
| client id | the store's record (`ext-<id>`) |
| environment name and the extension's URL there | the environment configuration ([app-store-api.md](app-store-api.md) §10) |
| `client_auth` | the manifest |
| issuer | the identity service's public issuer string |
| app redirect URI | the platform (one value for all extensions) |
| app origins | the platform, per environment |
| app name, back labels, accent colour | the platform (one value for all extensions; optional, §3.2) |
| service URLs | the service catalog's `baseUrls[environment]` for each service named in the manifest, keyed by the **manifest's service name** |
| approved scopes | the union over `APPROVED` and `LIVE` versions |
| client identifier at the identity service, time | provisioning result, now |

A minimal generator (Python, standard library only), producing the layout of §2:

```python
import io, json, re, zipfile

ZIP_TIME = (1980, 1, 1, 0, 0, 0)               # ZIP cannot hold earlier dates; a fixed one makes the bytes reproducible
UNSAFE = re.compile(r"[\s\"'`$\\\x00-\x1f\x7f]")   # breaks an env-file line, or is expanded by `docker compose`
UNSAFE_QUOTED = re.compile(r"['\\$\x00-\x1f\x7f]")  # what may not stand inside a single-quoted value


def env_line(key: str, value: str) -> str:
    if not value or UNSAFE.search(value):
        raise ValueError(f"{key}: {value!r} cannot be written to an env file")
    return f"{key}={value}\n"


def quoted_line(key: str, value: str) -> str:          # free text, JSON, a colour: single-quoted
    if not value or UNSAFE_QUOTED.search(value):
        raise ValueError(f"{key}: {value!r} cannot be written to an env file")
    return f"{key}='{value}'\n"


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)   # a JSON string is a valid TOML basic string for these values


def build_bundle(*, extension, client_id, version, environment, client_auth, issuer, public_url,
                 app_redirect_uri=None, app_origins=(), service_urls=None, approved_scopes=(),
                 app_name="", app_back_labels=None, app_accent="",
                 idp_client_id="", generated_at) -> bytes:
    env = [("APPEXT_ENV", environment), ("APPEXT_ISSUER", issuer), ("APPEXT_CLIENT_ID", client_id),
           ("APPEXT_CLIENT_AUTH", client_auth), ("APPEXT_PUBLIC_URL", public_url)]
    if app_redirect_uri:
        env.append(("APPEXT_APP_REDIRECT_URI", app_redirect_uri))
    if app_origins:
        env.append(("APPEXT_APP_ORIGINS", ",".join(app_origins)))
    for name, url in sorted((service_urls or {}).items()):        # name: the `[[services]]` name of the manifest
        env.append((f"APPEXT_SERVICE_{name.upper()}_URL", url))
    text = "".join(env_line(k, v) for k, v in env)
    if app_name:                                                   # free text comes last, single-quoted
        text += quoted_line("APPEXT_APP_NAME", app_name)
    if app_back_labels:
        text += quoted_line("APPEXT_APP_BACK_LABELS", json.dumps(app_back_labels, ensure_ascii=False, separators=(",", ":")))
    if app_accent:
        text += quoted_line("APPEXT_APP_ACCENT", app_accent)       # quoted: a `#` may start a comment
    scopes = ", ".join(toml_string(s) for s in sorted(set(approved_scopes)))
    files = {
        "appext.env": text,
        "extension.lock.toml": (
            "[lock]\n"
            f"extension = {toml_string(extension)}\nversion = {toml_string(version)}\n"
            f"client_id = {toml_string(client_id)}\nenvironment = {toml_string(environment)}\n"
            f"generated_at = {toml_string(generated_at)}\napproved_scopes = [{scopes}]\n"
            f"keycloak_client_uuid = {toml_string(idp_client_id)}\n"),
        "README.md": f"# Auth bundle: {extension} {version} ({environment})\n",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, body in files.items():                           # a fixed order
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3                                 # Unix – otherwise the platform leaks into the bytes
            info.external_attr = 0o644 << 16
            archive.writestr(info, body.encode("utf-8"))
    return out.getvalue()
```

For a bundle with `extension="projects"`, `version="1.2.0"`, `environment="prod"`, one service `export`, one app origin, the app redirect URI `com.example.app:/callback` and the platform's name, labels and accent, it writes:

```
APPEXT_ENV=prod
APPEXT_ISSUER=https://auth.example.com/realms/example
APPEXT_CLIENT_ID=ext-projects
APPEXT_CLIENT_AUTH=private_key_jwt
APPEXT_PUBLIC_URL=https://projects.apps.example.com
APPEXT_APP_REDIRECT_URI=com.example.app:/callback
APPEXT_APP_ORIGINS=https://app.example.com
APPEXT_SERVICE_EXPORT_URL=https://export.example.com/api
APPEXT_APP_NAME='Example Platform'
APPEXT_APP_BACK_LABELS='{"en":"Back to {app}","de":"Back to {app} (de)"}'
APPEXT_APP_ACCENT='#0b9f6a'
```

**Rules for the generator**

- Issue only from `APPROVED` on, and only for an extension (never for a link): `409 invalid_state` otherwise.
- The values must be exactly those the provisioning step used: the issuer string, the client id, the URLs whose `/auth/callback` is a registered redirect URI. A bundle that disagrees with the identity service's configuration produces `redirect_uri` errors at the first sign-in.
- Never write a secret, and never write the key file path: those are the deployment's.
- Record that a bundle was issued (who, when, which environment).

## 7. Checking a bundle

Before you ship the generator, load a generated bundle with the SDK itself. This script reads the manifest, the bundle and two throw-away secret files, and raises with **every** problem listed:

```sh
appext keys generate --out .appext && appext keys session --out .appext/session_key
unzip -o auth-bundle.zip -d auth-bundle
python3 - <<'EOF'
from pathlib import Path
from appext import ExtensionSettings, check_lock, load_manifest, read_lock
from appext.cli.run import read_env_file            # the reader behind `appext dev --env-file`: removes the quotes

manifest = load_manifest("extension.toml")
env = read_env_file(Path("auth-bundle/appext.env"))
env.update({"APPEXT_CLIENT_KEY_FILE": ".appext/client_key.pem",         # for client_secret: APPEXT_CLIENT_SECRET_FILE
            "APPEXT_SESSION_KEY_FILE": ".appext/session_key",
            "APPEXT_SESSION_STORE": "redis://localhost:6379/0"})
settings = ExtensionSettings.from_env(manifest, env)                     # ConfigError: every problem, one line each
check_lock(manifest, read_lock("auth-bundle/extension.lock.toml"),
           environment=settings.env, client_id=settings.client_id)       # LockError: the first mismatch
print("bundle ok:", settings.public_url)
EOF
```

Beyond that: deploy it and ask `appext store verify` – the store reads `/_sdk/info` and `/readyz` ([app-store-api.md](app-store-api.md) §9) – and sign in once through the host app.

## 8. Conformance checklist

**Content**
- [ ] The ZIP holds `appext.env`, `extension.lock.toml` and (SHOULD) `README.md`, with plain relative member names.
- [ ] `appext.env` sets `APPEXT_ENV`, `APPEXT_ISSUER`, `APPEXT_CLIENT_ID`, `APPEXT_CLIENT_AUTH`, `APPEXT_PUBLIC_URL`, and one `APPEXT_SERVICE_<NAME>_URL` per declared service (name upper-cased) – and fails or warns when a service has no URL in the environment.
- [ ] `APPEXT_APP_REDIRECT_URI` and `APPEXT_APP_ORIGINS` are set exactly where the platform has a host app (phone / web), and nowhere else.
- [ ] If the platform's host app marker does not contain `-App-WebView/`, `APPEXT_APP_MARKER` carries it.
- [ ] Free-text values (`APPEXT_APP_NAME`, `APPEXT_APP_BACK_LABELS`, and the `#`-colour `APPEXT_APP_ACCENT`) are single-quoted, or set in the deployment, in a way your tooling reads correctly; the accent is a `#rgb`-style colour.
- [ ] No environment of the platform is named `local`.

**Lock file**
- [ ] `[lock]` has `extension`, `version`, `client_id`, `approved_scopes`; `environment` equals the bundle's `APPEXT_ENV`; `generated_at` is a string.
- [ ] `approved_scopes` is the sorted union of the scopes of every `APPROVED` and `LIVE` version, including service scopes.
- [ ] The bundle is for the newest `APPROVED` or `LIVE` version; the deployed manifest's `version` matches it.

**No secrets**
- [ ] Nothing in the ZIP is a private key, a client secret, a session key or a password; a `client_secret` extension gets its secret from `rotate-secret` only.
- [ ] The same inputs give the same bytes (recommended).

**Test**
- [ ] The script of §7 passes on a generated bundle, and fails with the messages of §4 when the manifest asks for an unapproved scope, the version, the client or the environment differ, or a service URL is missing.
