from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar

import pydantic
from pydantic import BaseModel

from ar_pipeline.config import get_settings

if TYPE_CHECKING:
    import anthropic

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when the LLM backend fails or returns nothing usable."""


class LLMRefused(LLMError):
    """Raised when the model refuses to answer."""


class LLMTruncated(LLMError):
    """Raised when the model's structured output hit the ``max_tokens`` limit."""


class LLMClient(Protocol):
    def parse(self, *, system: str, user: str, output_model: type[T]) -> T: ...


class AnthropicLLMClient:
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

    def parse(self, *, system: str, user: str, output_model: type[T]) -> T:
        import anthropic

        try:
            response = self._get_client().messages.parse(
                model=self._model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=output_model,
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            # SDK retries transient failures (max_retries=2); this catches a persistent one.
            raise LLMError(str(exc)) from exc
        except pydantic.ValidationError as exc:
            raise LLMError(f"LLM response failed schema validation: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMRefused("LLM refused to answer")

        if response.stop_reason == "max_tokens":
            raise LLMTruncated("LLM output hit the max_tokens limit")

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("LLM returned no parsed output")
        return parsed


def get_llm_client() -> LLMClient:
    s = get_settings()
    if s.llm_provider == "anthropic":
        return AnthropicLLMClient(model=s.llm_model)
    if s.llm_provider == "stub":
        from ar_pipeline.normalize.stub_client import StubLLMClient

        return StubLLMClient()
    raise LLMError(f"unknown llm_provider {s.llm_provider!r}")
