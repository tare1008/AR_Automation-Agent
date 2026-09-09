"""A canned :class:`VisionExtractor` for tests that must not hit the network."""

from __future__ import annotations

from ar_pipeline.extract.base import ExtractedContent

_DEFAULT_TEXT = "Payment advice\nInvoice | Amount\nINV-1 | 100.00"


class FakeVisionExtractor:
    """Returns a fixed ``ExtractedContent`` regardless of input."""

    def __init__(
        self,
        text: str = _DEFAULT_TEXT,
        tables: list[list[list[str]]] | None = None,
        meta: dict[str, object] | None = None,
    ) -> None:
        self._text = text
        self._tables = tables if tables is not None else []
        self._meta = meta if meta is not None else {"model": "fake", "via": "vision"}
        self.calls: list[tuple[bytes, str]] = []

    def extract_image(self, data: bytes, media_type: str) -> ExtractedContent:
        self.calls.append((data, media_type))
        return ExtractedContent(text=self._text, tables=self._tables, meta=dict(self._meta))
