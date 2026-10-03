from __future__ import annotations

import pytest

from ar_pipeline.config import get_settings
from ar_pipeline.ledger.checks import refresh_pending_flags
from ar_pipeline.normalize.payer import FLAG_PAYER_IS_CLIENT
from ar_pipeline.normalize.recheck import TRUNCATED_FLAG, recheck, sticky_flags
from ar_pipeline.schema.canonical import RemittancePayload
from ar_pipeline.tables.totals import FLAG_TOTALS
from ar_pipeline.threads.dedupe import FLAG_HISTORICAL, FLAG_POSSIBLE_DUPLICATE

HIST = f"{FLAG_HISTORICAL} payment is older than the newest message"
DUP = f"{FLAG_POSSIBLE_DUPLICATE} of payment 123 (same payer, amount and date)"


@pytest.fixture
def clean_settings():
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def test_sticky_flags():
    flags = [HIST, TRUNCATED_FLAG, DUP, "draft 1: schema validation failed: x", "line 0: other"]
    assert sticky_flags(flags, key_changed=False) == flags[:4]
    assert sticky_flags(flags, key_changed=True) == [
        HIST,
        TRUNCATED_FLAG,
        "draft 1: schema validation failed: x",
    ]


def test_refresh_keeps_sticky_flags(db_session, seed_pending):
    _email, ext = seed_pending()
    ext.validation_flags = [HIST, "line 0: stale ledger flag"]
    db_session.flush()
    refresh_pending_flags(db_session)
    assert HIST in ext.validation_flags
    assert "line 0: stale ledger flag" not in ext.validation_flags


def test_recheck_adds_totals_and_payer_flags(db_session, seed_pending, monkeypatch, clean_settings):
    monkeypatch.setenv("CLIENT_NAMES", "Acme Corp")
    get_settings.cache_clear()
    _email, ext = seed_pending()
    ext.read_info = {"path": "table", "document_totals": {"words": "500.00"}}
    payload = RemittancePayload.model_validate(ext.canonical)
    flags = recheck(db_session, ext, payload, key_changed=False)
    assert FLAG_PAYER_IS_CLIENT in flags
    assert any(f.startswith(FLAG_TOTALS) for f in flags)
