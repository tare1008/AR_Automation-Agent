"""Bank references, payment signals and payment keys. Pure: no DB, no I/O."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ar_pipeline.ledger.matching import payer_slug

# keyword-anchored only: bare code-shaped tokens include invoice numbers
# (JHMUR2510007033), which would make the "all references recorded" check
# never fire. Bounded whitespace prevents catastrophic backtracking. Scoped
# case-insensitive for keywords only; token is case-sensitive (digits/uppercase/hyphens only).
# Regex structure:
# 1. Keyword: UTR|RTGS|NEFT|IMPS (case-insensitive)
# 2. Optional: modifiers (no|number|ref|reference) with bounded whitespace
# 3. Optional: separator (:/#/-/.) with optional modifiers after
# 4. Token: case-sensitive, uppercase/digits/hyphens only (no spaces)
_REF_RE = re.compile(
    r"(?i:UTR|RTGS|NEFT|IMPS)\b"
    r"(?:[ \t/]+(?:(?i:no|number|ref(?:erence)?)\b\.?[ \t/]*)*)?[ \t]{0,3}"
    r"[:#.\-]?[ \t]{0,3}(?:(?i:no|number|ref(?:erence)?)\b\.?[ \t/]*)*"
    r"((?=[A-Z0-9\-]{0,40}\d)[A-Z0-9](?:[A-Z0-9\-]){6,40}[A-Z0-9])"
)
_AMOUNT_RE = re.compile(
    r"(?<!\d[./])\b\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?\b|(?<![\d.])\d+\.\d{2}(?![.\d])"
)
# currency-marked plain integer ("Rs 50000", "INR 50000", "₹50000", "$4250"); bare numbers
# such as invoice ids never count. Grouped/decimal amounts are left to _AMOUNT_RE.
_MARKED_AMOUNT_RE = re.compile(r"(?i:\brs\.?|\binr|₹|\$)[ \t]*\d{3,}(?!\d|[,.]\d)")
_BANK_TYPES = {"utr", "rtgs", "neft", "imps"}
_DOC_TYPES = {"payer_document", "request_number"}


def normalize_ref(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def find_references(text: str) -> set[str]:
    refs = {normalize_ref(m.group(1)) for m in _REF_RE.finditer(text or "")}
    return {r for r in refs if any(c.isdigit() for c in r) and len(r) >= 8}


def flatten_text(body_text: str) -> str:
    """Collapse line breaks and ``>`` quote prefixes (a quoted copy re-wraps lines)."""
    return re.sub(r"\s*\n[\s>]*", " ", body_text or "")


def amount_occurrences(flat: str) -> int:
    return len(_AMOUNT_RE.findall(flat)) + len(_MARKED_AMOUNT_RE.findall(flat))


def has_payment_signal(text: str, tables: list[list[list[str]]] | None = None) -> bool:
    flat = flatten_text(text)
    if find_references(flat) or _AMOUNT_RE.search(flat) or _MARKED_AMOUNT_RE.search(flat):
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
