from unittest.mock import MagicMock

import pytest

from ar_pipeline.extract.vision import (
    AnthropicVisionExtractor,
    VisionRefused,
    VisionTruncated,
)


def _resp(text, stop="end_turn"):
    r = MagicMock()
    r.stop_reason = stop
    blk = MagicMock()
    blk.type = "text"
    blk.text = text
    r.content = [blk]
    return r


def test_vision_transcribes_text_and_tables():
    client = MagicMock()
    client.messages.create.return_value = _resp(
        "Payment advice\nInvoice | Amount\nINV-1 | 100.00\nINV-2 | 200.00"
    )
    ext = AnthropicVisionExtractor(client=client, model="claude-opus-5")
    raw = ext.extract_image(b"\x89PNG...", "image/png")
    assert "INV-1 | 100.00" in raw.text
    assert raw.meta["via"] == "vision"
    args, kwargs = client.messages.create.call_args
    assert kwargs["model"] == "claude-opus-5"
    content = kwargs["messages"][0]["content"]
    assert any(b.get("type") == "image" for b in content)


def test_vision_raises_on_refusal():
    client = MagicMock()
    client.messages.create.return_value = _resp("", stop="refusal")
    with pytest.raises(VisionRefused):
        AnthropicVisionExtractor(client=client).extract_image(b"x", "image/png")


def test_vision_raises_on_truncation():
    client = MagicMock()
    client.messages.create.return_value = _resp("partial...", stop="max_tokens")
    with pytest.raises(VisionTruncated):
        AnthropicVisionExtractor(client=client).extract_image(b"x", "image/png")


@pytest.mark.live
def test_vision_live_smoke():
    pytest.importorskip("anthropic")
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("no ANTHROPIC_API_KEY")
    # a tiny generated PNG of the text "INV-42  9,433.00" — assert the number comes back
    ...
