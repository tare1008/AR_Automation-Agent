from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

from ar_pipeline.config import get_settings

if TYPE_CHECKING:
    import anthropic

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when the LLM backend fails or returns nothing usable."""


class LLMRefused(LLMError):
    """Raised when the model refuses to answer."""


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
        except anthropic.APIStatusError as exc:
            raise LLMError(str(exc)) from exc

        if response.stop_reason == "refusal":
            raise LLMRefused("LLM refused to answer")

        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("LLM returned no parsed output")
        return parsed


def get_llm_client() -> LLMClient:
    s = get_settings()
    if s.llm_provider == "anthropic":
        return AnthropicLLMClient(model=s.llm_model)
    raise LLMError(f"unknown llm_provider {s.llm_provider!r}")
