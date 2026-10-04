# Quickstart

You need Python 3.12, and for the SPA template Node 22. For `appext dev` you need a
Keycloak that knows your extension – the local FMIS stack (`backend/docker-compose.yml`:
Keycloak on `http://127.0.0.1:58080`, FMIS API on `http://127.0.0.1:8000`) or one of your
own. Both ways are below.

(If all you want is an entry in the app that opens a web page in the browser, you need none of
this: that is a *link* – `appext new <id> --template link`, edit the address, `appext store
register`. See [manifest.md](manifest.md#links).)

## 1. Create the project

```sh
appext new hello --template spa     # --template htmx for server-rendered pages
cd hello
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
```

`hello` becomes the extension id: lower case, digits and hyphens, 3 to 40 characters. It is
the subdomain in production and the Keycloak client `ext-hello`. The command refuses an id
the manifest rules reject and a target directory that is not empty.

You get `extension.toml`, `app/main.py`, a Dockerfile, a Compose file, a unit test, an
icon, a `.gitignore` and `.dockerignore` that keep keys out of git and out of the image,
and – SPA – `frontend/` (Vite, vanilla JS, `package-lock.json` committed) or – HTMX –
`app/templates/` and `static/` (htmx vendored, no CDN).

```sh
(cd frontend && npm ci && npm run build)   # SPA only; `npm run watch` rebuilds on change
pytest                                     # no Keycloak, no FMIS API
```

## 2. Run it

```sh
appext dev
```

serves `http://127.0.0.1:8100` in **browser mode** and reloads on Python changes. It creates
a dev key under `.appext/` on first use, uses an in-memory session store and prints what it
did. Why `127.0.0.1` and port 8100: the store's local environment registers
`http://127.0.0.1:<dev_port>/auth/callback` as the redirect URI (so open exactly that
address, not `localhost`), and port 8000 is the FMIS backend. Set `dev_port` in the manifest
to change it.

Opening the page now redirects to Keycloak – which answers "client not found" until the
client exists. Create it in one of two ways.

### A) With the local App Store (what production does)

```sh
appext store login            # device flow: open the printed address, confirm the code
appext store register         # uploads extension.toml and .appext/client_key.jwk.json (public)
appext store submit
```

A person with the role `store-reviewer` approves (`appext store approve hello`); a
manifest with *restricted* scopes needs a second approval by a different `store-admin`.
On approval the store creates the Keycloak client `ext-hello` with your public key. Then:

```sh
appext store bundle --env local --out auth-bundle   # issuer, client id, URLs, lock file – no secret
appext dev --env-file auth-bundle/appext.env
```

(Add `APPEXT_LOCK_FILE=auth-bundle/extension.lock.toml` to rehearse the start-up check against
the approved scopes; without it a local run does not consult the lock file.)

To make the extension appear in the app, it has to be **live**: run it with `appext dev --env-file auth-bundle/appext.env --host 0.0.0.0`
and `appext store verify`. The local store runs in a container and asks your machine at `host.docker.internal`
(`probeUrlTemplate` in the compose file), which a server bound to `127.0.0.1` does not answer. People and the
app still open `http://127.0.0.1:8100`.

The developer account needs the role `store-developer`; for a local realm
`python3 tool/keycloak_store.py --dev-user` creates one (`store-dev@localhost.invalid`).

### B) With a Keycloak of your own

```sh
appext keycloak export --out .appext/realm-ext.json --dev-user
docker compose --profile keycloak up --build        # Keycloak imports the file on first start
```

`export` writes a realm import: the client with your public key, the scopes your manifest
names (with their audience mappers), a stand-in client for each audience (Keycloak's token
exchange needs the audience to exist), and optionally a test user `dev@localhost.invalid` /
`dev`. The Keycloak of the profile listens on port 58180 as `keycloak.localhost` – a name
that reaches the host from a browser and the Keycloak container from the extension's, so the
issuer is the same on both sides – and the extension is on `http://localhost:8100`. It is a stand-in for the store on a laptop, not a production tool. See `compose.yaml`
in the project for the exact commands.

## 3. Look around

- `http://127.0.0.1:8100/_sdk/info` – id, version, client, environment (what the store's
  deployment check reads); `/healthz`, `/readyz`.
- `app/main.py` – the whole backend; `ext.router()` guards every route, `ext.service("fmis")`
  makes an authenticated call to the FMIS API.
- `appext manifest check` – the manifest rules; `--scan-secrets` is for CI.

Next: [manifest.md](manifest.md), [authentication.md](authentication.md),
[store.md](store.md) (publishing), [deployment.md](deployment.md).
