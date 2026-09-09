"""Canonical remittance payload — one payload == one payment. The single
source of truth for extracted settlement data; referenced by normalization,
the review UI, and the stub backend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_Currency = Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{3}$", to_upper=True)]

DeductionType = Literal["tds", "credit_note", "advance_adjustment", "discount", "rounding", "other"]


class Deduction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: DeductionType
    amount: Decimal
    reason: str | None = None


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_id: str
    source_email_id: str
    payment_index: int = 0
    vendor_guess: str | None = None
    extracted_at: datetime
    reviewed_by: str | None = None


class Header(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_name: str
    payer_id: str | None = None
    payment_reference: str | None = None
    payment_reference_type: str | None = None
    payment_date: date | None = None
    payment_method: str | None = None
    currency: _Currency = "INR"
    total_paid_amount: Decimal
    deductions: list[Deduction] = Field(default_factory=list)


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str
    invoice_date: date | None = None
    invoice_amount: Decimal
    deductions: list[Deduction] = Field(default_factory=list)
    amount_paid: Decimal


class RemittancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: Envelope
    header: Header
    line_items: list[LineItem] = Field(min_length=1)


CANONICAL_JSON_SCHEMA: dict = RemittancePayload.model_json_schema()
