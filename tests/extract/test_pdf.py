from ar_pipeline.extract.pdf import extract_pdf
from tests.fixtures.loader import EMAILS_DIR, eml_to_graph


def _pdf_bytes(name: str) -> bytes:
    _, atts = eml_to_graph(EMAILS_DIR / f"{name}.eml")
    att = next(a for a in atts if a.name.lower().endswith(".pdf"))
    return att.content


def test_extract_pdf_nordicauto_single_page() -> None:
    raw = extract_pdf(_pdf_bytes("01_nordicauto_hsbc_pdf"))

    assert raw.meta["page_count"] == 1
    assert "Remittance amount: INR 6,633,624.61" in raw.text
    assert "ACM2510006275" in raw.text
    assert "Other reference: GTBN52026021920531478" in raw.text


def test_extract_pdf_contibus_two_pages() -> None:
    raw = extract_pdf(_pdf_bytes("03_contibus_pdf"))

    assert raw.meta["page_count"] == 2
    assert "Total" in raw.text
    assert "CBB2510004583" in raw.text
    assert "2510004583DISCO" in raw.text
    assert "RTGS PAYMENT" in raw.text
