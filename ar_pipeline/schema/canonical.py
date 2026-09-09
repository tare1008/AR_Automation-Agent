"""Canonical remittance payload — the single source of truth for the
shape of extracted settlement data. Referenced by normalization,
the review UI, and the stub backend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_Currency = Annotated[str, StringConstraints(min_length=3, max_length=3, to_upper=True)]


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_id: str
    source_email_id: str
    vendor_guess: str | None = None
    extracted_at: datetime
    reviewed_by: str | None = None


class Header(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_name: str
    payer_id: str | None = None
    payment_reference: str
    payment_date: date
    payment_method: str | None = None
    currency: _Currency = "INR"
    total_paid_amount: Decimal


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str
    invoice_date: date | None = None
    invoice_amount: Decimal
    discount_taken: Decimal | None = None
    deduction_amount: Decimal | None = None
    deduction_reason: str | None = None
    amount_paid: Decimal


class RemittancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: Envelope
    header: Header
    line_items: list[LineItem] = Field(min_length=1)


CANONICAL_JSON_SCHEMA: dict = RemittancePayload.model_json_schema()
