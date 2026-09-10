"""Review-queue reads and mutations. Every function takes a ``Session`` and
never commits — the request-scoped ``get_db`` dependency owns the transaction.
"""

from __future__ import annotations

import json
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
from ar_pipeline.pipeline.routing import approve_and_queue, settle_email
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


@dataclass(frozen=True)
class JourneyExtraction:
    id: uuid.UUID
    payment_index: int
    outcome: str
    confidence: Decimal | None
    flag_count: int
    delivery: str


@dataclass(frozen=True)
class JourneyRow:
    email_id: uuid.UUID
    subject: str
    sender_address: str
    received_at: datetime
    attachment_count: int
    stage: str
    error_detail: str | None
    extractions: list[JourneyExtraction]


_STAGE_LABELS = {
    "new": "Received",
    "classified": "Classified",
    "extracted": "Extracted",
    "normalized": "Normalizing",
    "review": "Awaiting review",
    "done": "Complete",
    "error": "Error",
}


def _extraction_outcome(ext: Extraction) -> str:
    if ext.status == "approved":
        return "auto-approved" if ext.reviewed_by == "auto" else "approved"
    if ext.status == "pending_review":
        if not ext.is_remittance or not ext.canonical:
            return "not a remittance"
        return "awaiting review"
    if ext.status == "rejected":
        return "rejected"
    return "superseded"


@dataclass(frozen=True)
class ExtractionView:
    extraction: Extraction
    email: Email
    outcome: str
    delivery: str
    canonical_json: str


def load_extraction_view(session: Session, extraction_id: uuid.UUID) -> ExtractionView:
    ext = session.get(Extraction, extraction_id)
    if ext is None:
        raise ReviewError("extraction not found")
    email = session.get(Email, ext.email_id)
    assert email is not None  # FK guarantees it
    delivery = session.scalar(select(Delivery.status).where(Delivery.extraction_id == ext.id))
    return ExtractionView(
        extraction=ext,
        email=email,
        outcome=_extraction_outcome(ext),
        delivery=delivery or "—",
        canonical_json=json.dumps(ext.canonical, indent=2, sort_keys=False, default=str),
    )


def list_journey(session: Session) -> list[JourneyRow]:
    email_rows = session.execute(
        select(Email, func.count(Attachment.id))
        .outerjoin(Attachment, Attachment.email_id == Email.id)
        .group_by(Email.id)
        .order_by(Email.received_at.desc())
    ).all()

    extractions_by_email: dict[uuid.UUID, list[Extraction]] = {}
    for ext in session.scalars(select(Extraction).order_by(Extraction.created_at.asc())):
        extractions_by_email.setdefault(ext.email_id, []).append(ext)

    delivery_status_by_extraction: dict[uuid.UUID, str] = {}
    for extraction_id, status in session.execute(select(Delivery.extraction_id, Delivery.status)):
        delivery_status_by_extraction[extraction_id] = status

    out: list[JourneyRow] = []
    for email, attachment_count in email_rows:
        exts: list[JourneyExtraction] = []
        for ext in extractions_by_email.get(email.id, []):
            env = ext.canonical.get("envelope") if isinstance(ext.canonical, dict) else None
            idx = env.get("payment_index", 0) if isinstance(env, dict) else 0
            exts.append(
                JourneyExtraction(
                    id=ext.id,
                    payment_index=int(idx) if isinstance(idx, int) else 0,
                    outcome=_extraction_outcome(ext),
                    confidence=ext.confidence,
                    flag_count=len(ext.validation_flags or []),
                    delivery=delivery_status_by_extraction.get(ext.id, "—"),
                )
            )
        out.append(
            JourneyRow(
                email_id=email.id,
                subject=email.subject,
                sender_address=email.sender_address,
                received_at=email.received_at,
                attachment_count=attachment_count,
                stage=_STAGE_LABELS.get(email.status, email.status),
                error_detail=email.error_detail,
                extractions=exts,
            )
        )
    return out


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


def _check_approvable(ext: Extraction) -> None:
    if not ext.is_remittance or not ext.canonical:
        raise ReviewError("cannot approve a non-remittance / empty extraction — reject it instead")


def approve_extraction(session: Session, extraction_id: uuid.UUID, user: User) -> None:
    ext = _require_pending(session.get(Extraction, extraction_id))
    _check_approvable(ext)
    email = session.get(Email, ext.email_id)
    assert email is not None
    approve_and_queue(session, ext, reviewed_by=user.name)
    settle_email(session, email)


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
    settle_email(session, email)


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
    already_approved = session.scalar(
        select(func.count())
        .select_from(Extraction)
        .where(Extraction.email_id == email_id, Extraction.status == "approved")
    )
    if already_approved:
        raise ReviewError(
            "cannot reprocess — this email already has an approved payment; "
            "reject the pending one instead if it needs correcting"
        )
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
