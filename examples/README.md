# Examples

## hello

A working extension: a Python backend and a small Vite frontend that list the signed-in person's
items, read from a service of the platform. It is exactly what
`appext new hello --template spa --name Hello` generates when no platform is configured – so the
platform is called "the platform" and the example service is `data` (audience `data-api`, scope
`data-read`), the defaults of `[platform.starter]` – kept in the repository. A test
(`tests/test_templates_render.py`) fails when it drifts away from the template, so the example is
always what a new project looks like.

What it demonstrates: `Extension.from_manifest`, a session-guarded `/api/me`, and `/api/items`,
which reads `GET /items` through `ext.service("data")` – a token exchange for the audience
`data-api` with the scope `data-read`. Its frontend is a Vite page using `extFetch`. Its
`appext.toml` is the platform file `appext new` writes: the hints in it say what is still missing.

```sh
cd examples/hello
python3 -m venv .venv && . .venv/bin/activate && pip install -e ../.. && pip install -e ".[test]"
(cd frontend && npm ci && npm run build)    # frontend/dist is not committed
pytest                                      # needs no platform and no network
```

To run it against a real platform (`appext dev`, http://127.0.0.1:8100):

1. Fill in `appext.toml`: `[platform]` `issuer` (the OAuth service) and `store_url` (the App Store),
   and, for the service the example calls, `[platform.services]` with `data-api = "<base URL>"`
   (or set `APPEXT_SERVICE_DATA_URL`).
2. Adjust the `[[services]]` block of `extension.toml` and the path in `app/main.py` to a service
   and a route the platform really offers.
3. Make the client `ext-hello` known to the OAuth service: register it with the platform's store
   (`appext store login && appext store register && appext store submit`, then a reviewer
   approves) or write a realm for a Keycloak of your own (`appext keycloak export`).
