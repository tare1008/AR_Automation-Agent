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


def test_approved_page_requires_login():
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/review/approved").status_code == 303


def test_approved_page_lists_nothing_when_empty(client):
    r = client.get("/review/approved")
    assert r.status_code == 200
    assert "Nothing approved yet" in r.text


def test_approved_page_shows_auto_and_human_approvals(client, db_session, seed_pending):
    from ar_pipeline.pipeline.routing import AUTO_REVIEWER, approve_and_queue

    auto_email, auto_ext = seed_pending()
    approve_and_queue(db_session, auto_ext, reviewed_by=AUTO_REVIEWER)
    db_session.flush()

    human_email, human_ext = seed_pending()
    approve_and_queue(db_session, human_ext, reviewed_by="Priya")
    db_session.flush()

    r = client.get("/review/approved")
    assert r.status_code == 200
    assert "Auto" in r.text
    assert "Priya" in r.text
    assert auto_email.subject in r.text
    assert human_email.subject in r.text
    # every approved row inserts a Delivery via approve_and_queue -> shows as pending
    assert r.text.count("pending") >= 2
    assert f"/review/extraction/{auto_ext.id}" in r.text


def test_approved_page_excludes_pending_and_rejected(client, db_session, seed_pending):
    from ar_pipeline.review.auth import User
    from ar_pipeline.review.service import reject_extraction

    pending_email, pending_ext = seed_pending()
    rejected_email, rejected_ext = seed_pending()
    reject_extraction(db_session, rejected_ext.id, User(name="Asha"), "duplicate")
    db_session.flush()

    r = client.get("/review/approved")
    assert pending_email.subject not in r.text
    assert rejected_email.subject not in r.text
