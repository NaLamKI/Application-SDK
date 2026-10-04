# appext – an SDK for extensions of a platform

An **extension** is a web page with its own Python backend that a platform's host app opens in a
WebView or a frame – or that people simply open in a browser. It is a container with one process:
the frontend, its API and the sign-in share one origin. `appext` gives that process what every
extension needs and nobody should write twice:

- **Sign-in** with the platform's OAuth 2.0 / OpenID Connect service (authorization code flow with
  PKCE), a server-side **session** (the browser holds only a cookie, never a token), and – where the
  platform has a host app – a silent hand-over of the person's sign-in from the app;
- **calls to other services on behalf of the person**, with a token cut to exactly one service and
  its scopes (RFC 8693 token exchange);
- the **protective defaults**: `__Host-` cookie, CSRF check, `default-src 'self'`,
  `frame-ancestors 'none'`, safe `return_to`;
- a **command line** that scaffolds, runs, checks and publishes: `appext new`, `dev`,
  `manifest check`, `keys`, `keycloak export`, `store …`;
- **test helpers** that need neither an OAuth service nor the target services.

Not everything needs a server. A **link** is an App Store entry that the host app simply opens in the
system browser – no container, no sign-in, nothing passed on to the page. It is a manifest with
`kind = "link"` and an address, created with `appext new <id> --template link`
([docs/manifest.md](docs/manifest.md#links)); the rest of this page is about extensions.

## Who this is for

- **Extension developers** write an extension for a platform. Start with the
  [five minutes](#five-minutes) below, then [docs/quickstart.md](docs/quickstart.md).
- **Platform operators** adopt the SDK for their own platform: they point it at *their* OAuth service
  and *their* App Store with a platform file ([docs/platform.md](docs/platform.md)) and make sure the
  platform provides what the SDK expects ([docs/platform-contract/README.md](docs/platform-contract/README.md)).

`appext` has no built-in platform. A **platform** is any system that follows the reference
architecture: it offers an OAuth 2.0 / OpenID Connect service (the **issuer**; Keycloak is the
reference implementation), an **App Store** that registers, reviews and lists extensions, and
optionally a **host app** that shows extensions. Where the SDK has to know about it – the issuer, the
store, the host app's return address – you say so once, in a small file ([docs/platform.md](docs/platform.md)).
Nothing is guessed.

## The picture

```
          ┌────────────┐   sign-in (system browser)      ┌───────────────────┐
          │  Host app  │────────────────────────────────▶│   OAuth service   │
          │  WebView   │                                 │    (the issuer)   │
          │  or frame  │                                 └─────────▲─────────┘
          └─────┬──────┘                                           │ code, token exchange,
                │ HTTPS, session cookie only                       │ refresh, back-channel logout
          ┌─────▼──────────────────────────────┐                   │
          │ Extension container  (one origin)  │───────────────────┘
          │  /            frontend (SPA or HTMX pages)
          │  /api/*       your routes   ◀── ext.router(), ext.current_user
          │  /auth/*      sign-in, logout          (appext)
          │  /_sdk/*      client.js, bridge.js, info, icon
          │  /healthz /readyz
          └─────┬───────────────────────┬──────┘
                │ exchanged token       │ sessions, token cache, locks
                ▼ (per service call)    ▼
          ┌────────────┐          ┌──────────┐
          │  Target    │          │  Redis   │   (memory store only for development)
          │  services  │          └──────────┘
          └────────────┘

   Control plane, not in the picture at run time: the App Store registers the manifest, a reviewer
   approves it, the store sets up the extension's client at the issuer and hands the developer the
   auth bundle; the host app loads its catalog from the store. Without a host app the extension is
   a website like any other.
```

## Five minutes

You need Python 3.12 or newer, and for the SPA template Node 22. You also need a platform to
write for: its issuer and its App Store (ask the platform's operator – see
[docs/platform.md](docs/platform.md)), or a Keycloak of your own.

```sh
python3 -m venv venv && . venv/bin/activate
pip install appext                       # or, from a checkout of this repository: pip install -e .
appext new hello --template spa          # or --template htmx (Jinja2 pages, no JavaScript build)
cd hello
pip install -e ".[test]"
(cd frontend && npm ci && npm run build)
pytest                                   # unit tests: no OAuth service, no platform needed
```

`appext new` writes `hello/appext.toml`, the file that says which platform the project is written
for. Until you fill it in, it holds hints. Name the platform's OAuth service and App Store (or pass
`--platform NAME` to `appext new` once you have a platform file, see
[docs/platform.md](docs/platform.md)):

```toml
# hello/appext.toml
[platform]
name = "Acme Platform"
issuer = "https://auth.acme.example/realms/acme"
store_url = "https://api.acme.example/api/v1"
```

```sh
appext dev                               # http://127.0.0.1:8100
```

What `appext dev` needs is an issuer that knows the client `ext-hello`: either the platform's App
Store provisions it (`appext store login && appext store register && appext store submit`, then a
reviewer approves), or `appext keycloak export` writes a realm for a Keycloak of your own.
[docs/quickstart.md](docs/quickstart.md) walks through both.

The whole backend of a new project:

```python
from fastapi import Depends
from appext import Extension, ServiceClient, User

ext = Extension.from_manifest("extension.toml")
api = ext.router(prefix="/api")                 # every route needs a session

@api.get("/me")
async def me(user: User = Depends(ext.current_user)):
    return {"sub": user.sub, "roles": sorted(user.roles)}

@api.get("/items")
async def items(data: ServiceClient = Depends(ext.service("data"))):   # token exchange
    return (await data.get("/items")).json()

app = ext.asgi(static_dir="frontend/dist")
```

`data` is the *starter service* of the platform: the service, audience and scope that `appext new`
writes into the manifest (`data`, `data-api`, `data-read` unless the platform file says otherwise).
Replace it with a service your platform really offers.

## Where things are

| | |
|---|---|
| `src/appext/` | the library: `Extension`, `ServiceClient`, `User`, `verify`, `testing`; the platform settings in `appext.platform`; the CLI in `appext.cli` |
| `templates/` | `appext new` renders these: `spa` (Vite), `htmx` (Jinja2) and `link` (only a manifest: no server) |
| `examples/hello/` | a working extension: what `appext new hello --template spa` writes, kept in the repository |
| `docker/` | the base image `appext/python:<version>-py3.12` and `build.sh` |
| `conformance/manifests/` | manifest cases that the SDK **and** the store must judge identically ([README](conformance/manifests/README.md)) |
| `docs/` | the guides below; `docs/platform-contract/` is the contract for platform builders |

## Guides

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | from nothing to a running extension, with the platform's App Store or your own Keycloak |
| [platform.md](docs/platform.md) | pointing the SDK at a platform: the platform file, where each setting comes from, several platforms side by side |
| [cli.md](docs/cli.md) | every `appext` command, what `appext dev` sets, the files the CLI writes |
| [manifest.md](docs/manifest.md) | every key of `extension.toml`, links (`kind = "link"`) and the rules |
| [authentication.md](docs/authentication.md) | sign-in, session, logout, app mode, `return_to`, CSRF |
| [services.md](docs/services.md) | calling other services, token exchange, `ConsentRequired`; writing a target service with `appext.verify` |
| [testing.md](docs/testing.md) | `appext.testing`: `configure_test_environment`, test client, service mocks, the fake identity provider |
| [deployment.md](docs/deployment.md) | image, auth bundle, Compose and Kubernetes, secrets, scaling |
| [store.md](docs/store.md) | the App Store from a developer's view; `appext store …` |
| [security.md](docs/security.md) | what the SDK protects against and how |
| [bridge.md](docs/bridge.md) | `/_sdk/bridge.js`: talking to the host app around the WebView |
| [platform-contract/README.md](docs/platform-contract/README.md) | **for platform builders:** what a platform must provide so that the SDK works |

Where these guides and the platform contract differ on what a platform must provide, the contract
wins – and please tell us.

## Status, version and licence

`appext` is at version 0.1.0 (`appext --version`); the projects that `appext new` creates depend on
`appext>=0.1,<1`. The package metadata currently says `Proprietary`; the licence is to be added by
the maintainers. Vulnerabilities in the SDK should be reported to the maintainers privately, not in a
public issue.

## Working on the SDK itself

```sh
pip install -e ".[test]"
pytest
```

The suite needs no Keycloak and no platform. Two groups of tests use external tools and
skip themselves when they are missing: the browser helpers (`/_sdk/client.js`, `bridge.js`) need
Node, and a Compose check needs Docker. The tests that build the templates' frontend run
`npm ci && npm run build`, which needs npm and the npm registry (or a warm cache); set
`APPEXT_SKIP_NPM=1` to skip them:

```sh
APPEXT_SKIP_NPM=1 pytest
```
