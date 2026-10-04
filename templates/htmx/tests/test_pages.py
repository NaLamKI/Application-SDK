"""Unit tests without Keycloak and without the FMIS API: `appext.testing` signs a test
person in directly and mocks the target service."""
import pytest

from appext.testing import ExtensionTestClient, service_mocks, test_user

from app.main import app, ext


@pytest.fixture
def client():
    client = ExtensionTestClient(app, user=test_user(sub="u-1", name="Ada"))
    yield client
    client.close()


def test_the_page_greets_the_signed_in_person(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Signed in as Ada" in response.text
    assert 'hx-get="/fields"' in response.text


def test_a_visitor_without_a_session_is_sent_to_the_sign_in():
    anonymous = ExtensionTestClient(app)
    response = anonymous.get("/", follow_redirects=False)
    anonymous.close()
    assert response.status_code == 302
    assert response.headers["location"].startswith("/auth/login")


def test_the_fields_fragment_comes_from_the_fmis_api(client):
    with service_mocks(ext) as mocks:
        mocks.get("fmis", "/fields").respond(json=[{"id": "f-1", "name": "North <b>field</b>", "area": 4.2, "areaUnit": "ha"}])
        response = client.get("/fields")
    assert response.status_code == 200
    assert "North &lt;b&gt;field&lt;/b&gt; – 4.2 ha" in response.text  # escaped: the data is not ours
    assert client.token_requests == [("fmis", "fmis-api", ("ext-data-read",), "user")]


def test_a_failing_service_is_a_bad_gateway_not_a_crash(client):
    with service_mocks(ext) as mocks:
        mocks.get("fmis", "/fields").respond(status_code=500)
        assert client.get("/fields").status_code == 502


def test_static_files_are_served_without_a_session():
    anonymous = ExtensionTestClient(app)
    script = anonymous.get("/htmx.min.js")
    anonymous.close()
    assert script.status_code == 200 and "Zero-Clause BSD" in script.text[:400]
