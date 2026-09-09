from ar_pipeline.extract.body_text import extract_body_text
from tests.fixtures.loader import EMAILS_DIR, eml_to_graph


def test_extract_body_text_bharat_freetext() -> None:
    msg, _ = eml_to_graph(EMAILS_DIR / "05_direct_body_freetext.eml")
    raw = extract_body_text(msg.body_text, msg.body_html)

    assert "PAYMENT DONE Rs. 2743303.70" in raw.text
    assert "LESS- TDS .1% = Rs. 2570.96" in raw.text
    assert "Unique Transaction Reference Number (UTR): STBK52026032800800086" in raw.text

    assert raw.tables == []
    assert raw.meta["source"] == "body_text"
    assert raw.text.count("\n") > 8


def test_extract_body_text_falls_back_to_html() -> None:
    raw = extract_body_text("", "<pre>hi\nthere 123</pre>")

    assert raw.meta["source"] == "body_html"
    assert "there 123" in raw.text
    assert raw.tables == []
