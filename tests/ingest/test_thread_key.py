from __future__ import annotations

from pathlib import Path

from ar_pipeline.ingest.eml import parse_eml
from ar_pipeline.ingest.gmail_client import GmailClient


def test_eml_thread_key_is_the_root_of_references(tmp_path: Path):
    p = tmp_path / "x.eml"
    p.write_text(
        "Message-ID: <c@x>\nReferences: <root@x> <b@x>\nIn-Reply-To: <b@x>\n"
        "From: a@b.com\nSubject: s\nDate: Wed, 18 Feb 2026 12:08:59 +0000\n\nbody\n"
    )
    msg, _ = parse_eml(p)
    assert msg.thread_key == "<root@x>"


def test_eml_without_references_uses_in_reply_to_then_own_id(tmp_path: Path):
    p = tmp_path / "y.eml"
    p.write_text("Message-ID: <solo@x>\nFrom: a@b.com\nSubject: s\n\nbody\n")
    msg, _ = parse_eml(p)
    assert msg.thread_key == "<solo@x>"


def test_gmail_thread_id_becomes_thread_key():
    client = GmailClient(auth=object())  # type: ignore[arg-type]
    try:
        msg = client._parse_message(  # noqa: SLF001 — parsing only, no network
            {
                "id": "m1",
                "threadId": "t-42",
                "internalDate": "0",
                "payload": {"headers": [{"name": "Message-Id", "value": "<m@x>"}]},
            }
        )
    finally:
        client.close()
    assert msg.thread_key == "t-42"
