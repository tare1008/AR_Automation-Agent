from __future__ import annotations

from decimal import Decimal

from ar_pipeline.schema.canonical import Deduction, LineItem, RemittancePayload


def _payload(**line) -> dict:
    return {
        "envelope": {
            "extraction_id": "x",
            "source_email_id": "e",
            "extracted_at": "2026-10-04T00:00:00+00:00",
        },
        "header": {"payer_name": "Acme", "total_paid_amount": "90.00"},
        "line_items": [
            {"invoice_number": "INV-1", "invoice_amount": "90.00", "amount_paid": "90.00", **line}
        ],
    }


def test_defaults_keep_old_payloads_valid():
    p = RemittancePayload.model_validate(_payload())
    assert p.envelope.schema_version == "2"
    assert p.line_items[0].kind == "invoice"
    assert p.line_items[0].applies_to is None


def test_adjustment_line_round_trips():
    line = LineItem(
        invoice_number="2510004583DISCO",
        invoice_amount=Decimal("0"),
        deductions=[Deduction(type="debit_note", amount=Decimal("12500"))],
        amount_paid=Decimal("-12500"),
        kind="adjustment",
        applies_to="CBB2510004583",
    )
    dumped = line.model_dump(mode="json")
    assert dumped["kind"] == "adjustment" and dumped["applies_to"] == "CBB2510004583"
    assert dumped["deductions"][0]["type"] == "debit_note"
