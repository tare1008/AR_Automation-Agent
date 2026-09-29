from decimal import Decimal

import pytest

from ar_pipeline.ledger.csv_import import CsvImportError, import_open_invoices
from ar_pipeline.ledger.posting import find_invoice

HEADER = "invoice_number,payer_name,invoice_date,invoice_amount,currency,outstanding_amount\n"


def _run(db_session, body: str):
    return import_open_invoices(db_session, (HEADER + body).encode())


def test_good_file(db_session):
    r = _run(
        db_session,
        'MST-2026-7550,Orion Fabricators Pvt Ltd,2026-09-01,"1,80,000.00",INR,\n'
        "MST-2026-7601,,2026-09-05,110000,,60000\n",
    )
    assert (r.imported, r.updated, r.skipped) == (2, 0, [])
    inv = find_invoice(db_session, "MST-2026-7601")
    assert inv.source == "books"
    assert inv.currency == "INR"
    assert inv.paid_before_import == Decimal("50000.00")
    assert find_invoice(db_session, "mst/2026/7550").amount == Decimal("180000.00")


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (",,,100,,", "row 2: invoice_number is blank"),
        ("A-1,,,abc,,", "row 2: invoice_amount is not a number"),
        ("A-1,,,0,,", "row 2: invoice_amount must be more than 0"),
        ("A-1,,,100,,-1", "row 2: outstanding_amount must be between 0 and invoice_amount"),
        ("A-1,,,100,,150", "row 2: outstanding_amount must be between 0 and invoice_amount"),
        ("A-1,,01/09/2026,100,,", "row 2: invoice_date must be YYYY-MM-DD"),
        ("A-1,,,100,RUPEES,", "row 2: currency must be a 3-letter code like INR"),
    ],
)
def test_bad_rows_are_skipped_with_a_numbered_reason(db_session, row, reason):
    r = _run(db_session, row + "\n")
    assert r.imported == 0
    assert r.skipped == [reason]


def test_duplicate_numbers_in_the_file_skip_both_rows(db_session):
    r = _run(db_session, "A-1,,,100,,\na/1,,,200,,\nB-2,,,50,,\n")
    assert r.imported == 1
    assert r.skipped == [
        "row 2: A-1 appears more than once in the file",
        "row 3: a/1 appears more than once in the file",
    ]


def test_unverified_invoice_is_upgraded_and_noted(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100", source="email")
    post_payment(inv, "25")
    r = _run(db_session, "INV-1,Acme Corp,,120,,\n")
    assert (r.imported, r.updated) == (0, 1)
    db_session.refresh(inv)
    assert (inv.source, inv.amount) == ("books", Decimal("120.00"))
    assert inv.note == "email said ₹100.00, your books say ₹120.00"


def test_missing_required_column(db_session):
    with pytest.raises(CsvImportError, match="missing column"):
        import_open_invoices(db_session, b"invoice_number,payer_name\nA-1,x\n")


def test_too_big(db_session):
    with pytest.raises(CsvImportError, match="1 MB"):
        import_open_invoices(db_session, b"x" * 1_000_001)


def test_too_many_rows(db_session):
    body = "".join(f"A-{i},,,1,,\n" for i in range(5001))
    with pytest.raises(CsvImportError, match="5,000"):
        _run(db_session, body)


def test_not_utf8(db_session):
    with pytest.raises(CsvImportError, match="UTF-8"):
        import_open_invoices(db_session, HEADER.encode() + b"\xff\xfe\n")
