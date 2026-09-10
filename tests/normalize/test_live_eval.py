"""Gated live normalization eval over the 6 email fixtures.

``@pytest.mark.live`` — deselected by default (``-m "not live"`` in addopts).
Run with ``uv run pytest -m live -s tests/normalize/test_live_eval.py`` and a
real ``ANTHROPIC_API_KEY`` to see the per-fixture report.

Soft assertions only: every fixture is a remittance and must yield >=1
``Extraction`` row. The synthetic fixtures are not guaranteed to reconcile to
the cent, so the validator flags are printed, never asserted — non-empty flags
are information for tightening the fixtures later, not a failure.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Extraction
from ar_pipeline.extract.vision import get_vision_extractor
from ar_pipeline.normalize.llm_client import get_llm_client
from ar_pipeline.normalize.normalizer import NormalizerOutput
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.storage import LocalBlobStore
from tests.fixtures.loader import FIXTURE_NAMES, load_email
from tests.normalize import eval_report
from tests.normalize.llm_fake import FakeLLMClient


@pytest.mark.live
def test_live_normalization_over_fixtures(db_session, tmp_path) -> None:
    pytest.importorskip("anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("no ANTHROPIC_API_KEY")

    for name in FIXTURE_NAMES:
        store = LocalBlobStore(str(tmp_path / name))
        email = load_email(name, db_session, store)

        # Drive to ``extracted``. The fixtures are deterministic-extractable so
        # vision never fires, and normalize is not reached yet, so the fake LLM
        # is unused here.
        advance_once(db_session, store, get_vision_extractor(), FakeLLMClient())
        advance_once(db_session, store, get_vision_extractor(), FakeLLMClient())
        db_session.refresh(email)
        assert email.status == "extracted"

        normalize_one(db_session, email, get_llm_client())
        db_session.refresh(email)

        rows = list(
            db_session.scalars(
                select(Extraction)
                .where(Extraction.email_id == email.id)
                .order_by(Extraction.created_at, Extraction.id)
            )
        )

        # M-c: only the payment_index == 0 row carries the full LLM dump; the
        # rest store just their own draft plus a pointer.
        row0 = next(
            (r for r in rows if (r.canonical.get("envelope") or {}).get("payment_index") == 0),
            rows[0],
        )
        out = NormalizerOutput.model_validate(row0.raw_llm_response)
        flags_per_payment = [list(row.validation_flags) for row in rows]
        print(eval_report.summarise(name, out, flags_per_payment))

        assert email.status == "review"
        assert len(rows) >= 1
        assert rows[0].is_remittance is True
