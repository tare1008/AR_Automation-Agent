from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ar_pipeline.extract.pdf import extract_pdf
from ar_pipeline.schema.canonical import Envelope, Header, RemittancePayload
from ar_pipeline.tables.mapping import apply_mapping, find_line_table, keyword_mapping
from ar_pipeline.tables.totals import (
    FLAG_TOTALS,
    document_totals,
    totals_flags,
    totals_status,
)
from tests.tables.advice_pdf import build_advice_pdf


def _read():
    raw = extract_pdf(build_advice_pdf()).to_payload()
    table = find_line_table([raw])
    mapped = apply_mapping(table, keyword_mapping(table.header))
    payload = RemittancePayload(
        envelope=Envelope(
            extraction_id="x", source_email_id="e", extracted_at=datetime(2026, 10, 4, tzinfo=UTC)
        ),
        header=Header(
            payer_name="Continental", total_paid_amount=sum(li.amount_paid for li in mapped.lines)
        ),
        line_items=mapped.lines,
    )
    return payload, document_totals(mapped.column_totals, [raw["text"]])


def test_document_totals_include_amount_in_words():
    _payload, doc = _read()
    assert doc == {
        "invoice_amount": "6956120.90",
        "adjustment": "139400.00",
        "tds": "6956.12",
        "amount_paid": "6809764.78",
        "words": "6809764.78",
    }


def test_matching_read_has_no_flags():
    payload, doc = _read()
    assert totals_flags(payload, doc) == []
    assert totals_status(payload, doc) == "match"


def test_a_dropped_row_is_caught():
    payload, doc = _read()
    short = payload.model_copy(update={"line_items": payload.line_items[:-1]})
    flags = totals_flags(short, doc)
    assert flags and all(f.startswith(FLAG_TOTALS) for f in flags)
    assert any("adjustments: document ₹1,39,400.00, extracted ₹1,30,600.00" in f for f in flags)
    assert totals_status(short, doc) == "mismatch"


def test_within_one_rupee_is_a_match_and_no_totals_is_not_found():
    payload, doc = _read()
    nudged = payload.model_copy(
        update={
            "header": payload.header.model_copy(
                update={"total_paid_amount": payload.header.total_paid_amount + Decimal("0.75")}
            )
        }
    )
    assert totals_flags(nudged, {"words": doc["words"]}) == []
    assert totals_flags(payload, {}) == [] and totals_status(payload, None) == "not_found"
