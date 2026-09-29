"""Checks a payment against the invoice ledger (spec §2).

Runs where a DB session exists — ``normalize.service.normalize_one`` and
``review.service.save_edits`` — after the pure ``validate_payload``. Every flag
starts with ``line {i}: `` so the review screen pins it to that line.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Invoice, InvoicePayment
from ar_pipeline.ledger.balance import TOLERANCE, balance_for
from ar_pipeline.ledger.matching import near_matches, number_key, payers_differ
from ar_pipeline.ledger.money import format_money
from ar_pipeline.schema.canonical import RemittancePayload


def _is_duplicate(session: Session, invoice: Invoice, reference: str, amount_paid: Decimal) -> bool:
    return (
        session.scalar(
            select(func.count())
            .select_from(InvoicePayment)
            .where(
                InvoicePayment.invoice_id == invoice.id,
                InvoicePayment.payment_reference == reference,
                InvoicePayment.amount_paid == amount_paid,
            )
        )
        or 0
    ) > 0


def check_against_ledger(session: Session, payload: RemittancePayload) -> list[str]:
    header = payload.header
    books_loaded = (
        session.scalar(select(func.count()).select_from(Invoice).where(Invoice.source == "books"))
        or 0
    ) > 0
    every_invoice: list[Invoice] | None = None
    flags: list[str] = []

    for i, line in enumerate(payload.line_items):
        if not number_key(line.invoice_number):
            continue  # "empty invoice number" is already flagged by validate_payload
        label = f"line {i}: {line.invoice_number}"
        settled = line.amount_paid + sum((d.amount for d in line.deductions), Decimal("0"))
        # the line only claims an invoice total when that total differs from what it
        # settles; an equal pair just describes the payment (spec §2 "Stated vs settled").
        states_total = abs(line.invoice_amount - settled) > TOLERANCE
        invoice = session.scalar(
            select(Invoice).where(Invoice.number_key == number_key(line.invoice_number))
        )

        if invoice is None:
            if every_invoice is None:
                every_invoice = list(session.scalars(select(Invoice)))
            near = near_matches(line.invoice_number, every_invoice, header.payer_name)
            if near:
                names = ", ".join(inv.invoice_number for inv in near)
                flags.append(f"line {i}: {line.invoice_number} not found — did you mean {names}?")
            elif books_loaded:
                flags.append(f"{label} isn't in your open invoices")
            if states_total and settled < line.invoice_amount:
                flags.append(
                    f"{label} — partial payment {format_money(settled, header.currency)} of "
                    f"{format_money(line.invoice_amount, header.currency)}; invoice amount comes "
                    "from this email, not your books"
                )
            continue

        def money(value: Decimal, _cur: str = invoice.currency) -> str:
            return format_money(value, _cur)

        if header.currency != invoice.currency:
            flags.append(
                f"{label} — payment in {header.currency}, invoice in {invoice.currency}; "
                "not applied to the balance"
            )
            continue
        if payers_differ(header.payer_name, invoice.payer_name):
            flags.append(
                f"{label} — invoice belongs to {invoice.payer_name}; "
                f"payment is from {header.payer_name}"
            )
        if states_total and abs(line.invoice_amount - invoice.amount) > TOLERANCE:
            where = "your books say" if invoice.source == "books" else "an earlier email said"
            flags.append(
                f"{label} — email says invoice {money(line.invoice_amount)}, "
                f"{where} {money(invoice.amount)}"
            )
        if header.payment_reference and _is_duplicate(
            session, invoice, header.payment_reference, line.amount_paid
        ):
            flags.append(
                f"{label} — reference {header.payment_reference} for "
                f"{money(line.amount_paid)} was already approved"
            )
        bal = balance_for(session, invoice)
        if bal.status in ("paid", "overpaid"):
            flags.append(f"{label} is already fully paid")
        elif settled > bal.outstanding + TOLERANCE:
            flags.append(
                f"{label} — pays {money(settled)} but only {money(bal.outstanding)} "
                f"outstanding; overpaid by {money(settled - bal.outstanding)}"
            )
        elif settled < bal.outstanding - TOLERANCE and invoice.source == "email":
            flags.append(
                f"{label} — partial payment {money(settled)} of {money(bal.outstanding)} "
                "outstanding; invoice amount comes from an email, not your books"
            )
    return flags
