"""Shared fixtures for the library tests: a project on disk, a fake IdP, an extension wired to it."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import Depends

from appext import Extension, ServiceClient, User
from appext.testing import FakeClock, FakeIdP, generate_client_key

MANIFEST = """
[extension]
id = "demo"
name = "Demo"
description = "A demo extension"
version = "1.2.0"
entry = "/"
icon = "icon.svg"
hosts = ["cdn.example.test"]

[extension.name_localized]
de = "Demo"

[consent]
scopes = ["ext-data-read"]

[[services]]
name = "projects"
audience = "projects-api"
scopes = ["svc-projects-read"]
mode = "user"

[[services]]
name = "export"
audience = "export-api"
scopes = ["svc-export-write"]
mode = "service"
"""

PUBLIC_URL = "https://demo.apps.test"
APP_REDIRECT = "org.agrifooddata.apps.fmis.web:/callback"
ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'


@dataclass
class Env:
    """Everything a test needs: the project dir, the IdP, the extension and its ASGI app."""

    root: Path
    idp: FakeIdP
    clock: FakeClock
    ext: Extension
    app: object
    environ: dict[str, str]
    private_key_pem: str
    public_jwk: dict

    @property
    def public_url(self) -> str:
        return PUBLIC_URL


def write_project(root: Path, *, display: str = "in_app") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = MANIFEST if display == "in_app" else MANIFEST.replace('entry = "/"', f'entry = "/"\ndisplay = "{display}"', 1)
    assert manifest != MANIFEST or display == "in_app", "the manifest of the tests has no entry line to put `display` after"
    (root / "extension.toml").write_text(manifest)
    (root / "icon.svg").write_text(ICON)
    dist = root / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>Demo</title><div id=app></div>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    return root / "extension.toml"


def add_routes(ext: Extension) -> None:
    """The routes a developer would write; the library tests exercise them through the real app."""
    api = ext.router(prefix="/api")

    @api.get("/me")
    async def me(user: User = Depends(ext.current_user)):
        return {"sub": user.sub, "name": user.name, "email": user.email, "roles": sorted(user.roles), "scopes": list(user.scopes)}

    @api.get("/projects")
    async def projects(client: ServiceClient = Depends(ext.service("projects"))):
        response = await client.get("/v1/projects", params={"limit": "5"})
        return response.json()

    @api.post("/export")
    async def export(user: User = Depends(ext.require_role("analyst")), client: ServiceClient = Depends(ext.service("export"))):
        response = await client.post("/v1/jobs", json={"requested_by": user.sub})
        return response.json()

    @api.get("/admin")
    async def admin(user: User = Depends(ext.require_role("admin"))):
        return {"ok": True}

    @api.post("/echo")
    async def echo():
        return {"ok": True}

    pages = ext.pages(prefix="/pages")

    @pages.get("/hello")
    async def hello(user: User = Depends(ext.current_user)):
        return {"hello": user.sub}

    @pages.post("/form")
    async def form():
        return {"posted": True}


def build_env(
    tmp_path: Path,
    *,
    extra_environ: dict[str, str] | None = None,
    client_auth: str = "private_key_jwt",
    public_pages: bool = False,
    store_factory=None,
    display: str = "in_app",
    **idp_options,
) -> Env:
    clock = FakeClock()
    idp = FakeIdP("https://idp.test/realms/test", clock=clock, **idp_options)
    manifest_path = write_project(tmp_path / "project", display=display)
    pem, jwk = generate_client_key("RS256")
    key_file = tmp_path / "client_key.pem"
    key_file.write_text(pem)
    environ = {
        "APPEXT_ENV": "local",
        "APPEXT_ISSUER": idp.issuer,
        "APPEXT_PUBLIC_URL": PUBLIC_URL,
        "APPEXT_CLIENT_KEY_FILE": str(key_file),
        "APPEXT_SERVICE_PROJECTS_URL": "https://projects.test/api",
        "APPEXT_SERVICE_EXPORT_URL": "https://export.test",
        **(extra_environ or {}),
    }
    if client_auth == "client_secret":
        secret_file = tmp_path / "client_secret"
        secret_file.write_text("s3cret-value")
        environ["APPEXT_CLIENT_AUTH"] = "client_secret"
        environ["APPEXT_CLIENT_SECRET_FILE"] = str(secret_file)
    idp.add_client(
        "ext-demo",
        redirect_uris={f"{PUBLIC_URL}/auth/callback", APP_REDIRECT},
        public_jwk=jwk if client_auth == "private_key_jwt" else None,
        secret="s3cret-value" if client_auth == "client_secret" else None,
        default_scopes={"ext-data-read"},
        optional_scopes={"svc-projects-read", "svc-export-write", "profile", "email"},
        scope_audiences={"ext-data-read": "fmis-api", "svc-projects-read": "projects-api", "svc-export-write": "export-api"},
        service_account=True,
        backchannel_logout_url=f"{PUBLIC_URL}/auth/backchannel-logout",
        post_logout_redirect_uris={f"{PUBLIC_URL}/"},
    )
    idp.add_user("u-1", name="Una Example", email="una@example.test", realm_roles=["analyst"])
    idp.add_user("u-2", name="Zed Other")
    store = store_factory(clock) if store_factory else None
    ext = Extension.from_manifest(manifest_path, environ=environ, http=idp.client(), clock=clock, store=store)
    add_routes(ext)
    app = ext.asgi(static_dir="frontend/dist", public_pages=public_pages)
    idp.backchannel_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL)
    return Env(tmp_path, idp, clock, ext, app, environ, pem, jwk)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return build_env(tmp_path)


class Browser:
    """A cookie-keeping client for the extension plus the identity provider's side of a sign-in."""

    def __init__(self, env: Env, *, user_agent: str = "Mozilla/5.0 (test)") -> None:
        self.env = env
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app),
            base_url=PUBLIC_URL,
            headers={"user-agent": user_agent},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def set_cookie(self, name: str, value: str) -> None:
        self.http.cookies.set(name, value, domain="demo.apps.test")

    def web_url(self, callback: str) -> str:
        """What the app does with the callback URL it got from the system browser: load it in the WebView."""
        if callback.startswith(APP_REDIRECT):
            return f"{PUBLIC_URL}/auth/callback" + callback[len(APP_REDIRECT):]
        return callback

    async def start(self, **params: str) -> httpx.Response:
        return await self.http.get("/auth/login", params=params)

    async def sign_in(self, user: str = "u-1", *, consent: bool = True, **params: str) -> httpx.Response:
        """Login, authorize at the IdP, load the callback; returns the callback's response."""
        started = await self.start(**params)
        assert started.status_code == 302, started.text
        callback = self.env.idp.authorize(started.headers["location"], user, consent=consent)
        return await self.http.get(self.web_url(callback))


@pytest.fixture
async def browser(env: Env):
    b = Browser(env)
    yield b
    await b.aclose()


def query_of(url: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlsplit

    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def json_of(response: httpx.Response):
    return json.loads(response.text)
