import pytest
from fastapi.testclient import TestClient

from ar_pipeline.main import app
from ar_pipeline.review.auth import COOKIE_NAME


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


def test_review_requires_login_redirects_to_login(client):
    r = client.get("/review")
    assert r.status_code == 303
    assert r.headers["location"] == "/review/login"


def test_login_page_renders(client):
    r = client.get("/review/login")
    assert r.status_code == 200
    assert "password" in r.text.lower()


def test_login_with_wrong_password_re_renders_with_error(client):
    r = client.post("/review/login", data={"password": "nope", "name": "Asha"})
    assert r.status_code == 200
    assert "incorrect" in r.text.lower()
    assert COOKIE_NAME not in r.cookies


def test_login_success_sets_cookie_and_redirects(client):
    r = client.post("/review/login", data={"password": "test-shared-secret", "name": "Asha Rao"})
    assert r.status_code == 303
    assert r.headers["location"] == "/review"
    assert client.cookies.get(COOKIE_NAME)


def test_login_requires_a_name(client):
    r = client.post("/review/login", data={"password": "test-shared-secret", "name": "  "})
    assert r.status_code == 200
    assert "name" in r.text.lower()


def test_login_rejects_the_reserved_auto_name(client):
    r = client.post("/review/login", data={"password": "test-shared-secret", "name": "Auto"})
    assert r.status_code == 200
    assert "reserved" in r.text.lower()
    assert COOKIE_NAME not in r.cookies


def test_logout_clears_cookie(client):
    client.post("/review/login", data={"password": "test-shared-secret", "name": "Asha"})
    r = client.post("/review/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/review/login"
