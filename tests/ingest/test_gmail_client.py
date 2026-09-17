import base64
from typing import Any

import httpx
import pytest

from ar_pipeline.ingest.client import DeltaExpired, GraphThrottled
from ar_pipeline.ingest.gmail_client import GmailClient, _b64url_decode, _extract_address, _header


class _Auth:
    def token(self) -> str:
        return "tok"


def _client(handler) -> GmailClient:
    transport = httpx.MockTransport(handler)
    return GmailClient(
        _Auth(),  # type: ignore[arg-type]
        http=httpx.Client(
            transport=transport, base_url="https://gmail.googleapis.com/gmail/v1/users/me"
        ),
    )


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _full_message(mid: str, *, with_attachment: bool = False) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [
        {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("hello")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>hello</p>")}},
            ],
        }
    ]
    if with_attachment:
        parts.append(
            {
                "mimeType": "application/pdf",
                "filename": "invoice.pdf",
                "body": {"attachmentId": "att1", "size": 100},
            }
        )
    return {
        "id": mid,
        "internalDate": "1700000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "AP Team <ap@vendor.com>"},
                {"name": "Subject", "value": "Remittance Advice"},
                {"name": "Message-Id", "value": "<abc@vendor.com>"},
            ],
            "mimeType": "multipart/mixed",
            "parts": parts,
        },
    }


def test_full_sync_lists_inbox_and_fetches_each_message():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "me@gmail.com", "historyId": "100"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})
        if path.endswith("/messages/m1"):
            return httpx.Response(200, json=_full_message("m1"))
        if path.endswith("/messages/m2"):
            return httpx.Response(200, json=_full_message("m2"))
        raise AssertionError(f"unexpected request: {path}")

    result = _client(handler).fetch_delta(None)
    assert [m.id for m in result.messages] == ["m1", "m2"]
    assert result.delta_link == "100"
    assert all(m.body_html == "<p>hello</p>" for m in result.messages)
    assert all(m.internet_message_id == "<abc@vendor.com>" for m in result.messages)
    assert all(m.sender_address == "ap@vendor.com" for m in result.messages)


def test_full_sync_follows_message_list_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "1"})
        if path.endswith("/messages") and "pageToken" not in params:
            return httpx.Response(200, json={"messages": [{"id": "m1"}], "nextPageToken": "page2"})
        if path.endswith("/messages") and params.get("pageToken") == "page2":
            return httpx.Response(200, json={"messages": [{"id": "m2"}]})
        return httpx.Response(200, json=_full_message(path.rsplit("/", 1)[-1]))

    result = _client(handler).fetch_delta(None)
    assert [m.id for m in result.messages] == ["m1", "m2"]


def test_incremental_sync_reports_added_and_deleted():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/history"):
            return httpx.Response(
                200,
                json={
                    "history": [
                        {"messagesAdded": [{"message": {"id": "m1"}}]},
                        {"messagesDeleted": [{"message": {"id": "m2"}}]},
                    ],
                    "historyId": "200",
                },
            )
        if path.endswith("/messages/m1"):
            return httpx.Response(200, json=_full_message("m1"))
        raise AssertionError(f"unexpected request: {path}")

    result = _client(handler).fetch_delta("150")
    assert result.delta_link == "200"
    ids = {m.id: m for m in result.messages}
    assert ids["m1"].removed is False
    assert ids["m2"].removed is True


def test_incremental_sync_dedupes_repeated_message_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/history"):
            return httpx.Response(
                200,
                json={
                    "history": [
                        {"messagesAdded": [{"message": {"id": "m1"}}]},
                        {"messagesAdded": [{"message": {"id": "m1"}}]},
                    ],
                    "historyId": "200",
                },
            )
        return httpx.Response(200, json=_full_message("m1"))

    result = _client(handler).fetch_delta("150")
    assert [m.id for m in result.messages] == ["m1"]


def test_incremental_sync_follows_history_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path.endswith("/history") and "pageToken" not in params:
            return httpx.Response(
                200,
                json={
                    "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
                    "historyId": "160",
                    "nextPageToken": "page2",
                },
            )
        if path.endswith("/history") and params.get("pageToken") == "page2":
            return httpx.Response(
                200,
                json={
                    "history": [{"messagesAdded": [{"message": {"id": "m2"}}]}],
                    "historyId": "200",
                },
            )
        return httpx.Response(200, json=_full_message(path.rsplit("/", 1)[-1]))

    result = _client(handler).fetch_delta("100")
    assert [m.id for m in result.messages] == ["m1", "m2"]
    assert result.delta_link == "200"


def test_fetch_delta_raises_delta_expired_on_404():
    def handler(request):
        return httpx.Response(404, json={"error": "not found"})

    with pytest.raises(DeltaExpired):
        _client(handler).fetch_delta("stale-history-id")


def test_get_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("ar_pipeline.ingest.gmail_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"history": [], "historyId": "1"})

    # incremental sync makes exactly one _get() call (unlike a first-run full
    # sync, which calls /profile then /messages) — isolates the retry count.
    result = _client(handler).fetch_delta("0")
    assert result.delta_link == "1"
    assert calls["n"] == 2


def test_get_raises_graph_throttled_after_budget():
    def handler(request):
        return httpx.Response(500)

    with pytest.raises(GraphThrottled):
        _client(handler).fetch_delta(None)


def test_download_attachments_fetches_and_decodes_content():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/messages/m1"):
            return httpx.Response(200, json=_full_message("m1", with_attachment=True))
        if path.endswith("/attachments/att1"):
            return httpx.Response(200, json={"size": 5, "data": _b64("PDFDATA")})
        raise AssertionError(f"unexpected request: {path}")

    attachments = _client(handler).download_attachments("m1")
    assert len(attachments) == 1
    assert attachments[0].name == "invoice.pdf"
    assert attachments[0].content_type == "application/pdf"
    assert attachments[0].content == b"PDFDATA"


def test_download_attachments_skips_inline_body_parts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_full_message("m1", with_attachment=False))

    assert _client(handler).download_attachments("m1") == []


def test_b64url_decode_handles_missing_padding():
    encoded = base64.urlsafe_b64encode(b"hello world").decode().rstrip("=")
    assert _b64url_decode(encoded) == b"hello world"


def test_b64url_decode_returns_empty_on_garbage():
    assert _b64url_decode("not-valid-base64!!!") == b""


def test_header_is_case_insensitive():
    headers = [{"name": "Subject", "value": "Hi"}]
    assert _header(headers, "subject") == "Hi"
    assert _header(headers, "missing") == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AP Team <ap@vendor.com>", "ap@vendor.com"),
        ("ap@vendor.com", "ap@vendor.com"),
        ("", ""),
    ],
)
def test_extract_address(raw, expected):
    assert _extract_address(raw) == expected
