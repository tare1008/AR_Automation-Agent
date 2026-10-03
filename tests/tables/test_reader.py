from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import ColumnMapping
from ar_pipeline.extract.pdf import extract_pdf
from ar_pipeline.normalize.llm_client import LLMError, LLMRefused, LLMTruncated
from ar_pipeline.normalize.stub_client import StubLLMClient
from ar_pipeline.tables.mapping import find_line_table, header_signature, mapping_output
from ar_pipeline.tables.models import HeaderOutput, MappingOutput
from ar_pipeline.tables.reader import read_by_table
from tests.normalize.llm_fake import FakeLLMClient
from tests.tables.advice_pdf import NET, build_advice_pdf


class Counting:
    def __init__(self):
        self.inner = StubLLMClient()
        self.models: list[type] = []

    def parse(self, *, system, user, output_model):
        self.models.append(output_model)
        return self.inner.parse(system=system, user=user, output_model=output_model)


def _read(session, llm, extra: list[dict] | None = None):
    raws = [extract_pdf(build_advice_pdf()).to_payload(), *(extra or [])]
    return read_by_table(
        session,
        email_id="e",
        sender="ap@ourco.com",
        subject="FW: advice",
        raws=raws,
        llm_client=llm,
        client_names=["Acme Metals"],
    )


def test_first_read_learns_and_saves_the_mapping(db_session):
    llm = Counting()
    read = _read(db_session, llm)
    assert read is not None
    assert llm.models == [MappingOutput, HeaderOutput]
    payload = read.payments[0].payload
    assert len(payload.line_items) == 20 and payload.header.total_paid_amount == NET
    assert read.read_info["path"] == "table" and read.read_info["mapping"] == "learned"
    assert read.read_info["document_totals"]["words"] == "6809764.78"
    assert db_session.scalar(select(ColumnMapping.uses)) == 1


def test_second_read_uses_the_saved_mapping(db_session):
    _read(db_session, Counting())
    llm = Counting()
    read = _read(db_session, llm)
    assert llm.models == [HeaderOutput]
    assert read.read_info["mapping"] == "saved"
    assert db_session.scalar(select(ColumnMapping.uses)) == 2


def test_corrupted_mapping_is_discarded_and_relearned(db_session):
    _read(db_session, Counting())
    row = db_session.scalar(select(ColumnMapping))
    row.columns = {**row.columns, "amount_paid": 3, "invoice_amount": 6}  # swapped
    db_session.flush()
    llm = Counting()
    read = _read(db_session, llm)
    assert read is not None and read.read_info["mapping"] == "learned"
    assert MappingOutput in llm.models
    # the bad row was deleted and a fresh one learned in its place
    assert db_session.scalar(select(ColumnMapping.columns))["amount_paid"] == 6


def test_no_line_table_means_full_ai_read(db_session):
    llm = Counting()
    read = read_by_table(
        db_session,
        email_id="e",
        sender="s",
        subject="s",
        raws=[{"text": "Paid Rs 500 vide UTR ABCD1234567", "tables": []}],
        llm_client=llm,
        client_names=[],
    )
    assert read is None and llm.models == []


def test_normalize_one_reads_the_advice_by_table(db_session, monkeypatch):
    from datetime import UTC, datetime

    from ar_pipeline.config import get_settings
    from ar_pipeline.db.models import (
        Email,
        EmailMessage,
        Extraction,
        ExtractionSource,
        Invoice,
        RawExtraction,
    )
    from ar_pipeline.ledger.matching import number_key
    from ar_pipeline.normalize.service import normalize_one

    monkeypatch.setenv("CLIENT_NAMES", "Acme Metals")
    get_settings.cache_clear()
    try:
        db_session.add(
            Invoice(
                invoice_number="CBB2510004516",
                number_key=number_key("CBB2510004516"),
                payer_name="Continental Bus Body Builders",
                amount=Decimal("300.00"),
                currency="INR",
                source="books",
                paid_before_import=Decimal("0"),
            )
        )
        email = Email(
            internet_message_id="<adv@x>",
            sender_address="ap@ourco.com",
            sender_domain="ourco.com",
            subject="FW: advice",
            received_at=datetime(2026, 1, 20, tzinfo=UTC),
            status="extracted",
        )
        db_session.add(email)
        db_session.flush()
        msg = EmailMessage(
            email_id=email.id,
            position=0,
            raw_header="",
            is_internal=True,
            carries_attachments=True,
            status="new",
        )
        db_session.add(msg)
        db_session.flush()
        src = ExtractionSource(
            email_id=email.id, kind="pdf_text", ref="att-1", email_message_id=msg.id
        )
        db_session.add(src)
        db_session.flush()
        db_session.add(
            RawExtraction(
                extraction_source_id=src.id,
                payload=extract_pdf(build_advice_pdf()).to_payload(),
            )
        )
        db_session.flush()

        assert normalize_one(db_session, email, Counting()) == 1
        ext = db_session.scalar(select(Extraction).where(Extraction.email_id == email.id))
        lines = ext.canonical["line_items"]
        assert lines[1]["applies_to"] == "CBB2510004583"  # exact, same payment
        assert lines[2]["applies_to"] == "CBB2510004516"  # exact, your books
        assert (
            "line 10: adjustment of ₹4,100.00 — did you mean CBB25100035016?"
            in ext.validation_flags
        )
        assert "line 3: adjustment of ₹11,200.00 — which invoice does it reduce?" in (
            ext.validation_flags
        )
        assert not any(f.startswith("header: doesn't match") for f in ext.validation_flags)
        assert ext.read_info["path"] == "table"
        assert ext.canonical["header"]["payer_name"] == "CONTINENTAL BUS BODY BUILDERS LIMITED"
    finally:
        get_settings.cache_clear()


GOOD_COLS = {
    "invoice_number": 0,
    "invoice_date": 1,
    "invoice_amount": 3,
    "adjustment": 4,
    "tds": 5,
    "amount_paid": 6,
}


def _header(**kw) -> HeaderOutput:
    return HeaderOutput(**{"is_remittance": True, "payer_name": "Some Payer Ltd", **kw})


def _excel_raws() -> list[dict]:
    import email
    import email.policy
    from pathlib import Path

    from ar_pipeline.extract.excel import extract_excel

    eml = Path(__file__).parents[1] / "fixtures" / "emails" / "06_direct_excel.eml"
    msg = email.message_from_bytes(eml.read_bytes(), policy=email.policy.default)
    (part,) = list(msg.iter_attachments())
    return [extract_excel(part.get_payload(decode=True)).to_payload()]


def test_banner_and_zero_padding_sheet_reads_only_the_real_invoices(db_session):
    cols = {"invoice_number": 0, "invoice_date": 1, "invoice_amount": 2, "tds": 4}
    cols |= {"adjustment": 5, "amount_paid": 6, "payment_reference": 7}
    llm = FakeLLMClient(responses=[mapping_output(cols), _header()])
    read = read_by_table(
        db_session,
        email_id="e",
        sender="s@x.com",
        subject="TDS detail",
        raws=_excel_raws(),
        llm_client=llm,
        client_names=[],
    )
    assert read is not None
    lines = read.payments[0].payload.line_items
    assert [li.invoice_number for li in lines] == ["ZCC2610000038", "ZCC2610000037"]
    assert read.payments[0].payload.header.total_paid_amount == Decimal("9433014.543")
    table = find_line_table(_excel_raws())
    assert table is not None and table.header[0] == "Inv no"
    saved = db_session.scalar(select(ColumnMapping))
    assert saved.signature == header_signature(table.header) and saved.header[0] == "Inv no"


def test_learned_mapping_failing_totals_is_not_saved(db_session):
    swapped = {**GOOD_COLS, "amount_paid": 3, "invoice_amount": 6}
    llm = FakeLLMClient(responses=[mapping_output(swapped), _header()])
    assert _read(db_session, llm) is None
    assert db_session.scalar(select(ColumnMapping)) is None


def test_not_a_remittance_header_keeps_the_saved_mapping(db_session):
    _read(db_session, Counting())
    llm = FakeLLMClient(responses=[_header(is_remittance=False)])
    assert _read(db_session, llm) is None
    assert db_session.scalar(select(ColumnMapping.uses)) == 1


def test_long_header_message_marks_the_read_truncated(db_session):
    filler = {"text": "lorem ipsum " * 5000, "tables": []}
    read = _read(db_session, Counting(), extra=[filler])
    assert read is not None and read.read_info["header_truncated"] is True
    assert "header_truncated" not in _read(db_session, Counting()).read_info


def _advice_email(db_session, extra_payloads: list[dict] | None = None):
    from datetime import UTC, datetime

    from ar_pipeline.db.models import Email, EmailMessage, ExtractionSource, RawExtraction

    email = Email(
        internet_message_id="<adv@x>",
        sender_address="ap@ourco.com",
        sender_domain="ourco.com",
        subject="FW: advice",
        received_at=datetime(2026, 1, 20, tzinfo=UTC),
        status="extracted",
    )
    db_session.add(email)
    db_session.flush()
    msg = EmailMessage(
        email_id=email.id,
        position=0,
        raw_header="",
        is_internal=True,
        carries_attachments=True,
        status="new",
    )
    db_session.add(msg)
    db_session.flush()
    payloads = [extract_pdf(build_advice_pdf()).to_payload(), *(extra_payloads or [])]
    for i, payload in enumerate(payloads):
        src = ExtractionSource(
            email_id=email.id, kind="pdf_text", ref=f"att-{i + 1}", email_message_id=msg.id
        )
        db_session.add(src)
        db_session.flush()
        db_session.add(RawExtraction(extraction_source_id=src.id, payload=payload))
    db_session.flush()
    return email


def test_normalize_one_flags_a_truncated_table_read(db_session):
    from ar_pipeline.db.models import Extraction
    from ar_pipeline.normalize.recheck import TRUNCATED_FLAG
    from ar_pipeline.normalize.service import normalize_one

    email = _advice_email(db_session, [{"text": "lorem ipsum " * 5000, "tables": []}])
    assert normalize_one(db_session, email, Counting()) == 1
    ext = db_session.scalar(select(Extraction).where(Extraction.email_id == email.id))
    assert ext.read_info["path"] == "table"
    assert TRUNCATED_FLAG in ext.validation_flags


def _plain_table_raws() -> list[dict]:
    rows = [["Invoice No", "Invoice Date", "Amount", "TDS", "Net"]]
    rows += [[f"INV-10{i}", "01.09.2026", "1000.00", "10.00", "990.00"] for i in range(6)]
    return [{"text": "Payment details below.", "tables": [rows]}]


def test_read_without_document_totals_is_returned_but_not_learned(db_session):
    # R9.6: nothing verified the mapping, so it is not saved
    cols = {"invoice_number": 0, "invoice_date": 1, "invoice_amount": 2, "tds": 3}
    cols |= {"amount_paid": 4}
    llm = FakeLLMClient(responses=[mapping_output(cols), _header()])
    read = read_by_table(
        db_session,
        email_id="e",
        sender="s@x.com",
        subject="advice",
        raws=_plain_table_raws(),
        llm_client=llm,
        client_names=[],
    )
    assert read is not None and read.read_info["document_totals"] == {}
    assert len(read.payments[0].payload.line_items) == 6
    assert db_session.scalar(select(ColumnMapping)) is None


def _validation_error() -> Exception:
    from pydantic import ValidationError

    try:
        HeaderOutput.model_validate({"confidence": "not a number"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


@pytest.mark.parametrize(
    "responses",
    [
        [LLMRefused("no")],
        [_validation_error()],
        [mapping_output(GOOD_COLS), LLMTruncated("cut")],
        [mapping_output(GOOD_COLS), LLMError("boom")],
        [mapping_output(GOOD_COLS), _validation_error()],
    ],
    ids=[
        "mapping-refused",
        "mapping-invalid",
        "header-truncated",
        "header-error",
        "header-invalid",
    ],
)
def test_llm_errors_in_the_table_read_fall_back(db_session, responses):
    # R9.2
    assert _read(db_session, FakeLLMClient(responses=responses)) is None


def test_normalize_one_falls_back_to_the_full_read_when_mapping_is_refused(db_session):
    from ar_pipeline.db.models import Extraction
    from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
    from ar_pipeline.normalize.service import normalize_one
    from ar_pipeline.schema.canonical import LineItem

    full = NormalizerOutput(
        is_remittance=True,
        payments=[
            PaymentDraft(
                payer_name="Continental Bus Body Builders",
                total_paid_amount=Decimal("100.00"),
                line_items=[
                    LineItem(
                        invoice_number="INV-1",
                        invoice_amount=Decimal("100.00"),
                        amount_paid=Decimal("100.00"),
                    )
                ],
                confidence=0.9,
            )
        ],
    )
    email = _advice_email(db_session)
    llm = FakeLLMClient(responses=[LLMRefused("refused"), full])
    assert normalize_one(db_session, email, llm) == 1
    ext = db_session.scalar(select(Extraction).where(Extraction.email_id == email.id))
    assert ext is not None and ext.read_info["path"] == "ai"
    assert ext.canonical["line_items"][0]["invoice_number"] == "INV-1"
