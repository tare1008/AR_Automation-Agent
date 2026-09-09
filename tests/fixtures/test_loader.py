import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Attachment
from ar_pipeline.storage import LocalBlobStore
from tests.fixtures.loader import FIXTURE_NAMES, eml_to_graph, load_email


def test_fixture_names():
    assert FIXTURE_NAMES == [
        "01_fwd_bank_advice_pdf",
        "02_fwd_body_table",
        "03_fwd_multiline_pdf",
        "04_direct_body_multi_payment",
        "05_direct_body_freetext",
        "06_direct_excel",
    ]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_loads_into_the_db(name, db_session, tmp_path):
    email = load_email(name, db_session, LocalBlobStore(str(tmp_path)))
    db_session.flush()
    assert email.status == "new"
    assert email.sender_address
    assert email.body_html or email.body_text


def test_pdf_fixture_has_attachment_stored(db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email("01_fwd_bank_advice_pdf", db_session, store)
    db_session.flush()
    atts = db_session.scalars(select(Attachment).where(Attachment.email_id == email.id)).all()
    names = {a.filename for a in atts}
    assert "Payment_Advice.pdf" in names
    pdf = next(a for a in atts if a.filename == "Payment_Advice.pdf")
    assert store.get(
        pdf.blob_url.split("//", 1)[1] if "://" in pdf.blob_url else pdf.blob_url
    )  # smoke: blob exists


def test_body_only_fixture_has_no_attachments(db_session, tmp_path):
    email = load_email("05_direct_body_freetext", db_session, LocalBlobStore(str(tmp_path)))
    db_session.flush()
    assert db_session.scalars(select(Attachment).where(Attachment.email_id == email.id)).all() == []


def test_eml_to_graph_maps_sender_and_body():
    msg, atts = eml_to_graph_path("02_fwd_body_table")
    assert "acmemetals.example" in msg.sender_address
    assert "remitted" in msg.body_html.lower()


def eml_to_graph_path(name):
    from pathlib import Path

    return eml_to_graph(Path(__file__).parent / "emails" / f"{name}.eml")
