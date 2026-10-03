from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ar_pipeline.normalize.payer import FLAG_PAYER_IS_CLIENT, payer_flags
from ar_pipeline.schema.canonical import Envelope, Header, LineItem, RemittancePayload


def _payload(payer: str) -> RemittancePayload:
    return RemittancePayload(
        envelope=Envelope(
            extraction_id="x",
            source_email_id="e",
            extracted_at=datetime(2026, 10, 4, tzinfo=UTC),
        ),
        header=Header(payer_name=payer, total_paid_amount=Decimal("1")),
        line_items=[
            LineItem(invoice_number="A", invoice_amount=Decimal("1"), amount_paid=Decimal("1"))
        ],
    )


def test_payer_named_like_the_client_is_flagged():
    assert payer_flags(_payload("ACME METALS LTD"), ["Acme Metals"]) == [FLAG_PAYER_IS_CLIENT]
    assert payer_flags(_payload("Continental Bus Body Builders"), ["Acme Metals"]) == []
    assert payer_flags(_payload("ACME METALS LTD"), []) == []
    assert payer_flags(_payload(""), ["Acme Metals"]) == []
