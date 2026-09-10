import json
import uuid

import pytest
from fastapi.testclient import TestClient

from ar_pipeline.main import app


@pytest.fixture
def client(db_session):
    from ar_pipeline.review.app import get_db

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, follow_redirects=False) as c:
        c.post("/review/login", data={"password": "test-shared-secret", "name": "Asha"})
        yield c
    app.dependency_overrides.clear()


def test_extraction_page_renders_pretty_json_for_any_status(client, db_session, seed_pending):
    email, ext = seed_pending()
    ext.status = "approved"
    ext.reviewed_by = "auto"
    db_session.flush()
    r = client.get(f"/review/extraction/{ext.id}")
    assert r.status_code == 200
    assert "auto-approved" in r.text.lower()
    assert ext.canonical["header"]["payer_name"] in r.text


def test_extraction_raw_json_endpoint(client, seed_pending):
    email, ext = seed_pending()
    r = client.get(f"/review/extraction/{ext.id}?format=json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = json.loads(r.text)
    assert body["envelope"]["extraction_id"]


def test_extraction_view_unknown_id_404(client):
    assert client.get(f"/review/extraction/{uuid.uuid4()}").status_code == 404


def test_extraction_view_requires_login():
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get(f"/review/extraction/{uuid.uuid4()}").status_code == 303
