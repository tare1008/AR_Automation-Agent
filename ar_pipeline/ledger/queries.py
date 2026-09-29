"""Read models for the Invoices tab, invoice detail, and the review screen's
per-line ledger strip."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Email, Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.balance import InvoiceBalance, awaiting_by_key, balances, line_settled
from ar_pipeline.ledger.matching import number_key

_ZERO = Decimal("0")


@dataclass(frozen=True)
class InvoiceRow:
    id: uuid.UUID
    invoice_number: str
    payer_name: str | None
    amount: Decimal
    currency: str
    paid: Decimal
    awaiting_review: Decimal
    outstanding: Decimal
    status: str
    source: str
    progress_pct: int
    needs_attention: bool


def _row(bal: InvoiceBalance, awaiting: dict[str, Decimal]) -> InvoiceRow:
    inv = bal.invoice
    amount = Decimal(inv.amount)
    pct = int(min(Decimal(100), max(_ZERO, bal.paid / amount * 100))) if amount > 0 else 0
    return InvoiceRow(
        id=inv.id,
        invoice_number=inv.invoice_number,
        payer_name=inv.payer_name,
        amount=amount,
        currency=inv.currency,
        paid=bal.paid,
        awaiting_review=awaiting.get(inv.number_key, _ZERO),
        outstanding=bal.outstanding,
        status=bal.status,
        source=inv.source,
        progress_pct=pct,
        needs_attention=bal.status == "overpaid" or bool(inv.note),
    )


def list_invoice_rows(session: Session) -> list[InvoiceRow]:
    invoices = list(session.scalars(select(Invoice).order_by(Invoice.invoice_number)))
    awaiting = awaiting_by_key(session)
    return [_row(b, awaiting) for b in balances(session, invoices)]


def summarize(rows: list[InvoiceRow]) -> dict[str, object]:
    return {
        "outstanding_total": sum(
            (r.outstanding for r in rows if r.currency == "INR" and r.outstanding > 0), _ZERO
        ),
        "partially_paid": sum(1 for r in rows if r.status == "partially_paid"),
        "paid": sum(1 for r in rows if r.status == "paid"),
        "attention": sum(1 for r in rows if r.needs_attention),
    }


def filter_rows(rows: list[InvoiceRow], status: str | None) -> list[InvoiceRow]:
    if status == "attention":
        return [r for r in rows if r.needs_attention]
    if status == "outstanding":
        return [r for r in rows if r.status in ("open", "partially_paid")]
    if status in ("partially_paid", "paid"):
        return [r for r in rows if r.status == status]
    return rows


@dataclass(frozen=True)
class LedgerEntry:
    payment_date: date | None
    reference: str | None
    amount_paid: Decimal
    deductions: Decimal
    settled: Decimal
    balance_after: Decimal
    extraction_id: uuid.UUID
    email_id: uuid.UUID
    applied: bool  # False for a different-currency payment (not counted)


@dataclass(frozen=True)
class AwaitingEntry:
    extraction_id: uuid.UUID
    subject: str
    settled: Decimal


@dataclass(frozen=True)
class InvoiceDetail:
    row: InvoiceRow
    note: str | None
    paid_before_import: Decimal
    entries: list[LedgerEntry]
    awaiting: list[AwaitingEntry]


def invoice_detail(session: Session, invoice_id: uuid.UUID) -> InvoiceDetail | None:
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        return None
    row = _row(balances(session, [inv])[0], awaiting_by_key(session))
    balance = Decimal(inv.amount) - Decimal(inv.paid_before_import or 0)
    entries: list[LedgerEntry] = []
    payments = session.execute(
        select(InvoicePayment, Extraction.email_id)
        .join(Extraction, Extraction.id == InvoicePayment.extraction_id)
        .where(InvoicePayment.invoice_id == inv.id)
        .order_by(
            InvoicePayment.payment_date.asc().nulls_last(),
            InvoicePayment.created_at.asc(),
            InvoicePayment.id.asc(),
        )
    )
    for pay, email_id in payments:
        applied = pay.currency == inv.currency
        if applied:
            balance -= pay.settled
        entries.append(
            LedgerEntry(
                pay.payment_date,
                pay.payment_reference,
                pay.amount_paid,
                pay.deductions_total,
                pay.settled,
                balance,
                pay.extraction_id,
                email_id,
                applied,
            )
        )
    awaiting: list[AwaitingEntry] = []
    pending = session.execute(
        select(Extraction, Email.subject)
        .join(Email, Email.id == Extraction.email_id)
        .where(Extraction.status == "pending_review", Extraction.is_remittance.is_(True))
    )
    for ext, subject in pending:
        for line in (ext.canonical or {}).get("line_items") or []:
            if number_key(str(line.get("invoice_number") or "")) == inv.number_key:
                awaiting.append(AwaitingEntry(ext.id, subject, line_settled(line)))
    return InvoiceDetail(row, inv.note, Decimal(inv.paid_before_import or 0), entries, awaiting)
