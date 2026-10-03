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
from ar_pipeline.ledger.matching import number_key, payers_differ
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


def match_adjustment(
    number: str, same: list[str], ledger: list[str], foreign: list[str] | None = None
) -> AdjustmentMatch:
    """``foreign`` are ledger invoices of another payer: an exact match there is
    only ever a suggestion (R8)."""
    foreign = foreign or []
    d = _digits(number)
    if len(d) < _MIN_DIGITS:
        return AdjustmentMatch(None, [])
    exact = _unique([n for n in same if _digits(n) == d])
    if len(exact) == 1:
        return AdjustmentMatch(exact[0], [])
    if exact:
        return AdjustmentMatch(None, exact[:_LIMIT])
    own = _unique([n for n in ledger if _digits(n) == d])
    exact = _unique(own + [n for n in foreign if _digits(n) == d])
    if len(exact) == 1 and own:
        return AdjustmentMatch(exact[0], [])
    if exact:
        return AdjustmentMatch(None, exact[:_LIMIT])
    if len(d) < _MIN_NEAR:
        return AdjustmentMatch(None, [])
    near = _unique([n for n in same + ledger + foreign if _one_digit_apart(d, _digits(n))])
    return AdjustmentMatch(None, near[:_LIMIT])


def _has_adjustments(payload: RemittancePayload) -> bool:
    return any(li.kind == "adjustment" for li in payload.line_items)


@dataclass(frozen=True)
class _Pools:
    same: list[str]  # this payment's invoice lines
    ledger: list[str]  # ledger invoices of this payer (or of an unknown payer)
    foreign: list[str]  # ledger invoices that clearly belong to another payer

    def match(self, number: str) -> AdjustmentMatch:
        return match_adjustment(number, self.same, self.ledger, self.foreign)


def _pools(session: Session, payload: RemittancePayload) -> _Pools:
    same = [
        li.invoice_number
        for li in payload.line_items
        if li.kind == "invoice" and number_key(li.invoice_number)
    ]
    payer = payload.header.payer_name
    ledger: list[str] = []
    foreign: list[str] = []
    for number, invoice_payer in session.execute(
        select(Invoice.invoice_number, Invoice.payer_name)
    ):
        (foreign if payers_differ(payer, invoice_payer) else ledger).append(number)
    return _Pools(same, ledger, foreign)


def resolve_adjustments(session: Session, payload: RemittancePayload) -> RemittancePayload:
    """Fill ``applies_to`` where exactly one invoice matches by digits."""
    if not _has_adjustments(payload):
        return payload
    pools = _pools(session, payload)
    lines = []
    for line in payload.line_items:
        if line.kind == "adjustment" and not line.applies_to:
            found = pools.match(line.invoice_number)
            if found.target:
                line = line.model_copy(update={"applies_to": found.target})
        lines.append(line)
    return payload.model_copy(update={"line_items": lines})


def adjustment_suggestions(session: Session, payload: RemittancePayload, index: int) -> list[str]:
    line = payload.line_items[index]
    if line.kind != "adjustment":
        return []
    found = _pools(session, payload).match(line.invoice_number)
    # an exact target not yet confirmed (applies_to empty, e.g. the invoice was
    # imported after the read) is offered too, so the Use button can confirm it
    if found.target and not line.applies_to:
        return [found.target]
    return found.suggestions


def adjustment_flags(session: Session, payload: RemittancePayload) -> list[str]:
    if not _has_adjustments(payload):
        return []
    pools = _pools(session, payload)
    known = {number_key(n) for n in pools.same + pools.ledger + pools.foreign}
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
        found = pools.match(line.invoice_number)
        offered = [found.target] if found.target else found.suggestions
        if offered:
            flags.append(f"line {i}: adjustment of {money} — did you mean {', '.join(offered)}?")
        else:
            flags.append(f"line {i}: adjustment of {money} — which invoice does it reduce?")
    return flags
