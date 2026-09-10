from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from ar_pipeline.db.models import Attachment, Email
from ar_pipeline.ingest.eml import ingest_eml_file, parse_eml
from ar_pipeline.storage import LocalBlobStore, attachment_blob_key

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "emails"
_BODY_TABLE = _FIXTURES / "02_fwd_body_table.eml"
_WITH_PDF = _FIXTURES / "03_fwd_multiline_pdf.eml"
_MULTI_ATT = _FIXTURES / "01_fwd_bank_advice_pdf.eml"  # inline png + a pdf


def test_parse_eml_reads_headers_and_body():
    msg, atts = parse_eml(_BODY_TABLE)
    assert msg.internet_message_id
    assert msg.subject
    assert "@" in msg.sender_address
    assert msg.body_html or msg.body_text
    assert msg.received_at.tzinfo is not None


def test_parse_eml_extracts_attachments():
    msg, atts = parse_eml(_WITH_PDF)
    assert msg.has_attachments is True
    assert any(a.content_type == "application/pdf" for a in atts)
    assert all(a.size == len(a.content) for a in atts)


def test_parse_eml_picks_up_inline_image_and_file_attachment():
    _, atts = parse_eml(_MULTI_ATT)
    names = {a.name for a in atts}
    assert "Payment_Advice.pdf" in names
    assert any(a.content_type == "image/png" for a in atts)  # inline image kept


def test_ingest_eml_file_persists_email_and_blobs(db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    row = ingest_eml_file(db_session, store, _WITH_PDF)
    assert row is not None
    assert row.status == "new"
    assert row.sender_domain == row.sender_address.split("@", 1)[1].lower()

    atts = db_session.scalars(select(Attachment).where(Attachment.email_id == row.id)).all()
    assert atts
    for a in atts:
        assert a.blob_url
        assert store.get(attachment_blob_key(a))  # bytes are retrievable


def test_ingest_eml_file_dedups_on_message_id(db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    first = ingest_eml_file(db_session, store, _BODY_TABLE)
    assert first is not None
    again = ingest_eml_file(db_session, store, _BODY_TABLE)
    assert again is None
    n = db_session.scalar(
        select(Email.id).where(Email.internet_message_id == first.internet_message_id)
    )
    assert n == first.id
    assert len(db_session.scalars(select(Email).where(Email.id == first.id)).all()) == 1
