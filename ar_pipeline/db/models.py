from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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
EXTRACTION_STATUSES = (
    "pending_review",
    "approved",
    "rejected",
    "superseded",
    "already_recorded",
    "duplicate",
)
MESSAGE_STATUSES = ("new", "seen", "no_content", "failed")
DELIVERY_STATUSES = ("pending", "delivered", "failed")
INVOICE_SOURCES = ("books", "email")
INVOICE_PAYMENT_KINDS = ("payment", "adjustment")


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
    thread_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    attachments: Mapped[list[Attachment]] = relationship(back_populates="email")

    __table_args__ = (
        UniqueConstraint("internet_message_id", name="uq_email_internet_message_id"),
        CheckConstraint(_in("status", EMAIL_STATUSES), name="ck_email_status"),
    )


class EmailMessage(Base):
    """One message found inside an email's body (a quoted reply or forward is
    its own message). Only ``new`` messages are sent to the AI."""

    __tablename__ = "email_message"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_header: Mapped[str] = mapped_column(Text, default="")
    is_internal: Mapped[bool] = mapped_column(default=False)
    carries_attachments: Mapped[bool] = mapped_column(default=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    tables: Mapped[list | None] = mapped_column(JSONB)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="new")
    seen_reason: Mapped[str | None] = mapped_column(String(30))
    seen_in_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("email_message.id"))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("status", MESSAGE_STATUSES), name="ck_email_message_status"),
        UniqueConstraint("email_id", "position", name="uq_email_message_position"),
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
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_message.id"), index=True
    )
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
    reject_reason: Mapped[str | None] = mapped_column(Text)
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_message.id"), index=True
    )
    payment_key: Mapped[str | None] = mapped_column(Text)
    payment_key_strength: Mapped[str | None] = mapped_column(String(10))
    historical_reason: Mapped[str | None] = mapped_column(String(30))
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction.id"))
    read_info: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSONB))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "ux_extraction_payment_key",
            "payment_key",
            unique=True,
            postgresql_where=text(
                "payment_key_strength = 'strong' AND "
                "status IN ('pending_review', 'approved', 'already_recorded')"
            ),
        ),
        Index("ix_extraction_payment_key", "payment_key"),
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


class Invoice(Base):
    """One invoice in the client's books — from their CSV (``books``) or first
    seen in an approved remittance (``email``, shown as unverified)."""

    __tablename__ = "invoice"

    id: Mapped[uuid.UUID] = _uuid_pk()
    invoice_number: Mapped[str] = mapped_column(Text)
    number_key: Mapped[str] = mapped_column(Text)
    payer_name: Mapped[str | None] = mapped_column(Text)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    source: Mapped[str] = mapped_column(String(10))
    paid_before_import: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("number_key", name="uq_invoice_number_key"),
        CheckConstraint(_in("source", INVOICE_SOURCES), name="ck_invoice_source"),
    )


class InvoicePayment(Base):
    """One approved line item applied to an invoice. Written only at approval;
    never deleted (there is no un-approve path)."""

    __tablename__ = "invoice_payment"

    id: Mapped[uuid.UUID] = _uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice.id"), index=True)
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction.id"), index=True)
    line_index: Mapped[int] = mapped_column(Integer)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    deductions_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    settled: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    payment_reference: Mapped[str | None] = mapped_column(Text)
    payment_date: Mapped[date | None] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(12), default="payment", server_default="payment")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("extraction_id", "line_index", name="uq_invoice_payment_extraction_line"),
        CheckConstraint(_in("kind", INVOICE_PAYMENT_KINDS), name="ck_invoice_payment_kind"),
    )


class ColumnMapping(Base):
    """A learned column layout for one payment-advice table header (spec §4.2):
    role -> column index, reused for every later advice with the same header."""

    __tablename__ = "column_mapping"

    id: Mapped[uuid.UUID] = _uuid_pk()
    signature: Mapped[str] = mapped_column(String(64))
    header: Mapped[list] = mapped_column(JSONB)
    columns: Mapped[dict] = mapped_column(JSONB)
    payer_slug: Mapped[str | None] = mapped_column(Text)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("signature", name="uq_column_mapping_signature"),)
