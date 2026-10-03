from ar_pipeline.extract.pdf import extract_pdf
from tests.fixtures.loader import EMAILS_DIR, eml_to_graph


def _pdf_bytes(name: str) -> bytes:
    _, atts = eml_to_graph(EMAILS_DIR / f"{name}.eml")
    att = next(a for a in atts if a.name.lower().endswith(".pdf"))
    return att.content


def test_extract_pdf_single_page() -> None:
    raw = extract_pdf(_pdf_bytes("01_fwd_bank_advice_pdf"))

    assert raw.meta["page_count"] == 1
    assert "Remittance amount: INR 6,633,624.61" in raw.text
    assert "ACM2510006275" in raw.text
    assert "Other reference: GTBN52026021920531478" in raw.text


def test_extract_pdf_two_pages() -> None:
    raw = extract_pdf(_pdf_bytes("03_fwd_multiline_pdf"))

    assert raw.meta["page_count"] == 2
    assert "Total" in raw.text
    assert "CBB2510004583" in raw.text
    assert "2510004583DISCO" in raw.text
    assert "RTGS PAYMENT" in raw.text


def test_advice_table_is_cleaned_and_merged_across_pages() -> None:
    from tests.tables.advice_pdf import HEADER, build_advice_pdf

    raw = extract_pdf(build_advice_pdf())

    assert raw.meta["page_count"] == 2
    assert len(raw.tables) == 1
    table = raw.tables[0]
    assert (
        table[0]
        == ["BillNo", "BillDate", "A/C RefNo", "GrossAmount", "Adv/Debit", "TDS", "Net Payment"]
        == HEADER
    )
    numbers = [row[0] for row in table[1:]]
    assert numbers[0] == "CBB2510004583"  # wrapped cell glued, no space
    assert numbers[6] == "CBB25100035016"
    assert numbers[10] == "2510035016DISCO"  # continuation row glued across pages
    assert "CO" not in numbers
    assert table[-1][0] == "Total"
    assert len(table) == 1 + 20 + 1  # header + 20 lines + total
    # text outside the table only: the words line is there, table rows are not
    from ar_pipeline.tables.words import amount_in_words
    from tests.tables.advice_pdf import NET

    assert amount_in_words(raw.text) == NET  # Task 2 is done before this task
    assert "SIXTY EIGHT" in raw.text
    assert "Document No : 1500009001" in raw.text
    assert "RTGS/NEFT Reference : RTGS PAYMENT" in raw.text
    assert "1,248,750.00" not in raw.text
