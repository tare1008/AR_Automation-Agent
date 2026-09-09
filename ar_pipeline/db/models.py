from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ar_pipeline.db.base import Base

EMAIL_STATUSES = (
    "new",
    "classified",
    "extracted",
    "normalized",
    "review",
    "done",
    "error",
)
EXTRACTION_STATUSES = ("pending_review", "approved", "rejected")
DELIVERY_STATUSES = ("pending", "delivered", "failed")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _in(col: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{col} IN ({joined})"


class PollState(Base):
    __tablename__ = "poll_state"

    # singleton row; id is always 1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    delta_token: Mapped[str | None] = mapped_column(Text)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("id = 1", name="poll_state_singleton"),)


class Email(Base):
    __tablename__ = "email"

    id: Mapped[uuid.UUID] = _uuid_pk()
    internet_message_id: Mapped[str] = mapped_column(String(998))
    sender_address: Mapped[str] = mapped_column(String(320))
    sender_domain: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    raw_headers: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSONB), default=dict)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    attachments: Mapped[list[Attachment]] = relationship(back_populates="email")

    __table_args__ = (
        UniqueConstraint("internet_message_id", name="uq_email_internet_message_id"),
        CheckConstraint(_in("status", EMAIL_STATUSES), name="ck_email_status"),
    )


class Attachment(Base):
    __tablename__ = "attachment"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email.id"), index=True)
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(255), default="")
    size: Mapped[int] = mapped_column(default=0)
    blob_url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))

    email: Mapped[Email] = relationship(back_populates="attachments")


class ExtractionSource(Base):
    __tablename__ = "extraction_source"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    ref: Mapped[str] = mapped_column(Text)  # 'body' or attachment id
    skipped: Mapped[bool] = mapped_column(default=False)
    skip_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            _in("kind", ("body_table", "body_text", "excel", "pdf_text", "pdf_scanned", "image")),
            name="ck_extraction_source_kind",
        ),
    )


class RawExtraction(Base):
    __tablename__ = "raw_extraction"

    id: Mapped[uuid.UUID] = _uuid_pk()
    extraction_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extraction_source.id"), index=True
    )
    payload: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSONB), default=dict)
    extractor_version: Mapped[str] = mapped_column(String(50), default="")


class Extraction(Base):
    __tablename__ = "extraction"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email.id"), index=True)
    canonical: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSONB), default=dict)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    is_remittance: Mapped[bool] = mapped_column(default=True)
    validation_flags: Mapped[list] = mapped_column(MutableList.as_mutable(JSONB), default=list)
    llm_model: Mapped[str] = mapped_column(String(100), default="")
    prompt_version: Mapped[str] = mapped_column(String(50), default="")
    raw_llm_response: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending_review")
    reviewed_by: Mapped[str | None] = mapped_column(String(320))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("status", EXTRACTION_STATUSES), name="ck_extraction_status"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_extraction_confidence",
        ),
    )


class ExtractionEdit(Base):
    __tablename__ = "extraction_edit"

    id: Mapped[uuid.UUID] = _uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction.id"), index=True)
    field_path: Mapped[str] = mapped_column(Text)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    edited_by: Mapped[str] = mapped_column(String(320))
    edited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Delivery(Base):
    __tablename__ = "delivery"

    id: Mapped[uuid.UUID] = _uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("status", DELIVERY_STATUSES), name="ck_delivery_status"),
        Index("ix_delivery_status_next_attempt", "status", "next_attempt_at"),
    )


class Vendor(Base):
    __tablename__ = "vendor"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    sender_domains: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    format_hint: Mapped[str | None] = mapped_column(Text)
    column_hints: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSONB), default=dict)
    active: Mapped[bool] = mapped_column(default=True)
