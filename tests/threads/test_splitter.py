from __future__ import annotations

import re
import time
from datetime import UTC, datetime

from bs4 import BeautifulSoup

from ar_pipeline.threads.splitter import fingerprint, split_email
from tests.threads.chains import (
    FORWARD_BANNER_TEXT,
    GMAIL_REPLY_TEXT,
    chain_html,
    chain_text,
    remittance_html,
)

T0 = datetime(2026, 2, 18, 12, 8, tzinfo=UTC)
DOMAINS = ["adityabirla.com"]


def _split(html="", text="", sender="dharmendra.p.kumar-c@adityabirla.com"):
    return split_email(
        body_html=html, body_text=text, sender=sender, received_at=T0, client_domains=DOMAINS
    )


def _nonws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_outlook_html_chain_splits_into_one_part_per_message():
    parts = _split(html=chain_html([3, 2, 1]))
    assert [p.position for p in parts] == [0, 1, 2, 3]
    top = parts[0]
    assert top.is_internal and not top.has_payment_signal
    assert parts[1].sender == "str@alufluoride.com"
    assert parts[2].sender == "tejeswara rao s"  # bare display name in this copy
    for p in parts[1:]:
        assert p.has_payment_signal and len(p.tables) == 1
        assert "From:" in p.raw_header and "Sent:" in p.raw_header
    assert parts[1].sent_at is not None and parts[1].sent_at.day == 13


def test_plain_text_chain():
    parts = _split(text=chain_text([2, 1]))
    assert len(parts) == 3
    assert "PUNBR52026" in parts[1].body_text and parts[1].tables == []


def test_nothing_is_lost():
    html = chain_html([3, 2, 1])
    expected = _nonws(BeautifulSoup(html, "lxml").get_text())
    got = _nonws("".join(p.raw_header + p.body_text for p in _split(html=html)))
    assert got == expected


def test_gmail_reply():
    parts = _split(text=GMAIL_REPLY_TEXT, sender="ar@adityabirla.com")
    assert len(parts) == 2
    assert parts[1].sender == "str@alufluoride.com"
    assert parts[1].has_payment_signal


def test_forward_banner():
    parts = _split(text=FORWARD_BANNER_TEXT, sender="me@example.com")
    assert len(parts) == 2
    assert parts[1].sender == "dharmendra.p.kumar-c@adityabirla.com"
    assert parts[1].is_internal
    assert parts[1].sent_at == datetime(2026, 2, 18, 12, 8, 59, tzinfo=UTC)


def test_no_quotes_is_one_message():
    parts = _split(
        text="Payment of INR 1,000.00 made. UTR: HDFC52026092700118", sender="ap@payer.example"
    )
    assert len(parts) == 1 and parts[0].position == 0 and not parts[0].is_internal


def test_empty_body_gives_no_parts():
    assert _split() == []


def test_fingerprint_is_stable_across_html_and_reformatting():
    a = _split(html=chain_html([5]))[1]
    b = _split(html=chain_html([7, 5]))[2]  # same message quoted one level deeper
    assert a.fingerprint is not None and a.fingerprint == b.fingerprint


def test_fingerprint_ignores_caution_banner_and_quote_markers():
    body = remittance_html(1)
    text = BeautifulSoup(body, "lxml").get_text("\n")
    banner = (
        "CAUTION: This email originated from outside of the organization. Do not click links "
        "or open attachments unless you recognize the sender and know the content is safe.\n"
    )
    quoted = "\n".join("> " + line for line in text.splitlines())
    assert fingerprint(text) == fingerprint(banner + quoted)


def test_short_text_has_no_fingerprint():
    assert fingerprint("Please find attached.") is None


def test_splitting_large_bodies_is_fast():
    start = time.monotonic()
    _split(text="On Mon, " + " " * 5000 + "x\n" + "From: " + " " * 5000 + "y\n")
    _split(text="\n".join(f"line {i} Rs.{i}.00 From: a Sent: b" for i in range(2000)))
    _split(html="<p>" + " " * 5000 + "From:" + " " * 5000 + "</p>")
    assert time.monotonic() - start < 1.0
