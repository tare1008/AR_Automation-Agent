from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

from ar_pipeline.normalize.llm_client import AnthropicLLMClient


class _Out(BaseModel):
    ok: bool


class _Messages:
    def __init__(self):
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason="end_turn",
            parsed_output=_Out(ok=True),
            usage=SimpleNamespace(cache_read_input_tokens=900, cache_creation_input_tokens=0),
        )


def test_system_prompt_is_sent_as_a_cached_block():
    messages = _Messages()
    client = AnthropicLLMClient(client=SimpleNamespace(messages=messages))  # type: ignore[arg-type]
    assert client.parse(system="SYSTEM", user="u", output_model=_Out).ok
    assert messages.kwargs["system"] == [
        {"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}
    ]
