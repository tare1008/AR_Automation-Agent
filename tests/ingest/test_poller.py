from datetime import UTC, datetime

from sqlalchemy import select

from ar_pipeline.db.models import Attachment, Email, PollState
from ar_pipeline.ingest.poller import PollStats, _basename, poll_once, sender_domain
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
    assert store.get(f"{row.email_id}/{row.id}/s.xlsx") == b"abc"


def test_poll_skips_removed_messages(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>", removed=True)])
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    assert stats.removed == 1
    assert stats.new_emails == 0


def test_basename_strips_paths_and_dots():
    assert _basename("../../../etc/passwd") == "passwd"
    assert _basename(".hidden") == "hidden"
    assert _basename("") == "attachment"
    assert _basename("   ") == "attachment"


def test_poll_same_name_attachments_get_distinct_blobs(db_session, tmp_path):
    atts = [
        GraphAttachment(name="image001.png", content_type="image/png", size=3, content=b"AAA"),
        GraphAttachment(name="image001.png", content_type="image/png", size=3, content=b"BBB"),
    ]
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")], attachments={"m1": atts})
    store = LocalBlobStore(str(tmp_path))
    stats = poll_once(graph, store, db_session)
    db_session.flush()

    assert stats.attachments == 2
    rows = db_session.scalars(select(Attachment)).all()
    assert len(rows) == 2
    urls = {r.blob_url for r in rows}
    assert len(urls) == 2
    by_sha = {r.sha256: r for r in rows}
    assert (
        store.get(
            f"{by_sha[store.sha256(b'AAA')].email_id}/{by_sha[store.sha256(b'AAA')].id}/image001.png"
        )
        == b"AAA"
    )
    assert (
        store.get(
            f"{by_sha[store.sha256(b'BBB')].email_id}/{by_sha[store.sha256(b'BBB')].id}/image001.png"
        )
        == b"BBB"
    )


def test_poll_inline_only_attachments_are_downloaded(db_session, tmp_path):
    att = GraphAttachment(name="cid.png", content_type="image/png", size=3, content=b"abc")
    graph = FakeGraphClient(
        messages=[_msg("m1", "<a@v.com>", has_att=False)], attachments={"m1": [att]}
    )
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    db_session.flush()
    assert stats.attachments == 1


def test_poll_empty_internet_message_id_is_skipped(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "")])
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    db_session.flush()
    assert stats.new_emails == 0
    assert stats.failed == 1
    assert db_session.scalars(select(Email)).all() == []


class _ExplodingBlobStore(LocalBlobStore):
    def put(self, key: str, data: bytes) -> str:
        if b"poison" in data:
            raise ValueError("hostile key")
        return super().put(key, data)


def test_poll_isolates_poison_message_and_advances_token(db_session, tmp_path):
    good_att = GraphAttachment(name="ok.pdf", content_type="application/pdf", size=2, content=b"ok")
    bad_att = GraphAttachment(
        name="p.pdf", content_type="application/pdf", size=6, content=b"poison"
    )
    graph = FakeGraphClient(
        messages=[
            _msg("good", "<g1@v.com>"),
            _msg("poison", "<p1@v.com>"),
            _msg("good2", "<g2@v.com>"),
        ],
        attachments={"good": [good_att], "poison": [bad_att]},
    )
    stats = poll_once(graph, _ExplodingBlobStore(str(tmp_path)), db_session)
    db_session.flush()

    assert stats.new_emails == 2
    assert stats.failed == 1
    state = db_session.get(PollState, 1)
    assert state.delta_token == "delta:3"

    # the poison message's savepoint was rolled back: it left no Email row and
    # no half-ingested Attachment (blob_url still "").
    emails = db_session.scalars(
        select(Email.internet_message_id).order_by(Email.internet_message_id)
    ).all()
    assert emails == ["<g1@v.com>", "<g2@v.com>"]
    blob_urls = db_session.scalars(select(Attachment.blob_url)).all()
    assert blob_urls and all(url != "" for url in blob_urls)


def test_poll_dedup_on_full_redelivery(db_session, tmp_path):
    graph = FakeGraphClient(
        messages=[_msg("m1", "<a@v.com>"), _msg("m2", "<b@v.com>"), _msg("m3", "<c@v.com>")]
    )
    store = LocalBlobStore(str(tmp_path))
    first = poll_once(graph, store, db_session)
    db_session.flush()
    assert first.new_emails == 3

    graph.redeliver_all()
    second = poll_once(graph, store, db_session)
    db_session.flush()

    assert second.new_emails == 0
    assert second.duplicates == 3
    assert len(db_session.scalars(select(Email)).all()) == 3


def test_poll_dedup_within_a_single_batch(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<dup@v.com>"), _msg("m2", "<dup@v.com>")])
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    db_session.flush()

    assert stats.new_emails == 1
    assert stats.duplicates == 1
    assert len(db_session.scalars(select(Email)).all()) == 1


def test_poll_resyncs_on_delta_expired(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")])
    # seed a stale token
    db_session.add(PollState(id=1, delta_token="stale-token"))
    db_session.flush()
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    assert stats.resynced is True
    assert stats.new_emails == 1
