# Quickstart

For a developer writing a first extension, from an empty directory to a page that signs people in.

You need Python 3.12 or newer, and for the SPA template Node 22. You also need a **platform** to
write for: an OAuth 2.0 / OpenID Connect service (the issuer) that will know your extension's client, and –
if you want to publish – the platform's App Store. Ask the platform's operator for both addresses
([platform.md](platform.md)); or use a Keycloak of your own (path B below), which needs no platform.

(If all you want is an entry in the host app that opens a web page in the browser, you need none of
this: that is a *link* – `appext new <id> --template link`, edit the address, `appext store
register`. See [manifest.md](manifest.md#links).)

## 1. Create the project

```sh
python3 -m venv venv && . venv/bin/activate
pip install appext                    # or, from a checkout of this repository: pip install -e .
appext new hello --template spa       # --template htmx for server-rendered pages
cd hello
pip install -e ".[test]"
```

`hello` becomes the extension id: lower case, digits and hyphens, 3 to 40 characters. It is the
subdomain in production and the OAuth client `ext-hello`. The command refuses an id the manifest
rules reject and a target directory that is not empty.

You get `extension.toml` (the manifest), `appext.toml` (the platform, [below](#2-tell-it-about-your-platform)),
`app/main.py`, a Dockerfile, a Compose file, a unit test, an icon, a `.gitignore` and `.dockerignore`
that keep keys out of git and out of the image, and – SPA – `frontend/` (Vite, vanilla JS,
`package-lock.json` committed) or – HTMX – `app/templates/` and `static/` (htmx vendored, no CDN).

The sample backend has one route that calls a service of the platform, `/api/items` (HTMX: `/items`).
The service is the platform's *starter service*: `data`, audience `data-api`, scope `data-read`, unless
the platform file says otherwise ([platform.md](platform.md#the-starter-service)). Point it at a service
your platform really offers when you write your own extension.

## 2. Tell it about your platform

`appext new` printed what is still missing. Open `hello/appext.toml`: it holds hints for the platform's
name, its OAuth service and its App Store. Fill in what your platform's operator gave you:

```toml
[platform]
name = "Acme Platform"
issuer = "https://auth.acme.example/realms/acme"
store_url = "https://api.acme.example/api/v1"
```

If the operator published a platform file, save it as `~/.config/appext/platforms/acme.toml` and create
the project with `appext new hello --platform acme` – the file is then complete from the start. The
same settings can come from `--issuer` / `--store-url`, or from the variables `APPEXT_ISSUER` and
`APPEXT_STORE_URL`; [platform.md](platform.md) lists every way and the order.

Nothing is guessed: without an issuer, `appext dev` stops and `appext store …` says what to set.

## 3. Test it

```sh
(cd frontend && npm ci && npm run build)   # SPA only; `npm run watch` rebuilds on change
pytest                                     # no OAuth service, no platform, no network
```

The project's `tests/conftest.py` calls `configure_test_environment()`, which makes a test run the
same on every machine ([testing.md](testing.md)).

## 4. Run it

```sh
appext dev
```

serves `http://127.0.0.1:8100` in **browser mode** and reloads on Python changes. It creates a dev key
under `.appext/` on first use, uses an in-memory session store, takes the issuer, the host app's return
address and the service URLs from `appext.toml`, and prints what it did. Why `127.0.0.1` and port
8100: the client is registered with `http://127.0.0.1:<dev_port>/auth/callback` as its redirect URI
in the store's `local` environment (so open exactly that address, not `localhost`), and port 8000 is
where a platform's own API often runs on a developer machine. Set `dev_port` in the manifest to
change it.

If the service the sample calls runs on your machine, give `appext dev` its address – in
`appext.toml`, under the audience of the service, or as a variable:

```toml
[platform.services]
data-api = "http://127.0.0.1:8000/api/v1"
```

```sh
APPEXT_SERVICE_DATA_URL=http://127.0.0.1:8000/api/v1 appext dev      # the same, for one run
```

Opening the page now redirects to the issuer – which answers "client not found" until the client
exists. Create it in one of two ways.

### A) With the platform's App Store (what production does)

```sh
appext store login            # device flow: open the printed address, confirm the code
appext keys generate          # once: .appext/client_key.pem (private) and .appext/client_key.jwk.json (public)
appext store register         # uploads extension.toml and the public key
appext store submit
```

(`appext dev` creates the same key pair on its first start, so after one run `keys generate` is not
needed; `register` uploads whatever `.appext/client_key.jwk.json` holds.) The developer account needs the
store role `store-developer` – ask the platform's operator.

A person with the role `store-reviewer` approves (`appext store approve hello`); a manifest with
*restricted* scopes needs a second approval by a different `store-admin`. On approval the store creates
the OAuth client `ext-hello` at the issuer with your public key. Then:

```sh
appext store bundle --env local --out auth-bundle   # issuer, client id, URLs, lock file – no secret
appext dev --env-file auth-bundle/appext.env
```

(Add `APPEXT_LOCK_FILE=auth-bundle/extension.lock.toml` to rehearse the start-up check against the
approved scopes; without it a local run does not consult the lock file.)

To make the extension appear in the host app, it has to be **live**: run it with
`appext dev --env-file auth-bundle/appext.env --host 0.0.0.0` and `appext store verify --env local`.
A store that runs in a container on your machine asks for your extension at the host's address from
inside the container, which a server bound to `127.0.0.1` does not answer. People and the app still
open `http://127.0.0.1:8100`. The environment names (`local`, `prod`) are the store's; `bundle` and
`verify` default to `local` for a store on this machine and `prod` for any other, so say `--env local`
when you work against a remote store ([store.md](store.md#environments)).

### B) With a Keycloak of your own

No platform and no App Store: you only want to run and try the extension against a Keycloak on your
laptop. The project's `compose.yaml` has a `keycloak` profile for exactly this:

```sh
appext keys generate && appext keys session          # secrets in .appext/ (never committed); each refuses
                                                     # to overwrite a file that is there, `appext dev` makes both
appext keycloak export --realm local --out .appext/realm-ext.json --dev-user \
    --backchannel-url http://extension:8000/auth/backchannel-logout
APPEXT_ISSUER=http://keycloak.localhost:58180/realms/local docker compose --profile keycloak up --build
# open http://localhost:8100 and sign in with dev@localhost.invalid / dev
```

`export` writes a realm import: the client with your public key, the scopes your manifest names (with
their audience mappers), a stand-in client for each audience (Keycloak's token exchange needs the
audience to exist), and optionally a test user `dev@localhost.invalid` / `dev`. The Keycloak of the
profile listens on port 58180 as `keycloak.localhost` – a name that reaches the host from a browser
and the Keycloak container from the extension's, so the issuer is the same on both sides – and the
extension is on `http://localhost:8100`. It is a stand-in for the store on a laptop, not a production
tool. See `compose.yaml` in the project for the exact commands.

To run the extension with `appext dev` against a Keycloak you started yourself (`start-dev
--import-realm`, the file mounted into `/opt/keycloak/data/import/`), put its realm URL into
`appext.toml` as `issuer` and export the realm with the default back-channel host
(`host.docker.internal`, the host as seen from Keycloak in a container):

```sh
appext keycloak export --out .appext/realm-ext.json --dev-user     # the realm is named after the issuer's /realms/<name>
appext dev
```

## 5. Look around

- `http://127.0.0.1:8100/_sdk/info` – id, version, client, environment (what the store's
  deployment check reads); `/healthz`, `/readyz`.
- `app/main.py` – the whole backend; `ext.router()` guards every route, `ext.service("data")`
  makes an authenticated call to a service of the platform.
- `appext manifest check` – the manifest rules; `--scan-secrets` is for CI.

Next: [manifest.md](manifest.md), [authentication.md](authentication.md),
[store.md](store.md) (publishing), [deployment.md](deployment.md).
