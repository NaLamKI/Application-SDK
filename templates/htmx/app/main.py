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
    """The whole page. The list of fields is fetched by HTMX once the page is up."""
    return templates.TemplateResponse(request, "index.html", {"title": ext.manifest.name, "user": user})


@pages.get("/fields")
async def fields(request: Request, fmis: ServiceClient = Depends(ext.service("fmis"))):
    """An HTML fragment: the person's fields, read from the FMIS API.

    `ext.service("fmis")` exchanges the session's token for one that is valid
    only for `fmis-api` and only for the scopes the manifest names – and calls
    with it. The token never reaches the browser.
    """
    response = await fmis.get("/fields")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"the FMIS API answered {response.status_code}")
    return templates.TemplateResponse(request, "_fields.html", {"fields": response.json()})


app = ext.asgi(static_dir=ROOT / "static")
