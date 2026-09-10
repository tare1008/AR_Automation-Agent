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


def test_review_root_is_the_journey(client, seed_pending):
    email, ext = seed_pending()
    r = client.get("/review")
    assert r.status_code == 200
    assert "Journey" in r.text
    assert email.subject in r.text
    # a link to the read-only JSON view for that extraction
    assert f"/review/extraction/{ext.id}" in r.text


def test_journey_shows_auto_approved_vs_awaiting(client, db_session, seed_pending):
    from ar_pipeline.db.models import Delivery

    e1, x1 = seed_pending()  # pending -> "awaiting review"
    e2, x2 = seed_pending()
    x2.status = "approved"
    x2.reviewed_by = "auto"
    db_session.add(Delivery(extraction_id=x2.id, status="delivered"))
    db_session.flush()
    text = client.get("/review").text
    assert "awaiting review" in text.lower()
    assert "auto-approved" in text.lower()
    assert "delivered" in text.lower()


def test_queue_moved_to_review_queue(client, seed_pending):
    email, ext = seed_pending()
    assert client.get("/review/queue").status_code == 200
    assert email.subject in client.get("/review/queue").text


def test_journey_requires_login():
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/review").status_code == 303
