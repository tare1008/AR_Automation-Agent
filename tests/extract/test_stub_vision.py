from __future__ import annotations

from ar_pipeline.extract.stub_vision import StubVisionExtractor


def test_returns_a_placeholder_and_no_tables():
    out = StubVisionExtractor().extract_image(b"\x89PNG fake bytes", "image/png")
    assert "stub vision" in out.text.lower()
    assert out.tables == []
    assert out.meta["via"] == "stub"
    assert out.meta["media_type"] == "image/png"
    assert out.meta["bytes"] == len(b"\x89PNG fake bytes")


def test_to_payload_is_wellformed():
    payload = StubVisionExtractor().extract_image(b"pdf", "application/pdf").to_payload()
    assert isinstance(payload["text"], str) and payload["text"]
    assert payload["tables"] == []


def test_get_vision_extractor_returns_stub_for_stub_provider(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    from ar_pipeline.extract.vision import get_vision_extractor

    assert isinstance(get_vision_extractor(), StubVisionExtractor)
    config_module.get_settings.cache_clear()
