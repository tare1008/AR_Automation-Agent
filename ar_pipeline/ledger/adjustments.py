"""Which invoice an adjustment line (a discount / debit note against an earlier
invoice) reduces — spec §4.3. Exact digit match (this payment first, then the
ledger) is automatic; one inserted or missing digit is only a suggestion; a
substituted digit is a neighbouring invoice and never suggested."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Invoice
from ar_pipeline.ledger.matching import number_key
from ar_pipeline.ledger.money import format_money
from ar_pipeline.schema.canonical import RemittancePayload

_MIN_DIGITS = 4
_MIN_NEAR = 6
_LIMIT = 3


@dataclass(frozen=True)
class AdjustmentMatch:
    target: str | None
    suggestions: list[str]


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _one_digit_apart(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) != 1:
        return False
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def _unique(numbers: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in numbers:
        k = number_key(n)
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


def match_adjustment(number: str, same: list[str], ledger: list[str]) -> AdjustmentMatch:
    d = _digits(number)
    if len(d) < _MIN_DIGITS:
        return AdjustmentMatch(None, [])
    for pool in (same, ledger):
        exact = _unique([n for n in pool if _digits(n) == d])
        if len(exact) == 1:
            return AdjustmentMatch(exact[0], [])
        if exact:
            return AdjustmentMatch(None, exact[:_LIMIT])
    if len(d) < _MIN_NEAR:
        return AdjustmentMatch(None, [])
    near = _unique([n for n in same + ledger if _one_digit_apart(d, _digits(n))])
    return AdjustmentMatch(None, near[:_LIMIT])


def _pools(session: Session, payload: RemittancePayload) -> tuple[list[str], list[str]]:
    same = [
        li.invoice_number
        for li in payload.line_items
        if li.kind == "invoice" and number_key(li.invoice_number)
    ]
    ledger = list(session.scalars(select(Invoice.invoice_number)))
    return same, ledger


def resolve_adjustments(session: Session, payload: RemittancePayload) -> RemittancePayload:
    """Fill ``applies_to`` where exactly one invoice matches by digits."""
    same, ledger = _pools(session, payload)
    lines = []
    for line in payload.line_items:
        if line.kind == "adjustment" and not line.applies_to:
            found = match_adjustment(line.invoice_number, same, ledger)
            if found.target:
                line = line.model_copy(update={"applies_to": found.target})
        lines.append(line)
    return payload.model_copy(update={"line_items": lines})


def adjustment_suggestions(session: Session, payload: RemittancePayload, index: int) -> list[str]:
    line = payload.line_items[index]
    if line.kind != "adjustment":
        return []
    same, ledger = _pools(session, payload)
    return match_adjustment(line.invoice_number, same, ledger).suggestions


def adjustment_flags(session: Session, payload: RemittancePayload) -> list[str]:
    same, ledger = _pools(session, payload)
    known = {number_key(n) for n in same + ledger}
    currency = payload.header.currency
    flags: list[str] = []
    for i, line in enumerate(payload.line_items):
        if line.kind != "adjustment":
            continue
        money = format_money(sum((d.amount for d in line.deductions), Decimal("0")), currency)
        if line.applies_to:
            if number_key(line.applies_to) not in known:
                flags.append(
                    f"line {i}: adjustment of {money} reduces {line.applies_to}, "
                    "which isn't in this payment or your invoices"
                )
            continue
        found = match_adjustment(line.invoice_number, same, ledger)
        if found.suggestions:
            flags.append(
                f"line {i}: adjustment of {money} — did you mean {', '.join(found.suggestions)}?"
            )
        else:
            flags.append(f"line {i}: adjustment of {money} — which invoice does it reduce?")
    return flags
