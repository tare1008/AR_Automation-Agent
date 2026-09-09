"""The value type every extractor produces.

An extractor is a pure ``bytes | str -> ExtractedContent`` function: no DB,
no network, no import-time side effects. ``ExtractedContent`` is the shape
persisted into ``raw_extraction`` -- ``text`` / ``tables`` for the
normalizer, ``meta`` for provenance, all JSON-serializable via
``to_payload``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EXTRACTOR_VERSION = "1"
"""Bump on any behaviour change; stored in ``raw_extraction.extractor_version``."""


@dataclass(frozen=True)
class ExtractedContent:
    text: str
    tables: list[list[list[str]]] = field(default_factory=list)
    meta: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        """A JSON-serializable dict for the ``raw_extraction.payload`` jsonb."""
        return {"text": self.text, "tables": self.tables, "meta": self.meta}
