"""Load `.eml` fixtures through the real ingestion poller into the DB.

Parses a `.eml` file into the ingest dataclasses (`GraphMessage` /
`GraphAttachment`), feeds it through `poll_once` via `FakeGraphClient`, and
returns the persisted `Email` row -- so tests exercise the same code path as
production ingestion.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import re
from datetime import UTC, datetime
from email.message import MIMEPart
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Email
from ar_pipeline.ingest.poller import poll_once
from ar_pipeline.ingest.types import GraphAttachment, GraphMessage
from ar_pipeline.storage import BlobStore
from tests.ingest.fakes import FakeGraphClient

EMAILS_DIR = Path(__file__).parent / "emails"

FIXTURE_NAMES: list[str] = sorted(p.stem for p in EMAILS_DIR.glob("*.eml"))


def _slug(stem: str) -> str:
    """Stable id from a filename stem: lowercased, only `[a-z0-9_]`."""
    return re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")


def _part_content(part: MIMEPart | None) -> str:
    if part is None:
        return ""
    content = part.get_content()
    return content if isinstance(content, str) else ""


def eml_to_graph(path: Path) -> tuple[GraphMessage, list[GraphAttachment]]:
    """Parse a `.eml` file into ingest dataclasses."""
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)

    slug = _slug(path.stem)

    internet_message_id = msg["Message-ID"] or f"<{slug}@fixture>"

    _, sender_address = email.utils.parseaddr(msg["From"] or "")

    date_hdr = msg["Date"]
    received_at = (
        email.utils.parsedate_to_datetime(date_hdr)
        if date_hdr
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)

    body_html = _part_content(msg.get_body(preferencelist=("html",)))
    body_text = _part_content(msg.get_body(preferencelist=("plain",)))

    attachments: list[GraphAttachment] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        is_attachment = part.get_content_disposition() == "attachment"
        is_inline_image = part.get_content_maintype() == "image" and part["Content-ID"]
        if not (is_attachment or is_inline_image):
            continue
        data = part.get_payload(decode=True) or b""
        if not isinstance(data, bytes):
            data = b""
        attachments.append(
            GraphAttachment(
                name=part.get_filename() or "inline",
                content_type=part.get_content_type(),
                size=len(data),
                content=data,
            )
        )

    message = GraphMessage(
        id=slug,
        internet_message_id=internet_message_id,
        sender_address=sender_address,
        subject=msg["Subject"] or "",
        received_at=received_at,
        body_html=body_html,
        body_text=body_text,
        has_attachments=bool(attachments),
    )
    return message, attachments


def load_email(name: str, session: Session, blob_store: BlobStore) -> Email:
    """Feed the named fixture through `poll_once` and return the persisted `Email`."""
    msg, atts = eml_to_graph(EMAILS_DIR / f"{name}.eml")
    fake = FakeGraphClient([msg], {msg.id: atts})
    poll_once(fake, blob_store, session)
    session.flush()
    email_row = session.scalars(
        select(Email).where(Email.internet_message_id == msg.internet_message_id)
    ).one()
    return email_row
