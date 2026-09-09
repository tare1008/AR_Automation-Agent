"""Vision extractor: a scanned / photographed remittance image -> ``ExtractedContent``.

Claude transcribes the image verbatim; the normalizer downstream parses the
pipe-delimited rows out of ``text``. No import-time network: the Anthropic
client is built lazily.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any, Protocol

from ar_pipeline.config import get_settings
from ar_pipeline.extract.base import ExtractedContent

if TYPE_CHECKING:
    import anthropic

_SYSTEM = (
    "You are transcribing a payment remittance / settlement document. "
    "Output every line of text verbatim. Render any tabular data as "
    "pipe-delimited rows, one row per line. Do not summarise, interpret, "
    "or omit anything. No commentary."
)


class VisionRefused(Exception):
    """Raised when the model refuses to transcribe the image."""


class VisionExtractor(Protocol):
    def extract_image(self, data: bytes, media_type: str) -> ExtractedContent: ...


class AnthropicVisionExtractor:
    """Transcribes an image via one ``client.messages.create`` call."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = "claude-opus-5",
    ) -> None:
        self._client = client
        self._model = model

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def extract_image(self, data: bytes, media_type: str) -> ExtractedContent:
        b64 = base64.standard_b64encode(data).decode("utf-8")
        content: list[Any] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            },
            {"type": "text", "text": "Transcribe this document."},
        ]
        response = self._get_client().messages.create(
            model=self._model,
            max_tokens=8000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason == "refusal":
            raise VisionRefused("model refused to transcribe the image")
        text = "\n".join(b.text for b in response.content if b.type == "text")
        return ExtractedContent(
            text=text,
            tables=[],
            meta={"model": self._model, "via": "vision"},
        )


def get_vision_extractor() -> VisionExtractor:
    get_settings()
    return AnthropicVisionExtractor()
