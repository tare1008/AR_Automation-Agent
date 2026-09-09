import email
import email.policy
import glob
import io
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas
from sqlalchemy import select

from ar_pipeline.classify.classifier import classify_email, pdf_has_text_layer
from ar_pipeline.db.models import Attachment
from ar_pipeline.storage import LocalBlobStore
from tests.fixtures.loader import load_email

# name -> the set of (kind, skipped) tuples classify_email must produce
EXPECTED = {
    "01_nordicauto_hsbc_pdf": {("pdf_text", False), ("image", True)},
    "02_fluorochem_body_table": {("body_table", False), ("image", True)},
    "03_contibus_pdf": {("pdf_text", False)},
    "04_sunrise_body_multi_payment": {("body_table", False)},
    "05_bharat_body_freetext": {("body_text", False)},
    "06_zenith_excel": {("excel", False)},
}


@pytest.mark.parametrize("name,expected", EXPECTED.items())
def test_classifier_on_fixtures(name, expected, db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email(name, db_session, store)
    db_session.flush()
    atts = db_session.scalars(select(Attachment).where(Attachment.email_id == email.id)).all()
    specs = classify_email(email, list(atts), store)
    got = {(s.kind, s.skipped) for s in specs}
    assert got == expected


def test_body_only_email_never_emits_a_body_and_attachment_dupe(db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email("06_zenith_excel", db_session, store)
    db_session.flush()
    atts = list(db_session.scalars(select(Attachment)))
    specs = classify_email(email, atts, store)
    # zenith body is one line of boilerplate — no body source
    assert not any(s.kind in ("body_table", "body_text") and not s.skipped for s in specs)


def _pdf_bytes_from_eml(prefix: str) -> bytes:
    path = glob.glob(f"tests/fixtures/emails/{prefix}_*.eml")[0]
    msg = email.message_from_bytes(Path(path).read_bytes(), policy=email.policy.default)
    for part in msg.walk():
        name = part.get_filename() or ""
        if name.lower().endswith(".pdf"):
            data = part.get_payload(decode=True)
            assert isinstance(data, bytes)
            return data
    raise AssertionError("no pdf part found")


def test_pdf_has_text_layer_true_for_real_advice():
    assert pdf_has_text_layer(_pdf_bytes_from_eml("03")) is True


def test_pdf_has_text_layer_false_for_image_only_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 100, 200, 200, fill=1)
    c.showPage()
    c.save()
    assert pdf_has_text_layer(buf.getvalue()) is False
