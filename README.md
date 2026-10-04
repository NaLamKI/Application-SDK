# appext – the SDK for FMIS extensions

An **extension** is a web page with its own Python backend that the FMIS app opens in a
WebView. It is a container with one process: the frontend, its API and the sign-in
share one origin. `appext` gives that process what every extension needs and nobody
should write twice:

- **Sign-in** with the app's Keycloak session (no password prompt in the app), a
  server-side **session** (the browser holds only a cookie, never a token);
- **calls to other services on behalf of the person**, with a token cut to exactly one
  service and its scopes (RFC 8693 token exchange);
- the **protective defaults**: `__Host-` cookie, CSRF check, `default-src 'self'`,
  `frame-ancestors 'none'`, safe `return_to`;
- a **command line** that scaffolds, runs, checks and publishes: `appext new`, `dev`,
  `manifest check`, `keys`, `keycloak export`, `store …`;
- **test helpers** that need neither Keycloak nor the target services.

Not everything needs a server. A **link** is an App Store entry that the app simply opens in the
system browser – no container, no sign-in, nothing passed on to the page. It is a manifest with
`kind = "link"` and an address, created with `appext new <id> --template link`
([docs/manifest.md](docs/manifest.md#links)); the rest of this page is about extensions.

```
          ┌────────────┐   Auth sheet (system browser)   ┌──────────┐
          │ FMIS app   │────────────────────────────────▶│ Keycloak │
          │  WebView   │                                 └────▲─────┘
          └─────┬──────┘                                      │ code, token exchange,
                │ HTTPS, session cookie only                  │ refresh, back-channel logout
          ┌─────▼──────────────────────────────┐              │
          │ Extension container  (one origin)  │──────────────┘
          │  /            frontend (SPA or HTMX pages)
          │  /api/*       your routes   ◀── ext.router(), ext.current_user
          │  /auth/*      sign-in, logout          (appext)
          │  /_sdk/*      client.js, bridge.js, info, icon
          │  /healthz /readyz
          └─────┬───────────────────────┬──────┘
                │ exchanged token       │ sessions, token cache, locks
                ▼ (per call, per service)▼
          ┌────────────┐          ┌──────────┐
          │ FMIS API   │          │  Redis   │   (memory store only for development)
          │ other      │          └──────────┘
          │ services   │
          └────────────┘

   Control plane, not in the picture at run time: the App Store registers the manifest,
   a reviewer approves it, the store sets up the Keycloak client and hands the developer
   the auth bundle; the app loads its catalog from the store.
```

## Five minutes

```sh
pip install appext                       # or: pip install -e sdk  inside the FMIS repository
appext new hello --template spa          # or --template htmx (Jinja2 pages, no JavaScript build)
cd hello
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[test]"
(cd frontend && npm ci && npm run build)
pytest                                   # unit tests, no Keycloak needed
appext dev                               # http://127.0.0.1:8100
```

What `appext dev` needs is a Keycloak that knows the client `ext-hello`: either the local
App Store provisions it (`appext store login && appext store register && appext store
submit`, then a reviewer approves), or `appext keycloak export` writes a realm for a
Keycloak of your own. [docs/quickstart.md](docs/quickstart.md) walks through both.

The whole backend of a new project:

```python
from fastapi import Depends
from appext import Extension, ServiceClient, User

ext = Extension.from_manifest("extension.toml")
api = ext.router(prefix="/api")                 # every route needs a session

@api.get("/me")
async def me(user: User = Depends(ext.current_user)):
    return {"sub": user.sub, "roles": sorted(user.roles)}

@api.get("/fields")
async def fields(fmis: ServiceClient = Depends(ext.service("fmis"))):   # token exchange
    return (await fmis.get("/fields")).json()

app = ext.asgi(static_dir="frontend/dist")
```

## Where things are

| | |
|---|---|
| `src/appext/` | the library: `Extension`, `ServiceClient`, `User`, `verify`, `testing`; the CLI in `appext.cli` |
| `templates/` | `appext new` renders these: `spa` (Vite), `htmx` (Jinja2) and `link` (only a manifest: no server) |
| `docker/` | the base image `appext/python:<version>-py3.12` and `build.sh` |
| `examples/hello-fmis/` | a working extension: shows the signed-in person's fields from the FMIS API |
| `conformance/manifests/` | manifest cases that the SDK **and** the store must judge identically |
| `docs/` | the guides below |

## Guides

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | from nothing to a running extension, with the local store or your own Keycloak |
| [cli.md](docs/cli.md) | every `appext` command, what `appext dev` sets, the files the CLI writes |
| [manifest.md](docs/manifest.md) | every key of `extension.toml`, links (`kind = "link"`) and the rules |
| [authentication.md](docs/authentication.md) | sign-in, session, logout, app mode, `return_to`, CSRF |
| [services.md](docs/services.md) | calling other services, token exchange, `ConsentRequired`; writing a target service with `appext.verify` |
| [testing.md](docs/testing.md) | `appext.testing`: test client, service mocks, the fake identity provider |
| [deployment.md](docs/deployment.md) | image, auth bundle, Compose and Kubernetes, secrets, scaling |
| [store.md](docs/store.md) | the App Store from a developer's view; `appext store …` |
| [security.md](docs/security.md) | what the SDK protects against and how |
| [bridge.md](docs/bridge.md) | `/_sdk/bridge.js`: talking to the app around the WebView |

The contract between SDK, store and app is `concepts/app-store.md` in the repository; where
this documentation and that file differ, the file wins – and please tell us.
