"""Bank references, payment signals and payment keys. Pure: no DB, no I/O."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ar_pipeline.ledger.matching import payer_slug

# keyword-anchored only: bare code-shaped tokens include invoice numbers
# (JHMUR2510007033), which would make the "all references recorded" check
# never fire.
_REF_RE = re.compile(
    r"(?i)\b(?:UTR|RTGS|NEFT|IMPS)\b(?:\s*(?:no|number|ref(?:erence)?)\.?)?"
    r"\s*[:#.\-]?\s*((?=[A-Z0-9 \-]*\d)[A-Z0-9][A-Z0-9\-]{6,30}[A-Z0-9])"
)
_AMOUNT_RE = re.compile(
    r"(?<!\d[./])\b\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?\b|(?<![\d.])\d+\.\d{2}(?![.\d])"
)
_BANK_TYPES = {"utr", "rtgs", "neft", "imps"}
_DOC_TYPES = {"payer_document", "request_number"}


def normalize_ref(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def find_references(text: str) -> set[str]:
    refs = {normalize_ref(m.group(1)) for m in _REF_RE.finditer(text or "")}
    return {r for r in refs if any(c.isdigit() for c in r) and len(r) >= 8}


def has_payment_signal(text: str, tables: list[list[list[str]]] | None = None) -> bool:
    if find_references(text) or _AMOUNT_RE.search(text or ""):
        return True
    for table in tables or []:
        for row in table:
            if any(_AMOUNT_RE.search(cell or "") for cell in row):
                return True
    return False


def canonical_total(canonical: dict) -> Decimal | None:
    raw = ((canonical or {}).get("header") or {}).get("total_paid_amount")
    try:
        return Decimal(str(raw)) if raw not in (None, "") else None
    except InvalidOperation:
        return None


def _financial_year(raw: object) -> str:
    try:
        d = date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return "nofy"
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def payment_key_for(canonical: dict) -> tuple[str, str] | None:
    header = (canonical or {}).get("header") or {}
    ref = normalize_ref(str(header.get("payment_reference") or ""))
    rtype = str(header.get("payment_reference_type") or "").lower()
    payer = payer_slug(header.get("payer_name"))
    if ref and rtype in _BANK_TYPES:
        return f"utr:{ref}", "strong"
    if ref and payer and rtype == "cheque":
        return f"chq:{payer}:{ref}", "strong"
    if ref and payer and rtype in _DOC_TYPES:
        return f"doc:{payer}:{ref}:{_financial_year(header.get('payment_date'))}", "strong"
    total = canonical_total(canonical)
    if payer and total is not None:
        when = str(header.get("payment_date") or "nodate")
        return f"soft:{payer}:{total:.2f}:{when}", "weak"
    return None
