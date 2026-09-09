from datetime import UTC, datetime

import pytest

from ar_pipeline.ingest.client import DeltaExpired
from ar_pipeline.ingest.types import GraphAttachment, GraphMessage
from tests.ingest.fakes import FakeGraphClient


def _msg(mid: str, imid: str) -> GraphMessage:
    return GraphMessage(
        id=mid,
        internet_message_id=imid,
        sender_address="ap@vendor.com",
        subject="Remittance",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        body_html="<p>hi</p>",
        body_text="hi",
        has_attachments=False,
    )


def test_fake_returns_full_batch_when_delta_link_is_none():
    fake = FakeGraphClient(messages=[_msg("m1", "<a@v.com>"), _msg("m2", "<b@v.com>")])
    result = fake.fetch_delta(None)
    assert [m.id for m in result.messages] == ["m1", "m2"]
    assert result.delta_link  # non-empty token to persist


def test_fake_returns_only_new_messages_on_subsequent_delta():
    fake = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")])
    first = fake.fetch_delta(None)
    fake.add_message(_msg("m2", "<b@v.com>"))
    second = fake.fetch_delta(first.delta_link)
    assert [m.id for m in second.messages] == ["m2"]


def test_fake_raises_delta_expired_for_unknown_link():
    fake = FakeGraphClient(messages=[])
    with pytest.raises(DeltaExpired):
        fake.fetch_delta("bogus-link")


def test_fake_download_attachments():
    att = GraphAttachment(name="s.xlsx", content_type="application/vnd...", size=3, content=b"abc")
    fake = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")], attachments={"m1": [att]})
    got = fake.download_attachments("m1")
    assert got == [att]
    assert fake.download_attachments("m2") == []
