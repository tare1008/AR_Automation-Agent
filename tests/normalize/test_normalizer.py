from __future__ import annotations

import uuid
from decimal import Decimal

from ar_pipeline.normalize.normalizer import (
    NormalizerOutput,
    PaymentDraft,
    normalize_email,
)
from ar_pipeline.normalize.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_message
from ar_pipeline.schema.canonical import Deduction, LineItem
from tests.normalize.llm_fake import FakeLLMClient


def _line(
    invoice_number: str = "INV-1",
    invoice_amount: str = "100.00",
    tds: str = "10.00",
    amount_paid: str = "90.00",
) -> LineItem:
    return LineItem(
        invoice_number=invoice_number,
        invoice_amount=Decimal(invoice_amount),
        deductions=[Deduction(type="tds", amount=Decimal(tds))],
        amount_paid=Decimal(amount_paid),
    )


def _draft(**over: object) -> PaymentDraft:
    kwargs: dict[str, object] = dict(
        payer_name="Acme Corp",
        total_paid_amount=Decimal("90.00"),
        line_items=[_line()],
        confidence=0.9,
    )
    kwargs.update(over)
    return PaymentDraft(**kwargs)


def _call(out: NormalizerOutput, email_id: str = "em-1") -> tuple[NormalizerOutput, list]:
    client = FakeLLMClient(response=out)
    return normalize_email(
        email_id=email_id,
        sender_address="fwd@company.example",
        subject="FW: Payment advice",
        raw_extractions=[{"text": "hi", "tables": [], "meta": {}}],
        llm_client=client,
        model_name="claude-opus-5",
    )


def test_prompt_version_is_two() -> None:
    assert PROMPT_VERSION == "2"
    assert isinstance(SYSTEM_PROMPT, str) and len(SYSTEM_PROMPT) > 200


def test_single_reconciling_payment() -> None:
    out, results = _call(NormalizerOutput(is_remittance=True, payments=[_draft()]))
    assert len(results) == 1
    np = results[0]
    assert np.payload.envelope.payment_index == 0
    assert np.payload.envelope.source_email_id == "em-1"
    uuid.UUID(np.payload.envelope.extraction_id)
    assert np.validation_flags == []
    assert np.confidence == Decimal("0.900")
    assert np.is_remittance is True
    assert np.raw_llm_response["is_remittance"] is True


def test_two_payments_get_distinct_payment_index() -> None:
    out, results = _call(
        NormalizerOutput(
            is_remittance=True,
            payments=[
                _draft(payment_reference="UTR111"),
                _draft(payment_reference="UTR222"),
            ],
        )
    )
    assert [r.payload.envelope.payment_index for r in results] == [0, 1]


def test_not_a_remittance() -> None:
    out, results = _call(
        NormalizerOutput(is_remittance=False, notes="looks like a newsletter", payments=[])
    )
    assert results == []
    assert out.notes == "looks like a newsletter"


def test_non_reconciling_payment_flagged() -> None:
    out, results = _call(
        NormalizerOutput(
            is_remittance=True,
            payments=[_draft(total_paid_amount=Decimal("500.00"))],
        )
    )
    assert len(results) == 1
    assert results[0].validation_flags


def test_draft_with_no_line_items_only_payment_is_skipped() -> None:
    out, results = _call(NormalizerOutput(is_remittance=True, payments=[_draft(line_items=[])]))
    assert results == []
    assert "schema validation failed" in out.notes


def test_draft_with_no_line_items_surfaced_on_first_good_payment() -> None:
    out, results = _call(
        NormalizerOutput(
            is_remittance=True,
            payments=[_draft(line_items=[]), _draft()],
        )
    )
    assert len(results) == 1
    assert any("schema validation failed" in f for f in results[0].validation_flags)


def test_invalid_header_draft_skipped_index_stays_contiguous() -> None:
    out, results = _call(
        NormalizerOutput(
            is_remittance=True,
            payments=[_draft(currency="Rupees"), _draft()],
        )
    )
    assert len(results) == 1
    assert results[0].payload.envelope.payment_index == 0
    assert any("schema validation failed" in f for f in results[0].validation_flags)


def test_build_user_message_contents() -> None:
    msg = build_user_message(
        "fwd@company.example",
        "FW: Payment advice",
        [
            {
                "text": "Payment made against your invoices",
                "tables": [[["Invoice", "Amount"], ["INV-1", "100.00"]]],
                "meta": {"via": "pdf_text"},
            }
        ],
    )
    assert "fwd@company.example" in msg
    assert "FW: Payment advice" in msg
    assert "Payment made against your invoices" in msg
    assert "Invoice | Amount" in msg
    assert "pdf_text" in msg


def test_build_user_message_truncates(caplog) -> None:
    big = "x" * 60_000
    with caplog.at_level("WARNING"):
        msg = build_user_message("a@b.c", "Subj", [{"text": big, "tables": [], "meta": {}}])
    assert len(msg) <= 41_000
    assert "[... content truncated ...]" in msg
    assert "truncated" in caplog.text
