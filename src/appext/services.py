"""`ServiceClient`: call another service with the right token, without touching tokens.

An `httpx.AsyncClient` with three differences:

* the **base URL** comes from the configuration (`APPEXT_SERVICE_<NAME>_URL`), and
  a request can only go below it. An absolute URL to another host is refused,
  because this client attaches a bearer token and must not become a way to send
  one to a place the manifest never named;
* the **token** is fetched per request from a provider (a token exchange for
  `mode = "user"`, client credentials for `mode = "service"`, both cached);
* **one retry after `401`** with a fresh token, because a cached token can be
  revoked or expire between the cache check and the service's check. Never more:
  a second `401` is an answer.

Errors from getting the token (`ConsentRequired`, `SessionInvalid`,
`ExchangeFailed`) propagate unchanged; `Extension.asgi()` turns them into the
right HTTP answers (re-login, 401, 502).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

#: Returns a bearer token. `bypass_cache=True` asks for a new one (after a 401).
TokenProvider = Callable[[bool], Awaitable[str]]


class ServiceClient:
    def __init__(
        self,
        name: str,
        base_url: str | None,
        token_provider: TokenProvider,
        http: httpx.AsyncClient,
        *,
        audience: str = "",
        mode: str = "user",
    ) -> None:
        self.name = name
        self.audience = audience
        self.mode = mode
        self.base_url = base_url.rstrip("/") if base_url else None
        self._token = token_provider
        self._http = http

    async def __aenter__(self) -> "ServiceClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None  # the HTTP client is shared and closed with the extension

    def _url(self, url: str) -> str:
        if not self.base_url:
            raise RuntimeError(
                f"the URL of service {self.name!r} is not configured: set APPEXT_SERVICE_{self.name.upper()}_URL"
            )
        if "://" in url:
            if url == self.base_url or url.startswith(self.base_url + "/"):
                return url
            raise ValueError(f"{url!r} is outside the base URL of service {self.name!r}; pass a path")
        return f"{self.base_url}/{url.lstrip('/')}"

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request; `url` is a path below the service's base URL.

        The body must be re-sendable (`json=`, `data=`, `content=` as bytes or str) –
        a one-shot stream could not be repeated after a `401`.
        """
        target = self._url(url)
        headers = dict(kwargs.pop("headers", None) or {})
        for existing in [h for h in headers if h.lower() == "authorization"]:
            del headers[existing]  # the token is ours to set
        response: httpx.Response | None = None
        for attempt in (0, 1):
            token = await self._token(attempt == 1)
            response = await self._http.request(method, target, headers={**headers, "Authorization": f"Bearer {token}"}, **kwargs)
            if response.status_code != 401:
                return response
        assert response is not None
        return response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("HEAD", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)
