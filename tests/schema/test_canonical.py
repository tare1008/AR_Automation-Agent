from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ar_pipeline.schema.canonical import (
    CANONICAL_JSON_SCHEMA,
    Deduction,
    Header,
    LineItem,
    RemittancePayload,
)


def _valid_payload_dict():
    return {
        "envelope": {
            "extraction_id": "ext-1",
            "source_email_id": "email-1",
            "payment_index": 0,
            "vendor_guess": "Fluorochem Industries",
            "extracted_at": datetime(2026, 2, 18, tzinfo=UTC).isoformat(),
            "reviewed_by": None,
        },
        "header": {
            "payer_name": "Fluorochem Industries",
            "payment_reference": "STBK52026021813360279",
            "payment_reference_type": "utr",
            "payment_date": "2026-02-18",
            "payment_method": "RTGS",
            "currency": "INR",
            "total_paid_amount": "13670691.00",
            "deductions": [{"type": "tds", "amount": "17832.00", "reason": "IT TDS 194Q 0.1%"}],
        },
        "line_items": [
            {
                "invoice_number": "FCI2510007033",
                "invoice_date": "2026-02-02",
                "invoice_amount": "1452299.16",
                "deductions": [],
                "amount_paid": "1452299.16",
            }
        ],
    }


def test_valid_payload_parses():
    p = RemittancePayload.model_validate(_valid_payload_dict())
    assert p.header.total_paid_amount == Decimal("13670691.00")
    assert p.header.deductions[0].type == "tds"
    assert p.envelope.payment_index == 0


def test_payment_reference_is_optional():
    d = _valid_payload_dict()
    d["header"]["payment_reference"] = None
    d["header"]["payment_date"] = None
    RemittancePayload.model_validate(d)  # sample 03 / 06 have no real UTR


def test_line_item_deductions_default_empty_and_typed():
    li = LineItem.model_validate(
        {
            "invoice_number": "X",
            "invoice_amount": "100.00",
            "amount_paid": "90.00",
            "deductions": [{"type": "credit_note", "amount": "10.00"}],
        }
    )
    assert li.deductions[0].amount == Decimal("10.00")
    assert li.deductions[0].reason is None


def test_bad_deduction_type_rejected():
    with pytest.raises(ValidationError):
        Deduction.model_validate({"type": "vat", "amount": "1.00"})


def test_currency_defaults_to_inr_and_uppercases():
    h = Header(
        payer_name="X", payment_reference=None, payment_date=None, total_paid_amount=Decimal("1.00")
    )
    assert h.currency == "INR"
    d = _valid_payload_dict()
    d["header"]["currency"] = "inr"
    assert RemittancePayload.model_validate(d).header.currency == "INR"


def test_currency_must_be_three_letters():
    d = _valid_payload_dict()
    d["header"]["currency"] = "Rupees"
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(d)


def test_currency_rejects_non_alphabetic():
    d = _valid_payload_dict()
    d["header"]["currency"] = "1N5"
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(d)


def test_extra_field_forbidden():
    d = _valid_payload_dict()
    d["header"]["mystery"] = 1
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(d)


def test_line_items_min_length_one():
    d = _valid_payload_dict()
    d["line_items"] = []
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(d)


def test_json_schema_shape():
    assert CANONICAL_JSON_SCHEMA["title"] == "RemittancePayload"
    assert "Deduction" in CANONICAL_JSON_SCHEMA["$defs"]
