from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Attachment, Email, PollState
from ar_pipeline.ingest.client import DeltaExpired, GraphClient
from ar_pipeline.storage import BlobStore


@dataclass(frozen=True, slots=True)
class PollStats:
    new_emails: int
    attachments: int
    duplicates: int
    removed: int
    resynced: bool


def sender_domain(address: str) -> str:
    """``"a@B.com" -> "b.com"``; no ``@`` or empty input -> ``""``."""
    if "@" not in address:
        return ""
    domain = address.split("@", 1)[1].strip().lower()
    return domain


def poll_once(graph: GraphClient, blob_store: BlobStore, session: Session) -> PollStats:
    """Run one delta poll cycle inside ``session``. Caller owns the transaction."""
    state = session.get(PollState, 1)
    if state is None:
        state = PollState(id=1, delta_token=None)
        session.add(state)
        session.flush()

    resynced = False
    try:
        result = graph.fetch_delta(state.delta_token)
    except DeltaExpired:
        state.delta_token = None
        result = graph.fetch_delta(None)
        resynced = True

    new_emails = 0
    attachments = 0
    duplicates = 0
    removed = 0

    for message in result.messages:
        if message.removed:
            removed += 1
            continue

        existing = session.scalar(
            select(Email.id).where(Email.internet_message_id == message.internet_message_id)
        )
        if existing is not None:
            duplicates += 1
            continue

        email = Email(
            internet_message_id=message.internet_message_id,
            sender_address=message.sender_address,
            sender_domain=sender_domain(message.sender_address),
            subject=message.subject,
            received_at=message.received_at,
            body_html=message.body_html,
            body_text=message.body_text,
            status="new",
        )
        try:
            with session.begin_nested():
                session.add(email)
                session.flush()
        except IntegrityError:
            duplicates += 1
            continue

        if message.has_attachments:
            for att in graph.download_attachments(message.id):
                key = f"{email.id}/{att.name}"
                url = blob_store.put(key, att.content)
                session.add(
                    Attachment(
                        email_id=email.id,
                        filename=att.name,
                        content_type=att.content_type,
                        size=att.size,
                        blob_url=url,
                        sha256=blob_store.sha256(att.content),
                    )
                )
                attachments += 1

        new_emails += 1

    state.delta_token = result.delta_link
    state.last_poll_at = datetime.now(UTC)

    return PollStats(
        new_emails=new_emails,
        attachments=attachments,
        duplicates=duplicates,
        removed=removed,
        resynced=resynced,
    )
