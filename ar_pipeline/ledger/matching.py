"""Invoice-number and payer matching. Pure: no DB, no I/O.

``number_key`` is the automatic match. ``is_near_match`` only ever produces a
*suggestion* for a reviewer — never an automatic match (spec §4).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ar_pipeline.db.models import Invoice

_SEPARATORS_RE = re.compile(r"[\s\-/._]+")
_DIGITS_RE = re.compile(r"\d+")
_PAYER_STOPWORDS = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "the"}


def number_key(raw: str) -> str:
    return _SEPARATORS_RE.sub("", raw).upper()


def _zero_insensitive(raw: str) -> str:
    # per number group of the ORIGINAL string: stripping separators first would
    # merge "2026" and "07550" into one run and hide the leading zero.
    tokens = [t for t in _SEPARATORS_RE.split(raw.upper()) if t]
    return "".join(_DIGITS_RE.sub(lambda m: m.group(0).lstrip("0") or "0", t) for t in tokens)


def _one_edit_apart(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1 :]


def is_near_match(line_number: str, invoice_number: str) -> bool:
    line_key, inv_key = number_key(line_number), number_key(invoice_number)
    if not line_key or line_key == inv_key:
        return False
    if _zero_insensitive(line_number) == _zero_insensitive(invoice_number):
        return True
    if len(line_key) >= 4 and inv_key.endswith(line_key):
        return True
    return len(line_key) >= 6 and len(inv_key) >= 6 and _one_edit_apart(line_key, inv_key)


def _payer_words(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    return " ".join(w for w in cleaned.split() if w not in _PAYER_STOPWORDS)


def payer_slug(name: str | None) -> str:
    """Payer identity for keys: the normalised payer words joined by '-'."""
    return _payer_words(name).replace(" ", "-")


def payers_differ(a: str | None, b: str | None) -> bool:
    """True only when both names are present and clearly different companies."""
    wa, wb = _payer_words(a), _payer_words(b)
    if not wa or not wb:
        return False
    return not (wa == wb or wa in wb or wb in wa)


def near_matches(
    line_number: str, invoices: Iterable[Invoice], payer_name: str | None, limit: int = 3
) -> list[Invoice]:
    found = [inv for inv in invoices if is_near_match(line_number, inv.invoice_number)]

    def rank(inv: Invoice) -> tuple[int, str]:
        same = bool(payer_name and inv.payer_name and not payers_differ(payer_name, inv.payer_name))
        return (0 if same else 1, inv.invoice_number)

    return sorted(found, key=rank)[:limit]
