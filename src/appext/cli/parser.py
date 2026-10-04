"""The command tree. Handlers are named by string and imported on demand, so that
`appext health` (a Docker HEALTHCHECK every few seconds) never pays for `httpx`
or `cryptography`."""

from __future__ import annotations

import argparse

DEFAULT_ISSUER = "http://127.0.0.1:58080/realms/fmis"
DEFAULT_STORE_URL = "http://127.0.0.1:8000/api/v1"
TEMPLATES = ("spa", "htmx", "link")


def _store_options() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store-url", help=f"App Store API base (APPEXT_STORE_URL, default {DEFAULT_STORE_URL})")
    common.add_argument("--issuer", help=f"Keycloak realm of the sign-in (APPEXT_ISSUER, default {DEFAULT_ISSUER})")
    return common


def _extension_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("id", nargs="?", help="extension id (default: the one in ./extension.toml)")
    p.add_argument("--manifest", help="path to extension.toml, to take the id from")


def _env_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--env", help="environment (default: local for a local store, else prod)")


def _add_store(sub: argparse._SubParsersAction) -> None:
    store = sub.add_parser("store", help="talk to the App Store", description="Developer and reviewer commands of the App Store API.")
    cmds = store.add_subparsers(dest="store_command", required=True, metavar="<command>")
    common = _store_options()

    def add(name: str, handler: str, help: str) -> argparse.ArgumentParser:
        p = cmds.add_parser(name, parents=[common], help=help, description=help)
        p.set_defaults(handler=f"appext.cli.store:{handler}")
        return p

    p = add("login", "login", "sign in with the device authorization grant")
    p.add_argument("--no-browser", action="store_true", help="only print the address and code")
    p.add_argument("--client-id", help="public client of the CLI (default appext-cli)")
    p = add("logout", "logout", "forget the stored credentials")
    p.add_argument("--client-id", help=argparse.SUPPRESS)

    p = add("register", "register", "upload extension.toml (and the public key) as a new draft version")
    p.add_argument("--manifest", help="path to extension.toml (default ./extension.toml)")
    p.add_argument("--key", help="public JWK file (default .appext/client_key.jwk.json)")

    p = add("key", "key", "upload the public key (JWK) of an extension")
    _extension_arg(p)
    p.add_argument("--key", help="public JWK file (default .appext/client_key.jwk.json)")

    p = add("submit", "submit", "submit the newest draft version for review")
    _extension_arg(p)

    p = add("bundle", "bundle", "download the auth bundle and unpack it")
    _extension_arg(p)
    _env_arg(p)
    p.add_argument("--out", default="auth-bundle", help="directory to unpack into (default ./auth-bundle)")

    p = add("verify", "verify", "let the store check the running deployment")
    _extension_arg(p)
    _env_arg(p)

    p = add("status", "status", "show one extension, or all of yours")
    _extension_arg(p)
    p.add_argument("--all", action="store_true", help="every extension the store shows you (reviewers)")

    p = add("services", "services", "list the service catalog: audiences and scopes you may ask for")

    p = add("rotate-secret", "rotate_secret", "new client secret (client_secret extensions only)")
    _extension_arg(p)
    p.add_argument("--out", required=True, help="file to write the secret to (mode 0600); it is never printed")

    p = add("approve", "approve", "reviewer: approve a submitted version")
    p.add_argument("id")
    p.add_argument("--note")
    p = add("reject", "reject", "reviewer: reject a submitted version")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = add("suspend", "suspend", "admin: suspend an extension")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p = add("unsuspend", "unsuspend", "admin: lift a suspension")
    p.add_argument("id")
    p.add_argument("--reason", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="appext",
        description="Build, run and publish extensions for the FMIS app.",
    )
    parser.add_argument("--version", action="store_true", help="print the SDK version")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("new", help="create a project from a template")
    p.add_argument("id", help="extension id: lower case, digits, hyphens (the client becomes ext-<id>)")
    p.add_argument("--template", choices=TEMPLATES, default="spa", help="spa (Vite), htmx (Jinja pages) or link (an entry that opens a web page in the browser: just a manifest); default spa")
    p.add_argument("--dir", default=".", help="directory to create the project in (default .)")
    p.add_argument("--name", help="display name (default: derived from the id)")
    p.set_defaults(handler="appext.cli.scaffold:new")

    p = sub.add_parser("dev", help="run the extension in browser mode with reload")
    p.add_argument("app", nargs="?", default="app.main:app", help="module:attribute of the ASGI app (default app.main:app)")
    p.add_argument("--manifest", help="path to extension.toml (default ./extension.toml)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, help="default: dev_port of the manifest, else 8000")
    p.add_argument("--env-file", action="append", default=[], metavar="FILE",
                   help="KEY=VALUE lines that override the local defaults (e.g. auth-bundle/appext.env); repeatable")
    p.add_argument("--no-reload", action="store_true")
    p.set_defaults(handler="appext.cli.run:dev")

    p = sub.add_parser("serve", help="production entry: run an ASGI app with uvicorn")
    p.add_argument("app", help="module:attribute, e.g. app.main:app")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, help="default: $PORT, else 8000")
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(handler="appext.cli.run:serve")

    p = sub.add_parser("health", help="exit 0 if the local /healthz answers 200 (Docker HEALTHCHECK)")
    p.add_argument("--port", type=int, help="default: $PORT, else 8000")
    p.set_defaults(handler="appext.cli.run:health")

    manifest = sub.add_parser("manifest", help="manifest tools").add_subparsers(dest="manifest_command", required=True, metavar="<command>")
    p = manifest.add_parser("check", help="check extension.toml against the manifest rules")
    p.add_argument("path", nargs="?", help="extension.toml or the project directory (default .)")
    p.add_argument("--scan-secrets", action="store_true", help="also scan the project for private keys and client secrets (CI)")
    p.set_defaults(handler="appext.cli.check:check")

    keys = sub.add_parser("keys", help="key material").add_subparsers(dest="keys_command", required=True, metavar="<command>")
    p = keys.add_parser("generate", help="client key pair for private_key_jwt")
    p.add_argument("--out", default=".appext", help="directory for client_key.pem and client_key.jwk.json (default .appext)")
    p.add_argument("--alg", choices=("RS256", "ES256"), default="RS256")
    p.add_argument("--force", action="store_true", help="overwrite an existing key")
    p.set_defaults(handler="appext.cli.keys:generate")
    p = keys.add_parser("session", help="random key that encrypts the tokens in the session store")
    p.add_argument("--out", default=".appext/session_key", help="file to write (default .appext/session_key)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(handler="appext.cli.keys:session")

    kc = sub.add_parser("keycloak", help="Keycloak helpers").add_subparsers(dest="keycloak_command", required=True, metavar="<command>")
    p = kc.add_parser("export", help="realm import for a LOCAL Keycloak: client, scopes, public key")
    p.add_argument("path", nargs="?", help="extension.toml or the project directory (default .)")
    p.add_argument("--out", help="write here instead of stdout")
    p.add_argument("--realm", default="fmis", help="realm name (default fmis)")
    p.add_argument("--key", help="public JWK file (default .appext/client_key.jwk.json; created if missing)")
    p.add_argument("--consent-audience", default="fmis-api", help="audience of the consent scopes (default fmis-api)")
    p.add_argument("--backchannel-host", default="host.docker.internal",
                   help="host under which Keycloak reaches the extension for the back-channel logout")
    p.add_argument("--backchannel-url", help="the whole back-channel logout URL (default http://<backchannel-host>:<dev_port>/auth/backchannel-logout)")
    p.add_argument("--dev-user", action="store_true", help="add a test user (dev@localhost.invalid) for a fresh local realm")
    p.set_defaults(handler="appext.cli.realm:export")

    _add_store(sub)
    return parser
