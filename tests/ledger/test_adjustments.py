from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from ar_pipeline.db.models import Invoice, InvoicePayment
from ar_pipeline.ledger.adjustments import (
    adjustment_flags,
    adjustment_suggestions,
    match_adjustment,
    resolve_adjustments,
)
from ar_pipeline.ledger.balance import line_target
from ar_pipeline.ledger.checks import check_against_ledger
from ar_pipeline.ledger.matching import number_key
from ar_pipeline.ledger.posting import post_extraction
from ar_pipeline.schema.canonical import RemittancePayload
from tests.ledger.conftest import canonical_for


def _adjustment(number: str, amount: str, applies_to: str | None = None) -> dict:
    return {
        "invoice_number": number,
        "invoice_date": None,
        "invoice_amount": "0",
        "deductions": [{"type": "discount", "amount": amount, "reason": None}],
        "amount_paid": f"-{amount}",
        "kind": "adjustment",
        "applies_to": applies_to,
    }


def _payload(*adjustments: dict) -> dict:
    c = canonical_for(
        invoice_number="CBB2510004583", invoice_amount="1000.00", amount_paid="1000.00"
    )
    c["line_items"] += list(adjustments)
    paid = Decimal("1000.00") - sum(Decimal(a["deductions"][0]["amount"]) for a in adjustments)
    c["header"]["total_paid_amount"] = str(paid)
    return c


def test_match_rules():
    same = ["CBB2510004583", "CBB25100035016"]
    ledger = ["CBB2510004516", "CBB2510004515"]
    assert match_adjustment("2510004583DISCO", same, ledger).target == "CBB2510004583"
    assert match_adjustment("2510004516DISCO", same, ledger).target == "CBB2510004516"
    near = match_adjustment("2510035016DISCO", same, ledger)
    assert near.target is None and near.suggestions == ["CBB25100035016"]
    # a substituted digit is a neighbouring invoice, never a suggestion
    assert match_adjustment("2510004517DISCO", same, ledger) == match_adjustment(
        "9999999999DISCO", same, ledger
    )
    assert match_adjustment("2510004517DISCO", same, ledger).suggestions == []
    assert match_adjustment("12DISCO", same, ledger).suggestions == []


def test_resolve_and_flags(db_session, make_invoice):
    make_invoice("CBB2510004516", amount="300.00")
    payload = RemittancePayload.model_validate(
        _payload(
            _adjustment("2510004583DISCO", "50.00"),
            _adjustment("2510004516DISCO", "20.00"),
            _adjustment("7777777777DISCO", "10.00"),
        )
    )
    resolved = resolve_adjustments(db_session, payload)
    assert [li.applies_to for li in resolved.line_items] == [
        None,
        "CBB2510004583",
        "CBB2510004516",
        None,
    ]
    flags = adjustment_flags(db_session, resolved)
    assert flags == ["line 3: adjustment of ₹10.00 — which invoice does it reduce?"]


def test_suggestion_flag_and_list(db_session):
    payload = RemittancePayload.model_validate(_payload(_adjustment("251000458DISCO", "5.00")))
    assert adjustment_flags(db_session, payload) == [
        "line 1: adjustment of ₹5.00 — did you mean CBB2510004583?"
    ]
    assert adjustment_suggestions(db_session, payload, 1) == ["CBB2510004583"]


def test_ledger_checks_skip_adjustment_numbers(db_session, make_invoice):
    make_invoice("CBB2510004583", amount="1000.00")
    payload = RemittancePayload.model_validate(
        _payload(_adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"))
    )
    flags = check_against_ledger(db_session, payload)
    assert not any("2510004583DISCO" in f for f in flags)
    # the invoice is settled in full by line 0, so the adjustment over-settles it
    assert "line 1: adjustment reduces CBB2510004583, which is already fully paid" in flags


def test_line_target():
    assert line_target(_adjustment("1DISCO", "5.00", "INV-9")) == ("INV-9", Decimal("5.00"))
    assert line_target(
        {"invoice_number": "INV-1", "amount_paid": "90", "deductions": [{"amount": "10"}]}
    ) == ("INV-1", Decimal("100"))


def test_posting_applies_adjustments_to_their_invoice(db_session, seed_extraction):
    ext = seed_extraction(
        _payload(
            _adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"),
            _adjustment("7777777777DISCO", "10.00"),
        )
    )
    assert post_extraction(db_session, ext) == 2
    invoice = db_session.scalar(
        select(Invoice).where(Invoice.number_key == number_key("CBB2510004583"))
    )
    rows = db_session.scalars(
        select(InvoicePayment)
        .where(InvoicePayment.invoice_id == invoice.id)
        .order_by(InvoicePayment.line_index)
    ).all()
    assert [(r.kind, r.settled) for r in rows] == [
        ("payment", Decimal("1000.00")),
        ("adjustment", Decimal("50.00")),
    ]
    assert (
        db_session.scalar(
            select(Invoice).where(Invoice.number_key == number_key("2510004583DISCO"))
        )
        is None
    )
    # R7: line 0 settles the invoice in full, so the adjustment is flagged (posting unchanged)
    fresh = RemittancePayload.model_validate(
        _payload(_adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"))
    )
    assert "line 1: adjustment reduces CBB2510004583, which is already fully paid" in (
        check_against_ledger(db_session, fresh)
    )


def test_adjustment_cross_currency_flag(db_session, make_invoice):
    make_invoice("CBB2510004583", amount="1000.00", currency="USD")
    payload = RemittancePayload.model_validate(
        _payload(_adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"))
    )
    flags = check_against_ledger(db_session, payload)
    assert (
        "line 1: adjustment of ₹50.00 — payment in INR, invoice in USD; not applied to the balance"
        in flags
    )


def test_adjustment_overpay_flag(db_session, make_invoice):
    make_invoice("CBB2510004583", amount="1000.00", paid_before_import="980")
    c = canonical_for(invoice_number="OTHER-1", invoice_amount="10.00", amount_paid="10.00")
    c["line_items"].append(_adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"))
    payload = RemittancePayload.model_validate(c)
    flags = check_against_ledger(db_session, payload)
    assert (
        "line 1: adjustment reduces CBB2510004583 by ₹50.00 but only ₹20.00 outstanding; "
        "overpaid by ₹30.00" in flags
    )


def test_posting_adjustments_is_idempotent(db_session, seed_extraction):
    ext = seed_extraction(
        _payload(_adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"))
    )
    assert post_extraction(db_session, ext) == 2
    assert post_extraction(db_session, ext) == 0
    assert db_session.query(InvoicePayment).count() == 2


def test_no_adjustment_lines_means_no_query():
    payload = RemittancePayload.model_validate(_payload())
    # session=None: any DB query would raise
    assert resolve_adjustments(None, payload) == payload  # type: ignore[arg-type]
    assert adjustment_flags(None, payload) == []  # type: ignore[arg-type]


def test_same_payment_target_is_balance_checked(db_session):
    # R7: no books — the target is only an invoice line of this payment
    payload = RemittancePayload.model_validate(
        _payload(_adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"))
    )
    flags = check_against_ledger(db_session, payload)
    assert "line 1: adjustment reduces CBB2510004583, which is already fully paid" in flags


def test_same_payment_target_overpaid_by(db_session):
    c = canonical_for(
        invoice_number="CBB2510004583", invoice_amount="1000.00", amount_paid="980.00"
    )
    c["line_items"].append(_adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"))
    flags = check_against_ledger(db_session, RemittancePayload.model_validate(c))
    assert (
        "line 1: adjustment reduces CBB2510004583 by ₹50.00 but only ₹20.00 outstanding; "
        "overpaid by ₹30.00" in flags
    )


def _cross_payer_payload(applies_to: str | None = None) -> RemittancePayload:
    c = canonical_for(invoice_number="OTHER-1", invoice_amount="10.00", amount_paid="10.00")
    c["line_items"].append(_adjustment("DN/2025/4583", "50.00", applies_to=applies_to))
    return RemittancePayload.model_validate(c)


def test_cross_payer_exact_match_is_only_a_suggestion(db_session, make_invoice):
    # R8
    make_invoice("SO/2025/4583", amount="500.00", payer="Other Party Ltd")
    resolved = resolve_adjustments(db_session, _cross_payer_payload())
    assert resolved.line_items[1].applies_to is None
    assert adjustment_suggestions(db_session, resolved, 1) == ["SO/2025/4583"]
    assert "line 1: adjustment of ₹50.00 — did you mean SO/2025/4583?" in adjustment_flags(
        db_session, resolved
    )


def test_same_or_unknown_payer_exact_match_still_resolves(db_session, make_invoice):
    make_invoice("SO/2025/4583", amount="500.00", payer=None)
    resolved = resolve_adjustments(db_session, _cross_payer_payload())
    assert resolved.line_items[1].applies_to == "SO/2025/4583"


def test_adjustment_against_another_payers_invoice_is_flagged(db_session, make_invoice):
    make_invoice("SO/2025/4583", amount="500.00", payer="Other Party Ltd")
    flags = check_against_ledger(db_session, _cross_payer_payload(applies_to="SO/2025/4583"))
    assert (
        "line 1: adjustment reduces SO/2025/4583, which belongs to Other Party Ltd; "
        "payment is from Acme Corp" in flags
    )


def test_unconfirmed_exact_target_is_offered_as_a_suggestion(db_session, make_invoice):
    # R9.1: applies_to empty (e.g. the invoice was imported after the read)
    make_invoice("CBB2510004516", amount="300.00")
    payload = RemittancePayload.model_validate(_payload(_adjustment("2510004516DISCO", "20.00")))
    assert adjustment_suggestions(db_session, payload, 1) == ["CBB2510004516"]


def test_adjustment_rows_are_not_duplicates_of_a_payment(db_session, make_invoice, seed_extraction):
    # R9.4: an adjustment row (amount_paid 0) with the same reference is not a repeat payment
    inv = make_invoice("INV-1", amount="300.00")
    ext = seed_extraction(canonical_for())
    db_session.add(
        InvoicePayment(
            invoice_id=inv.id,
            extraction_id=ext.id,
            line_index=1,
            kind="adjustment",
            amount_paid=Decimal("0"),
            deductions_total=Decimal("20.00"),
            settled=Decimal("20.00"),
            currency="INR",
            payment_reference="UTR-1",
        )
    )
    db_session.flush()
    payload = RemittancePayload.model_validate(
        canonical_for(invoice_amount="100.00", amount_paid="0.00", tds="100.00")
    )
    flags = check_against_ledger(db_session, payload)
    assert not any("was already approved" in f for f in flags)
