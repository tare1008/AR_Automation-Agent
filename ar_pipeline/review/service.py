"""Review-queue reads and mutations. Every function takes a ``Session`` and
never commits — the request-scoped ``get_db`` dependency owns the transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import (
    Attachment,
    Delivery,
    Email,
    Extraction,
    ExtractionEdit,
    ExtractionSource,
    RawExtraction,
)
from ar_pipeline.normalize.validators import validate_payload
from ar_pipeline.review.auth import User
from ar_pipeline.review.forms import FieldEdit, canonical_diff
from ar_pipeline.schema.canonical import RemittancePayload


class ReviewError(Exception):
    """A reviewer action that cannot proceed; the message is shown as a flash."""


@dataclass(frozen=True)
class QueueRow:
    extraction_id: uuid.UUID
    email_id: uuid.UUID
    subject: str
    sender_address: str
    received_at: datetime
    payment_index: int
    is_remittance: bool
    confidence: Decimal | None
    validation_flags: list[str]


@dataclass(frozen=True)
class DetailView:
    extraction: Extraction
    email: Email
    attachments: list[Attachment]
    raw_extractions: list[dict]
    edits: list[ExtractionEdit]


def list_pending(session: Session) -> list[QueueRow]:
    rows = session.execute(
        select(Extraction, Email)
        .join(Email, Extraction.email_id == Email.id)
        .where(Extraction.status == "pending_review")
        .order_by(Email.received_at.asc(), Extraction.created_at.asc())
    ).all()
    out: list[QueueRow] = []
    for ext, email in rows:
        env = ext.canonical.get("envelope") if isinstance(ext.canonical, dict) else None
        idx = env.get("payment_index", 0) if isinstance(env, dict) else 0
        out.append(
            QueueRow(
                extraction_id=ext.id,
                email_id=email.id,
                subject=email.subject,
                sender_address=email.sender_address,
                received_at=email.received_at,
                payment_index=int(idx) if isinstance(idx, int) else 0,
                is_remittance=ext.is_remittance,
                confidence=ext.confidence,
                validation_flags=list(ext.validation_flags or []),
            )
        )
    return out


def load_detail(session: Session, extraction_id: uuid.UUID) -> DetailView:
    ext = session.get(Extraction, extraction_id)
    if ext is None:
        raise ReviewError("extraction not found")
    email = session.get(Email, ext.email_id)
    assert email is not None  # FK guarantees it
    attachments = list(session.scalars(select(Attachment).where(Attachment.email_id == email.id)))
    raws = list(
        session.scalars(
            select(RawExtraction.payload)
            .join(ExtractionSource, RawExtraction.extraction_source_id == ExtractionSource.id)
            .where(ExtractionSource.email_id == email.id)
            .order_by(ExtractionSource.kind, ExtractionSource.ref, RawExtraction.id)
        )
    )
    edits = list(
        session.scalars(
            select(ExtractionEdit)
            .where(ExtractionEdit.extraction_id == ext.id)
            .order_by(ExtractionEdit.edited_at.asc())
        )
    )
    return DetailView(ext, email, attachments, [dict(r) for r in raws], edits)


def _require_pending(ext: Extraction | None) -> Extraction:
    if ext is None:
        raise ReviewError("extraction not found")
    if ext.status != "pending_review":
        raise ReviewError(f"extraction is already {ext.status}")
    return ext


def _close_email_if_done(session: Session, email: Email) -> None:
    still_open = session.scalar(
        select(func.count())
        .select_from(Extraction)
        .where(Extraction.email_id == email.id, Extraction.status == "pending_review")
    )
    if not still_open:
        email.status = "done"


def _check_approvable(ext: Extraction) -> None:
    if not ext.is_remittance or not ext.canonical:
        raise ReviewError("cannot approve a non-remittance / empty extraction — reject it instead")


def approve_extraction(session: Session, extraction_id: uuid.UUID, user: User) -> None:
    ext = _require_pending(session.get(Extraction, extraction_id))
    _check_approvable(ext)
    ext.status = "approved"
    ext.reviewed_by = user.name
    ext.reviewed_at = func.now()
    if isinstance(ext.canonical, dict):
        env = ext.canonical.get("envelope")
        if isinstance(env, dict):
            # reassign the top-level key so MutableDict tracks the change
            # (a nested in-place mutation would not be flushed)
            ext.canonical["envelope"] = {**env, "reviewed_by": user.name}
    session.add(Delivery(extraction_id=ext.id, status="pending", next_attempt_at=func.now()))
    email = session.get(Email, ext.email_id)
    assert email is not None
    session.flush()
    _close_email_if_done(session, email)
    session.flush()


def reject_extraction(session: Session, extraction_id: uuid.UUID, user: User, reason: str) -> None:
    ext = _require_pending(session.get(Extraction, extraction_id))
    if reason.strip() == "":
        raise ReviewError("a rejection reason is required")
    ext.status = "rejected"
    ext.reject_reason = reason.strip()
    ext.reviewed_by = user.name
    ext.reviewed_at = func.now()
    email = session.get(Email, ext.email_id)
    assert email is not None
    session.flush()
    _close_email_if_done(session, email)
    session.flush()


def save_edits(
    session: Session,
    extraction_id: uuid.UUID,
    user: User,
    form_canonical: dict,
    *,
    approve: bool,
) -> list[FieldEdit]:
    ext = _require_pending(session.get(Extraction, extraction_id))
    if approve:
        _check_approvable(ext)
    stored = dict(ext.canonical) if isinstance(ext.canonical, dict) else {}
    envelope = stored.get("envelope") or {}
    full = {
        "envelope": envelope,
        "header": form_canonical.get("header", {}),
        "line_items": form_canonical.get("line_items", []),
    }
    try:
        payload = RemittancePayload.model_validate(full)
    except ValidationError as exc:
        raise ReviewError(f"the edited form is not valid: {exc}") from exc

    normalised = payload.model_dump(mode="json")
    edits = canonical_diff(
        {"header": stored.get("header", {}), "line_items": stored.get("line_items", [])},
        {"header": normalised["header"], "line_items": normalised["line_items"]},
    )
    for path, old, new in edits:
        session.add(
            ExtractionEdit(
                extraction_id=ext.id,
                field_path=path,
                old_value=old,
                new_value=new,
                edited_by=user.name,
            )
        )
    ext.canonical = normalised
    ext.validation_flags = validate_payload(payload)
    session.flush()

    if approve:
        approve_extraction(session, ext.id, user)
    return edits


def reprocess_email(session: Session, email_id: uuid.UUID) -> None:
    email = session.get(Email, email_id)
    if email is None:
        raise ReviewError("email not found")
    pending = list(
        session.scalars(
            select(Extraction).where(
                Extraction.email_id == email_id, Extraction.status == "pending_review"
            )
        )
    )
    if not pending:
        raise ReviewError("nothing to reprocess — no pending extraction for this email")
    for ext in pending:
        ext.status = "superseded"
    email.status = "classified"
    email.error_detail = None
    session.flush()


def list_errored(session: Session) -> list[Email]:
    return list(
        session.scalars(
            select(Email).where(Email.status == "error").order_by(Email.received_at.asc())
        )
    )


@dataclass(frozen=True)
class FailedDeliveryRow:
    delivery_id: uuid.UUID
    extraction_id: uuid.UUID
    email_subject: str
    sender_address: str
    attempts: int
    last_attempt_at: datetime | None
    last_error: str | None


def list_failed_deliveries(session: Session) -> list[FailedDeliveryRow]:
    rows = session.execute(
        select(Delivery, Email)
        .join(Extraction, Delivery.extraction_id == Extraction.id)
        .join(Email, Extraction.email_id == Email.id)
        .where(Delivery.status == "failed")
        .order_by(Delivery.last_attempt_at.desc().nulls_last())
    ).all()
    return [
        FailedDeliveryRow(
            delivery_id=d.id,
            extraction_id=d.extraction_id,
            email_subject=e.subject,
            sender_address=e.sender_address,
            attempts=d.attempts,
            last_attempt_at=d.last_attempt_at,
            last_error=d.last_error,
        )
        for d, e in rows
    ]


def resend_delivery(session: Session, delivery_id: uuid.UUID) -> None:
    d = session.get(Delivery, delivery_id)
    if d is None:
        raise ReviewError("delivery not found")
    if d.status != "failed":
        raise ReviewError(f"delivery is {d.status}, not failed")
    d.status = "pending"
    d.attempts = 0
    d.next_attempt_at = func.now()
    d.last_error = None
    d.delivered_at = None
    session.flush()


def retry_email(session: Session, email_id: uuid.UUID) -> None:
    email = session.get(Email, email_id)
    if email is None:
        raise ReviewError("email not found")
    if email.status != "error":
        raise ReviewError(f"email is {email.status}, not error")
    has_sources = session.scalar(
        select(func.count())
        .select_from(ExtractionSource)
        .where(ExtractionSource.email_id == email_id)
    )
    email.status = "classified" if has_sources else "new"
    email.error_detail = None
    session.flush()
