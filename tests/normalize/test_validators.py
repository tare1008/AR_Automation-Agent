from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ar_pipeline.normalize.validators import CHECK_VERSION, validate_payload
from ar_pipeline.schema.canonical import (
    Deduction,
    Envelope,
    Header,
    LineItem,
    RemittancePayload,
)


def _payload(**overrides: Any) -> RemittancePayload:
    """Build a valid, reconciling RemittancePayload; ``overrides`` perturb it.

    Recognised keys: ``line_items`` (list[LineItem]) and ``header`` (dict of
    Header kwargs merged over the defaults). By default ``total_paid_amount`` is
    derived so the payment-total identity holds.
    """
    line_items = overrides.pop("line_items", None)
    if line_items is None:
        line_items = [
            LineItem(
                invoice_number="INV-1",
                invoice_date=date(2026, 8, 1),
                invoice_amount=Decimal("100.00"),
                deductions=[Deduction(type="tds", amount=Decimal("10.00"))],
                amount_paid=Decimal("90.00"),
            )
        ]

    header_over: dict[str, Any] = dict(overrides.pop("header", {}))
    hdr_deductions = header_over.get("deductions", [])
    sum_lines = sum((li.amount_paid for li in line_items), Decimal("0"))
    sum_hdr = sum((d.amount for d in hdr_deductions), Decimal("0"))

    header_kwargs: dict[str, Any] = dict(
        payer_name="Acme Corp",
        payment_date=date(2026, 9, 1),
        currency="INR",
        total_paid_amount=sum_lines - sum_hdr,
    )
    header_kwargs.update(header_over)

    assert not overrides, f"unknown overrides: {sorted(overrides)}"

    return RemittancePayload(
        envelope=Envelope(
            extraction_id="ext-1",
            source_email_id="em-1",
            extracted_at=datetime(2026, 9, 1, 12, 0, 0),
        ),
        header=Header(**header_kwargs),
        line_items=line_items,
    )


def test_check_version_constant() -> None:
    assert CHECK_VERSION == "2"


def test_clean_payload_has_no_flags() -> None:
    assert validate_payload(_payload()) == []


def test_line_net_identity_off_by_one() -> None:
    flags = validate_payload(
        _payload(
            line_items=[
                LineItem(
                    invoice_number="INV-1",
                    invoice_date=date(2026, 8, 1),
                    invoice_amount=Decimal("100.00"),
                    deductions=[Deduction(type="tds", amount=Decimal("10.00"))],
                    amount_paid=Decimal("91.00"),
                )
            ]
        )
    )
    assert len(flags) == 1
    assert "line 0" in flags[0]


def test_payment_total_off_by_one() -> None:
    flags = validate_payload(_payload(header={"total_paid_amount": Decimal("91.00")}))
    assert len(flags) == 1
    assert "payment total" in flags[0]


def test_duplicate_invoice_numbers() -> None:
    flags = validate_payload(
        _payload(
            line_items=[
                LineItem(
                    invoice_number="DUP",
                    invoice_amount=Decimal("100.00"),
                    deductions=[Deduction(type="tds", amount=Decimal("10.00"))],
                    amount_paid=Decimal("90.00"),
                ),
                LineItem(
                    invoice_number="DUP",
                    invoice_amount=Decimal("50.00"),
                    amount_paid=Decimal("50.00"),
                ),
            ]
        )
    )
    assert flags == ["duplicate invoice number: DUP"]


def test_non_inr_currency_flag() -> None:
    flags = validate_payload(_payload(header={"currency": "USD"}))
    assert len(flags) == 1
    assert "non-INR" in flags[0]


def test_unknown_payment_reference_type_flag() -> None:
    flags = validate_payload(_payload(header={"payment_reference_type": "wire"}))
    assert flags == ["unknown payment_reference_type: wire"]


def test_known_payment_reference_type_is_clean() -> None:
    assert validate_payload(_payload(header={"payment_reference_type": "neft"})) == []


def test_tolerance_within_two_cents_is_clean() -> None:
    assert (
        validate_payload(
            _payload(
                line_items=[
                    LineItem(
                        invoice_number="INV-1",
                        invoice_amount=Decimal("100.00"),
                        deductions=[Deduction(type="tds", amount=Decimal("10.00"))],
                        amount_paid=Decimal("90.01"),
                    )
                ]
            )
        )
        == []
    )


def test_tolerance_beyond_two_cents_flags() -> None:
    flags = validate_payload(
        _payload(
            line_items=[
                LineItem(
                    invoice_number="INV-1",
                    invoice_amount=Decimal("100.00"),
                    deductions=[Deduction(type="tds", amount=Decimal("10.00"))],
                    amount_paid=Decimal("90.03"),
                )
            ]
        )
    )
    assert len(flags) == 1
    assert "line 0" in flags[0]


def test_header_tds_deduction_reconciles() -> None:
    flags = validate_payload(
        _payload(
            line_items=[
                LineItem(
                    invoice_number="INV-1",
                    invoice_amount=Decimal("100.00"),
                    amount_paid=Decimal("100.00"),
                )
            ],
            header={"deductions": [Deduction(type="tds", amount=Decimal("5.00"))]},
        )
    )
    assert flags == []


def test_empty_invoice_number_flag() -> None:
    flags = validate_payload(
        _payload(
            line_items=[
                LineItem(
                    invoice_number="   ",
                    invoice_amount=Decimal("100.00"),
                    deductions=[Deduction(type="tds", amount=Decimal("10.00"))],
                    amount_paid=Decimal("90.00"),
                )
            ]
        )
    )
    assert "line 0: empty invoice number" in flags


def test_payment_date_far_before_invoice_date_flags() -> None:
    flags = validate_payload(
        _payload(header={"payment_date": date(2026, 8, 1) - timedelta(days=500)})
    )
    assert any("payment_date" in f and "invoice_date" in f for f in flags)


def test_payment_date_far_in_future_flags() -> None:
    flags = validate_payload(_payload(header={"payment_date": date(2027, 6, 1)}))
    assert any("payment_date" in f and "after" in f for f in flags)


def test_negative_invoice_amount_flag() -> None:
    flags = validate_payload(
        _payload(
            line_items=[
                LineItem(
                    invoice_number="INV-1",
                    invoice_amount=Decimal("-10.00"),
                    amount_paid=Decimal("90.00"),
                )
            ]
        )
    )
    assert any("negative invoice_amount" in f and "line 0" in f for f in flags)


def test_negative_amount_paid_flag() -> None:
    flags = validate_payload(
        _payload(
            line_items=[
                LineItem(
                    invoice_number="INV-1",
                    invoice_amount=Decimal("100.00"),
                    amount_paid=Decimal("-5.00"),
                )
            ]
        )
    )
    assert any("negative amount_paid" in f and "line 0" in f for f in flags)


def test_negative_total_paid_amount_flag() -> None:
    flags = validate_payload(_payload(header={"total_paid_amount": Decimal("-1.00")}))
    assert any("negative total_paid_amount" in f for f in flags)


def test_missing_payer_name_flag() -> None:
    flags = validate_payload(_payload(header={"payer_name": "   "}))
    assert "header: payer name is missing" in flags


def test_present_payer_name_is_clean() -> None:
    flags = validate_payload(_payload(header={"payer_name": "Acme Corp"}))
    assert "header: payer name is missing" not in flags
