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


@api.get("/fields")
async def fields(fmis: ServiceClient = Depends(ext.service("fmis"))):
    """The person's fields, read from the FMIS API.

    `ext.service("fmis")` exchanges the session's token for one that is valid
    only for `fmis-api` and only for the scopes the manifest names – and calls
    with it. The token never reaches the browser.
    """
    response = await fmis.get("/fields")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"the FMIS API answered {response.status_code}")
    return [
        {"id": f["id"], "name": f.get("name"), "area": f.get("area"), "areaUnit": f.get("areaUnit")}
        for f in response.json()
    ]


app = ext.asgi(static_dir=ROOT / "frontend" / "dist")
