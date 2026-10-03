from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, EmailMessage, ExtractionSource
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.normalize.llm_fake import FakeLLMClient
from tests.threads.chains import chain_html


def _email(db_session, html, mid="m-chain-1"):
    e = Email(
        internet_message_id=mid,
        sender_address="dharmendra.p.kumar-c@adityabirla.com",
        sender_domain="adityabirla.com",
        subject="FW: Payment Remittance details",
        received_at=datetime(2026, 2, 18, 12, 8, tzinfo=UTC),
        body_html=html,
        body_text="",
        status="new",
    )
    db_session.add(e)
    db_session.flush()
    return e


def test_classify_splits_and_creates_one_source_per_new_message(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_DOMAINS", "adityabirla.com")
    config_module.get_settings.cache_clear()
    try:
        e = _email(db_session, chain_html([3, 2, 1]))
        advance_once(
            db_session, LocalBlobStore(str(tmp_path)), FakeVisionExtractor(), FakeLLMClient()
        )
        msgs = db_session.scalars(
            select(EmailMessage)
            .where(EmailMessage.email_id == e.id)
            .order_by(EmailMessage.position)
        ).all()
        assert [m.status for m in msgs] == ["no_content", "new", "new", "new"]
        srcs = db_session.scalars(
            select(ExtractionSource).where(ExtractionSource.email_id == e.id)
        ).all()
        assert sorted(s.kind for s in srcs) == ["body_table"] * 3
        assert {s.email_message_id for s in srcs} == {m.id for m in msgs[1:]}
        assert msgs[0].body_text is not None  # no_content still keeps its own text
    finally:
        config_module.get_settings.cache_clear()


def test_extract_reads_the_message_not_the_whole_email(db_session, tmp_path):
    e = _email(db_session, chain_html([2, 1]), mid="m-chain-2")
    store = LocalBlobStore(str(tmp_path))
    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # classify
    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # extract
    from ar_pipeline.db.models import RawExtraction

    payloads = db_session.scalars(
        select(RawExtraction.payload)
        .join(ExtractionSource)
        .where(ExtractionSource.email_id == e.id)
    ).all()
    assert len(payloads) == 2
    for p in payloads:
        assert p["text"].count("PUNBR52026") == 1  # each source holds one message
        assert len(p["tables"]) == 1


def test_splitter_failure_falls_back_to_one_message(db_session, tmp_path, monkeypatch):
    import ar_pipeline.pipeline.advance as advance_module

    def boom(**_kwargs):
        raise RuntimeError("splitter exploded")

    monkeypatch.setattr(advance_module, "split_email", boom)
    e = _email(db_session, chain_html([2, 1]), mid="m-chain-boom")
    advance_once(db_session, LocalBlobStore(str(tmp_path)), FakeVisionExtractor(), FakeLLMClient())
    db_session.refresh(e)
    assert e.status == "classified"
    msgs = db_session.scalars(select(EmailMessage).where(EmailMessage.email_id == e.id)).all()
    assert len(msgs) == 1
    assert msgs[0].position == 0
    assert msgs[0].status == "new"
    assert len(msgs[0].tables) >= 1  # recovered from the HTML
    assert msgs[0].body_text


# ---- fix round 1 ---------------------------------------------------------------------------

from ar_pipeline.db.models import Attachment, Extraction  # noqa: E402
from ar_pipeline.pipeline.advance import _ensure_messages  # noqa: E402

PAY = (
    "Please apply the payment of Rs 1,20,000.00 received today against our open invoice "
    "number 55 and confirm the receipt to the accounts team."
)


def _text_email(db_session, body, mid, sender="pay@client.com"):
    e = Email(
        internet_message_id=mid,
        sender_address=sender,
        sender_domain=sender.split("@")[1],
        subject="Payment",
        received_at=datetime(2026, 2, 18, 12, 8, tzinfo=UTC),
        body_html="",
        body_text=body,
        status="new",
    )
    db_session.add(e)
    db_session.flush()
    return e


def _quoted(body, note="FYI"):
    return (
        f"{note}\n\nFrom: Payer <pay@client.com>\nSent: 10 January 2026 03:24 PM\n"
        f"To: AR\nSubject: Re: Payment\n\n{body}\n"
    )


def _record(db_session, email, *, status="approved", remit=True, key=None):
    """Mark every new message of ``email`` as having an extraction."""
    msgs = _ensure_messages(db_session, email, [])
    for m in msgs:
        db_session.add(
            Extraction(
                email_id=email.id,
                email_message_id=m.id,
                canonical={},
                status=status,
                is_remittance=remit,
                payment_key=key,
                payment_key_strength="strong" if key else None,
            )
        )
    db_session.flush()
    return msgs


def _run(db_session, tmp_path):
    advance_once(db_session, LocalBlobStore(str(tmp_path)), FakeVisionExtractor(), FakeLLMClient())


def _msgs(db_session, e):
    return db_session.scalars(
        select(EmailMessage).where(EmailMessage.email_id == e.id).order_by(EmailMessage.position)
    ).all()


def _attach(db_session, e, name="x.zip", ctype="application/zip"):
    db_session.add(
        Attachment(
            email_id=e.id, filename=name, content_type=ctype, size=10, blob_url="x", sha256="0" * 64
        )
    )
    db_session.flush()


def test_fingerprint_seen_through_ensure_messages(db_session, tmp_path):
    first = _text_email(db_session, PAY, "fp-1")
    earlier = _record(db_session, first)[0]
    second = _text_email(db_session, _quoted(PAY), "fp-2")
    msgs = _ensure_messages(db_session, second, [])
    seen = [m for m in msgs if m.status == "seen"]
    assert len(seen) == 1
    assert seen[0].seen_reason == "fingerprint"
    assert seen[0].seen_in_message_id == earlier.id
    assert seen[0].body_text  # seen messages keep their text (R15)


def test_fingerprint_not_seen_when_earlier_rejected_as_bad(db_session):
    first = _text_email(db_session, PAY, "fp-3")
    _record(db_session, first, status="rejected", remit=True)
    second = _text_email(db_session, _quoted(PAY), "fp-4")
    assert all(m.status != "seen" for m in _ensure_messages(db_session, second, []))


def test_carrier_is_never_seen(db_session):
    body = PAY + " UTR SBIN55555555"
    first = _text_email(db_session, body, "fp-5")
    _record(db_session, first, key="utr:SBIN55555555")
    second = _text_email(db_session, body, "fp-6")
    att = Attachment(
        email_id=second.id,
        filename="a.xlsx",
        content_type="x",
        size=1,
        blob_url="x",
        sha256="0" * 64,
    )
    db_session.add(att)
    db_session.flush()
    msgs = _ensure_messages(db_session, second, [att])
    carriers = [m for m in msgs if m.carries_attachments]
    assert len(carriers) == 1
    assert carriers[0].status == "new"


def test_existing_messages_are_not_resplit(db_session):
    first = _text_email(db_session, PAY, "fp-7")
    _record(db_session, first)
    second = _text_email(db_session, _quoted(PAY), "fp-8")
    before = [(m.id, m.status) for m in _ensure_messages(db_session, second, [])]
    after = [(m.id, m.status) for m in _ensure_messages(db_session, second, [])]
    assert before == after


def test_seen_quote_plus_short_new_message_is_not_done(db_session, tmp_path):
    first = _text_email(db_session, PAY, "d-1")
    _record(db_session, first)
    note = "Released Rs 50,000 vide UTR SBIN99999999 against inv 7."
    second = _text_email(db_session, note + "\n" + _quoted(PAY, note=""), "d-2")
    _run(db_session, tmp_path)
    db_session.refresh(second)
    assert second.status == "classified"


def test_seen_plus_unsupported_attachment_not_done_and_skipped_source_written(db_session, tmp_path):
    first = _text_email(db_session, PAY, "d-3")
    _record(db_session, first)
    second = _text_email(db_session, _quoted(PAY), "d-4")
    _attach(db_session, second)
    _run(db_session, tmp_path)
    db_session.refresh(second)
    assert second.status == "classified"
    srcs = db_session.scalars(
        select(ExtractionSource).where(ExtractionSource.email_id == second.id)
    ).all()
    assert any(s.skipped and s.ref != "body" for s in srcs)


def test_all_seen_without_attachments_is_done(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_DOMAINS", "client.com")
    config_module.get_settings.cache_clear()
    first = _text_email(db_session, PAY, "d-5")
    _record(db_session, first)
    second = _text_email(db_session, _quoted(PAY), "d-6")
    _run(db_session, tmp_path)
    db_session.refresh(second)
    config_module.get_settings.cache_clear()
    assert second.status == "done"


def test_short_new_payment_message_gets_body_text_source(db_session, tmp_path):
    e = _email(
        db_session,
        chain_html([1], top_note="Released Rs 1,20,000 vide UTR SBIN12345678 against inv 55."),
        mid="short-1",
    )
    _run(db_session, tmp_path)
    srcs = db_session.scalars(
        select(ExtractionSource).where(ExtractionSource.email_id == e.id)
    ).all()
    assert "body_text" in [s.kind for s in srcs]


def _refs_record(db_session, key):
    e = _text_email(db_session, "unrelated", "r-" + key)
    m = EmailMessage(
        email_id=e.id, position=1, raw_header="", is_internal=False, carries_attachments=False,
        status="new",
    )  # fmt: skip
    db_session.add(m)
    db_session.flush()
    db_session.add(
        Extraction(
            email_id=e.id, email_message_id=m.id, canonical={}, status="approved",
            is_remittance=True, payment_key=key, payment_key_strength="strong",
        )
    )  # fmt: skip
    db_session.flush()


def test_reference_rule_not_applied_with_extra_amount(db_session):
    _refs_record(db_session, "utr:SBIN11111111")
    body = "Paid UTR SBIN11111111 Rs 1,00,000 and UTR\n> SBIN22222222 Rs 50,000 as per the list"
    e = _text_email(db_session, _quoted(body), "rr-1")
    assert all(m.status != "seen" for m in _ensure_messages(db_session, e, []))


def test_reference_rule_not_applied_with_numeric_table(db_session):
    _refs_record(db_session, "utr:SBIN33333333")
    html = (
        "<p>Paid vide UTR SBIN33333333 as per the table below, kindly confirm receipt please.</p>"
        "<table><tr><th>Inv</th><th>Amt</th><th>Bal</th></tr>"
        "<tr><td>A1</td><td>1,000.00</td><td>1,000.00</td></tr></table>"
    )
    e = Email(
        internet_message_id="rr-2", sender_address="p@c.com", sender_domain="c.com", subject="s",
        received_at=datetime(2026, 2, 18, tzinfo=UTC), body_html=html, body_text="", status="new",
    )  # fmt: skip
    db_session.add(e)
    db_session.flush()
    assert all(m.status != "seen" for m in _ensure_messages(db_session, e, []))


def test_reference_rule_seen_with_single_recorded_reference(db_session):
    _refs_record(db_session, "utr:SBIN44444444")
    body = "Paid Rs 1,00,000 vide UTR SBIN44444444 against the invoices, kindly confirm receipt."
    e = _text_email(db_session, _quoted(body), "rr-3")
    msgs = _ensure_messages(db_session, e, [])
    assert [m.seen_reason for m in msgs if m.status == "seen"] == ["references_recorded"]


def test_reforward_with_short_external_note_is_done(db_session, tmp_path):
    first = _text_email(db_session, PAY, "d-7")
    _record(db_session, first)
    second = _text_email(db_session, _quoted(PAY, note="FYI, see below"), "d-8")
    _run(db_session, tmp_path)
    db_session.refresh(second)
    assert second.status == "done"


# ---- fix round 2 (R17-R19) -------------------------------------------------------------------

from ar_pipeline.threads.references import has_payment_signal  # noqa: E402


def test_cover_note_with_live_attachment_gets_only_the_attachment_source(db_session, tmp_path):
    e = _text_email(db_session, "Please find attached payment advice for Rs 1,20,000.00.", "c-1")
    _attach(db_session, e, name="advice.xlsx", ctype="application/vnd.ms-excel.sheet.macroEnabled")
    _run(db_session, tmp_path)
    srcs = db_session.scalars(
        select(ExtractionSource).where(ExtractionSource.email_id == e.id)
    ).all()
    live = [s.kind for s in srcs if not s.skipped]
    assert live == ["excel"]


def _seen_quote_plus(db_session, tmp_path, note, mid):
    first = _text_email(db_session, PAY, mid + "a")
    _record(db_session, first)
    second = _text_email(db_session, note + "\n" + _quoted(PAY, note=""), mid + "b")
    _run(db_session, tmp_path)
    db_session.refresh(second)
    return second


def test_wrapped_reference_note_gets_a_source(db_session, tmp_path):
    e = _seen_quote_plus(db_session, tmp_path, "Paid vide UTR\nSBIN12345678 against inv 7.", "w1")
    assert e.status == "classified"


def test_currency_marked_integer_note_gets_a_source(db_session, tmp_path):
    e = _seen_quote_plus(db_session, tmp_path, "Paid Rs 50000 today against inv 7.", "w2")
    assert e.status == "classified"


def test_payment_signal_ignores_bare_numbers():
    assert not has_payment_signal("Invoice 55 dated 7")
    assert not has_payment_signal("inv 7 and ref 123456")
    for t in ("Rs.50000", "INR 50000", "₹50000", "$4250"):
        assert has_payment_signal(t), t


def test_reference_rule_counts_amount_occurrences(db_session):
    _refs_record(db_session, "utr:SBIN44444444")
    body = "Paid Rs 50,000.00 vide UTR SBIN44444444 and another Rs 50,000.00 by cheque today"
    e = _text_email(db_session, _quoted(body), "occ-1")
    assert all(m.status != "seen" for m in _ensure_messages(db_session, e, []))
