from ar_pipeline.db.models import Attachment, ExtractionSource


def _attach(db_session, email, *, filename, content_type, skip_reason=None):
    att = Attachment(
        email_id=email.id,
        filename=filename,
        content_type=content_type,
        size=1000,
        blob_url="blob://x",
        sha256="0" * 64,
    )
    db_session.add(att)
    db_session.flush()
    db_session.add(
        ExtractionSource(
            email_id=email.id,
            kind=(
                "excel"
                if filename.endswith((".xls", ".xlsx"))
                else "image"
                if content_type.startswith("image/")
                else "pdf_text"
            ),
            ref=str(att.id),
            skipped=skip_reason is not None,
            skip_reason=skip_reason,
        )
    )
    db_session.flush()
    return att


def test_review_screen_shows_why_an_attachment_was_not_read(client, db_session, seed_pending):
    email, ext = seed_pending()
    _attach(
        db_session,
        email,
        filename="old.xls",
        content_type="application/vnd.ms-excel",
        skip_reason="legacy .xls not supported",
    )
    r = client.get(f"/review/{ext.id}")
    assert "not read" in r.text
    assert "legacy .xls not supported" in r.text


def test_email_view_shows_why_an_attachment_was_not_read(client, db_session, seed_pending):
    email, _ext = seed_pending()
    _attach(
        db_session,
        email,
        filename="old.xls",
        content_type="application/vnd.ms-excel",
        skip_reason="legacy .xls not supported",
    )
    r = client.get(f"/review/email/{email.id}")
    assert "legacy .xls not supported" in r.text


def test_read_attachment_carries_no_skip_badge(client, db_session, seed_pending):
    email, ext = seed_pending()
    _attach(db_session, email, filename="advice.pdf", content_type="application/pdf")
    assert "not read" not in client.get(f"/review/{ext.id}").text
    assert "not read" not in client.get(f"/review/email/{email.id}").text


def test_journey_row_counts_attachments_not_read(client, db_session, seed_pending):
    email, _ext = seed_pending()
    _attach(
        db_session,
        email,
        filename="logo.png",
        content_type="image/png",
        skip_reason="inline logo/signature image",
    )
    _attach(db_session, email, filename="advice.pdf", content_type="application/pdf")
    r = client.get("/review/journey-rows")
    assert "1 attachment not read" in r.text
