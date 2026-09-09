from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from ar_pipeline.normalize.llm_client import (
    AnthropicLLMClient,
    LLMError,
    LLMRefused,
    get_llm_client,
)


class _Out(BaseModel):
    value: int


def _resp(parsed, stop="end_turn"):
    r = MagicMock()
    r.stop_reason = stop
    r.parsed_output = parsed
    return r


def test_parse_returns_validated_model():
    client = MagicMock()
    client.messages.parse.return_value = _resp(_Out(value=7))
    out = AnthropicLLMClient(client=client, model="claude-opus-5").parse(
        system="s", user="u", output_model=_Out
    )
    assert out.value == 7
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["output_format"] is _Out
    assert kwargs["messages"][0]["content"] == "u"
    assert kwargs["system"] == "s"


def test_parse_raises_on_refusal():
    client = MagicMock()
    client.messages.parse.return_value = _resp(None, stop="refusal")
    with pytest.raises(LLMRefused):
        AnthropicLLMClient(client=client).parse(system="s", user="u", output_model=_Out)


def test_parse_raises_llm_error_on_none_output():
    client = MagicMock()
    client.messages.parse.return_value = _resp(None)
    with pytest.raises(LLMError):
        AnthropicLLMClient(client=client).parse(system="s", user="u", output_model=_Out)


def test_get_llm_client_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    with pytest.raises(LLMError):
        get_llm_client()
    config_module.get_settings.cache_clear()
