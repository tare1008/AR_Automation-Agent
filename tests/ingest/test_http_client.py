import base64

import httpx
import pytest

from ar_pipeline.ingest.client import DeltaExpired, HttpGraphClient


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
