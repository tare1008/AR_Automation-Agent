"""An offline ``VisionExtractor`` for demos and local dev — no API key, no network.

``LLM_PROVIDER=stub`` selects this. It transcribes nothing; it returns a
placeholder line so an image / scanned-PDF email still reaches the review
queue (where a human reads the original and types the details in).
"""

from __future__ import annotations

from ar_pipeline.extract.base import ExtractedContent

_PLACEHOLDER = (
    "[stub vision: LLM_PROVIDER=stub — this attachment was not transcribed. "
    "Open the original in the review UI and enter the settlement details by hand.]"
)


class StubVisionExtractor:
    def extract_image(self, data: bytes, media_type: str) -> ExtractedContent:
        return ExtractedContent(
            text=_PLACEHOLDER,
            meta={"via": "stub", "media_type": media_type, "bytes": len(data)},
        )
