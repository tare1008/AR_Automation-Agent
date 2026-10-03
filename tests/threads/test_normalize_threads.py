from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, EmailMessage, Extraction
from ar_pipeline.normalize.llm_client import LLMError
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.schema.canonical import LineItem
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.normalize.llm_fake import FakeLLMClient
from tests.threads.chains import chain_html


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("CLIENT_DOMAINS", "adityabirla.com")
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def _draft(n: int) -> PaymentDraft:
    amount = Decimal((n + 1) * 1000)
    return PaymentDraft(
        payer_name="Alufluoride Limited",
        payment_reference=f"PUNBR52026{n:011d}",
        payment_reference_type="utr",
        total_paid_amount=amount,
        line_items=[
            LineItem(invoice_number=f"JHMUR25100{n:05d}", invoice_amount=amount, amount_paid=amount)
        ],
        confidence=0.95,
    )


def _out(n: int) -> NormalizerOutput:
    return NormalizerOutput(is_remittance=True, payments=[_draft(n)])


def _ingest(db_session, html, mid):
    e = Email(
        internet_message_id=mid,
        sender_address="dharmendra.p.kumar-c@adityabirla.com",
        sender_domain="adityabirla.com",
        subject="FW: Payment Remittance details",
        received_at=datetime(2026, 2, 18, 12, 8, tzinfo=UTC),
        body_html=html,
        status="new",
    )
    db_session.add(e)
    db_session.flush()
    return e


def _run(db_session, tmp_path, llm):
    store = LocalBlobStore(str(tmp_path))
    for _ in range(3):
        advance_once(db_session, store, FakeVisionExtractor(), llm)


def _rows(db_session, email):
    return db_session.scalars(
        select(Extraction).where(Extraction.email_id == email.id).order_by(Extraction.created_at)
    ).all()


def test_first_sight_processes_every_message_oldest_first(db_session, tmp_path, settings_env):
    e = _ingest(db_session, chain_html([3, 2, 1]), "t1")
    llm = FakeLLMClient(responses=[_out(1), _out(2), _out(3)])  # oldest first
    _run(db_session, tmp_path, llm)
    rows = _rows(db_session, e)
    assert len(llm.calls) == 3 and len(rows) == 3
    by_ref = {r.canonical["header"]["payment_reference"]: r for r in rows}
    newest = by_ref["PUNBR52026" + "3".zfill(11)]
    assert newest.status == "approved" and newest.historical_reason is None
    for n in (1, 2):
        older = by_ref["PUNBR52026" + str(n).zfill(11)]
        assert older.status == "pending_review" and older.historical_reason == "earlier_message"


def test_reforward_reads_only_the_new_message(db_session, tmp_path, settings_env):
    _ingest(db_session, chain_html([2, 1]), "t2a")
    _run(db_session, tmp_path, FakeLLMClient(responses=[_out(1), _out(2)]))
    e2 = _ingest(db_session, chain_html([3, 2, 1]), "t2b")
    llm = FakeLLMClient(responses=[_out(3)])
    _run(db_session, tmp_path, llm)
    assert len(llm.calls) == 1
    msgs = db_session.scalars(
        select(EmailMessage).where(EmailMessage.email_id == e2.id).order_by(EmailMessage.position)
    ).all()
    assert [m.status for m in msgs] == ["no_content", "new", "seen", "seen"]
    assert {m.seen_reason for m in msgs[2:]} == {"fingerprint"}


def test_duplicate_payment_from_a_mangled_copy(db_session, tmp_path, settings_env):
    e1 = _ingest(db_session, chain_html([1]), "t3a")
    _run(db_session, tmp_path, FakeLLMClient(responses=[_out(1)]))
    (first,) = _rows(db_session, e1)
    # same payment, different wording: the fingerprint misses, and a message with a
    # numeric table is always read (R14), so the payment key catches it (R20)
    html = chain_html([1]).replace("Dear Dharmendra Ji,", "Hi team, see corrected below.")
    e2 = _ingest(db_session, html, "t3b")
    llm = FakeLLMClient(responses=[_out(1)])
    _run(db_session, tmp_path, llm)
    assert len(llm.calls) == 1
    (second,) = _rows(db_session, e2)
    assert second.status == "duplicate"
    assert second.duplicate_of_id == first.id
    assert db_session.get(Email, e2.id).status == "done"


def test_one_failed_message_does_not_block_its_siblings(db_session, tmp_path, settings_env):
    e = _ingest(db_session, chain_html([2, 1]), "t4")
    _run(db_session, tmp_path, FakeLLMClient(responses=[LLMError("boom"), _out(2)]))
    msgs = db_session.scalars(
        select(EmailMessage).where(EmailMessage.email_id == e.id).order_by(EmailMessage.position)
    ).all()
    assert [m.status for m in msgs] == ["no_content", "new", "failed"]
    assert "boom" in (msgs[2].error_detail or "")
    assert len(_rows(db_session, e)) == 1


class _CountingLLM:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        return self.inner.parse(**kwargs)


def test_offline_stub_rehearsal_reads_only_new_messages(db_session, tmp_path, settings_env):
    from ar_pipeline.normalize.stub_client import StubLLMClient

    llm = _CountingLLM(StubLLMClient())
    _ingest(db_session, chain_html([2, 1]), "t5a")
    _run(db_session, tmp_path, llm)
    assert llm.calls == 2
    llm.calls = 0
    _ingest(db_session, chain_html([3, 2, 1]), "t5b")
    _run(db_session, tmp_path, llm)
    assert llm.calls == 1
