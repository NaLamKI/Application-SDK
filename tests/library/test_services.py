"""ServiceClient: base URL, token handling, one retry after 401."""
from __future__ import annotations

import httpx
import pytest

from appext.services import ServiceClient


def make_client(handler, tokens):
    """A ServiceClient whose HTTP goes to `handler` and whose tokens come from the list `tokens` (one per call)."""
    calls = []

    async def provider(bypass: bool) -> str:
        calls.append(bypass)
        return tokens[min(len(calls) - 1, len(tokens) - 1)]

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ServiceClient("projects", "https://projects.test/api/", provider, http, audience="projects-api"), calls


async def test_requests_go_below_the_base_url_with_a_bearer_token():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client, calls = make_client(handler, ["tok-1"])
    for path in ("/v1/projects", "v1/projects"):
        response = await client.get(path, params={"a": "1"})
        assert response.json() == {"ok": True}
    assert [str(r.url) for r in seen] == ["https://projects.test/api/v1/projects?a=1"] * 2
    assert all(r.headers["authorization"] == "Bearer tok-1" for r in seen)
    assert calls == [False, False]


async def test_all_verbs_and_bodies():
    seen = []

    def handler(request):
        seen.append((request.method, request.content))
        return httpx.Response(204)

    client, _ = make_client(handler, ["t"])
    await client.post("/x", json={"a": 1})
    await client.put("/x", content=b"raw")
    await client.patch("/x", data={"k": "v"})
    await client.delete("/x")
    await client.head("/x")
    assert [m for m, _ in seen] == ["POST", "PUT", "PATCH", "DELETE", "HEAD"]
    assert seen[0][1] == b'{"a":1}' and seen[1][1] == b"raw" and seen[2][1] == b"k=v"


@pytest.mark.parametrize("url", ["https://evil.test/steal", "http://projects.test/api/x", "https://projects.test/other", "https://projects.test.evil.test/api/x"])
async def test_an_absolute_url_elsewhere_is_refused_before_any_token_is_fetched(url):
    def handler(request):
        raise AssertionError("must not be sent")

    client, calls = make_client(handler, ["t"])
    with pytest.raises(ValueError, match="outside the base URL"):
        await client.get(url)
    assert calls == []


async def test_an_absolute_url_below_the_base_is_fine():
    client, _ = make_client(lambda r: httpx.Response(200), ["t"])
    assert (await client.get("https://projects.test/api/v1/x")).status_code == 200


async def test_protocol_relative_paths_stay_below_the_base():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    client, _ = make_client(handler, ["t"])
    await client.get("//evil.test/x")
    assert seen == ["https://projects.test/api/evil.test/x"]


async def test_a_401_is_retried_once_with_a_fresh_token():
    attempts = []

    def handler(request):
        attempts.append(request.headers["authorization"])
        return httpx.Response(401 if request.headers["authorization"] == "Bearer old" else 200, json={})

    client, calls = make_client(handler, ["old", "new"])
    response = await client.get("/x")
    assert response.status_code == 200
    assert attempts == ["Bearer old", "Bearer new"] and calls == [False, True]  # the retry asks to bypass the cache


async def test_a_second_401_is_an_answer_not_a_loop():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(401)

    client, calls = make_client(handler, ["a", "b", "c"])
    response = await client.get("/x")
    assert response.status_code == 401 and len(attempts) == 2 and calls == [False, True]


async def test_other_errors_are_not_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(403)

    client, _ = make_client(handler, ["a"])
    assert (await client.get("/x")).status_code == 403 and len(attempts) == 1


async def test_a_caller_supplied_authorization_header_is_replaced():
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200)

    client, _ = make_client(handler, ["ours"])
    await client.get("/x", headers={"authorization": "Bearer theirs", "X-Other": "1"})
    assert seen == ["Bearer ours"]


async def test_token_errors_propagate_unchanged():
    from appext.core import ConsentRequired

    async def provider(bypass):
        raise ConsentRequired(["svc-x"])

    client = ServiceClient("p", "https://p.test", provider, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    with pytest.raises(ConsentRequired):
        await client.get("/x")


async def test_unconfigured_service_says_which_variable_to_set():
    client = ServiceClient("projects", None, lambda bypass: None, httpx.AsyncClient())
    with pytest.raises(RuntimeError, match="APPEXT_SERVICE_PROJECTS_URL"):
        await client.get("/x")


async def test_async_context_manager_does_not_close_the_shared_client():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    async def provider(bypass):
        return "t"

    async with ServiceClient("p", "https://p.test", provider, http) as client:
        await client.get("/x")
    assert not http.is_closed
