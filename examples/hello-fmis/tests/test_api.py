"""Unit tests without Keycloak and without the FMIS API: `appext.testing` signs a test
person in directly and mocks the target service."""
import pytest

from appext.testing import ExtensionTestClient, service_mocks, test_user

from app.main import app, ext


@pytest.fixture
def client():
    client = ExtensionTestClient(app, user=test_user(sub="u-1", name="Ada", roles=["farmer"]))
    yield client
    client.close()


def test_me_describes_the_signed_in_person(client):
    response = client.get("/api/me")
    assert response.status_code == 200
    assert response.json() == {"sub": "u-1", "name": "Ada", "email": "test.user@example.test", "roles": ["farmer"]}


def test_the_api_needs_a_session():
    anonymous = ExtensionTestClient(app)
    response = anonymous.get("/api/me")
    anonymous.close()
    assert response.status_code == 401
    assert response.json()["login_url"].startswith("/auth/login")


def test_fields_come_from_the_fmis_api(client):
    with service_mocks(ext) as mocks:
        mocks.get("fmis", "/fields").respond(json=[
            {"id": "f-1", "name": "North field", "area": 4.2, "areaUnit": "ha", "farmId": "x"},
            {"id": "f-2", "name": "Meadow", "area": None, "areaUnit": None},
        ])
        response = client.get("/api/fields")
    assert response.json() == [
        {"id": "f-1", "name": "North field", "area": 4.2, "areaUnit": "ha"},
        {"id": "f-2", "name": "Meadow", "area": None, "areaUnit": None},
    ]
    # The token was exchanged for the one service and the scopes the manifest names – nothing more.
    assert client.token_requests == [("fmis", "fmis-api", ("ext-data-read",), "user")]


def test_a_failing_service_is_a_bad_gateway_not_a_crash(client):
    with service_mocks(ext) as mocks:
        mocks.get("fmis", "/fields").respond(status_code=403)
        response = client.get("/api/fields")
    assert response.status_code == 502
