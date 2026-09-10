"""Import a settlement email from a local ``.eml`` file.

The normal ingestion path is the Graph delta poller (``poller.py``). This
module is the offline equivalent: parse an RFC-822 file into the same
``GraphMessage`` / ``GraphAttachment`` dataclasses and persist it as a
``status="new"`` email, so a demo (or a manual reprocess of a message that
was dropped) can seed the pipeline without a live mailbox.

It does NOT touch ``poll_state`` — running this never disturbs a real
delta cursor.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
from datetime import UTC, datetime
from email.message import MIMEPart
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Attachment, Email
from ar_pipeline.ingest.poller import _basename, sender_domain
from ar_pipeline.ingest.types import GraphAttachment, GraphMessage
from ar_pipeline.storage import BlobStore, attachment_blob_key


def _text(part: MIMEPart | None) -> str:
    if part is None:
        return ""
    content = part.get_content()
    return content if isinstance(content, str) else ""


def parse_eml(path: Path) -> tuple[GraphMessage, list[GraphAttachment]]:
    """Parse a ``.eml`` file into ingest dataclasses (no DB, no I/O beyond the read)."""
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)

    internet_message_id = (msg["Message-ID"] or f"<{path.stem}@local-import>").strip()
    _, sender_address = email.utils.parseaddr(msg["From"] or "")

    date_hdr = msg["Date"]
    received_at = email.utils.parsedate_to_datetime(date_hdr) if date_hdr else datetime.now(tz=UTC)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)

    attachments: list[GraphAttachment] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        is_attachment = part.get_content_disposition() == "attachment"
        is_inline_image = part.get_content_maintype() == "image" and part["Content-ID"]
        if not (is_attachment or is_inline_image):
            continue
        data = part.get_payload(decode=True)
        data = data if isinstance(data, bytes) else b""
        attachments.append(
            GraphAttachment(
                name=part.get_filename() or "inline",
                content_type=part.get_content_type(),
                size=len(data),
                content=data,
            )
        )

    message = GraphMessage(
        id=path.stem,
        internet_message_id=internet_message_id,
        sender_address=sender_address,
        subject=msg["Subject"] or "",
        received_at=received_at,
        body_html=_text(msg.get_body(preferencelist=("html",))),
        body_text=_text(msg.get_body(preferencelist=("plain",))),
        has_attachments=bool(attachments),
    )
    return message, attachments


def ingest_eml_file(session: Session, blob_store: BlobStore, path: Path) -> Email | None:
    """Persist one ``.eml`` file as a ``status="new"`` email.

    Returns the ``Email`` row, or ``None`` if a message with the same
    ``internet_message_id`` is already in the database. Flushes; does not commit.
    """
    message, attachments = parse_eml(path)

    existing = session.scalar(
        select(Email.id).where(Email.internet_message_id == message.internet_message_id)
    )
    if existing is not None:
        return None

    email_row = Email(
        internet_message_id=message.internet_message_id,
        sender_address=message.sender_address,
        sender_domain=sender_domain(message.sender_address),
        subject=message.subject,
        received_at=message.received_at,
        body_html=message.body_html,
        body_text=message.body_text,
        status="new",
    )
    session.add(email_row)
    session.flush()

    for att in attachments:
        attachment = Attachment(
            email_id=email_row.id,
            filename=_basename(att.name),
            content_type=att.content_type,
            size=att.size,
            blob_url="",
            sha256=blob_store.sha256(att.content),
        )
        session.add(attachment)
        session.flush()
        attachment.blob_url = blob_store.put(attachment_blob_key(attachment), att.content)

    return email_row
