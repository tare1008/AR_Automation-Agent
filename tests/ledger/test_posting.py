from decimal import Decimal

from sqlalchemy import select

from ar_pipeline.db.models import Invoice, InvoicePayment
from ar_pipeline.ledger.posting import backfill, find_invoice, post_extraction
from ar_pipeline.pipeline.routing import approve_and_queue


def _payments(db_session):
    return db_session.scalars(select(InvoicePayment)).all()


def test_posts_one_row_per_line_against_a_books_invoice(db_session, make_invoice, make_extraction):
    inv = make_invoice("INV-1", "100")
    ext = make_extraction(invoice_number="inv/1", invoice_amount="100", amount_paid="90", tds="10")
    assert post_extraction(db_session, ext) == 1
    (row,) = _payments(db_session)
    assert row.invoice_id == inv.id
    assert (row.amount_paid, row.deductions_total, row.settled) == (
        Decimal("90.00"),
        Decimal("10.00"),
        Decimal("100.00"),
    )
    assert row.payment_reference == "UTR-1"
    assert row.currency == "INR"


def test_posting_twice_does_not_double_count(db_session, make_invoice, make_extraction):
    make_invoice("INV-1", "100")
    ext = make_extraction(amount_paid="25", invoice_amount="100")
    post_extraction(db_session, ext)
    assert post_extraction(db_session, ext) == 0
    assert len(_payments(db_session)) == 1


def test_unknown_invoice_is_created_unverified_from_the_stated_amount(db_session, make_extraction):
    ext = make_extraction(
        invoice_number="ORB-2026-3390",
        invoice_amount="8400",
        amount_paid="8400",
        payer_name="Orbital Components Ltd",
    )
    post_extraction(db_session, ext)
    inv = find_invoice(db_session, "orb 2026 3390")
    assert inv is not None
    assert (inv.source, inv.amount, inv.payer_name) == (
        "email",
        Decimal("8400.00"),
        "Orbital Components Ltd",
    )


def test_second_unverified_posting_reuses_the_invoice(db_session, make_extraction):
    post_extraction(
        db_session, make_extraction(invoice_number="NEW-1", amount_paid="10", invoice_amount="30")
    )
    post_extraction(
        db_session, make_extraction(invoice_number="new/1", amount_paid="20", invoice_amount="30")
    )
    assert len(db_session.scalars(select(Invoice)).all()) == 1
    assert len(_payments(db_session)) == 2


def test_approve_and_queue_posts(db_session, make_invoice, make_extraction):
    make_invoice("INV-1", "100")
    ext = make_extraction(amount_paid="25", invoice_amount="100")
    approve_and_queue(db_session, ext, reviewed_by="Asha")
    assert len(_payments(db_session)) == 1


def test_not_a_remittance_or_empty_canonical_posts_nothing(db_session, make_extraction):
    ext = make_extraction()
    ext.is_remittance = False
    assert post_extraction(db_session, ext) == 0
    ext2 = make_extraction()
    ext2.canonical = {}
    assert post_extraction(db_session, ext2) == 0


def test_backfill_posts_approved_only_and_is_rerunnable(db_session, make_invoice, make_extraction):
    make_invoice("INV-1", "100")
    make_extraction(status="approved", amount_paid="25", invoice_amount="100")
    make_extraction(status="pending_review", amount_paid="25", invoice_amount="100")
    assert backfill(db_session) == (1, 1)
    assert backfill(db_session) == (0, 0)
