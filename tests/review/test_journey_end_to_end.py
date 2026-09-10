"""Ingest several fixtures with LLM_PROVIDER=stub + a 0.9 auto-approve
threshold; some auto-send, some land in the review queue; the journey shows
both, and the stub backend received the auto-sent ones."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ar_pipeline.db.models import Extraction
from ar_pipeline.deliver.backend_client import BackendClient
from ar_pipeline.deliver.deliverer import run_deliveries
from ar_pipeline.main import app
from ar_pipeline.storage import LocalBlobStore
from stub_backend.app import app as stub_app
from stub_backend.store import _IDEMPOTENCY, RECEIVED
from tests.extract.vision_fake import FakeVisionExtractor
from tests.fixtures.loader import FIXTURE_NAMES, load_email


@pytest.fixture
def client(db_session):
    from ar_pipeline.review.app import get_db

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, follow_redirects=False) as c:
        c.post("/review/login", data={"password": "test-shared-secret", "name": "Asha"})
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _stub_clear():
    RECEIVED.clear()
    _IDEMPOTENCY.clear()
    yield
    RECEIVED.clear()
    _IDEMPOTENCY.clear()


def test_end_to_end_journey_split(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.9")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    import ar_pipeline.config as cfg

    cfg.get_settings.cache_clear()

    from ar_pipeline.normalize.llm_client import get_llm_client
    from ar_pipeline.pipeline.advance import advance_once

    store = LocalBlobStore(str(tmp_path))
    vision = FakeVisionExtractor()
    for name in FIXTURE_NAMES:
        load_email(name, db_session, store)
    for _ in range(3):
        advance_once(db_session, store, vision, get_llm_client())

    exts = db_session.scalars(select(Extraction)).all()
    auto = [x for x in exts if x.status == "approved" and x.reviewed_by == "auto"]
    pend = [x for x in exts if x.status == "pending_review"]
    assert auto and pend, f"expected a split; auto={len(auto)} pending={len(pend)}"

    # the journey page shows both
    text = client.get("/review").text
    assert "auto-approved" in text.lower() and "awaiting review" in text.lower()

    # deliver the auto-approved ones to the stub
    backend = BackendClient(
        base_url="http://stub",
        http=TestClient(stub_app),  # type: ignore[arg-type]
    )  # TestClient is an httpx.Client
    run_deliveries(db_session, backend)
    assert len(RECEIVED) == len(auto)
    cfg.get_settings.cache_clear()
