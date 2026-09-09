"""Turn one ``Email`` + its ``Attachment`` rows into extraction-source specs.

The classifier decides *what* should be handed to an extractor and *how*:

* each attachment becomes at most one spec (``excel`` / ``pdf_text`` /
  ``pdf_scanned`` / ``image``), possibly ``skipped``;
* the email body becomes at most one spec: a ``body_table`` is always
  emitted when the body carries a numeric table (real signal even next to
  an attachment -- the normalizer dedups), while a prose ``body_text`` is
  emitted only when no non-skipped attachment source already exists, so a
  plain cover note for a PDF/spreadsheet is not re-extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

import pdfplumber
from bs4 import BeautifulSoup

from ar_pipeline.db.models import Attachment, Email
from ar_pipeline.storage import BlobStore, attachment_blob_key

_BODY_REF = "body"

_EXCEL_MARKERS = ("spreadsheetml", "excel", "ms-excel")
_EXCEL_EXTS = (".xlsx", ".xls")

# Media types Claude accepts in a live ``image`` block (plus the ``image/jpg``
# alias); anything else is skipped rather than routed to the vision extractor.
_SUPPORTED_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
}

# A "strong" inline-image name (logo / signature / Outlook block) is skipped
# regardless of size; a bare ``imageNNN`` is only skipped when also small.
_STRONG_INLINE_IMAGE_RE = re.compile(r"(?i)(logo|signature|outlook-)")
_WEAK_INLINE_IMAGE_RE = re.compile(r"(?i)image\d+")

# Money-looking cell: a thousands-grouped number (Western or Indian grouping,
# optional decimals) OR a plain 2dp decimal. A dotted date like ``19.02.2026``
# no longer matches (the lookarounds reject a run bracketed by more digits/dots).
_NUMERIC_CELL_RE = re.compile(
    r"(?<!\d[./])\b\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?\b|(?<![\d.])\d+\.\d{2}(?![.\d])"
)

_INLINE_IMAGE_MAX_SIZE = 25_000
_PDF_TEXT_MIN_CHARS = 20
_BODY_TEXT_MIN_CHARS = 120


@dataclass(frozen=True)
class SourceSpec:
    kind: str  # body_table | body_text | excel | pdf_text | pdf_scanned | image
    ref: str  # "body" or an attachment id string
    skipped: bool = False
    skip_reason: str | None = None


def pdf_has_text_layer(data: bytes) -> bool:
    """``True`` when the PDF carries a usable text layer.

    Concatenates ``page.extract_text()`` across every page; the PDF is
    considered text-bearing when that has >= 20 non-whitespace characters.
    """
    chunks: list[str] = []
    with pdfplumber.open(BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            chunks.append(text)
    joined = "".join(chunks)
    return len(re.sub(r"\s+", "", joined)) >= _PDF_TEXT_MIN_CHARS


def _non_ws_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _is_excel(content_type: str, filename: str) -> bool:
    ct = content_type.lower()
    name = filename.lower()
    return any(m in ct for m in _EXCEL_MARKERS) or name.endswith(_EXCEL_EXTS)


def _is_legacy_xls(content_type: str, filename: str) -> bool:
    """A legacy OLE2 ``.xls`` -- content-type ``application/vnd.ms-excel`` or a
    ``.xls`` filename that is not ``.xlsx``. Only true OOXML is extractable."""
    ct = content_type.lower()
    name = filename.lower()
    return ct == "application/vnd.ms-excel" or (
        name.endswith(".xls") and not name.endswith(".xlsx")
    )


def _is_pdf(content_type: str, filename: str) -> bool:
    ct = content_type.lower()
    if ct == "application/pdf":
        return True
    return ct == "application/octet-stream" and filename.lower().endswith(".pdf")


def _classify_attachment(att: Attachment, blob_store: BlobStore) -> SourceSpec:
    ref = str(att.id)
    content_type = att.content_type or ""
    filename = att.filename or ""

    if _is_excel(content_type, filename):
        if _is_legacy_xls(content_type, filename):
            # openpyxl only reads OOXML (.xlsx); a legacy OLE2 .xls raises and
            # would error the whole email.
            return SourceSpec("excel", ref, skipped=True, skip_reason="legacy .xls not supported")
        return SourceSpec("excel", ref)

    if _is_pdf(content_type, filename):
        data = blob_store.get(attachment_blob_key(att))
        kind = "pdf_text" if pdf_has_text_layer(data) else "pdf_scanned"
        return SourceSpec(kind, ref)

    if content_type.lower().startswith("image/"):
        if content_type.lower() not in _SUPPORTED_IMAGE_TYPES:
            # Claude rejects anything but jpeg/png/gif/webp in an image block.
            return SourceSpec(
                "image",
                ref,
                skipped=True,
                skip_reason=f"unsupported image type {content_type}",
            )
        if _STRONG_INLINE_IMAGE_RE.search(filename):
            return SourceSpec("image", ref, skipped=True, skip_reason="inline logo/signature image")
        # ``Attachment.size`` from Graph is the base64-inflated MIME-part length
        # (~1.37x the raw bytes), so this threshold is approximate.
        if att.size < _INLINE_IMAGE_MAX_SIZE and _WEAK_INLINE_IMAGE_RE.search(filename):
            return SourceSpec(
                "image", ref, skipped=True, skip_reason="inline image below threshold"
            )
        return SourceSpec("image", ref)

    # kind="image" is cosmetic here — skipped=True means it is never handed to an
    # extractor; the CHECK constraint has no "unknown" value.
    return SourceSpec(
        "image",
        ref,
        skipped=True,
        skip_reason=f"unsupported attachment type {content_type}",
    )


def _body_text(email: Email) -> str:
    if email.body_text and email.body_text.strip():
        return email.body_text
    if email.body_html:
        return BeautifulSoup(email.body_html, "lxml").get_text(" ")
    return ""


def _has_numeric_table(body_html: str) -> bool:
    if not body_html:
        return False
    soup = BeautifulSoup(body_html, "lxml")
    for table in soup.find_all("table"):
        # Skip an outer wrapper table with a nested <table>; the inner one is
        # counted on its own and we would otherwise double-count its cells.
        if table.find("table"):
            continue
        cells = table.find_all(["td", "th"])
        numeric = sum(1 for c in cells if _NUMERIC_CELL_RE.search(c.get_text()))
        if numeric >= 2:
            return True
    return False


def _classify_body(email: Email, has_live_attachment_source: bool) -> SourceSpec | None:
    if _has_numeric_table(email.body_html):
        return SourceSpec("body_table", _BODY_REF)

    if has_live_attachment_source:
        return None  # a prose cover note alongside an attachment is not a source

    text = _body_text(email)
    if _non_ws_len(text) >= _BODY_TEXT_MIN_CHARS and any(ch.isdigit() for ch in text):
        return SourceSpec("body_text", _BODY_REF)

    return None


def classify_email(
    email: Email, attachments: list[Attachment], blob_store: BlobStore
) -> list[SourceSpec]:
    specs: list[SourceSpec] = [_classify_attachment(att, blob_store) for att in attachments]

    has_live_attachment_source = any(not s.skipped for s in specs)
    body_spec = _classify_body(email, has_live_attachment_source)
    if body_spec is not None:
        specs.append(body_spec)

    if not any(not s.skipped for s in specs):
        specs.append(
            SourceSpec(
                "body_text",
                _BODY_REF,
                skipped=True,
                skip_reason="no extractable content",
            )
        )

    return specs
