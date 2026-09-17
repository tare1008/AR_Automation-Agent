import base64
from unittest.mock import patch

import httpx
import pytest

from ar_pipeline.ingest.auth import DelegatedGraphAuth
from ar_pipeline.ingest.client import (
    DeltaExpired,
    GraphProtocolError,
    GraphThrottled,
    HttpGraphClient,
)


class _Auth:
    def token(self) -> str:
        return "tok"


def _client(handler) -> HttpGraphClient:
    transport = httpx.MockTransport(handler)
    return HttpGraphClient(
        _Auth(),  # type: ignore[arg-type]
        "ar@company.com",
        http=httpx.Client(transport=transport, base_url="https://graph.microsoft.com/v1.0"),
    )


def _message(mid, imid, *, html="<p>x</p>"):
    return {
        "id": mid,
        "internetMessageId": imid,
        "from": {"emailAddress": {"address": "ap@vendor.com"}},
        "subject": "Remittance",
        "receivedDateTime": "2026-09-09T10:00:00Z",
        "body": {"contentType": "html", "content": html},
        "bodyPreview": "x",
        "hasAttachments": True,
    }


def test_client_close_and_context_manager_close_http():
    closed = {"n": 0}

    def handler(request):
        return httpx.Response(200, json={"value": []})

    client = _client(handler)
    original_close = client._http.close

    def _tracked_close():
        closed["n"] += 1
        original_close()

    client._http.close = _tracked_close  # type: ignore[method-assign]
    with client as c:
        assert c is client
    assert closed["n"] == 1


@patch("ar_pipeline.ingest.auth.msal.PublicClientApplication")
def test_delegated_auth_uses_me_not_users_mailbox(mock_app_cls, tmp_path):
    auth = DelegatedGraphAuth(
        "client-id", "https://login.microsoftonline.com/consumers", tmp_path / "cache.json"
    )
    mock_app_cls.return_value.get_accounts.return_value = [{"username": "someone@outlook.com"}]
    mock_app_cls.return_value.acquire_token_silent.return_value = {"access_token": "tok"}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, json={"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA"}
        )

    transport = httpx.MockTransport(handler)
    client = HttpGraphClient(
        auth,
        "someone@outlook.com",
        http=httpx.Client(transport=transport, base_url="https://graph.microsoft.com/v1.0"),
    )
    client.fetch_delta(None)
    assert calls[0].startswith("https://graph.microsoft.com/v1.0/me/mailFolders/inbox/")
    assert "/users/" not in calls[0]


def test_fetch_delta_follows_pages_and_returns_delta_link():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "nextpage" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [_message("m2", "<b@v.com>")],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [_message("m1", "<a@v.com>")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/nextpage",
            },
        )

    result = _client(handler).fetch_delta(None)
    assert [m.id for m in result.messages] == ["m1", "m2"]
    assert result.delta_link == "https://graph.microsoft.com/v1.0/DELTA"
    assert len(calls) == 2


def test_fetch_delta_maps_removed_messages():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [{"id": "gone", "@removed": {"reason": "deleted"}}],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA",
            },
        )

    result = _client(handler).fetch_delta(None)
    assert result.messages[0].removed is True
    assert result.messages[0].id == "gone"


def test_fetch_delta_raises_delta_expired_on_410():
    def handler(request):
        return httpx.Response(410, json={"error": {"code": "SyncStateNotFound"}})

    with pytest.raises(DeltaExpired):
        _client(handler).fetch_delta("https://graph.microsoft.com/v1.0/OLD")


def test_fetch_delta_retries_on_429_then_succeeds():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA",
            },
        )

    result = _client(handler).fetch_delta(None)
    assert result.delta_link.endswith("/DELTA")
    assert state["n"] == 2


def test_fetch_delta_raises_when_no_delta_link(monkeypatch):
    monkeypatch.setattr("ar_pipeline.ingest.client.time.sleep", lambda _s: None)

    def handler(request):
        return httpx.Response(200, json={"value": [_message("m1", "<a@v.com>")]})

    with pytest.raises(GraphProtocolError):
        _client(handler).fetch_delta(None)


def test_fetch_delta_retries_on_500_then_succeeds(monkeypatch):
    monkeypatch.setattr("ar_pipeline.ingest.client.time.sleep", lambda _s: None)
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json={})
        return httpx.Response(
            200,
            json={"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA"},
        )

    result = _client(handler).fetch_delta(None)
    assert result.delta_link.endswith("/DELTA")
    assert state["n"] == 2


def test_fetch_delta_raises_graph_throttled_after_budget(monkeypatch):
    monkeypatch.setattr("ar_pipeline.ingest.client.time.sleep", lambda _s: None)

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "not-a-number"}, json={})

    with pytest.raises(GraphThrottled):
        _client(handler).fetch_delta(None)


def test_parse_message_tolerates_missing_received_date():
    def handler(request):
        item = _message("m1", "<a@v.com>")
        del item["receivedDateTime"]
        return httpx.Response(
            200,
            json={"value": [item], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA"},
        )

    result = _client(handler).fetch_delta(None)
    assert result.messages[0].received_at is not None


def test_download_attachments_skips_file_attachment_without_content_bytes():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "big.pdf",
                        "contentType": "application/pdf",
                        "size": 9999999,
                    }
                ]
            },
        )

    assert _client(handler).download_attachments("m1") == []


def test_download_attachments_url_encodes_message_id():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"value": []})

    _client(handler).download_attachments("AAA/BBB+CC=")
    assert "AAA/BBB+CC=" not in seen["url"]
    assert "AAA%2FBBB%2BCC%3D" in seen["url"]


def test_download_attachments_decodes_file_attachments():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "s.xlsx",
                        "contentType": "application/vnd.ms-excel",
                        "size": 3,
                        "contentBytes": base64.b64encode(b"abc").decode(),
                    },
                    {"@odata.type": "#microsoft.graph.itemAttachment", "name": "skip"},
                ]
            },
        )

    got = _client(handler).download_attachments("m1")
    assert len(got) == 1
    assert got[0].name == "s.xlsx"
    assert got[0].content == b"abc"
