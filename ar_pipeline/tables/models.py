"""Structured AI outputs for the table path (spec §4.2). Pure pydantic."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROLES = (
    "invoice_number",
    "invoice_date",
    "invoice_amount",
    "tds",
    "adjustment",
    "other_deduction",
    "amount_paid",
    "payment_reference",
    "ignore",
)
Role = Literal[
    "invoice_number",
    "invoice_date",
    "invoice_amount",
    "tds",
    "adjustment",
    "other_deduction",
    "amount_paid",
    "payment_reference",
    "ignore",
]


class ColumnAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    role: Role


class MappingOutput(BaseModel):
    """Which line-item field each column of the table holds."""

    model_config = ConfigDict(extra="forbid")

    is_line_table: bool
    columns: list[ColumnAssignment] = Field(default_factory=list)
    notes: str = ""


class HeaderOutput(BaseModel):
    """The payment header, read while the table's rows are read in code."""

    model_config = ConfigDict(extra="forbid")

    is_remittance: bool
    notes: str = ""
    payer_name: str = ""
    payer_id: str | None = None
    payment_reference: str | None = None
    payment_reference_type: str | None = None
    payment_date: date | None = None
    payment_method: str | None = None
    currency: str = "INR"
    vendor_guess: str | None = None
    confidence: float = 0.0
