"""Invoice balances, always derived from payment rows — never stored."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.matching import number_key

TOLERANCE = Decimal("0.02")
_ZERO = Decimal("0")


def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else _ZERO
    except InvalidOperation:
        return _ZERO


def line_settled(line: dict) -> Decimal:
    deductions = line.get("deductions") or []
    return _dec(line.get("amount_paid")) + sum((_dec(d.get("amount")) for d in deductions), _ZERO)


def line_target(line: dict) -> tuple[str, Decimal]:
    """(invoice number the line settles, amount it settles). An adjustment
    settles its deductions against the invoice it reduces (``applies_to``)."""
    if line.get("kind") == "adjustment":
        deductions = line.get("deductions") or []
        return (
            str(line.get("applies_to") or ""),
            sum((_dec(d.get("amount")) for d in deductions), _ZERO),
        )
    return str(line.get("invoice_number") or ""), line_settled(line)


def status_for(amount: Decimal, paid: Decimal) -> str:
    outstanding = amount - paid
    if outstanding < -TOLERANCE:
        return "overpaid"
    if abs(outstanding) <= TOLERANCE:
        return "paid"
    if paid == 0:
        return "open"
    return "partially_paid"


@dataclass(frozen=True)
class InvoiceBalance:
    invoice: Invoice
    paid: Decimal
    outstanding: Decimal
    status: str


def balances(session: Session, invoices: Sequence[Invoice]) -> list[InvoiceBalance]:
    if not invoices:
        return []
    ids = [inv.id for inv in invoices]
    rows = session.execute(
        select(InvoicePayment.invoice_id, func.sum(InvoicePayment.settled))
        .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
        .where(InvoicePayment.invoice_id.in_(ids), InvoicePayment.currency == Invoice.currency)
        .group_by(InvoicePayment.invoice_id)
    )
    settled: dict[uuid.UUID, Decimal] = {}
    for invoice_id, total in rows:
        settled[invoice_id] = Decimal(total)
    out = []
    for inv in invoices:
        paid = Decimal(inv.paid_before_import or 0) + settled.get(inv.id, _ZERO)
        outstanding = Decimal(inv.amount) - paid
        status = status_for(inv.amount, paid)
        out.append(InvoiceBalance(inv, paid, outstanding, status))
    return out


def balance_for(session: Session, invoice: Invoice) -> InvoiceBalance:
    return balances(session, [invoice])[0]


def awaiting_by_key(session: Session, exclude: uuid.UUID | None = None) -> dict[str, Decimal]:
    """Σ settled of lines on still-pending extractions, by invoice number key.
    Display only — never part of a balance (spec Q3)."""
    query = select(Extraction).where(
        Extraction.status == "pending_review", Extraction.is_remittance.is_(True)
    )
    if exclude is not None:
        query = query.where(Extraction.id != exclude)
    out: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for ext in session.scalars(query):
        for line in (ext.canonical or {}).get("line_items") or []:
            number, settled = line_target(line)
            key = number_key(number)
            if key:
                out[key] += settled
    return dict(out)
