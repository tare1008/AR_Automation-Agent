import time
from decimal import Decimal

import pytest

from ar_pipeline.ledger.matching import payer_slug
from ar_pipeline.threads.references import (
    canonical_total,
    find_references,
    has_payment_signal,
    normalize_ref,
    payment_key_for,
)


@pytest.mark.parametrize(
    ("text", "refs"),
    [
        ("vide UTR no.PUNBR52026021813360279 on 18.02", {"PUNBR52026021813360279"}),
        ("UTR: HDFC52026092700118", {"HDFC52026092700118"}),
        ("SWIFT/UTR Reference: BARC20260928X4471", {"BARC20260928X4471"}),
        ("NEFT ref - SBIN0012345678", {"SBIN0012345678"}),
        ("RTGS/NEFT Reference : RTGS PAYMENT", set()),
        ("Invoice JHMUR2510007033 for 1,452,299.16", set()),
    ],
)
def test_find_references(text, refs):
    assert find_references(text) == refs


def test_normalize_ref():
    assert normalize_ref(" hdfc-5202 6092 ") == "HDFC52026092"


@pytest.mark.parametrize(
    ("text", "signal"),
    [
        ("Rs.1,36,70,691.00 paid", True),
        ("UTR: HDFC52026092700118", True),
        ("Dear All, Please applied below payment.", False),
        ("", False),
    ],
)
def test_has_payment_signal(text, signal):
    assert has_payment_signal(text) is signal


def test_table_with_amounts_is_a_signal():
    assert has_payment_signal("", [[["Invoice", "Amount"], ["X1", "1,000.00"]]])


def test_payer_slug():
    assert payer_slug("Global TVS Bus Body Builders Ltd.") == "global-tvs-bus-body-builders"
    assert payer_slug(None) == ""


def _c(ref=None, rtype=None, payer="Acme Corp", total="100.00", date="2026-02-18"):
    return {
        "header": {
            "payment_reference": ref,
            "payment_reference_type": rtype,
            "payer_name": payer,
            "total_paid_amount": total,
            "payment_date": date,
        }
    }


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [
        (_c("hdfc-1234567890", "utr"), ("utr:HDFC1234567890", "strong")),
        (_c("SBIN99", "neft"), ("utr:SBIN99", "strong")),
        (_c("000123", "cheque"), ("chq:acme-corp:000123", "strong")),
        (
            _c("1500005408", "payer_document", date="2026-01-17"),
            ("doc:acme-corp:1500005408:2025-26", "strong"),
        ),
        (
            _c("1500005408", "payer_document", date="2026-04-02"),
            ("doc:acme-corp:1500005408:2026-27", "strong"),
        ),
        (_c("ZZ1", "something_else"), ("soft:acme-corp:100.00:2026-02-18", "weak")),
        (_c(None, None, date=None), ("soft:acme-corp:100.00:nodate", "weak")),
        (_c(None, None, payer=""), None),
    ],
)
def test_payment_key_for(canonical, expected):
    assert payment_key_for(canonical) == expected


def test_canonical_total():
    assert canonical_total(_c(total="1,000.50".replace(",", ""))) == Decimal("1000.50")
    assert canonical_total({}) is None


# R7 fix: stacked qualifiers (issue 2)
@pytest.mark.parametrize(
    ("text", "refs"),
    [
        ("UTR Ref No: HDFC52026092700118", {"HDFC52026092700118"}),
        ("UTR/Ref No HDFC52026092700118", {"HDFC52026092700118"}),
        ("IMPS Reference Number 123456789012", {"123456789012"}),
        ("NEFT - Reference no. SBIN0012345678", {"SBIN0012345678"}),
    ],
)
def test_find_references_stacked_qualifiers(text, refs):
    """Test that stacked qualifier keywords are handled correctly."""
    assert find_references(text) == refs


# R7 fix: space-grouped references (issue 3)
def test_find_references_space_grouped():
    """Test that spaces within tokens are preserved and normalized."""
    assert find_references("UTR HDFC5202 6092 7001 18") == {"HDFC52026092700118"}
    assert find_references("UTR HDFC52026092700118 dated 18.02") == {"HDFC52026092700118"}


# R7 fix: lowercase words rejection (issue 4)
def test_find_references_lowercase_rejected():
    """Test that lowercase tokens are rejected."""
    assert find_references("RTGS payment2024abc") == set()
    assert find_references("RTGS Payment2024ABC") == set()


# R7 fix: catastrophic backtracking performance (issue 1)
def test_find_references_no_backtracking_long_spaces():
    """Regression test: no catastrophic backtracking on long space sequences."""
    text = "UTR" + " " * 5000 + "!"
    start = time.perf_counter()
    result = find_references(text)
    elapsed = time.perf_counter() - start
    assert result == set()
    assert elapsed < 0.5, f"find_references took {elapsed:.2f}s (expected < 0.5s)"


def test_find_references_no_backtracking_repeated_keywords():
    """Regression test: no backtracking on repeated keywords."""
    text = "NEFT " * 2000
    start = time.perf_counter()
    result = find_references(text)
    elapsed = time.perf_counter() - start
    assert result == set()
    assert elapsed < 0.5, f"find_references took {elapsed:.2f}s (expected < 0.5s)"
