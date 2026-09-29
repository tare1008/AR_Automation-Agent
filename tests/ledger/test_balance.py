from decimal import Decimal

import pytest

from ar_pipeline.ledger.balance import awaiting_by_key, balance_for, line_settled, status_for


@pytest.mark.parametrize(
    ("amount", "paid", "status"),
    [
        ("100", "0", "open"),
        ("100", "25", "partially_paid"),
        ("100", "100", "paid"),
        ("100", "99.99", "paid"),  # within tolerance
        ("100", "100.02", "paid"),
        ("100", "105", "overpaid"),
    ],
)
def test_status_for(amount, paid, status):
    assert status_for(Decimal(amount), Decimal(paid)) == status


def test_line_settled_counts_deductions():
    line = {"amount_paid": "90.00", "deductions": [{"type": "tds", "amount": "10.00"}]}
    assert line_settled(line) == Decimal("100.00")


def test_installments_add_up_to_overpaid(make_invoice, post_payment, db_session):
    inv = make_invoice(amount="100")
    for amt in ("25", "50", "30"):
        post_payment(inv, amt)
    bal = balance_for(db_session, inv)
    assert bal.paid == Decimal("105")
    assert bal.outstanding == Decimal("-5")
    assert bal.status == "overpaid"


def test_paid_before_import_counts(make_invoice, post_payment, db_session):
    inv = make_invoice(amount="100", paid_before_import="40")
    post_payment(inv, "10")
    bal = balance_for(db_session, inv)
    assert bal.paid == Decimal("50")
    assert bal.status == "partially_paid"


def test_other_currency_payment_is_excluded(make_invoice, post_payment, db_session):
    inv = make_invoice(amount="100")
    post_payment(inv, "60", currency="USD")
    assert balance_for(db_session, inv).status == "open"


def test_awaiting_by_key_sums_pending_lines_only(make_extraction, db_session):
    a = make_extraction(invoice_number="INV-1", invoice_amount="100", amount_paid="40")
    make_extraction(invoice_number="inv/1", invoice_amount="100", amount_paid="10", tds="5")
    make_extraction(status="approved", invoice_number="INV-1", amount_paid="99")
    assert awaiting_by_key(db_session) == {"INV1": Decimal("55")}
    assert awaiting_by_key(db_session, exclude=a.id) == {"INV1": Decimal("15")}
