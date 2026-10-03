from __future__ import annotations

from decimal import Decimal

from ar_pipeline.extract.pdf import extract_pdf
from ar_pipeline.tables.mapping import (
    apply_mapping,
    find_line_table,
    header_signature,
    keyword_mapping,
    validate_mapping,
)
from ar_pipeline.tables.models import ColumnAssignment, MappingOutput
from tests.tables.advice_pdf import HEADER, build_advice_pdf

EXPECTED = {
    "invoice_number": 0,
    "invoice_date": 1,
    "invoice_amount": 3,
    "adjustment": 4,
    "tds": 5,
    "amount_paid": 6,
}


def _raws():
    return [extract_pdf(build_advice_pdf()).to_payload()]


def test_finds_the_one_line_table_and_its_total_row():
    table = find_line_table(_raws())
    assert table is not None
    assert table.header == HEADER
    assert len(table.rows) == 20
    assert table.total_row is not None and table.total_row[0] == "Total"


def test_small_or_duplicate_tables_are_not_line_tables():
    small = {"text": "", "tables": [[["Invoice", "Amount", "Net"], ["A1", "1.00", "1.00"]]]}
    assert find_line_table([small]) is None
    raws = _raws()
    assert find_line_table(raws + raws) is None  # two candidates: not "one clear table"


def test_keyword_mapping_reads_glued_headers():
    assert keyword_mapping(HEADER) == EXPECTED
    assert keyword_mapping(
        ["Bill No", "Bill Date", "A/C RefNo", "Gross Amount", "TDS", "Net Payment"]
    ) == {
        "invoice_number": 0,
        "invoice_date": 1,
        "invoice_amount": 3,
        "tds": 4,
        "amount_paid": 5,
    }
    assert keyword_mapping(["Name", "City"]) is None


def test_signature_ignores_case_spacing_and_punctuation():
    assert header_signature(HEADER) == header_signature(
        ["Bill No", "Bill date", "A/C Ref No", "Gross Amount", "Adv / Debit", "tds", "NetPayment"]
    )


def test_validate_mapping_rejects_bad_ai_output():
    good = MappingOutput(
        is_line_table=True, columns=[ColumnAssignment(index=i, role=r) for r, i in EXPECTED.items()]
    )
    assert validate_mapping(good, HEADER) == EXPECTED
    assert validate_mapping(MappingOutput(is_line_table=False), HEADER) is None
    twice = MappingOutput(
        is_line_table=True,
        columns=[
            ColumnAssignment(index=0, role="invoice_number"),
            ColumnAssignment(index=1, role="invoice_number"),
            ColumnAssignment(index=6, role="amount_paid"),
        ],
    )
    assert validate_mapping(twice, HEADER) is None
    out_of_range = MappingOutput(
        is_line_table=True,
        columns=[
            ColumnAssignment(index=0, role="invoice_number"),
            ColumnAssignment(index=9, role="amount_paid"),
        ],
    )
    assert validate_mapping(out_of_range, HEADER) is None


def test_apply_mapping_copies_every_row_exactly():
    table = find_line_table(_raws())
    mapped = apply_mapping(table, EXPECTED)
    lines = mapped.lines
    assert len(lines) == 20
    first = lines[0]
    assert first.kind == "invoice" and first.invoice_number == "CBB2510004583"
    assert first.invoice_amount == Decimal("1250000.00")
    assert [(d.type, d.amount) for d in first.deductions] == [("tds", Decimal("1250.00"))]
    assert first.amount_paid == Decimal("1248750.00")
    adj = lines[1]
    assert adj.kind == "adjustment" and adj.invoice_number == "2510004583DISCO"
    assert adj.invoice_amount == 0 and adj.amount_paid == Decimal("-12500.00")
    assert [(d.type, d.amount) for d in adj.deductions] == [("discount", Decimal("12500.00"))]
    assert sum(1 for li in lines if li.kind == "adjustment") == 15
    assert sum(li.amount_paid for li in lines) == Decimal("6809764.78")
    assert mapped.column_totals == {
        "invoice_amount": Decimal("6956120.90"),
        "adjustment": Decimal("139400.00"),
        "tds": Decimal("6956.12"),
        "amount_paid": Decimal("6809764.78"),
    }


def test_negative_gross_rows_are_adjustments_too():
    header = ["Bill No", "Bill Date", "Gross Amount", "TDS", "Net Payment"]
    rows = [["CBB1", "01.01.2026", "1,000.00", "1.00", "999.00"]] * 5 + [
        ["1DISCO", "01.01.2026", "-15,924.00", "0.00", "-15,924.00"]
    ]
    table = find_line_table([{"text": "", "tables": [[header, *rows]]}])
    mapped = apply_mapping(table, keyword_mapping(header))
    assert mapped.lines[-1].kind == "adjustment"
    assert mapped.lines[-1].amount_paid == Decimal("-15924.00")


def test_rows_with_different_bank_references_are_not_one_payment():
    import pytest

    from ar_pipeline.tables.mapping import MappingError

    header = ["Invoice No", "UTR", "Amount", "Net Paid"]
    rows = [[f"INV{i}", f"UTR{i}", "10.00", "10.00"] for i in range(6)]
    table = find_line_table([{"text": "", "tables": [[header, *rows]]}])
    with pytest.raises(MappingError):
        apply_mapping(table, keyword_mapping(header))


def test_two_thousand_rows():
    header = ["Bill No", "Gross Amount", "TDS", "Net Payment"]
    rows = [[f"CBB{i:06d}", "1,000.00", "1.00-", "999.00"] for i in range(2000)]
    table = find_line_table([{"text": "", "tables": [[header, *rows]]}])
    mapped = apply_mapping(table, keyword_mapping(header))
    assert len(mapped.lines) == 2000
    assert sum(li.amount_paid for li in mapped.lines) == Decimal("1998000.00")


def _table(header, rows):
    return find_line_table([{"text": "", "tables": [[header, *rows]]}])


def test_total_row_followed_by_words_row_and_subtotals():
    header = ["Bill No", "Gross Amount", "Net Payment"]
    rows = [[f"B{i}", "10.00", "10.00"] for i in range(6)]
    rows.insert(3, ["Sub Total", "30.00", "30.00"])
    rows += [["Total", "60.00", "60.00"], ["Rupees Sixty only", "", ""]]
    table = _table(header, rows)
    assert table.total_row is not None and table.total_row[0] == "Total"
    assert len(table.rows) == 6
    mapped = apply_mapping(table, keyword_mapping(header))
    assert len(mapped.lines) == 6
    assert mapped.column_totals == {
        "invoice_amount": Decimal("60.00"),
        "amount_paid": Decimal("60.00"),
    }


def test_more_keyword_headers():
    assert keyword_mapping(["Doc No", "Doc Date", "Bill Amt", "Deduction", "Paid Amount"]) == {
        "invoice_number": 0,
        "invoice_date": 1,
        "invoice_amount": 2,
        "other_deduction": 3,
        "amount_paid": 4,
    }
    assert keyword_mapping(["Bill No", "Bill Date", "Amount", "TDS", "Net Amount"]) == {
        "invoice_number": 0,
        "invoice_date": 1,
        "invoice_amount": 2,
        "tds": 3,
        "amount_paid": 4,
    }


def test_negative_gross_with_positive_net_is_adjustment():
    header = ["Bill No", "Bill Date", "Gross Amount", "TDS", "Net Payment"]
    rows = [["B1", "01.01.2026", "100.00", "0", "100.00"]] * 5 + [
        ["CN1", "01.01.2026", "-500", "0", "500"]
    ]
    mapped = apply_mapping(_table(header, rows), keyword_mapping(header))
    assert mapped.lines[-1].kind == "adjustment"
    assert mapped.lines[-1].amount_paid == Decimal("-500")
