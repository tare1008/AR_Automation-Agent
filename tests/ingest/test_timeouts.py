"""Mailbox clients must fail over quickly when a network path is dead.

Python tries every resolved address in turn, waiting the connect timeout on
each. With broken IPv6 and 8 AAAA records ahead of the A records, a 30s
connect timeout meant ~4 minutes per call before reaching a working address.
"""

from __future__ import annotations

from ar_pipeline.ingest.client import HttpGraphClient
from ar_pipeline.ingest.gmail_client import GmailClient


def test_gmail_client_uses_a_short_connect_timeout():
    client = GmailClient(auth=object())  # type: ignore[arg-type]
    try:
        assert client._http.timeout.connect == 3.0
        assert client._http.timeout.read == 30.0
    finally:
        client.close()


def test_graph_client_uses_a_short_connect_timeout():
    client = HttpGraphClient(object(), "ar@example.com")  # type: ignore[arg-type]
    try:
        assert client._http.timeout.connect == 3.0
        assert client._http.timeout.read == 30.0
    finally:
        client.close()
