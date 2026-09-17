from urllib.parse import unquote

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


def test_poll_now_requires_login():
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.post("/review/poll-now").status_code == 303


def test_poll_now_runs_poll_and_advance_then_redirects(client, monkeypatch):
    from ar_pipeline.ingest.poller import PollStats
    from ar_pipeline.pipeline.advance import AdvanceStats

    monkeypatch.setattr(
        "ar_pipeline.ingest.service.run_poll",
        lambda: PollStats(
            new_emails=2, attachments=1, duplicates=0, removed=0, failed=0, resynced=False
        ),
    )
    monkeypatch.setattr(
        "ar_pipeline.pipeline.advance.advance_once",
        lambda session, blob, vision, llm: AdvanceStats(
            classified=2, extracted=2, normalized=1, errored=0
        ),
    )

    resp = client.post("/review/poll-now")
    location = unquote(resp.headers["location"])

    assert resp.status_code == 303
    assert location.startswith("/review?flash=")
    assert "2 new" in location
    assert "2 classified" in location


def test_poll_now_skips_delivery_when_backend_url_unset(client, monkeypatch):
    from ar_pipeline.ingest.poller import PollStats
    from ar_pipeline.pipeline.advance import AdvanceStats

    monkeypatch.setattr(
        "ar_pipeline.ingest.service.run_poll",
        lambda: PollStats(
            new_emails=0, attachments=0, duplicates=0, removed=0, failed=0, resynced=False
        ),
    )
    monkeypatch.setattr(
        "ar_pipeline.pipeline.advance.advance_once",
        lambda session, blob, vision, llm: AdvanceStats(),
    )

    resp = client.post("/review/poll-now")

    # conftest pins BACKEND_URL="" for tests that don't opt into a value.
    assert "deliver" not in unquote(resp.headers["location"])


def test_poll_now_reports_not_configured_when_mailbox_missing(client, monkeypatch):
    from ar_pipeline.ingest.auth import GraphNotConfigured
    from ar_pipeline.pipeline.advance import AdvanceStats

    def _raise():
        raise GraphNotConfigured

    monkeypatch.setattr("ar_pipeline.ingest.service._mailbox_client", _raise)
    monkeypatch.setattr(
        "ar_pipeline.pipeline.advance.advance_once",
        lambda session, blob, vision, llm: AdvanceStats(),
    )

    resp = client.post("/review/poll-now")

    assert "not configured" in unquote(resp.headers["location"])
