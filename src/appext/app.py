"""`Extension`: the manifest, the settings and the core, as a FastAPI application.

    ext = Extension.from_manifest("extension.toml")
    api = ext.router(prefix="/api")          # every route needs a session

    @api.get("/me")
    async def me(user: User = Depends(ext.current_user)): ...

    app = ext.asgi(static_dir="frontend/dist")

`asgi()` assembles what the concept lists: the auth routes, `/_sdk/*`, health,
the developer's routers and the static frontend with a single-page-app
fallback – wrapped in the protective middleware (headers, CSRF, proxy handling).
Before it returns it runs the **lock-file check**: a manifest that asks for more
than the review approved does not start.

Where HTTP shows through:

* `ext.router()` answers a missing session with `401` and a JSON body naming
  the login URL (an API must not redirect: a `fetch` cannot start a login,
  only a top-level navigation can, and `/_sdk/client.js` does that).
* `ext.pages()` and the static HTML redirect to `/auth/login` instead.
* A failed token exchange because of missing consent becomes `401
  consent_required` with a login URL that asks for exactly those scopes.
"""
from __future__ import annotations

import html
import ipaddress
import json
import logging
import mimetypes
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .config import ConfigError, ExtensionSettings
from .core import (
    AuthCore,
    ConsentRequired,
    ExchangeFailed,
    LoginDenied,
    LoginError,
    LogoutTokenError,
    SessionInvalid,
    TokenEndpointError,
    UpstreamError,
    safe_return_to,
)
from .lock import check_startup
from .manifest import Manifest, ManifestError, ServiceSpec, load_manifest
from .services import ServiceClient
from .session import LockTimeout, Session, SessionStore, TokenCipher, User, open_store

log = logging.getLogger("appext")

CSRF_HEADER = "x-appext-csrf"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
#: Prefixes the SDK owns; the developer's routes and the SPA fallback never answer here.
RESERVED_PREFIXES = ("/auth", "/_sdk")

#: (service, session, bypass_cache) -> bearer token. A seam for `appext.testing`.
TokenSource = Callable[[ServiceSpec, "Session | None", bool], Awaitable[str]]


class LoginRequired(Exception):
    """No usable session. Handled by the app: `401` JSON for APIs, a redirect for pages."""

    def __init__(self, *, redirect: bool, clear_cookie: bool = False) -> None:
        self.redirect = redirect
        self.clear_cookie = clear_cookie


def _refuse_link(manifest: Manifest) -> None:
    """A link is an entry in the store, not a program: there is no server to run and nothing to sign in to."""
    if manifest.is_link:
        raise ManifestError([{
            "path": "extension.kind",
            "message": f"{manifest.id!r} is a link: it has no server, so there is no Extension to run "
                       "(a link is published with `appext store register`, see docs/manifest.md)",
        }])


class Extension:
    def __init__(
        self,
        manifest: Manifest,
        settings: ExtensionSettings,
        *,
        store: SessionStore | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        _refuse_link(manifest)
        self.manifest = manifest
        self.settings = settings
        if store is None:
            keys = [bytes(k.reveal()) for k in settings.session_keys]  # type: ignore[arg-type]
            store = open_store(settings.session_store, TokenCipher(keys) if keys else TokenCipher.generate(), clock=clock)
        self.store = store
        self.core = AuthCore(settings, manifest, store, http=http, clock=clock)
        self.token_source: TokenSource = self._exchange_or_credentials
        self._api_routers: list[APIRouter] = []
        self._page_routers: list[APIRouter] = []
        self._service_http: httpx.AsyncClient | None = None

    @classmethod
    def from_manifest(
        cls,
        path: str | os.PathLike[str] = "extension.toml",
        *,
        settings: ExtensionSettings | None = None,
        environ: dict[str, str] | None = None,
        store: SessionStore | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> "Extension":
        """Read the manifest and the `APPEXT_*` environment (see `ExtensionSettings.from_env`)."""
        manifest = load_manifest(Path(path))
        _refuse_link(manifest)  # before the settings: a link has none, and "missing APPEXT_…" would hide the reason
        settings = settings or ExtensionSettings.from_env(manifest, environ)
        return cls(manifest, settings, store=store, http=http, clock=clock)

    # -- what the developer uses ------------------------------------------------------------------

    def router(self, prefix: str = "/api", **kwargs: Any) -> APIRouter:
        """A router whose every route needs a session. No session: `401` with a `login_url`."""
        router = APIRouter(prefix=prefix, dependencies=[Depends(self._require_api_session)], **kwargs)
        self._api_routers.append(router)
        return router

    def pages(self, prefix: str = "", **kwargs: Any) -> APIRouter:
        """Like `router()` for server-rendered pages: no session redirects to the sign-in."""
        router = APIRouter(prefix=prefix, dependencies=[Depends(self._require_page_session)], **kwargs)
        self._page_routers.append(router)
        return router

    async def current_user(self, request: Request) -> User:
        """Dependency: the signed-in person."""
        return (await self._session(request, redirect=False)).user

    def require_role(self, role: str) -> Callable[..., Awaitable[User]]:
        """Dependency factory: the person must have the realm or client role `role` (else `403`)."""

        async def dependency(user: User = Depends(self.current_user)) -> User:
            if not user.has_role(role):
                raise HTTPException(403, detail={"code": "forbidden", "message": f"This needs the role {role!r}."})
            return user

        return dependency

    def service(self, name: str) -> Callable[..., Awaitable[ServiceClient]]:
        """Dependency factory: a `ServiceClient` for a service of the manifest.

        `mode = "user"` exchanges the person's token (needs a session);
        `mode = "service"` uses the extension's own credentials.
        """
        spec = self.manifest.service(name)  # an unknown name fails at definition time, not at the first request

        async def dependency(request: Request) -> ServiceClient:
            session = await self._session(request, redirect=False) if spec.mode == "user" else None
            return self.service_client(spec.name, session)

        return dependency

    def service_client(self, name: str, session: Session | None = None) -> ServiceClient:
        """A client for use outside a request (background work). `mode = "user"` services need the session."""
        spec = self.manifest.service(name)
        if spec.mode == "user" and session is None:
            raise ValueError(f"service {name!r} acts on behalf of a person: pass the session")

        async def token(bypass: bool) -> str:
            return await self.token_source(spec, session, bypass)

        return ServiceClient(
            spec.name, self.settings.services.get(spec.name), token, self.service_http, audience=spec.audience, mode=spec.mode
        )

    @property
    def service_http(self) -> httpx.AsyncClient:
        if self._service_http is None or self._service_http.is_closed:
            self._service_http = httpx.AsyncClient(timeout=self.settings.http_timeout, follow_redirects=False)
        return self._service_http

    async def _exchange_or_credentials(self, spec: ServiceSpec, session: Session | None, bypass: bool) -> str:
        if spec.mode == "user":
            assert session is not None
            token = await self.core.exchange_token(session.id, spec.audience, spec.scopes, bypass_cache=bypass)
        else:
            token = await self.core.service_token(spec.audience, spec.scopes, bypass_cache=bypass)
        return token.token

    async def aclose(self) -> None:
        await self.core.aclose()
        await self.store.aclose()
        if self._service_http is not None:
            await self._service_http.aclose()

    # -- sessions ---------------------------------------------------------------------------------

    async def _session(self, request: Request, *, redirect: bool) -> Session:
        cached = getattr(request.state, "appext_session", None)
        if cached is not None:
            return cached
        session_id = request.cookies.get(self.settings.session_cookie_name)
        if not session_id:
            raise LoginRequired(redirect=redirect)
        try:
            session = await self.core.fresh_session(session_id)
        except SessionInvalid:
            raise LoginRequired(redirect=redirect, clear_cookie=True) from None
        request.state.appext_session = session
        return session

    async def _require_api_session(self, request: Request) -> None:
        await self._session(request, redirect=False)

    async def _require_page_session(self, request: Request) -> None:
        await self._session(request, redirect=True)

    def _set_cookie(self, response: Response, name: str, value: str, max_age: int | None = None) -> None:
        response.set_cookie(
            name, value, max_age=max_age, path="/", secure=self.settings.cookie_secure, httponly=True, samesite="lax"
        )

    def _delete_cookie(self, response: Response, name: str) -> None:
        response.delete_cookie(name, path="/", secure=self.settings.cookie_secure, httponly=True, samesite="lax")

    # -- the application ----------------------------------------------------------------------------

    def asgi(
        self,
        static_dir: str | os.PathLike[str] | None = None,
        *,
        csp: str | None = None,
        public_pages: bool = False,
        check_lock: bool = True,
        **fastapi_kwargs: Any,
    ) -> FastAPI:
        """Build the ASGI application. Call it after all routes were added to the routers.

        `public_pages=True` serves the static HTML without requiring a session
        (the default sends signed-out visitors to the sign-in first).
        `csp` replaces the default Content-Security-Policy. Other keyword arguments go to
        `FastAPI(...)` (OpenAPI pages are off unless you ask: `docs_url="/docs"`); a `lifespan`
        of yours runs inside the SDK's.
        """
        if check_lock:
            lock = check_startup(self.manifest, self.settings)
            log.info("lock file %s", "checked" if lock else "not present (local development)")

        own_lifespan = fastapi_kwargs.pop("lifespan", None)

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            try:
                if own_lifespan is None:
                    yield
                else:  # the developer's start-up and shutdown run inside ours
                    async with own_lifespan(app):
                        yield
            finally:
                await self.aclose()

        fastapi_kwargs.setdefault("docs_url", None)
        fastapi_kwargs.setdefault("redoc_url", None)
        fastapi_kwargs.setdefault("openapi_url", None)
        app = FastAPI(title=self.manifest.name, version=self.manifest.version, lifespan=lifespan, **fastapi_kwargs)
        app.state.appext = self

        self._add_error_handlers(app)
        self._add_auth_routes(app)
        self._add_sdk_routes(app)
        for router in (*self._api_routers, *self._page_routers):
            app.include_router(router)
        if static_dir is not None:
            self._add_static(app, self._resolve_static(static_dir), public_pages)

        # add_middleware wraps: the last one added is the outermost.
        app.add_middleware(
            CSRFMiddleware,
            origin=self.settings.origin,
            api_prefixes=[r.prefix for r in self._api_routers],
            page_prefixes=[r.prefix for r in self._page_routers],
        )
        app.add_middleware(
            SecurityHeadersMiddleware,
            csp=csp or default_csp(self.manifest, self.frame_ancestors),
            # `X-Frame-Options` cannot name an origin: with a web app allowed to embed us, `frame-ancestors` alone speaks.
            frame_options=self.frame_ancestors == "'none'",
            hsts=self.settings.public_url.startswith("https://"),
            private_prefixes=["/api", "/auth", "/readyz", *(r.prefix for r in (*self._api_routers, *self._page_routers))],
        )
        app.add_middleware(ProxyHeadersMiddleware, trusted=self.settings.trusted_proxies)
        return app

    def _resolve_static(self, static_dir: str | os.PathLike[str]) -> Path:
        path = Path(static_dir)
        if not path.is_absolute() and self.manifest.base_dir is not None:
            path = self.manifest.base_dir / path
        if not path.is_dir():
            log.warning("static directory %s does not exist yet (frontend not built?)", path)
        return path

    # -- errors -------------------------------------------------------------------------------------

    def _login_url(self, return_to: str, scopes: Iterable[str] = ()) -> str:
        query = {"return_to": return_to}
        scopes = list(scopes)
        if scopes:
            query["scope"] = " ".join(scopes)
        return "/auth/login?" + urlencode(query)

    def _api_return_to(self, request: Request) -> str:
        """Where to come back to after the sign-in of an API call: the page that made it, if it said so."""
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            if f"{parts.scheme}://{parts.netloc}" == self.settings.origin:
                return safe_return_to(parts.path + (f"?{parts.query}" if parts.query else ""))
        return "/"

    def _add_error_handlers(self, app: FastAPI) -> None:
        def no_store(response: Response) -> Response:
            response.headers["Cache-Control"] = "no-store"
            return response

        @app.exception_handler(LoginRequired)
        async def login_required(request: Request, exc: LoginRequired) -> Response:
            if exc.redirect:
                target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                response: Response = RedirectResponse(self._login_url(safe_return_to(target)), status_code=302)
            else:
                response = JSONResponse(
                    {"error": "unauthenticated", "login_url": self._login_url(self._api_return_to(request))}, status_code=401
                )
            if exc.clear_cookie:
                self._delete_cookie(response, self.settings.session_cookie_name)
            return no_store(response)

        @app.exception_handler(SessionInvalid)
        async def session_invalid(request: Request, exc: SessionInvalid) -> Response:
            return await login_required(request, LoginRequired(redirect=False, clear_cookie=True))

        @app.exception_handler(ConsentRequired)
        async def consent_required(request: Request, exc: ConsentRequired) -> Response:
            url = self._login_url(self._api_return_to(request), exc.missing_scopes)
            return no_store(
                JSONResponse(
                    {"error": "consent_required", "missing_scopes": list(exc.missing_scopes), "login_url": url}, status_code=401
                )
            )

        @app.exception_handler(ExchangeFailed)
        @app.exception_handler(UpstreamError)
        @app.exception_handler(TokenEndpointError)
        async def upstream(request: Request, exc: Exception) -> Response:
            # The details (error codes) are logged, not shown: the browser cannot act on them.
            log.warning("upstream authentication problem: %s", exc)
            return no_store(
                JSONResponse(
                    {"error": "upstream_auth_failed", "message": "A service could not be reached with your sign-in."}, status_code=502
                )
            )

        @app.exception_handler(ConfigError)
        async def misconfigured(request: Request, exc: ConfigError) -> Response:
            # The operator needs the details (in the log); the browser does not.
            log.error("%s", exc)
            return no_store(JSONResponse({"error": "misconfigured", "message": "The extension is not configured correctly."}, status_code=500))

        @app.exception_handler(LockTimeout)
        async def busy(request: Request, exc: LockTimeout) -> Response:
            return no_store(JSONResponse({"error": "busy", "message": "Please try again."}, status_code=503))

    # -- /auth/* ------------------------------------------------------------------------------------

    @property
    def frame_ancestors(self) -> str:
        """Who may embed the extension: the web app, for an extension the app shows itself.

        One that the app opens in the system browser (`display = "external"`) is never framed –
        so it forbids framing altogether, even where `APPEXT_APP_ORIGINS` names a web app (the
        operator sets that for every extension alike, and the web app is only meant to be allowed
        where it frames).
        """
        return "'none'" if self.manifest.external else self.settings.frame_ancestors

    def _add_auth_routes(self, app: FastAPI) -> None:
        settings = self.settings
        frame_ancestors = self.frame_ancestors
        tx_cookie = settings.transaction_cookie_name

        def redirect(url: str, status: int = 302) -> RedirectResponse:
            response = RedirectResponse(url, status_code=status)
            response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/auth/login", include_in_schema=False)
        async def login(request: Request, return_to: str | None = None, prompt: str | None = None, scope: str | None = None) -> Response:
            """Start the sign-in. A top-level navigation (never a fetch), so the app can intercept it."""
            try:
                start = await self.core.start_login(
                    user_agent=request.headers.get("user-agent"),
                    return_to=return_to,
                    prompt=prompt if prompt in ("login", "consent") else None,
                    extra_scopes=(scope or "").split(),
                )
            except UpstreamError as err:
                log.warning("cannot start a login: %s", err)
                return message_page("Sign-in unavailable", "The identity provider cannot be reached right now. Please try again in a moment.", 503, frame_ancestors=frame_ancestors)
            response = redirect(start.url)
            # The transaction cookie ties `state` to exactly this browser/WebView: it closes login CSRF.
            self._set_cookie(response, tx_cookie, start.state, max_age=600)
            return response

        @app.get("/auth/callback", include_in_schema=False)
        async def callback(request: Request) -> Response:
            params = {k: v for k, v in request.query_params.items()}
            old_session = request.cookies.get(settings.session_cookie_name)
            try:
                result = await self.core.handle_callback(
                    params,
                    transaction_cookie=request.cookies.get(tx_cookie),
                    app_sub=request.cookies.get("app_sub"),
                )
            except LoginDenied as err:
                if err.error == "access_denied":
                    # Keycloak says this for refused consent and for a closed sheet alike.
                    text = "The extension was not given access: you declined, or the sign-in window was closed."
                else:
                    text = f"The identity provider reported an error ({err.error})."
                response = message_page("Not signed in", text, err.status, retry=True, frame_ancestors=frame_ancestors)
                self._delete_cookie(response, tx_cookie)
                return response
            except LoginError as err:
                response = message_page("Sign-in failed", err.message, err.status, retry=True, frame_ancestors=frame_ancestors)
                self._delete_cookie(response, tx_cookie)
                return response
            except TokenEndpointError as err:
                # Typically a code that was used twice or expired: starting over is the cure.
                log.info("the identity provider refused the code: %s", err.error)
                response = message_page("Sign-in failed", f"The identity provider did not accept this sign-in ({err.error}).", 400, retry=True, frame_ancestors=frame_ancestors)
                self._delete_cookie(response, tx_cookie)
                return response
            except UpstreamError as err:
                log.warning("callback failed: %s", err)
                return message_page("Sign-in unavailable", "The identity provider could not be reached. Please try again.", 502, retry=True, frame_ancestors=frame_ancestors)

            if result.restart is not None:
                response = redirect(result.restart.url)
                self._set_cookie(response, tx_cookie, result.restart.state, max_age=600)
                return response
            if old_session:
                await self.store.delete_session(old_session)  # a new login never inherits a session id
            response = redirect(result.return_to)
            self._set_cookie(response, settings.session_cookie_name, result.session_id or "", max_age=int(result.ttl))
            self._delete_cookie(response, tx_cookie)
            return response

        @app.api_route("/auth/logout", methods=["GET", "POST"], include_in_schema=False)
        async def logout(request: Request, return_to: str | None = None, sso: bool = False) -> Response:
            """End this extension's session. `sso=true` also ends the sign-in at the identity provider.

            By default the single sign-on session stays: closing one extension must not sign the person out of the app.
            """
            session_id = request.cookies.get(settings.session_cookie_name)
            session = await self.store.load_session(session_id) if session_id else None
            if session_id:
                await self.store.delete_session(session_id)
            target = redirect(safe_return_to(return_to))
            if sso and session is not None:
                end = await self.core.end_session_url(session)
                if end:
                    target = redirect(end)
            self._delete_cookie(target, settings.session_cookie_name)
            return target

        @app.post("/auth/backchannel-logout", include_in_schema=False)
        async def backchannel_logout(request: Request) -> Response:
            """Keycloak tells us a session ended (OIDC Back-Channel Logout 1.0)."""
            headers = {"Cache-Control": "no-store"}
            if int(request.headers.get("content-length") or 0) > 65536:
                return JSONResponse({"error": "invalid_request"}, status_code=413, headers=headers)
            form = parse_qs((await request.body()).decode("utf-8", "replace"))
            token = (form.get("logout_token") or [""])[0]
            if not token:
                return JSONResponse({"error": "invalid_request", "error_description": "logout_token is missing"}, status_code=400, headers=headers)
            try:
                ended = await self.core.handle_backchannel_logout(token)
            except LogoutTokenError as err:
                return JSONResponse({"error": "invalid_request", "error_description": str(err)}, status_code=400, headers=headers)
            log.info("back-channel logout ended %d session(s)", ended)
            return Response(status_code=200, headers=headers)

    # -- /_sdk/*, health ---------------------------------------------------------------------------

    def _add_sdk_routes(self, app: FastAPI) -> None:
        manifest, settings = self.manifest, self.settings

        @app.get("/_sdk/info", include_in_schema=False)
        async def info() -> dict[str, str]:
            return {
                "sdk": "appext",
                "sdkVersion": __version__,
                "id": manifest.id,
                "version": manifest.version,
                "clientId": settings.client_id,
                "environment": settings.env,
            }

        @app.get("/_sdk/icon", include_in_schema=False)
        async def icon() -> Response:
            path = self._icon_path()
            if path is None:
                raise HTTPException(404, detail={"code": "not_found", "message": "No icon."})
            media_type = "image/svg+xml" if path.suffix.lower() == ".svg" else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            # Public and without credentials: the app on the web (another origin) draws it as a picture.
            headers = {"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"}
            if media_type == "image/svg+xml":
                # An SVG can carry script; as an image it must never run any.
                headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
            return FileResponse(path, media_type=media_type, headers=headers)

        for name in ("client.js", "bridge.js"):
            source = resources.files("appext").joinpath("js", name).read_text(encoding="utf-8")
            if name == "bridge.js":
                source = self._bridge_config_line() + source

            def make(body: str):
                async def serve() -> Response:
                    return Response(body, media_type="text/javascript; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})

                return serve

            app.add_api_route(f"/_sdk/{name}", make(source), methods=["GET"], include_in_schema=False)

        @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @app.api_route("/readyz", methods=["GET", "HEAD"], include_in_schema=False)
        async def readyz() -> Response:
            checks: dict[str, str] = {}
            try:
                await self.core.discovery(fresh=True)
                checks["identity_provider"] = "ok"
            except Exception as err:  # noqa: BLE001 - any failure means "not ready"
                checks["identity_provider"] = f"unavailable ({type(err).__name__})"
            try:
                await self.store.ping()
                checks["session_store"] = "ok"
            except Exception as err:  # noqa: BLE001
                checks["session_store"] = f"unavailable ({type(err).__name__})"
            ready = all(v == "ok" for v in checks.values())
            return JSONResponse(
                {"status": "ready" if ready else "unavailable", "checks": checks},
                status_code=200 if ready else 503,
                headers={"Cache-Control": "no-store"},
            )

    def _icon_path(self) -> Path | None:
        base = self.manifest.base_dir
        if base is None:
            return None
        path = (base / self.manifest.icon).resolve()
        # The icon is a file of the project; a manifest must not make us serve any other file.
        return path if path.is_file() and path.is_relative_to(base.resolve()) else None

    # -- static frontend ------------------------------------------------------------------------------

    def _add_static(self, app: FastAPI, root: Path, public_pages: bool) -> None:
        """Serve the frontend for every path no route claimed.

        Installed as the router's *default* (what answers when nothing matches)
        rather than as a catch-all route: a route the developer adds to the app
        afterwards (`@app.get("/health")`) must not be shadowed by it.
        """

        async def static(request: Request) -> Response:
            path = request.url.path
            reserved = (*RESERVED_PREFIXES, *self._reserved_api_prefixes())
            if request.method not in ("GET", "HEAD") or any(path == p or path.startswith(p + "/") for p in reserved):
                raise HTTPException(404, detail={"code": "not_found", "message": "Not found."})
            file = _locate(root, path.lstrip("/"))
            if file is None:
                raise HTTPException(404, detail={"code": "not_found", "message": "Not found."})
            is_html = file.suffix.lower() in (".html", ".htm")
            if is_html and not public_pages:
                try:
                    await self._session(request, redirect=True)
                except LoginRequired as exc:
                    target = path + (f"?{request.url.query}" if request.url.query else "")
                    response = RedirectResponse(self._login_url(safe_return_to(target)), status_code=302)
                    if exc.clear_cookie:
                        self._delete_cookie(response, self.settings.session_cookie_name)
                    response.headers["Cache-Control"] = "no-store"
                    return response
            cache = "no-cache" if is_html else "public, max-age=3600"
            if is_html:
                return HTMLResponse(inject_bridge(file.read_text(encoding="utf-8")), headers={"Cache-Control": cache})
            return FileResponse(file, headers={"Cache-Control": cache})

        not_found = app.router.default

        async def default(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await not_found(scope, receive, send)
                return
            request = Request(scope, receive, send)
            response = await static(request)
            await response(scope, receive, send)

        app.router.default = default

    def _bridge_config_line(self) -> str:
        """What `bridge.js` must know and cannot ask: where the web app lives, and what to call itself.

        One JSON line in front of the script (never inlined into a page, so the CSP stays
        `default-src 'self'`). `json.dumps` escapes `<`; the script is not HTML in any case.
        """
        manifest = self.manifest
        config = {
            "appOrigins": list(self.settings.app_origins),
            "appName": self.settings.app_name,
            "appMarker": self.settings.app_marker,
            "backLabels": dict(self.settings.app_back_labels),
            "appAccent": self.settings.app_accent,
            "name": manifest.name,
            "nameLocalized": dict(manifest.name_localized),
        }
        return f"window.__APPEXT_CONFIG__ = {json.dumps(config, ensure_ascii=False)};\n"

    def _reserved_api_prefixes(self) -> list[str]:
        return ["/api", *(r.prefix for r in self._api_routers if r.prefix)]


BRIDGE_TAG = '<script src="/_sdk/bridge.js"></script>'


def inject_bridge(page: str) -> str:
    """Load `/_sdk/bridge.js` on every page the SDK serves – the author need not remember.

    It is what tells the web app that the page is up (`ready`), carries theme and language into
    it, and – outside the app – draws the bar that leads back. Loaded twice it does nothing the
    second time (`window.AppExt` guards), so an author who included it themselves loses nothing.
    """
    if "/_sdk/bridge.js" in page:
        return page
    match = re.search(r"</head\s*>", page, flags=re.IGNORECASE)
    if match is None:
        return BRIDGE_TAG + page
    return page[: match.start()] + BRIDGE_TAG + page[match.start():]


def _locate(root: Path, path: str) -> Path | None:
    """The file for a URL path: the file itself, a directory's index, or the SPA's `index.html` for app routes."""
    if any(part.startswith(".") and part != ".well-known" for part in path.split("/") if part):
        return None  # .git, .env and friends never belong to what a visitor may fetch, whatever lies in the directory
    try:
        base = root.resolve()
        candidate = (base / path).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(base):
        return None  # `..` and symlinks leading out of the directory
    if candidate.is_file():
        return candidate
    if candidate.is_dir() and (candidate / "index.html").is_file():
        return candidate / "index.html"
    last = path.rsplit("/", 1)[-1]
    if "." not in last and (base / "index.html").is_file():
        return base / "index.html"  # a client-side route: the SPA decides what to show
    return None


# --- pages ---------------------------------------------------------------------------------------------


def message_page(title: str, text: str, status: int, *, retry: bool = False, frame_ancestors: str = "'none'") -> HTMLResponse:
    """A small, self-contained page for sign-in problems (no scripts, no external files)."""
    retry_link = '<p><a href="/auth/login">Try again</a></p>' if retry else ""
    body = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1.25rem}"
        "a{color:inherit}</style>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>{retry_link}"
    )
    response = HTMLResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    # Own policy: this page carries a style element, which the default policy (default-src 'self') would block.
    # Shown inside the web app's frame as well – a blank frame would hide what went wrong.
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors {frame_ancestors}"
    )
    return response


# --- middleware ----------------------------------------------------------------------------------------


def default_csp(manifest: Manifest, frame_ancestors: str = "'none'") -> str:
    """`default-src 'self'` and no inline scripts; images may also be `data:` URIs (bundlers inline small ones).

    Only the host app's web app may embed the extension (`frame_ancestors`, from `APPEXT_APP_ORIGINS`);
    without one, nothing may.
    """
    hosts = "".join(f" https://{h}" for h in manifest.hosts)
    return (
        "default-src 'self'; "
        f"img-src 'self' data:{hosts}; font-src 'self'{hosts}; "
        f"base-uri 'self'; object-src 'none'; frame-ancestors {frame_ancestors}"
    )


class SecurityHeadersMiddleware:
    """Standard response headers, set only where the application did not set its own."""

    def __init__(self, app: ASGIApp, *, csp: str, hsts: bool, private_prefixes: list[str], frame_options: bool = True) -> None:
        self.app = app
        self.private_prefixes = private_prefixes
        self.defaults = {
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
            # `same-origin`, not `no-referrer`: the SDK's own client uses the Referer to find the page to return to.
            "Referrer-Policy": "same-origin",
        }
        if frame_options:
            self.defaults["X-Frame-Options"] = "DENY"
        if hsts:
            self.defaults["Strict-Transport-Security"] = "max-age=31536000"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        # Answers that depend on who is asking must never be stored by a cache.
        private = any(p == "" or path == p or path.startswith(p + "/") for p in self.private_prefixes)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.defaults.items():
                    if name not in headers:
                        headers[name] = value
                if private and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class CSRFMiddleware:
    """Cross-site request forgery protection for writing requests.

    Cookies are `SameSite=Lax`, which already keeps cross-site POSTs from
    carrying the session. This is the second layer, aimed at the attack that
    matters with a subdomain per extension – a *sibling* site, which is
    same-site:

    * `/api` routes: the custom header `X-Appext-CSRF` is required (a cross-origin
      page cannot set it without a CORS preflight, which we never grant), and an
      `Origin` header, when present, must be our own origin.
    * `pages()` routes (plain HTML forms cannot set headers): `Origin` must be ours,
      or absent with `Sec-Fetch-Site` saying same-origin.

    `/auth/*` and `/_sdk/*` are exempt (the back-channel logout is server-to-server).
    """

    def __init__(self, app: ASGIApp, *, origin: str, api_prefixes: list[str], page_prefixes: list[str]) -> None:
        self.app = app
        self.origin = origin
        # `/api` is always protected, also for routes the developer added by hand; "" means "everything".
        self.api_prefixes = sorted({"/api", *api_prefixes})
        self.page_prefixes = page_prefixes

    @staticmethod
    def _under(path: str, prefix: str) -> bool:
        return prefix == "" or path == prefix or path.startswith(prefix + "/")

    def _kind(self, path: str) -> str | None:
        if any(path == p or path.startswith(p + "/") for p in RESERVED_PREFIXES):
            return None
        if any(self._under(path, p) for p in self.api_prefixes):
            return "api"
        if any(self._under(path, p) for p in self.page_prefixes):
            return "page"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        kind = self._kind(scope.get("path", ""))
        if kind is None:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        problem = self._problem(kind, headers)
        if problem:
            response = JSONResponse({"detail": {"code": "csrf_rejected", "message": problem}}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _problem(self, kind: str, headers: dict[str, str]) -> str | None:
        origin = headers.get("origin")
        if origin is not None and origin != self.origin:
            return "The request comes from another origin."
        if kind == "api":
            if headers.get(CSRF_HEADER) is None:
                return "The X-Appext-CSRF header is required on writing requests (use extFetch from /_sdk/client.js)."
            site = headers.get("sec-fetch-site")
            if origin is None and site not in (None, "same-origin", "none"):
                return "Cross-site request."
            return None
        if origin is None and headers.get("sec-fetch-site") not in ("same-origin", "none"):
            return "Cannot tell where this request comes from."
        return None


class ProxyHeadersMiddleware:
    """Honour `X-Forwarded-For` / `-Proto` – but only from the configured proxies.

    Anyone can send those headers; believing them from an arbitrary peer would
    let a client choose the address that shows up in logs. Nothing in the SDK's
    security decisions depends on them (cookie flags follow the configured
    public URL, the CSRF origin is configured as well), so this only affects
    what `request.client` and `request.url.scheme` report.
    """

    def __init__(self, app: ASGIApp, *, trusted: tuple[str, ...]) -> None:
        self.app = app
        self.trust_all = "*" in trusted
        self.networks = [ipaddress.ip_network(t, strict=False) for t in trusted if t != "*"]

    def _is_trusted(self, host: str | None) -> bool:
        if self.trust_all:
            return True
        try:
            address = ipaddress.ip_address(host or "")
        except ValueError:
            return False
        return any(address in n for n in self.networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and (self.trust_all or self.networks):
            client = scope.get("client")
            if client and self._is_trusted(client[0]):
                headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
                proto = headers.get("x-forwarded-proto", "").split(",")[0].strip()
                if proto in ("http", "https"):
                    scope["scheme"] = proto if scope["type"] == "http" else {"http": "ws", "https": "wss"}[proto]
                forwarded = [h.strip() for h in headers.get("x-forwarded-for", "").split(",") if h.strip()]
                if self.trust_all and forwarded:
                    scope["client"] = (forwarded[0], 0)  # "trust everyone": the left-most entry, as uvicorn does
                else:
                    # Walk from the right: the first address that is not one of our proxies is the client.
                    for host in reversed(forwarded):
                        if not self._is_trusted(host):
                            scope["client"] = (host, 0)
                            break
        await self.app(scope, receive, send)
