"""Split one email body into the messages of its thread (spec §2). Pure.

Boundaries are quoted-header blocks: Outlook ``From: / Sent:`` blocks, Gmail
``On … wrote:`` lines and ``Forwarded message`` banners. HTML is walked once
into a linear text (block elements become newlines) so the same patterns work
for HTML and plain text, and each innermost ``<table>`` is assigned to the
message whose text span contains it. Nothing is dropped: the parts' headers
and bodies, concatenated, are the whole linear text.
"""

from __future__ import annotations

import email.utils
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ar_pipeline.extract.html_table import table_rows
from ar_pipeline.threads.references import _AMOUNT_RE, find_references, has_payment_signal

_BLOCK_TAGS = {
    "p", "div", "tr", "li", "table", "blockquote", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "pre", "section", "ul", "ol",
}  # fmt: skip
_SKIP_TAGS = {"script", "style", "head", "title"}
_LEAD = r"[ \t>*]*"
_SEP = r"(?:[ \t]*\n)?"  # at most one blank line between header lines (one <p> per line)
_OUTLOOK_RE = re.compile(
    rf"(?mi)^{_LEAD}From:[ \t*]*(?P<sender>[^\n]+)\n{_SEP}"
    rf"{_LEAD}(?:Sent|Date):[ \t*]*(?P<sent>[^\n]+)\n"
    rf"(?P<run>(?:{_SEP}{_LEAD}(?:To|Cc|Bcc|Subject|Importance):[^\n]*(?:\n|$))+)"
)
_SUBJECT_RE = re.compile(r"(?mi)^[ \t>*]*Subject:")
# "On <date and name> <address> wrote:": the date is split from the name afterwards by
# trimming words off the end of the prefix, so an odd date format never loses the boundary.
_GMAIL_RE = re.compile(
    r"(?mi)^[ \t>]*On (?P<prefix>[^\n<]{1,200})<(?P<addr>[^>\n]{1,200})>"
    r"[ \t]*\n?[ \t>]*wrote:[ \t]*$\n?"
)
_MAX_DATE_TRIMS = 30
_TZ_TAIL_RE = re.compile(
    r"(?:(?<=\d)|(?<=[AP]M))\s+(?:GMT|UTC|[A-Z]{3,4})(?:\s*[+-]\d{1,2}(?::?\d{2})?)?$"
)
_OFFSET_RE = re.compile(r"(?:GMT|UTC)\s*([+-])(\d{1,2})(?::?(\d{2}))?$")
_AMPM_FIX_RE = re.compile(r"(?i)(?<=\d)\s*([ap])\.?m\.?(?![A-Za-z])")
# a trustworthy end of a date: a time of day or an explicit offset (not a name word)
_DATE_END_RE = re.compile(
    r"(?i)(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?|(?:GMT|UTC)\s*[+-]\d{1,2}(?::?\d{2})?"
    r"|[+-]\d{4})$"
)
_FORWARD_RE = re.compile(
    r"(?mi)^[ \t>]*-{2,}[ \t]*Forwarded message[ \t]*-{2,}[ \t]*\n"
    r"(?P<hdrs>(?:[ \t>]*(?:From|Date|Sent|Subject|To|Cc):[^\n]*(?:\n|$))+)"
)
_HDR_FIELD_RE = re.compile(r"(?mi)^[ \t>]*(?P<name>From|Date|Sent):[ \t]*(?P<value>[^\n]+)")
_CAUTION_RE = re.compile(
    r"(?is)caution:\s*this (?:e-?mail|message) originated from outside.{0,250}?\bsafe\b\.?"
)
_AM_PM_RE = re.compile(r"(?i)\b[AP]\.?M\b")
_WORD_RE = re.compile(r"[a-z0-9]+")
_DATE_FORMATS = (
    "%d %B %Y %H:%M", "%d %B %Y %I:%M %p", "%d %b %Y %H:%M", "%d %b %Y %I:%M %p",
    "%A, %B %d, %Y %I:%M %p", "%A, %d %B %Y %H:%M", "%A, %d %B, %Y %I:%M %p",
    "%a, %b %d, %Y %I:%M %p", "%a, %d %b %Y %I:%M %p", "%a, %d %b %Y %H:%M",
    "%a, %b %d, %Y %H:%M", "%a, %b %d, %Y",
    "%A, %B %d, %Y %I:%M:%S %p", "%a, %b %d, %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p",
    "%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
)  # fmt: skip
_FINGERPRINT_WORDS = 60
_MIN_FINGERPRINT_WORDS = 8


@dataclass(frozen=True)
class MessagePart:
    position: int
    sender: str | None
    sent_at: datetime | None
    raw_header: str
    body_text: str
    tables: list[list[list[str]]]
    is_internal: bool
    has_payment_signal: bool
    fingerprint: str | None


@dataclass(frozen=True)
class _Boundary:
    start: int
    end: int
    sender: str | None
    sent: str | None


def fingerprint(body_text: str) -> str | None:
    text = _CAUTION_RE.sub(" ", (body_text or "").lower())
    words = _WORD_RE.findall(text)
    if len(words) < _MIN_FINGERPRINT_WORDS:
        return None
    # a quoted copy re-wraps lines, which can split "UTR" from its number
    flat = re.sub(r"\s*\n[\s>]*", " ", body_text or "")
    refs = sorted(find_references(flat))
    amounts = sorted({re.sub(r"[^\d.]", "", m.group()) for m in _AMOUNT_RE.finditer(flat)})
    material = " ".join(words[:_FINGERPRINT_WORDS]) + "|" + ",".join(refs) + "|" + ",".join(amounts)
    return hashlib.sha256(material.encode()).hexdigest()


def _linearize_html(html: str) -> tuple[str, list[tuple[int, Tag]]]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    pos = 0
    tables: list[tuple[int, Tag]] = []

    def emit(s: str) -> None:
        nonlocal pos
        out.append(s)
        pos += len(s)

    stack: list[object] = list(reversed(list(soup.children)))
    while stack:
        item = stack.pop()
        if isinstance(item, str) and not isinstance(item, NavigableString):
            emit(item)
            continue
        if isinstance(item, Comment):
            continue
        if isinstance(item, NavigableString):
            s = re.sub(r"\s+", " ", str(item))
            if s.strip():
                emit(s)
            continue
        if not isinstance(item, Tag) or item.name in _SKIP_TAGS:
            continue
        if item.name == "br":
            emit("\n")  # a single newline: header lines stay on consecutive lines
            continue
        block = item.name in _BLOCK_TAGS
        if block:
            emit("\n")
        if item.name == "table" and item.find("table") is None:
            tables.append((pos, item))
        if item.name in ("td", "th"):
            emit(" ")
        if block:
            stack.append("\n")
        stack.extend(reversed(list(item.children)))

    return "".join(out), tables


def _parse_sender(raw: str | None) -> str | None:
    if not raw:
        return None
    name, addr = email.utils.parseaddr(raw.replace("&lt;", "<").replace("&gt;", ">"))
    if "@" in addr:
        return addr.strip().lower()
    cleaned = (name or raw).strip().strip("*").strip()
    return cleaned.lower() or None


def _normalize_sent(raw: str) -> str:
    raw = raw.replace("\u202f", " ").replace("\u00a0", " ")
    raw = re.sub(r",(\s+\d{1,2}:\d{2})", r"\1", re.sub(r"(?i),?\s+at\s+", " ", raw.strip()))
    raw = re.sub(r"\s+", " ", raw).strip(" ,")
    return _AMPM_FIX_RE.sub(lambda m: " " + m.group(1).upper() + "M", raw)


def _strptime(raw: str, *, ampm: bool) -> datetime | None:
    offset = _OFFSET_RE.search(raw)
    stripped = _TZ_TAIL_RE.sub("", raw)
    for fmt in _DATE_FORMATS:
        if ampm != ("%p" in fmt):
            continue
        try:
            dt = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        if offset:
            delta = timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3) or 0))
            dt = dt.replace(tzinfo=timezone(-delta if offset.group(1) == "-" else delta))
        return dt
    return None


def _parse_sent(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = _normalize_sent(raw)
    has_ampm = bool(_AM_PM_RE.search(raw))
    dt: datetime | None = None
    if not has_ampm:  # parsedate silently drops AM/PM, so it only sees 24-hour strings
        try:
            dt = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            dt = None
    if dt is None:
        dt = _strptime(raw, ampm=has_ampm)
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _split_attribution(prefix: str) -> tuple[str | None, str]:
    """Split "<date> <display name>" by trimming words off the end until the date parses."""
    words = prefix.split()
    date_only: tuple[str, str] | None = None
    for k in range(min(len(words), _MAX_DATE_TRIMS) + 1):
        head = words[: len(words) - k]
        if not head or _parse_sent(" ".join(head)) is None:
            continue
        split = (" ".join(head), " ".join(words[len(words) - k :]))
        if _DATE_END_RE.search(_normalize_sent(split[0])):
            return split  # longest head that ends in a time or offset
        date_only = split  # keeps the shortest (most trimmed) date-only head
    return date_only if date_only else (None, prefix)


def _boundaries(text: str) -> list[_Boundary]:
    found: list[_Boundary] = []
    for m in _OUTLOOK_RE.finditer(text):
        if not _SUBJECT_RE.search(m.group("run")):
            continue
        if _parse_sent(m.group("sent")) is None and "@" not in m.group("sender"):
            continue  # "From: Accounts Team / Sent: by courier" in a message body
        found.append(_Boundary(m.start(), m.end(), m.group("sender"), m.group("sent")))
    for m in _GMAIL_RE.finditer(text):
        sent, name = _split_attribution(m.group("prefix"))
        addr = m.group("addr").strip()
        who = addr.lower() if "@" in addr else (name.strip(" ,") or addr)
        found.append(_Boundary(m.start(), m.end(), who, sent))
    for m in _FORWARD_RE.finditer(text):
        fields = {
            f.group("name").lower(): f.group("value")
            for f in _HDR_FIELD_RE.finditer(m.group("hdrs"))
        }
        found.append(
            _Boundary(
                m.start(), m.end(), fields.get("from"), fields.get("date") or fields.get("sent")
            )
        )
    found.sort(key=lambda b: (b.start, -b.end))
    kept: list[_Boundary] = []
    for b in found:  # a Gmail forward matches both banner and From/Date: keep the wider one
        if kept and b.start < kept[-1].end:
            continue
        kept.append(b)
    return kept


def _is_internal(sender: str | None, client_domains: list[str]) -> bool:
    if not sender or "@" not in sender:
        return False
    domain = sender.rsplit("@", 1)[1]
    return any(domain == d or domain.endswith("." + d) for d in client_domains)


def split_email(
    *,
    body_html: str,
    body_text: str,
    sender: str,
    received_at: datetime,
    client_domains: list[str],
) -> list[MessagePart]:
    if body_html and body_html.strip():
        text, table_tags = _linearize_html(body_html)
    else:
        text, table_tags = (body_text or "").replace("\r\n", "\n"), []
    text = text.replace("\u202f", " ").replace("\u00a0", " ")
    if not text.strip():
        return []

    bounds = _boundaries(text)
    spans: list[tuple[int, int, str, str | None, datetime | None]] = []
    first = bounds[0].start if bounds else len(text)
    spans.append((0, first, "", _parse_sender(sender), received_at))
    for i, b in enumerate(bounds):
        end = bounds[i + 1].start if i + 1 < len(bounds) else len(text)
        spans.append(
            (b.start, end, text[b.start : b.end], _parse_sender(b.sender), _parse_sent(b.sent))
        )

    parts: list[MessagePart] = []
    for start, end, header, who, when in spans:
        body_start = start + len(header)
        body = text[body_start:end]
        if not header and not body.strip():
            continue  # empty top: the thread starts with a quoted header
        tables = [table_rows(t) for p, t in table_tags if body_start <= p < end]
        parts.append(
            MessagePart(
                position=len(parts),
                sender=who,
                sent_at=when,
                raw_header=header,
                body_text=body,
                tables=tables,
                is_internal=_is_internal(who, client_domains),
                has_payment_signal=has_payment_signal(body, tables),
                fingerprint=fingerprint(body),
            )
        )
    return parts
