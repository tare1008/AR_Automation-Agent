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


def _two_line_payload(first: str, second: str, *, number2: str = "INV 1") -> RemittancePayload:
    canon = canonical_for(invoice_amount="100", amount_paid=first)
    extra = dict(canon["line_items"][0], invoice_number=number2, amount_paid=second)
    extra["invoice_amount"] = "100"
    canon["line_items"].append(extra)
    return RemittancePayload.model_validate(canon)


def test_lines_sharing_a_key_are_checked_against_the_running_outstanding(db_session, make_invoice):
    make_invoice("INV-1", "100")
    flags = check_against_ledger(db_session, _two_line_payload("60", "50"))
    assert flags == ["line 1: INV 1 — pays ₹50.00 but only ₹40.00 outstanding; overpaid by ₹10.00"]


def test_second_line_after_a_full_first_line_is_already_paid(db_session, make_invoice):
    make_invoice("INV-1", "100")
    flags = check_against_ledger(db_session, _two_line_payload("100", "50"))
    assert flags == ["line 1: INV 1 is already fully paid"]


def _capture_sql(db_session):
    from sqlalchemy import event

    statements: list[str] = []

    def _before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _before)
    return statements, lambda: event.remove(engine, "before_cursor_execute", _before)


def test_matched_invoice_is_row_locked(db_session, make_invoice):
    make_invoice("INV-1", "100")
    statements, stop = _capture_sql(db_session)
    try:
        _flags(db_session, invoice_amount="100", amount_paid="25")
    finally:
        stop()
    assert any("FOR UPDATE" in s and "invoice" in s for s in statements)


def test_unknown_invoice_takes_an_advisory_lock_on_the_key(db_session):
    statements, stop = _capture_sql(db_session)
    try:
        _flags(db_session, invoice_number="NEW-9", invoice_amount="100", amount_paid="100")
    finally:
        stop()
    assert any("pg_advisory_xact_lock" in s for s in statements)


def test_checking_twice_in_one_transaction_does_not_deadlock(db_session, make_invoice):
    make_invoice("INV-1", "100", source="email")
    for _ in range(2):
        assert _flags(db_session, invoice_amount="100", amount_paid="100") == []
        assert (
            _flags(db_session, invoice_number="NEW-9", invoice_amount="100", amount_paid="100")
            == []
        )


def test_refresh_replaces_an_old_format_flag(db_session, make_invoice, make_extraction):
    from ar_pipeline.ledger.checks import refresh_pending_flags

    make_invoice("INV-1", "100", source="email")
    ext = make_extraction(invoice_amount="100", amount_paid="25")
    ext.validation_flags = ["line 0: amount_paid + deductions != invoice_amount"]
    db_session.flush()
    assert refresh_pending_flags(db_session) == 1
    (flag,) = ext.validation_flags
    assert flag.startswith("line 0: INV-1 — partial payment ₹25.00 of ₹100.00")


def test_refresh_clears_a_books_partial(db_session, make_invoice, make_extraction):
    from ar_pipeline.ledger.checks import refresh_pending_flags

    make_invoice("INV-1", "100")
    ext = make_extraction(invoice_amount="100", amount_paid="25")
    ext.validation_flags = ["line 0: INV-1 isn't in your open invoices"]
    approved = make_extraction(status="approved", invoice_amount="100", amount_paid="25")
    approved.validation_flags = ["line 0: stale"]
    db_session.flush()
    assert refresh_pending_flags(db_session) == 1
    assert ext.validation_flags == []
    assert approved.validation_flags == ["line 0: stale"]  # only pending items


def test_refresh_is_zero_when_nothing_changes(db_session, make_invoice, make_extraction):
    from ar_pipeline.ledger.checks import refresh_pending_flags

    make_invoice("INV-1", "100")
    make_extraction(invoice_amount="100", amount_paid="25")
    assert refresh_pending_flags(db_session) == 0


def test_refresh_skips_a_canonical_that_does_not_validate(db_session, make_extraction):
    from ar_pipeline.ledger.checks import refresh_pending_flags

    ext = make_extraction()
    ext.canonical = {"header": {}, "line_items": "nope"}
    ext.validation_flags = ["line 0: old"]
    db_session.flush()
    assert refresh_pending_flags(db_session) == 0
    assert ext.validation_flags == ["line 0: old"]


def test_refresh_keeps_skipped_draft_flags(db_session, make_invoice, make_extraction):
    from ar_pipeline.ledger.checks import refresh_pending_flags

    make_invoice("INV-1", "100")
    ext = make_extraction(invoice_amount="100", amount_paid="25")
    ext.validation_flags = ["line 0: old", "draft 1: schema validation failed: x"]
    db_session.flush()
    assert refresh_pending_flags(db_session) == 1
    assert ext.validation_flags == ["draft 1: schema validation failed: x"]
