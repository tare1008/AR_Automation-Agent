"""Checks a payment against the invoice ledger (spec §2).

Runs where a DB session exists — ``normalize.service.normalize_one`` and
``review.service.save_edits`` — after the pure ``validate_payload``. Every flag
starts with ``line {i}: `` so the review screen pins it to that line.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Invoice, InvoicePayment
from ar_pipeline.ledger.adjustments import adjustment_flags
from ar_pipeline.ledger.balance import TOLERANCE, balance_for, status_for
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
                InvoicePayment.kind == "payment",
            )
        )
        or 0
    ) > 0


def _adjustment_balance_flags(
    i: int, number: str, invoice_amount: Decimal, paid: Decimal, amount: Decimal, currency: str
) -> list[str]:
    if status_for(invoice_amount, paid) in ("paid", "overpaid"):
        return [f"line {i}: adjustment reduces {number}, which is already fully paid"]
    outstanding = invoice_amount - paid
    if amount > outstanding + TOLERANCE:
        return [
            f"line {i}: adjustment reduces {number} by {format_money(amount, currency)} but "
            f"only {format_money(outstanding, currency)} outstanding; overpaid by "
            f"{format_money(amount - outstanding, currency)}"
        ]
    return []


def check_against_ledger(session: Session, payload: RemittancePayload) -> list[str]:
    header = payload.header
    books_loaded = (
        session.scalar(select(func.count()).select_from(Invoice).where(Invoice.source == "books"))
        or 0
    ) > 0
    every_invoice: list[Invoice] | None = None
    flags: list[str] = []
    # Σ settled by earlier lines of this payment, per number key (two lines for
    # "INV-1" and "INV 1" must not each pass against the full outstanding).
    earlier: dict[str, Decimal] = {}
    # invoice lines of this payment, by number key: what the first such line says
    # the invoice is for (an adjustment may reduce one of them — R7)
    stated: dict[str, Decimal] = {}

    for i, line in enumerate(payload.line_items):
        if line.kind == "adjustment":
            continue
        if not number_key(line.invoice_number):
            continue  # "empty invoice number" is already flagged by validate_payload
        label = f"line {i}: {line.invoice_number}"
        settled = line.amount_paid + sum((d.amount for d in line.deductions), Decimal("0"))
        # the line only claims an invoice total when that total differs from what it
        # settles; an equal pair just describes the payment (spec §2 "Stated vs settled").
        states_total = abs(line.invoice_amount - settled) > TOLERANCE
        key = number_key(line.invoice_number)
        # serialize check-then-post per invoice across concurrent approvals: row
        # lock on the invoice, or an advisory lock on the key while it doesn't
        # exist yet. Both release at commit/rollback (spec §2 "Re-check at approval").
        invoice = session.scalar(select(Invoice).where(Invoice.number_key == key).with_for_update())

        if invoice is None:
            stated.setdefault(key, line.invoice_amount)
            earlier[key] = earlier.get(key, Decimal("0")) + settled
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
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
        before = earlier.get(key, Decimal("0"))
        earlier[key] = before + settled
        outstanding = bal.outstanding - before
        if status_for(invoice.amount, bal.paid + before) in ("paid", "overpaid"):
            flags.append(f"{label} is already fully paid")
        elif settled > outstanding + TOLERANCE:
            flags.append(
                f"{label} — pays {money(settled)} but only {money(outstanding)} "
                f"outstanding; overpaid by {money(settled - outstanding)}"
            )
        elif settled < outstanding - TOLERANCE and invoice.source == "email":
            flags.append(
                f"{label} — partial payment {money(settled)} of {money(outstanding)} "
                "outstanding; invoice amount comes from an email, not your books"
            )
    for i, line in enumerate(payload.line_items):
        if line.kind != "adjustment" or not number_key(line.applies_to or ""):
            continue
        key = number_key(line.applies_to or "")
        target = session.scalar(select(Invoice).where(Invoice.number_key == key).with_for_update())
        amount = sum((d.amount for d in line.deductions), Decimal("0"))
        before = earlier.get(key, Decimal("0"))
        if target is None:
            # R7: the target is one of this payment's own (new) invoice lines
            invoice_amount = stated.get(key)
            if invoice_amount is None or invoice_amount <= 0:
                continue
            earlier[key] = before + amount
            flags.extend(
                _adjustment_balance_flags(
                    i, line.applies_to or "", invoice_amount, before, amount, header.currency
                )
            )
            continue
        if target.currency != header.currency:
            flags.append(
                f"line {i}: adjustment of {format_money(amount, header.currency)} — "
                f"payment in {header.currency}, invoice in {target.currency}; "
                "not applied to the balance"
            )
            continue
        if payers_differ(header.payer_name, target.payer_name):
            flags.append(
                f"line {i}: adjustment reduces {target.invoice_number}, which belongs to "
                f"{target.payer_name}; payment is from {header.payer_name}"
            )
        bal = balance_for(session, target)
        earlier[key] = before + amount
        flags.extend(
            _adjustment_balance_flags(
                i, target.invoice_number, target.amount, bal.paid + before, amount, target.currency
            )
        )
    flags.extend(adjustment_flags(session, payload))
    return flags


def refresh_pending_flags(session: Session) -> int:
    """Recompute the flags of every pending remittance against the current ledger."""
    from ar_pipeline.normalize.recheck import refresh_pending

    return refresh_pending(session)
