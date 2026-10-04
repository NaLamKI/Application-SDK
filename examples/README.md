# Examples

## hello-fmis

A working extension that shows the signed-in person's fields from the FMIS API. It is the
`spa` template (`appext new hello-fmis --template spa`), kept in the repository: a test
(`sdk/tests/test_templates_render.py`) fails when it drifts away from the template, so the
example is always what a new project looks like.

What it demonstrates: `Extension.from_manifest`, a session-guarded `/api/me`, and
`/api/fields`, which reads `GET /fields` of the FMIS API through `ext.service("fmis")` – a
token exchange for the audience `fmis-api` with the scope `ext-data-read`. Its frontend is a
Vite page using `extFetch`.

```sh
cd sdk/examples/hello-fmis
python3 -m venv .venv && . .venv/bin/activate && pip install -e ../.. && pip install -e ".[test]"
(cd frontend && npm ci && npm run build)    # frontend/dist is not committed
pytest
appext dev                                  # http://127.0.0.1:8100
```

`appext dev` needs the client `ext-hello-fmis` in Keycloak: register it with the local store
(`appext store login && appext store register && appext store submit`, then a reviewer
approves) or write a realm for a Keycloak of your own (`appext keycloak export`).
The FMIS API must be reachable at `http://127.0.0.1:8000/api/v1` (the SDK's default in the
`local` environment; override with `APPEXT_SERVICE_FMIS_URL`).
