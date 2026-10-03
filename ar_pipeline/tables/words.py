"""The amount in words an Indian payment advice prints ("ONE CRORE THIRTY ONE
LAKH … Rupees THIRTY NINE Paise") as a Decimal. Pure — no DB, no I/O."""

from __future__ import annotations

import re
from decimal import Decimal

_UNITS = {
    w: i
    for i, w in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
        ]
    )
}
_TENS = {
    w: 10 * (i + 2)
    for i, w in enumerate(
        ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
    )
}
_SCALES = {
    "thousand": 10**3,
    "lakh": 10**5,
    "lakhs": 10**5,
    "lac": 10**5,
    "lacs": 10**5,
    "crore": 10**7,
    "crores": 10**7,
}
_RUPEE = {"rupee", "rupees"}
_PAISE = {"paise", "paisa"}
_FILLER = {"and", "only"}
_VOCAB = set(_UNITS) | set(_TENS) | {"hundred"} | set(_SCALES) | _RUPEE | _PAISE | _FILLER
# "RupeesTHIRTY" is printed glued; split the currency words off first
_SPLIT_RE = re.compile(r"(?i)(rupees?|paise|paisa)")
_WORD_RE = re.compile(r"[a-z]+")


def _value(words: list[str]) -> int | None:
    total = current = 0
    seen = False
    for w in words:
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w in _SCALES:
            total += (current or 1) * _SCALES[w]
            current = 0
        elif w in _FILLER:
            continue
        else:
            return None
        seen = True
    return total + current if seen else None


def _runs(text: str) -> list[list[str]]:
    tokens = _WORD_RE.findall(_SPLIT_RE.sub(r" \1 ", text).lower())
    runs: list[list[str]] = []
    current: list[str] = []
    for t in tokens:
        if t in _VOCAB:
            current.append(t)
            if t == "only":
                runs.append(current)
                current = []
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _parse_run(run: list[str]) -> Decimal | None:
    while run and run[0] in _RUPEE | _FILLER:
        run = run[1:]
    while run and run[-1] in _FILLER:
        run = run[:-1]
    paise = 0
    if run and run[-1] in _PAISE:
        run = run[:-1]
        if any(w in _RUPEE for w in run):
            cut = max(i for i, w in enumerate(run) if w in _RUPEE)
        elif "and" in run:
            cut = len(run) - 1 - run[::-1].index("and")
        else:
            cut = -1
        rupee_words, paise_words = (run[:cut], run[cut + 1 :]) if cut >= 0 else ([], run)
        p = _value(paise_words)
        if p is None or p > 99:
            return None
        paise = p
    else:
        rupee_words = [w for w in run if w not in _RUPEE]
    rupees = _value(rupee_words) if rupee_words else 0
    if rupees is None:
        return None
    return (Decimal(rupees) + Decimal(paise) / 100).quantize(Decimal("0.01"))


def amount_in_words(text: str) -> Decimal | None:
    """The longest run of number words that names rupees or paise, or None."""
    best: tuple[int, Decimal] | None = None
    for run in _runs(text):
        if not set(run) & (_RUPEE | _PAISE):
            continue
        number_words = sum(1 for w in run if w not in _RUPEE | _PAISE | _FILLER)
        if number_words < 2:
            continue
        amount = _parse_run(run)
        if amount is not None and (best is None or number_words > best[0]):
            best = (number_words, amount)
    return best[1] if best else None
