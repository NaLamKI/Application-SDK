"""Unit tests without an OAuth service and without the platform's services: `appext.testing` signs a
test person in directly and mocks the target service."""
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
    assert 'hx-get="/items"' in response.text


def test_a_visitor_without_a_session_is_sent_to_the_sign_in():
    anonymous = ExtensionTestClient(app)
    response = anonymous.get("/", follow_redirects=False)
    anonymous.close()
    assert response.status_code == 302
    assert response.headers["location"].startswith("/auth/login")


def test_the_items_fragment_comes_from_the_service(client):
    with service_mocks(ext) as mocks:
        mocks.get("{{service}}", "/items").respond(json=[{"id": "i-1", "name": "First <b>item</b>"}, {"id": "i-2"}])
        response = client.get("/items")
    assert response.status_code == 200
    assert "First &lt;b&gt;item&lt;/b&gt;" in response.text  # escaped: the data is not ours
    assert "<li>i-2</li>" in response.text  # no name: the id is shown
    assert client.token_requests == [("{{service}}", "{{audience}}", ("{{scope}}",), "user")]


def test_a_failing_service_is_a_bad_gateway_not_a_crash(client):
    with service_mocks(ext) as mocks:
        mocks.get("{{service}}", "/items").respond(status_code=500)
        assert client.get("/items").status_code == 502


def test_static_files_are_served_without_a_session():
    anonymous = ExtensionTestClient(app)
    script = anonymous.get("/htmx.min.js")
    anonymous.close()
    assert script.status_code == 200 and "Zero-Clause BSD" in script.text[:400]
