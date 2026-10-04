# Deployment

An extension is one image with one process. The same image runs in development and in
production; what differs is the **auth bundle** (secret-free configuration from the store)
and the **secrets** (files you provide). Sessions live in Redis, so the container is
stateless and scales horizontally.

## The image

The base image `appext/python:<version>-py3.12` (`sdk/docker/build.sh`) is Python 3.12 slim,
the SDK with uvicorn and the Redis client, and the unprivileged user `appext`. Its command is
`appext serve app.main:app`, its health check `appext health`. An extension's Dockerfile
builds on it (this is what `appext new` writes; the HTMX template has no Node stage):

```dockerfile
ARG APPEXT_IMAGE=appext/python:0.1.0-py3.12

FROM node:22-alpine AS frontend                 # SPA only
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ${APPEXT_IMAGE}
WORKDIR /app
COPY pyproject.toml extension.toml icon.svg ./
COPY app/ ./app/
USER root                                       # the one privileged step
RUN pip install --no-cache-dir .
COPY --from=frontend /src/dist ./frontend/dist
USER appext
EXPOSE 8000
HEALTHCHECK CMD ["appext", "health"]
CMD ["appext", "serve", "app.main:app"]
```

`appext serve` runs uvicorn without reload on `0.0.0.0:$PORT` (default 8000). It trusts
proxy headers (`X-Forwarded-For`, `-Proto`) **only from the addresses in
`APPEXT_TRUSTED_PROXIES`** – comma-separated IPs or networks, e.g. your ingress controller's
pod network; unset means from nobody. The SDK adds the standard security headers itself.
`appext health` exits 0 when `http://127.0.0.1:$PORT/healthz` answers 200.

Keep **everything environment-specific out of the image**: no bundle, no key, no URL. The
`.dockerignore` of the templates lists `.appext`, `*.pem`, `auth-bundle` and friends; CI should
run `appext manifest check --scan-secrets`, which fails the build when a private key or a
literal `client_secret` is in the build context.

## Configuration

| Variable | Example | Purpose | From |
|---|---|---|---|
| `APPEXT_ENV` | `prod` | `local` is for the desk (defaults for everything). Any other value is a deployment: nothing is guessed, every missing value is named at start. | bundle |
| `APPEXT_ISSUER` | `https://sso.example.com/realms/fmis` | Keycloak realm (https outside `local`) | bundle |
| `APPEXT_CLIENT_ID` | `ext-projektauswertung` | Client id | bundle |
| `APPEXT_CLIENT_AUTH` | `private_key_jwt` | or `client_secret` | bundle |
| `APPEXT_PUBLIC_URL` | `https://projektauswertung.apps.example.com` | The origin only. Redirect URI in browser mode, CSRF origin, cookie flags | bundle |
| `APPEXT_APP_ORIGINS` | `https://app.example.com` | Where the FMIS **web** app lives (comma-separated origins). It may embed the extension (`frame-ancestors`), `bridge.js` talks to it, and the bar in a browser tab leads back to it. Empty = nothing may frame the extension. For the frame to sign in, app, extension and Keycloak must share one registrable domain (cookies are `SameSite=Lax`) | bundle |
| `APPEXT_APP_REDIRECT_URI` | `org.agrifooddata.apps.fmis.web:/callback` | Redirect URI in app mode | bundle |
| `APPEXT_SERVICE_<NAME>_URL` | `https://fmis.example.com/api/v1` | Base URL of each service in the manifest (name upper-cased) | bundle |
| `APPEXT_CLIENT_KEY_FILE` | `/run/secrets/client_key` | Private key for `private_key_jwt`: PEM (or a private JWK) | **secret** |
| `APPEXT_CLIENT_KEY_ID` | `4Zk83C…` | Optional: the `kid` in the client assertion. With one key registered Keycloak finds it without; set it to the JWK's `kid` (printed by `appext keys generate`) to pick a key during a rotation | deployment |
| `APPEXT_CLIENT_SECRET_FILE` | `/run/secrets/client_secret` | Only for `client_secret` | **secret** |
| `APPEXT_SESSION_STORE` | `redis://redis:6379/3` | `memory` (development only) or `redis://` / `rediss://` | deployment |
| `APPEXT_SESSION_KEY_FILE` | `/run/secrets/session_key` | Encrypts tokens in the store; one key per line, the first encrypts, all decrypt | **secret** |
| `APPEXT_TRUSTED_PROXIES` | `10.0.0.0/8` | Who may set `X-Forwarded-*` | deployment |
| `APPEXT_COOKIE_SECURE` | `auto` | `auto` follows the public URL (https → `Secure` and the `__Host-` prefix) | deployment |
| `APPEXT_SESSION_MAX_AGE` | `2592000` | Seconds, default 30 days (never beyond the refresh token) | deployment |
| `APPEXT_HTTP_TIMEOUT` | `10` | Seconds for calls to Keycloak and services | deployment |
| `APPEXT_LOCK_FILE` | `/app/extension.lock.toml` | Where the lock file is; default next to the manifest or in the working directory | deployment |
| `PORT` | `8000` | Port of `appext serve` and `appext health` | deployment |

Use Redis (`APPEXT_SESSION_STORE=redis://…`) also while developing against the app: with the in-process
store every restart of the extension drops the sessions, and the app has to hand the sign-in to the
system browser again (an operating-system prompt) at the next opening. `tool/dev_stack.py` does this.

`APPEXT_SESSION_STORE=memory` outside `local` is an error: with a second replica or a restart
every session would be gone. Secrets can be given as files (preferred: Docker/Kubernetes
secrets) – the SDK never logs them and masks them in any `repr`.

## The auth bundle

After a reviewer approves a version, `appext store bundle --env prod --out auth-bundle`
downloads and unpacks:

```
auth-bundle/
├── appext.env              # APPEXT_ENV, _ISSUER, _CLIENT_ID, _CLIENT_AUTH, _PUBLIC_URL,
│                           # _APP_REDIRECT_URI, _SERVICE_<NAME>_URL – no secret
├── extension.lock.toml     # the approved state: version, client, approved scopes
└── README.md
```

The **lock file is more than documentation**: at start the SDK compares the manifest with
`approved_scopes` and refuses to start (`LockError`) if the manifest asks for more, if the
version or the client id differs, or if the file was generated for another environment. A
missing lock file is allowed only in `local`. It must match the **version of the manifest in
the image** – deploy the bundle that was fetched for that version.

The bundle does **not** go into the image: it is mounted per environment, so the same image
runs everywhere. A `client_secret` is never in the bundle.

## Secrets

```sh
appext keys generate --out keys          # keys/client_key.pem (mode 0600) + keys/client_key.jwk.json (public)
appext keys session  --out keys/session_key
appext store key --key keys/client_key.jwk.json   # upload the public half (register does it too, from .appext/)
```

The private key never leaves the machine or the secret store of the deployment; only the
public JWK is uploaded. The session key is a random 32-byte key (base64, one per line);
**every replica must use the same file**. To rotate it, put the new key on the first line and
keep the old one(s) below: new sessions are encrypted with the first, existing ones still
decrypt. To rotate the client key: generate a new pair, upload the new public key (`appext store key`;
the store keeps one key per extension and replaces it), and switch the deployment's secret
(and `APPEXT_CLIENT_KEY_ID`, if you set it) **right after** – the swap takes effect at once,
and every sign-in, refresh and token exchange authenticates with the key, so the gap between
the two steps is a short outage.

Mounted secret files must be readable by the container user (`appext`, uid 10001). Docker
Desktop shows host files to the container as readable; on Linux a `0600` file owned by you is
not, so `chown 10001 keys/*` (or make them group-readable and set `group_add`). Kubernetes
secrets mounted with `defaultMode: 0440` and `fsGroup: 10001` do the same.

## Docker Compose (one host)

```yaml
services:
  projektauswertung:
    image: registry.example.com/ext/projektauswertung:1.2.0
    env_file: ./auth-bundle/appext.env
    environment:
      APPEXT_SESSION_STORE: redis://redis:6379/3
      APPEXT_CLIENT_KEY_FILE: /run/secrets/client_key
      APPEXT_SESSION_KEY_FILE: /run/secrets/session_key
      APPEXT_TRUSTED_PROXIES: 172.18.0.0/16        # the reverse proxy's network
    volumes:
      - ./auth-bundle/extension.lock.toml:/app/extension.lock.toml:ro
    secrets: [client_key, session_key]
  redis:
    image: redis:7-alpine

secrets:
  client_key:  { file: ./keys/client_key.pem }     # generated locally, never uploaded
  session_key: { file: ./keys/session_key }
```

## Kubernetes

`appext.env` becomes a ConfigMap, the lock file a mounted ConfigMap entry, the keys Secrets:

```sh
kubectl create configmap projektauswertung-bundle --from-env-file=auth-bundle/appext.env
kubectl create configmap projektauswertung-lock --from-file=extension.lock.toml=auth-bundle/extension.lock.toml
kubectl create secret generic projektauswertung-keys \
    --from-file=client_key=keys/client_key.pem --from-file=session_key=keys/session_key
```

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: projektauswertung }
spec:
  replicas: 2
  selector: { matchLabels: { app: projektauswertung } }
  template:
    metadata: { labels: { app: projektauswertung } }
    spec:
      containers:
        - name: extension
          image: registry.example.com/ext/projektauswertung:1.2.0
          ports: [{ containerPort: 8000 }]
          envFrom: [{ configMapRef: { name: projektauswertung-bundle } }]
          env:
            - { name: APPEXT_SESSION_STORE, value: "redis://redis.apps:6379/3" }
            - { name: APPEXT_CLIENT_KEY_FILE, value: /run/secrets/ext/client_key }
            - { name: APPEXT_SESSION_KEY_FILE, value: /run/secrets/ext/session_key }
            - { name: APPEXT_TRUSTED_PROXIES, value: "10.0.0.0/8" }   # your ingress controller's network
          volumeMounts:
            - { name: keys, mountPath: /run/secrets/ext, readOnly: true }
            - { name: lock, mountPath: /app/extension.lock.toml, subPath: extension.lock.toml, readOnly: true }
          livenessProbe:  { httpGet: { path: /healthz, port: 8000 } }
          readinessProbe: { httpGet: { path: /readyz,  port: 8000 } }
          securityContext: { runAsNonRoot: true, allowPrivilegeEscalation: false, readOnlyRootFilesystem: true }
      volumes:
        - { name: keys, secret: { secretName: projektauswertung-keys } }
        - { name: lock, configMap: { name: projektauswertung-lock } }
```

Plus a `Service` and an `Ingress` for `projektauswertung.apps.example.com` with TLS. (A Helm
template generated from the manifest is phase 3 and not built.) `/readyz` checks that the
Keycloak discovery document and the session store are reachable; use it for readiness, and
`/healthz` (the process is alive) for liveness – otherwise a Keycloak outage restarts all
replicas.

## Hosting and scaling

- **One subdomain per extension** (`<id>.apps.example.com`, wildcard certificate). Only then
  are origins and cookies separate; under paths of one host an extension could use another's
  session. The `__Host-` cookie has no `Domain` attribute for the same reason.
- **Horizontally scalable**: sessions, the token cache and the refresh locks live in Redis.
  Discovery and JWKS are cached per instance. Use a separate Redis **database number** (or
  instance) per extension; the SDK accepts any `redis://` URL.
- **TLS ends at the ingress**; set `APPEXT_PUBLIC_URL` to the public `https` origin and
  `APPEXT_TRUSTED_PROXIES` to the ingress.
- **One process per container**, uvicorn without workers: scale by replicas.

## Going live

```sh
appext store verify --env prod      # the store reads /_sdk/info and /readyz of your deployment
```

`verify` checks that the deployment answers with the right id, version and client id and is
ready; on success a version in `APPROVED` becomes `LIVE` and appears in the app's catalog.
See [store.md](store.md).
