"""Vision extractor: a scanned / photographed remittance -> ``ExtractedContent``.

Claude transcribes the document verbatim; the normalizer downstream parses the
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

# Media types Claude's Messages API accepts inside an ``image`` content block.
_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class VisionRefused(Exception):
    """Raised when the model refuses to transcribe the document."""


class VisionTruncated(Exception):
    """Raised when the transcription hit the output token limit."""


class VisionUnsupportedMedia(Exception):
    """Raised when the media type cannot be sent to the vision model.

    Claude accepts only ``image/jpeg|png|gif|webp`` in an ``image`` block and
    ``application/pdf`` in a ``document`` block; anything else is rejected here
    rather than sent as a doomed request.
    """


class VisionEmpty(Exception):
    """Raised when the model returned no transcribed text."""


class VisionExtractor(Protocol):
    def extract_image(self, data: bytes, media_type: str) -> ExtractedContent: ...


class AnthropicVisionExtractor:
    """Transcribes a document via one ``client.messages.create`` call.

    Handles both scanned PDFs -- sent as a ``document`` block, so every page is
    transcribed in a single call -- and photos / scanned images, sent as an
    ``image`` block.
    """

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
        normalized = "image/jpeg" if media_type == "image/jpg" else media_type

        block: dict[str, Any]
        if normalized == "application/pdf":
            block = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64,
                },
            }
        elif normalized in _IMAGE_MEDIA_TYPES:
            block = {
                "type": "image",
                "source": {"type": "base64", "media_type": normalized, "data": b64},
            }
        else:
            raise VisionUnsupportedMedia(media_type)

        content: list[Any] = [
            block,
            {"type": "text", "text": "Transcribe this document."},
        ]
        response = self._get_client().messages.create(
            model=self._model,
            max_tokens=16000,
            system=_SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason == "refusal":
            raise VisionRefused("model refused to transcribe the document")
        if response.stop_reason == "max_tokens":
            raise VisionTruncated("transcription hit the output token limit")
        text = "\n".join(b.text for b in response.content if b.type == "text")
        if not text.strip():
            raise VisionEmpty("model returned no transcribed text")
        return ExtractedContent(
            text=text,
            tables=[],
            meta={"model": self._model, "via": "vision"},
        )


def get_vision_extractor() -> VisionExtractor:
    return AnthropicVisionExtractor(model=get_settings().llm_model)
