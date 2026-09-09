from ar_pipeline.extract.excel import extract_excel
from tests.fixtures.loader import EMAILS_DIR, eml_to_graph


def _xlsx_bytes(name: str) -> bytes:
    _, atts = eml_to_graph(EMAILS_DIR / f"{name}.eml")
    att = next(a for a in atts if a.name.lower().endswith(".xlsx"))
    return att.content


def test_extract_excel_zenith() -> None:
    raw = extract_excel(_xlsx_bytes("06_zenith_excel"))

    assert raw.meta["sheet_names"] == ["Sheet1"]

    all_rows = [row for table in raw.tables for row in table]
    assert any("ZCC2610000038" in row for row in all_rows)
    assert any("ZCC2610000037" in row for row in all_rows)

    total_rows = [row for row in all_rows if "TOTAL" in row]
    assert total_rows
    assert any(
        "ZCC/AXIS/31/2026" in row and any("9433014.543" in cell for cell in row)
        for row in total_rows
    )

    assert "ZCC2610000038" in raw.text
    assert raw.to_payload()["meta"]["sheet_names"] == ["Sheet1"]  # type: ignore[index]
