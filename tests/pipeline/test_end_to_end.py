from __future__ import annotations

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import ExtractionSource, RawExtraction
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.fixtures.loader import FIXTURE_NAMES, load_email

MARKERS = {
    "01_fwd_bank_advice_pdf": "ACM2510006275",
    "02_fwd_body_table": "FCI2510007033",
    "03_fwd_multiline_pdf": "CBB2510004583",
    "04_direct_body_multi_payment": "SXE2510051648",
    "05_direct_body_freetext": "STBK52026032800800086",
    "06_direct_excel": "ZCC2610000038",
}


def _haystack(session, email_id) -> str:
    parts: list[str] = []
    raws = session.scalars(
        select(RawExtraction)
        .join(ExtractionSource, RawExtraction.extraction_source_id == ExtractionSource.id)
        .where(ExtractionSource.email_id == email_id)
    ).all()
    for r in raws:
        parts.append(str(r.payload.get("text") or ""))
        for table in r.payload.get("tables") or []:
            for row in table:
                parts.extend(str(cell) for cell in row)
    return "\n".join(parts)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_reaches_extracted_with_marker(name, db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email(name, db_session, store)
    vision = FakeVisionExtractor()

    advance_once(db_session, store, vision)
    advance_once(db_session, store, vision)

    db_session.refresh(email)
    assert email.status == "extracted"

    haystack = _haystack(db_session, email.id)
    assert MARKERS[name] in haystack, f"{name}: marker not found in {haystack!r}"
