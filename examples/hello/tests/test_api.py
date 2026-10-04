"""Unit tests without an OAuth service and without the platform's services: `appext.testing` signs a
test person in directly and mocks the target service."""
import pytest

from appext.testing import ExtensionTestClient, service_mocks, test_user

from app.main import app, ext


@pytest.fixture
def client():
    client = ExtensionTestClient(app, user=test_user(sub="u-1", name="Ada", roles=["member"]))
    yield client
    client.close()


def test_me_describes_the_signed_in_person(client):
    response = client.get("/api/me")
    assert response.status_code == 200
    assert response.json() == {"sub": "u-1", "name": "Ada", "email": "test.user@example.test", "roles": ["member"]}


def test_the_api_needs_a_session():
    anonymous = ExtensionTestClient(app)
    response = anonymous.get("/api/me")
    anonymous.close()
    assert response.status_code == 401
    assert response.json()["login_url"].startswith("/auth/login")


def test_items_come_from_the_service(client):
    with service_mocks(ext) as mocks:
        mocks.get("data", "/items").respond(json=[
            {"id": "i-1", "name": "First item", "owner": "x"},
            {"id": "i-2"},
        ])
        response = client.get("/api/items")
    # Only what the backend picks is passed on to the browser.
    assert response.json() == [{"id": "i-1", "name": "First item"}, {"id": "i-2", "name": None}]
    # The token was exchanged for the one service and the scopes the manifest names – nothing more.
    assert client.token_requests == [("data", "data-api", ("data-read",), "user")]


def test_a_failing_service_is_a_bad_gateway_not_a_crash(client):
    with service_mocks(ext) as mocks:
        mocks.get("data", "/items").respond(status_code=403)
        response = client.get("/api/items")
    assert response.status_code == 502
