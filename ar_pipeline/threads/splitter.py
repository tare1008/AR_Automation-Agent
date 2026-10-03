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
from datetime import UTC, datetime

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ar_pipeline.extract.html_table import table_rows
from ar_pipeline.threads.references import has_payment_signal

_BLOCK_TAGS = {
    "p", "div", "tr", "li", "table", "blockquote", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "pre", "section", "ul", "ol",
}  # fmt: skip
_SKIP_TAGS = {"script", "style", "head", "title"}
_LEAD = r"[ \t>*]*"
_OUTLOOK_RE = re.compile(
    rf"(?mi)^{_LEAD}From:[ \t*]*(?P<sender>[^\n]+)\n"
    rf"{_LEAD}(?:Sent|Date):[ \t*]*(?P<sent>[^\n]+)\n"
    rf"(?:{_LEAD}(?:To|Cc|Bcc|Subject|Importance):[^\n]*(?:\n|$))+"
)
# the sender group excludes "," and the date group is bounded, so a long line cannot
# trigger quadratic backtracking
_GMAIL_RE = re.compile(
    r"(?mi)^[ \t>]*On (?P<sent>[^\n]{1,120}?),?[ \t]*(?P<sender>[^\n,<]{0,120}<[^>\n]{1,200}>)"
    r"[ \t]*wrote:[ \t]*$\n?"
)
_FORWARD_RE = re.compile(
    r"(?mi)^[ \t>]*-{2,}[ \t]*Forwarded message[ \t]*-{2,}[ \t]*\n"
    r"(?P<hdrs>(?:[ \t>]*(?:From|Date|Sent|Subject|To|Cc):[^\n]*(?:\n|$))+)"
)
_HDR_FIELD_RE = re.compile(r"(?mi)^[ \t>]*(?P<name>From|Date|Sent):[ \t]*(?P<value>[^\n]+)")
_CAUTION_RE = re.compile(r"(?is)caution:.{0,300}?\bsafe\b\.?")
_WORD_RE = re.compile(r"[a-z0-9]+")
_DATE_FORMATS = (
    "%d %B %Y %H:%M", "%d %B %Y %I:%M %p", "%d %b %Y %H:%M", "%d %b %Y %I:%M %p",
    "%A, %B %d, %Y %I:%M %p", "%A, %d %B %Y %H:%M", "%A, %d %B, %Y %I:%M %p",
    "%a, %b %d, %Y at %I:%M %p", "%a, %b %d, %Y",
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
    return hashlib.sha256(" ".join(words[:_FINGERPRINT_WORDS]).encode()).hexdigest()


def _linearize_html(html: str) -> tuple[str, list[tuple[int, Tag]]]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    pos = 0
    tables: list[tuple[int, Tag]] = []

    def emit(s: str) -> None:
        nonlocal pos
        out.append(s)
        pos += len(s)

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                s = re.sub(r"\s+", " ", str(child))
                if s.strip():
                    emit(s)
                continue
            if not isinstance(child, Tag) or child.name in _SKIP_TAGS:
                continue
            if child.name == "br":
                emit("\n")  # a single newline: header lines stay on consecutive lines
                continue
            block = child.name in _BLOCK_TAGS
            if block:
                emit("\n")
            if child.name == "table" and child.find("table") is None:
                tables.append((pos, child))
            if child.name in ("td", "th"):
                emit(" ")
            walk(child)
            if block:
                emit("\n")

    walk(soup)
    return "".join(out), tables


def _parse_sender(raw: str | None) -> str | None:
    if not raw:
        return None
    name, addr = email.utils.parseaddr(raw.replace("&lt;", "<").replace("&gt;", ">"))
    if "@" in addr:
        return addr.strip().lower()
    cleaned = (name or raw).strip().strip("*").strip()
    return cleaned.lower() or None


def _parse_sent(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _boundaries(text: str) -> list[_Boundary]:
    found: list[_Boundary] = []
    for m in _OUTLOOK_RE.finditer(text):
        found.append(_Boundary(m.start(), m.end(), m.group("sender"), m.group("sent")))
    for m in _GMAIL_RE.finditer(text):
        found.append(_Boundary(m.start(), m.end(), m.group("sender"), m.group("sent")))
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
