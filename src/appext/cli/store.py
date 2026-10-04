"""`appext store …`: the developer's (and reviewer's) side of the App Store API
(`concepts/app-store.md` §7).

Every command is one or two requests; what the store answers is shown as the
store says it – error `code` and `message` included.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from . import login as signin
from .context import CliError, Context
from .keys import JWK_FILE, load_public_jwk, thumbprint, write_secret
from .parser import DEFAULT_ISSUER, DEFAULT_STORE_URL
from .project import extension_id, load_project, manifest_path

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def warn_if_unencrypted(ctx: Context, url: str, what: str) -> None:
    """A bearer token over plain http to another machine is readable by anyone on the way."""
    parts = urlparse(url)
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        ctx.warn(f"{what} {url} is not https: your sign-in token travels unencrypted")


# --- Settings ------------------------------------------------------------


def store_url(args, ctx: Context) -> str:
    return (args.store_url or ctx.env.get("APPEXT_STORE_URL") or DEFAULT_STORE_URL).rstrip("/")


def issuer(args, ctx: Context) -> str:
    return (args.issuer or ctx.env.get("APPEXT_ISSUER") or DEFAULT_ISSUER).rstrip("/")


def default_environment(url: str) -> str:
    """A store on this machine serves the `local` environment; a remote one is production."""
    return "local" if urlparse(url).hostname in LOCAL_HOSTS else "prod"


# --- The API -------------------------------------------------------------


def explain(response) -> CliError:
    """An API error as the person should read it: `code: message`, plus the manifest errors if any."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = body.get("detail", body) if isinstance(body, dict) else {}
    lines: list[str] = []
    code = message = ""
    if isinstance(detail, dict):
        code, message = detail.get("code", ""), detail.get("message", "")
        for item in detail.get("errors") or body.get("errors") or []:
            lines.append(f"  {item.get('path') or '(manifest)'}: {item.get('message')}")
    elif isinstance(detail, list):  # FastAPI's own request validation
        for item in detail:
            lines.append(f"  {'.'.join(str(p) for p in item.get('loc', []))}: {item.get('msg')}")
        code = "validation_error"
    elif isinstance(detail, str):
        message = detail
    head = f"{code}: {message}".strip(": ") or f"HTTP {response.status_code}"
    hint = {
        401: "Run `appext store login`.",
        403: "Your account lacks the role for this (store-developer, store-reviewer or store-admin).",
    }.get(response.status_code)
    return CliError("\n".join([f"the store answered {response.status_code} {head}", *lines]), hint=hint)


class Store:
    """The store API with the token handled: sign in once, renewed here when it is due."""

    def __init__(self, args, ctx: Context) -> None:
        self.ctx = ctx
        self.url = store_url(args, ctx)
        warn_if_unencrypted(ctx, self.url, "the store at")
        self.http = ctx.http(base_url=self.url + "/store/")  # §7: the store lives under <api>/store
        self._token = signin.access_token(ctx, issuer(args, ctx))

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.http.close()

    def call(self, method: str, path: str, **options):
        headers = {**options.pop("headers", {}), "Authorization": f"Bearer {self._token}"}
        try:
            response = self.http.request(method, path.lstrip("/"), headers=headers, **options)
        except Exception as error:  # connection refused, DNS, timeout
            raise CliError(f"cannot reach the store at {self.url}: {error}", hint="Check --store-url / APPEXT_STORE_URL.") from None
        if response.status_code >= 400:
            raise explain(response)
        return response

    def json(self, method: str, path: str, **options):
        response = self.call(method, path, **options)
        return response.json() if response.content else None


# --- Output --------------------------------------------------------------


def _versions(extension: dict) -> str:
    parts = []
    for v in extension.get("versions", []):
        flag = " (restricted scopes)" if v.get("restricted") else ""
        parts.append(f"{v['version']} {v['status']}{flag}")
    return " · ".join(parts) or "-"


def show(ctx: Context, extension: dict) -> None:
    status = "SUSPENDED" if extension.get("suspended") else extension.get("status", "?")
    urls = sorted((extension.get("urls") or {}).items())
    ctx.say(f"{extension['id']}  {extension.get('name', '')}  [{status}]")
    if extension.get("kind") == "link":  # no Keycloak client, no key: the store answers `clientId: null`
        ctx.say("  kind       link (the app opens it in the system browser; no server, key or deployment)")
        for entry in dict.fromkeys(u["entry"] for _, u in urls if u.get("entry")):  # the same address in every environment
            ctx.say(f"  entry      {entry}")
    else:
        ctx.say(f"  client     {extension.get('clientId') or '-'} ({extension.get('clientAuth') or '-'}, key: {'yes' if extension.get('hasKey') else 'no'})")
    ctx.say(f"  live       {extension.get('liveVersion') or '-'}")
    ctx.say(f"  versions   {_versions(extension)}")
    if extension.get("kind") != "link":
        for env, env_urls in urls:
            ctx.say(f"  {env:<10} {env_urls.get('entry', '-')}")


# --- Sign-in -------------------------------------------------------------


def login(args, ctx: Context) -> int:
    realm = issuer(args, ctx)
    warn_if_unencrypted(ctx, realm, "the sign-in at")
    entry = signin.device_login(ctx, realm, args.client_id or ctx.env.get("APPEXT_CLI_CLIENT_ID") or signin.DEFAULT_CLIENT_ID,
                                open_browser=not args.no_browser)
    signin.save_credentials(ctx, realm, entry)
    ctx.say(f"Signed in as {signin.describe(entry)}.")
    if "no store role" in signin.describe(entry):
        ctx.warn("this account has no store role; ask an administrator for store-developer")
    return 0


def logout(args, ctx: Context) -> int:
    gone = signin.forget_credentials(ctx, issuer(args, ctx))
    ctx.say("Signed out." if gone else "You were not signed in.")
    return 0


# --- Developer -----------------------------------------------------------


def _key_path(args, ctx: Context, root: Path) -> Path:
    return ctx.path(args.key) if args.key else root / ".appext" / JWK_FILE


def _upload_key(store: Store, extension: str, jwk_file: Path) -> dict:
    jwk = load_public_jwk(jwk_file)  # refuses anything with a private member before it leaves the machine
    store.call("PUT", f"/extensions/{extension}/key", json={"jwk": jwk})
    return jwk


def register(args, ctx: Context) -> int:
    project = load_project(ctx, args.manifest)  # the rules the store applies, answered before the round trip
    with Store(args, ctx) as store:
        extension = store.json("POST", "/extensions", content=project.text.encode("utf-8"),
                               headers={"Content-Type": "application/toml"})
        if project.is_link:
            ctx.say(f"Link registered: {project.id} {project.version}, draft created. It has no server, no key and no deployment.")
            if args.key:
                ctx.warn("--key is ignored: a link has no key")
        else:
            ctx.say(f"Registered {project.id} {project.version}: draft created.")
            if project.client_auth != "private_key_jwt":
                ctx.say("client_secret extension: the secret is issued with the first auth bundle.")
            else:
                jwk_file = _key_path(args, ctx, project.root)
                if jwk_file.is_file():
                    jwk = _upload_key(store, project.id, jwk_file)
                    ctx.say(f"Public key uploaded (kid {jwk.get('kid') or thumbprint(jwk)}).")
                    extension = store.json("GET", f"/extensions/{project.id}") or extension  # now with the key
                else:
                    ctx.warn(f"no public key at {jwk_file}; run `appext keys generate`, then `appext store key`")
        if extension:
            show(ctx, extension)
    if project.is_link:
        ctx.say("Next:")
        ctx.say("  appext store submit     sends it to review")
        ctx.say("  a reviewer approves it  (appext store approve <id>)")
        ctx.say("  appext store verify     puts it live at once: there is no deployment to check")
    else:
        ctx.say("Next: `appext store submit`.")
    return 0


def key(args, ctx: Context) -> int:
    ident = extension_id(ctx, args.id, args.manifest, not_for_a_link="no key to upload")
    root = manifest_path(ctx, args.manifest).parent if not args.id else ctx.cwd
    jwk_file = _key_path(args, ctx, root)
    with Store(args, ctx) as store:
        jwk = _upload_key(store, ident, jwk_file)
    ctx.say(f"Public key of {ident} uploaded (kid {jwk.get('kid') or thumbprint(jwk)}).")
    return 0


def submit(args, ctx: Context) -> int:
    ident = extension_id(ctx, args.id, args.manifest)
    with Store(args, ctx) as store:
        extension = store.json("POST", f"/extensions/{ident}/submit")
    ctx.say(f"Submitted {ident}.")
    if extension:
        show(ctx, extension)
    return 0


def _unpack(archive: bytes, target: Path) -> list[str]:
    """Extracts a bundle, refusing any member that would land outside `target`."""
    names = []
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise CliError(f"the bundle holds an unsafe path: {member.filename}")
            if member.is_dir():
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bundle.read(member))
            names.append(relative.as_posix())
    return names


def bundle(args, ctx: Context) -> int:
    ident = extension_id(ctx, args.id, args.manifest, not_for_a_link="no auth bundle to download")
    with Store(args, ctx) as store:
        env = args.env or default_environment(store.url)
        response = store.call("GET", f"/extensions/{ident}/auth-bundle", params={"env": env})
    try:
        names = _unpack(response.content, ctx.path(args.out))
    except zipfile.BadZipFile:
        raise CliError("the store did not return a ZIP file") from None
    ctx.say(f"Auth bundle of {ident} for {env} unpacked into {args.out}/:")
    for name in sorted(names):
        ctx.say(f"  {name}")
    ctx.say("It holds no secret; the private key and the session key stay with you.")
    return 0


def verify(args, ctx: Context) -> int:
    ident = extension_id(ctx, args.id, args.manifest)
    with Store(args, ctx) as store:
        env = args.env or default_environment(store.url)
        result = store.json("POST", f"/extensions/{ident}/verify", params={"env": env}) or {}
    for check in result.get("checks", []):
        mark = "ok  " if check.get("ok") else "FAIL"
        ctx.say(f"  {mark} {check.get('name', '?')}: {check.get('message', '')}")
    status = result.get("status", "?")
    ctx.say(f"{ident} in {env}: {status}")
    return 0 if status == "LIVE" else 1


def status(args, ctx: Context) -> int:
    ident = args.id
    if not ident and not args.all:
        try:
            ident = load_project(ctx, args.manifest).id
        except CliError:
            ident = None  # no project here: list instead
    with Store(args, ctx) as store:
        if ident:
            show(ctx, store.json("GET", f"/extensions/{ident}"))
            return 0
        listing = store.json("GET", "/extensions", params={} if args.all else {"mine": "true"}) or []
    for extension in listing:
        show(ctx, extension)
    if not listing:
        ctx.say("No extensions yet. Create one with `appext new`, then `appext store register`.")
    return 0


def services(args, ctx: Context) -> int:
    with Store(args, ctx) as store:
        catalog = store.json("GET", "/services") or []
    for service in catalog:
        ctx.say(f"{service['audience']}  {service.get('title', '')}")
        for scope in service.get("scopes", []):
            flag = "  [restricted: needs a second approval]" if scope.get("restricted") else ""
            ctx.say(f"  {scope['name']:<24} {scope.get('consentText', '')}{flag}")
    return 0


def rotate_secret(args, ctx: Context) -> int:
    ident = extension_id(ctx, args.id, args.manifest, not_for_a_link="no client secret to rotate")
    with Store(args, ctx) as store:
        result = store.json("POST", f"/extensions/{ident}/rotate-secret") or {}
    secret = result.get("clientSecret") or result.get("secret")
    if not secret:
        raise CliError("the store returned no secret")
    write_secret(ctx.path(args.out), secret.encode("utf-8") + b"\n", overwrite=True)
    ctx.say(f"New client secret written to {args.out} (mode 0600). The old one no longer works.")
    return 0


# --- Reviewer and admin --------------------------------------------------


def _act(args, ctx: Context, action: str, body: dict | None, done: str) -> int:
    with Store(args, ctx) as store:
        result = store.json("POST", f"/extensions/{args.id}/{action}", **({"json": body} if body is not None else {}))
    ctx.say(f"{args.id}: {done}.")
    if isinstance(result, dict) and result.get("pendingApprovals"):
        ctx.say(f"  still needs {result['pendingApprovals']} more approval(s) (restricted scopes: a second store-admin)")
    return 0


def approve(args, ctx: Context) -> int:
    return _act(args, ctx, "approve", {"note": args.note} if args.note else None, "approval recorded")


def reject(args, ctx: Context) -> int:
    return _act(args, ctx, "reject", {"reason": args.reason}, "rejected")


def suspend(args, ctx: Context) -> int:
    return _act(args, ctx, "suspend", {"reason": args.reason}, "suspended")


def unsuspend(args, ctx: Context) -> int:
    return _act(args, ctx, "unsuspend", {"reason": args.reason}, "suspension lifted")
