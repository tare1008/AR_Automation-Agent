from datetime import UTC, datetime

from sqlalchemy import select

from ar_pipeline.db.models import Attachment, Email, PollState
from ar_pipeline.ingest.poller import PollStats, poll_once, sender_domain
from ar_pipeline.ingest.types import GraphAttachment, GraphMessage
from ar_pipeline.storage import LocalBlobStore
from tests.ingest.fakes import FakeGraphClient


def _msg(mid, imid, *, has_att=False, removed=False):
    return GraphMessage(
        id=mid,
        internet_message_id=imid,
        sender_address="ap@vendor.com",
        subject="Remittance",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        body_html="<p>hi</p>",
        body_text="hi",
        has_attachments=has_att,
        removed=removed,
    )


def test_sender_domain():
    assert sender_domain("ap@Vendor.com") == "vendor.com"
    assert sender_domain("garbage") == ""
    assert sender_domain("") == ""


def test_poll_inserts_new_emails_and_persists_delta_token(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>"), _msg("m2", "<b@v.com>")])
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    db_session.flush()

    assert isinstance(stats, PollStats)
    assert stats.new_emails == 2
    imids = db_session.scalars(
        select(Email.internet_message_id).order_by(Email.internet_message_id)
    ).all()
    assert imids == ["<a@v.com>", "<b@v.com>"]
    state = db_session.get(PollState, 1)
    assert state.delta_token == "delta:2"
    assert state.last_poll_at is not None


def test_poll_is_idempotent_across_runs(db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")])
    poll_once(graph, store, db_session)
    db_session.flush()
    graph.add_message(_msg("m2", "<b@v.com>"))
    stats = poll_once(graph, store, db_session)
    db_session.flush()

    assert stats.new_emails == 1
    found = db_session.scalar(select(Email).where(Email.internet_message_id == "<b@v.com>"))
    assert found is not None
    assert db_session.scalars(select(Email)).unique().all().__len__() == 2


def test_poll_downloads_and_stores_attachments(db_session, tmp_path):
    att = GraphAttachment(name="s.xlsx", content_type="application/x", size=3, content=b"abc")
    graph = FakeGraphClient(
        messages=[_msg("m1", "<a@v.com>", has_att=True)], attachments={"m1": [att]}
    )
    store = LocalBlobStore(str(tmp_path))
    stats = poll_once(graph, store, db_session)
    db_session.flush()

    assert stats.attachments == 1
    row = db_session.scalar(select(Attachment))
    assert row.filename == "s.xlsx"
    assert row.sha256 == store.sha256(b"abc")
    assert store.get(f"{row.email_id}/s.xlsx") == b"abc"


def test_poll_skips_removed_messages(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>", removed=True)])
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    assert stats.removed == 1
    assert stats.new_emails == 0


def test_poll_resyncs_on_delta_expired(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")])
    # seed a stale token
    db_session.add(PollState(id=1, delta_token="stale-token"))
    db_session.flush()
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    assert stats.resynced is True
    assert stats.new_emails == 1
