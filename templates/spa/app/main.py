"""Backend of {{name}}.

The SDK serves sign-in, the session and the built frontend; this file is what
is specific to the extension. Every route on `api` requires a signed-in person –
without a session the SDK answers `401` with a `login_url`, which `extFetch`
(frontend/src/main.js) follows.
"""
from pathlib import Path

from fastapi import Depends, HTTPException

from appext import Extension, ServiceClient, User

ROOT = Path(__file__).resolve().parent.parent  # the manifest and the build sit next to app/

ext = Extension.from_manifest(ROOT / "extension.toml")
api = ext.router(prefix="/api")


@api.get("/me")
async def me(user: User = Depends(ext.current_user)):
    """Who is signed in – from the session, no call to anyone."""
    return {"sub": user.sub, "name": user.name, "email": user.email, "roles": sorted(user.roles)}


@api.get("/items")
async def items(service: ServiceClient = Depends(ext.service("{{service}}"))):
    """The person's items, read from the `{{service}}` service of the platform.

    `/items` is an example: use a path your service offers. `ext.service("{{service}}")`
    exchanges the session's token for one that is valid only for `{{audience}}` and only
    for the scopes the manifest names – and calls with it. The token never reaches the browser.
    """
    response = await service.get("/items")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"the service answered {response.status_code}")
    return [{"id": item["id"], "name": item.get("name")} for item in response.json()]


app = ext.asgi(static_dir=ROOT / "frontend" / "dist")
