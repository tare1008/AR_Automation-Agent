from ar_pipeline.ledger.checks import check_against_ledger
from ar_pipeline.schema.canonical import RemittancePayload
from tests.ledger.conftest import canonical_for


def _flags(db_session, **canon) -> list[str]:
    return check_against_ledger(
        db_session, RemittancePayload.model_validate(canonical_for(**canon))
    )


def test_partial_against_a_books_invoice_is_clean(db_session, make_invoice):
    make_invoice("INV-1", "100")
    assert _flags(db_session, invoice_amount="100", amount_paid="25") == []


def test_installment_that_only_describes_the_payment_is_clean(
    db_session, make_invoice, post_payment
):
    inv = make_invoice("INV-1", "100000")
    post_payment(inv, "25000")
    # stub-style line: invoice_amount == amount_paid, i.e. no claim about the invoice total
    assert _flags(db_session, invoice_amount="50000", amount_paid="50000") == []


def test_partial_against_an_unverified_invoice_is_flagged(db_session, make_invoice):
    make_invoice("INV-1", "100", source="email")
    (flag,) = _flags(db_session, invoice_amount="100", amount_paid="25")
    assert flag.startswith("line 0: INV-1")
    assert "partial payment ₹25.00 of ₹100.00" in flag
    assert "not your books" in flag


def test_partial_against_an_unknown_invoice_is_flagged(db_session):
    (flag,) = _flags(db_session, invoice_number="NEW-9", invoice_amount="100", amount_paid="25")
    assert "partial payment ₹25.00 of ₹100.00" in flag


def test_full_payment_on_unknown_invoice_before_any_csv_is_clean(db_session):
    assert _flags(db_session, invoice_number="NEW-9", invoice_amount="100", amount_paid="100") == []


def test_overpayment(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100")
    post_payment(inv, "25")
    post_payment(inv, "50")
    (flag,) = _flags(db_session, invoice_amount="30", amount_paid="30")
    assert "pays ₹30.00 but only ₹25.00 outstanding" in flag
    assert "overpaid by ₹5.00" in flag


def test_already_paid(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100")
    post_payment(inv, "100")
    (flag,) = _flags(db_session, invoice_amount="10", amount_paid="10")
    assert flag == "line 0: INV-1 is already fully paid"


def test_amount_differs_from_books(db_session, make_invoice):
    make_invoice("INV-1", "120")
    (flag,) = _flags(db_session, invoice_amount="100", amount_paid="90", tds="5")
    assert flag == "line 0: INV-1 — email says invoice ₹100.00, your books say ₹120.00"


def test_amount_differs_from_earlier_email(db_session, make_invoice):
    make_invoice("INV-1", "120", source="email")
    # the line must claim a total (invoice_amount != settled) for the flag to apply
    flags = _flags(db_session, invoice_amount="100", amount_paid="80")
    assert "line 0: INV-1 — email says invoice ₹100.00, an earlier email said ₹120.00" in flags


def test_possible_duplicate(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100")
    post_payment(inv, "50", reference="UTR-1")
    flags = _flags(db_session, invoice_amount="50", amount_paid="50", reference="UTR-1")
    assert "line 0: INV-1 — reference UTR-1 for ₹50.00 was already approved" in flags


def test_currency_mismatch(db_session, make_invoice):
    make_invoice("INV-1", "100")
    (flag,) = _flags(db_session, invoice_amount="100", amount_paid="100", currency="USD")
    assert flag == "line 0: INV-1 — payment in USD, invoice in INR; not applied to the balance"


def test_payer_mismatch(db_session, make_invoice):
    make_invoice("INV-1", "100", payer="Meridian Steel Pvt Ltd")
    (flag,) = _flags(
        db_session, invoice_amount="100", amount_paid="100", payer_name="Arcadia Foods"
    )
    assert flag == (
        "line 0: INV-1 — invoice belongs to Meridian Steel Pvt Ltd; payment is from Arcadia Foods"
    )


def test_near_match_replaces_not_in_books(db_session, make_invoice):
    make_invoice("MST-2026-7801", "100")
    (flag,) = _flags(
        db_session, invoice_number="MST/2026/780", invoice_amount="100", amount_paid="100"
    )
    assert flag == "line 0: MST/2026/780 not found — did you mean MST-2026-7801?"


def test_not_in_books_once_a_csv_exists(db_session, make_invoice):
    make_invoice("ZZZ-1", "100")
    (flag,) = _flags(db_session, invoice_number="INV-9999", invoice_amount="100", amount_paid="100")
    assert flag == "line 0: INV-9999 isn't in your open invoices"


def test_email_only_invoices_do_not_trigger_not_in_books(db_session, make_invoice):
    make_invoice("ZZZ-1", "100", source="email")
    assert (
        _flags(db_session, invoice_number="INV-9999", invoice_amount="100", amount_paid="100") == []
    )
