# {{name}}

An extension for the FMIS app, created with `appext new {{id}} --template htmx`:
a Python backend (FastAPI, the `appext` SDK) that renders its pages with Jinja2 and
updates them with HTMX. The SDK handles sign-in, the server-side session and calls to
other services; the browser only ever holds a session cookie. No JavaScript build.

```
app/main.py            routes: pages behind the session, one call to the FMIS API
app/templates/         Jinja2 pages and fragments
static/                htmx.min.js (vendored, 0BSD), app.js, style.css
extension.toml         manifest: id, version, scopes, services
icon.svg               shown in the app's store
tests/                 unit tests without Keycloak (appext.testing)
Dockerfile compose.yaml
```

The default Content-Security-Policy is `default-src 'self'`: no inline scripts, no inline
styles, no CDN. That is why htmx is vendored and why `base.html` switches off its injected
style and `eval`.

## Run it

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"                   # needs the appext package
pytest                                     # no Keycloak needed
appext dev                                 # http://127.0.0.1:8100, reloads on Python changes
```

`appext dev` needs a Keycloak that knows this client. Either register the extension
in the local App Store (`appext store login`, `register`, `submit`; a reviewer approves),
or run a Keycloak of your own (`appext keycloak export`, see `compose.yaml`).

## Publish it

```sh
appext manifest check --scan-secrets       # in CI
appext store register && appext store submit
# after a reviewer approved:
appext store bundle --env prod --out auth-bundle
docker build -t registry.example.com/ext/{{id}}:0.1.0 .
# deploy with auth-bundle/appext.env, the lock file and your secrets, then:
appext store verify --env prod
```

Keys and secrets live in `.appext/` (git- and docker-ignored). Never commit them,
never put them in the image: the SDK reads them from files at run time.
