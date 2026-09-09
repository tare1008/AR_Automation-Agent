from ar_pipeline.extract.html_table import extract_html_tables
from tests.fixtures.loader import EMAILS_DIR, eml_to_graph


def _body_html(name: str) -> str:
    msg, _ = eml_to_graph(EMAILS_DIR / f"{name}.eml")
    return msg.body_html


def test_extract_html_tables_fluorochem() -> None:
    raw = extract_html_tables(_body_html("02_fluorochem_body_table"))

    assert len(raw.tables) >= 1
    main = max(raw.tables, key=len)
    assert len(main) >= 14

    assert any("FCI2510007033" in row and "1,452,299.16" in row for row in main)

    all_rows = [row for table in raw.tables for row in table]
    assert any(
        "LESS: IT TDS on Goods 194Q (0.1%)" in row and "17,832.00" in row for row in all_rows
    ) or ("LESS: IT TDS on Goods 194Q (0.1%)" in raw.text and "17,832.00" in raw.text)

    assert "remitted the payment of Rs.1,36,70,691.00" in raw.text
    assert "STBK52026021813360279" in raw.text


def test_extract_html_tables_sunrise_multi_payment() -> None:
    raw = extract_html_tables(_body_html("04_sunrise_body_multi_payment"))

    table_count = raw.meta["table_count"]
    assert isinstance(table_count, int) and table_count >= 2

    cells = [cell for table in raw.tables for row in table for cell in row]
    assert "SXE2510051648" in cells
    assert "5,01,808.11" in cells

    assert "STBK52026040800971738" in raw.text
    assert "STBK52026041300482210" in raw.text
