"""Backend of {{name}}: server-rendered pages with Jinja2 and HTMX.

The SDK serves sign-in, the session and the static files; this file is what is
specific to the extension. `ext.pages()` is `ext.router()` for HTML: a visitor
without a session is redirected to the sign-in instead of getting a `401`.
"""
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from appext import Extension, ServiceClient, User

ROOT = Path(__file__).resolve().parent.parent  # the manifest and static/ sit next to app/

ext = Extension.from_manifest(ROOT / "extension.toml")
pages = ext.pages()
templates = Jinja2Templates(directory=ROOT / "app" / "templates")  # autoescaping is on for .html


@pages.get("/")
async def index(request: Request, user: User = Depends(ext.current_user)):
    """The whole page. The list of items is fetched by HTMX once the page is up."""
    return templates.TemplateResponse(request, "index.html", {"title": ext.manifest.name, "user": user})


@pages.get("/items")
async def items(request: Request, service: ServiceClient = Depends(ext.service("{{service}}"))):
    """An HTML fragment: the person's items, read from the `{{service}}` service of the platform.

    `/items` is an example: use a path your service offers. `ext.service("{{service}}")`
    exchanges the session's token for one that is valid only for `{{audience}}` and only
    for the scopes the manifest names – and calls with it. The token never reaches the browser.
    """
    response = await service.get("/items")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"the service answered {response.status_code}")
    return templates.TemplateResponse(request, "_items.html", {"items": response.json()})


app = ext.asgi(static_dir=ROOT / "static")
