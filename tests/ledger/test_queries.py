from datetime import date
from decimal import Decimal

from ar_pipeline.ledger.queries import filter_rows, invoice_detail, list_invoice_rows, summarize


def test_rows_carry_balance_awaiting_and_progress(
    db_session, make_invoice, post_payment, make_extraction
):
    inv = make_invoice("INV-1", "100")
    post_payment(inv, "25")
    make_extraction(invoice_number="INV-1", invoice_amount="50", amount_paid="50")
    (row,) = list_invoice_rows(db_session)
    assert (row.paid, row.outstanding, row.awaiting_review) == (
        Decimal("25"),
        Decimal("75"),
        Decimal("50"),
    )
    assert row.status == "partially_paid"
    assert row.progress_pct == 25
    assert row.needs_attention is False


def test_summary_and_filters(db_session, make_invoice, post_payment):
    a = make_invoice("A-1", "100")
    b = make_invoice("B-1", "100")
    make_invoice("C-1", "100")
    post_payment(a, "40")
    post_payment(b, "130")
    rows = list_invoice_rows(db_session)
    s = summarize(rows)
    assert s["outstanding_total"] == Decimal("160")  # 60 + 100; overpaid B doesn't subtract
    assert (s["partially_paid"], s["paid"], s["attention"]) == (1, 0, 1)
    assert [r.invoice_number for r in filter_rows(rows, "attention")] == ["B-1"]
    assert [r.invoice_number for r in filter_rows(rows, "outstanding")] == ["A-1", "C-1"]


def test_detail_running_balance(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100", paid_before_import="10")
    for amt, day in (("25", 1), ("50", 4), ("30", 8)):
        post_payment(inv, amt, payment_date=date(2026, 10, day))
    detail = invoice_detail(db_session, inv.id)
    assert detail.paid_before_import == Decimal("10")
    assert [e.balance_after for e in detail.entries] == [
        Decimal("65"),
        Decimal("15"),
        Decimal("-15"),
    ]
    assert detail.row.status == "overpaid"


def test_after_this_ignores_a_payment_in_another_currency(
    db_session, make_invoice, make_extraction
):
    from ar_pipeline.ledger.queries import line_ledgers

    make_invoice("INV-1", "100")
    ext = make_extraction(invoice_amount="100", amount_paid="25", currency="USD")
    (strip,) = line_ledgers(db_session, ext)
    assert strip.outstanding == Decimal("100.00")
    assert strip.after_this == Decimal("100.00")


def test_after_this_subtracts_a_same_currency_payment(db_session, make_invoice, make_extraction):
    from ar_pipeline.ledger.queries import line_ledgers

    make_invoice("INV-1", "100")
    ext = make_extraction(invoice_amount="100", amount_paid="25")
    (strip,) = line_ledgers(db_session, ext)
    assert strip.after_this == Decimal("75.00")
