from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ar_pipeline.schema.canonical import (
    CANONICAL_JSON_SCHEMA,
    Header,
    LineItem,
    RemittancePayload,
)


def _valid_payload_dict():
    return {
        "envelope": {
            "extraction_id": "ext-1",
            "source_email_id": "email-1",
            "vendor_guess": "Acme Corp",
            "extracted_at": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
            "reviewed_by": None,
        },
        "header": {
            "payer_name": "Acme Corp",
            "payer_id": None,
            "payment_reference": "EFT-88213",
            "payment_date": "2026-09-05",
            "payment_method": "ACH",
            "currency": "INR",
            "total_paid_amount": "12450.00",
        },
        "line_items": [
            {
                "invoice_number": "INV-1001",
                "invoice_date": "2026-08-01",
                "invoice_amount": "5000.00",
                "discount_taken": "100.00",
                "deduction_amount": "0.00",
                "deduction_reason": None,
                "amount_paid": "4900.00",
            }
        ],
    }


def test_valid_payload_parses():
    payload = RemittancePayload.model_validate(_valid_payload_dict())
    assert payload.header.total_paid_amount == Decimal("12450.00")
    assert payload.header.currency == "INR"
    assert payload.line_items[0].invoice_date == date(2026, 8, 1)


def test_currency_defaults_to_inr():
    h = Header(
        payer_name="X",
        payer_id=None,
        payment_reference="R",
        payment_date=date(2026, 9, 5),
        payment_method=None,
        total_paid_amount=Decimal("1.00"),
    )
    assert h.currency == "INR"


def test_amounts_reject_float_noise():
    li = LineItem.model_validate(
        {
            "invoice_number": "INV-1",
            "invoice_date": None,
            "invoice_amount": "10.10",
            "discount_taken": None,
            "deduction_amount": None,
            "deduction_reason": None,
            "amount_paid": "10.10",
        }
    )
    assert li.invoice_amount == Decimal("10.10")


def test_missing_required_field_rejected():
    bad = _valid_payload_dict()
    del bad["header"]["payment_reference"]
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(bad)


def test_currency_must_be_three_letters():
    bad = _valid_payload_dict()
    bad["header"]["currency"] = "Rupees"
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(bad)


def test_currency_rejects_non_alphabetic():
    bad = _valid_payload_dict()
    bad["header"]["currency"] = "1N5"
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(bad)


def test_currency_is_uppercased():
    d = _valid_payload_dict()
    d["header"]["currency"] = "inr"
    assert RemittancePayload.model_validate(d).header.currency == "INR"


def test_json_schema_is_dict_with_defs():
    assert isinstance(CANONICAL_JSON_SCHEMA, dict)
    assert CANONICAL_JSON_SCHEMA["title"] == "RemittancePayload"
    assert "$defs" in CANONICAL_JSON_SCHEMA
