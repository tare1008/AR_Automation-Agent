"""The document's own totals against what was extracted (spec §4.4): the
Total row's columns and the amount in words. Pure — no DB, no network."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from ar_pipeline.ledger.money import format_money
from ar_pipeline.schema.canonical import RemittancePayload
from ar_pipeline.tables.words import amount_in_words

TOTALS_TOLERANCE = Decimal("1.00")
FLAG_TOTALS = "header: doesn't match the document's own totals"
_LABELS = {
    "invoice_amount": "gross",
    "tds": "TDS",
    "adjustment": "adjustments",
    "amount_paid": "net paid",
    "words": "amount in words",
}
_ZERO = Decimal("0")


def document_totals(column_totals: dict[str, Decimal], texts: list[str]) -> dict[str, str]:
    out = {k: str(v) for k, v in column_totals.items()}
    words = amount_in_words("\n".join(texts))
    if words is not None:
        out["words"] = str(words)
    return out


def _extracted(payload: RemittancePayload) -> dict[str, Decimal]:
    invoices = [li for li in payload.line_items if li.kind == "invoice"]
    adjustments = [li for li in payload.line_items if li.kind == "adjustment"]

    def deducted(lines: list, types: set[str]) -> Decimal:
        return sum((d.amount for li in lines for d in li.deductions if d.type in types), _ZERO)

    return {
        "invoice_amount": sum((li.invoice_amount for li in invoices), _ZERO),
        "tds": deducted(invoices, {"tds"}),
        "adjustment": sum((d.amount for li in adjustments for d in li.deductions), _ZERO)
        + deducted(invoices, {"advance_adjustment"}),
        "amount_paid": sum((li.amount_paid for li in payload.line_items), _ZERO),
        "words": payload.header.total_paid_amount,
    }


def totals_flags(payload: RemittancePayload, document: dict[str, str] | None) -> list[str]:
    if not document:
        return []
    got = _extracted(payload)
    currency = payload.header.currency
    flags: list[str] = []
    for key, printed in document.items():
        if key not in got:
            continue
        doc = Decimal(printed)
        if abs(doc - got[key]) > TOTALS_TOLERANCE:
            flags.append(
                f"{FLAG_TOTALS} ({_LABELS[key]}: document {format_money(doc, currency)}, "
                f"extracted {format_money(got[key], currency)})"
            )
    return flags


def totals_status(
    payload: RemittancePayload, document: dict[str, str] | None
) -> Literal["match", "mismatch", "not_found"]:
    if not document:
        return "not_found"
    return "mismatch" if totals_flags(payload, document) else "match"
