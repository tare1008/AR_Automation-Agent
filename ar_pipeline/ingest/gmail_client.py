"""httpx-backed Gmail client implementing the same ``GraphClient`` protocol
as ``client.py``'s ``HttpGraphClient`` — ``poller.py`` runs unchanged against
either provider.

Gmail's incremental-sync primitive is its History API (a numeric
``historyId`` cursor), which plays the same role as Graph's opaque delta
link: store it, hand it back next poll, get only what changed since. The
``delta_link`` field in ``DeltaResult``/``PollState.delta_token`` just holds
that historyId as a string here instead of a Graph delta URL.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from ar_pipeline.ingest.client import DeltaExpired, GraphProtocolError, GraphThrottled
from ar_pipeline.ingest.gmail_auth import GmailAuth
from ar_pipeline.ingest.types import DeltaResult, GraphAttachment, GraphMessage

log = logging.getLogger(__name__)

_GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
_DEFAULT_RETRY_DELAY = 2.0
_LIST_PAGE_SIZE = 100


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    translated = padded.translate({ord("-"): ord("+"), ord("_"): ord("/")})
    try:
        return base64.b64decode(translated, validate=True)
    except binascii.Error:
        return b""


def _header(headers: list[dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _walk_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a (possibly nested multipart) message payload into a list of
    every leaf part — Gmail nests multipart/alternative and multipart/mixed
    arbitrarily deep for a message with both an HTML body and attachments."""
    parts = payload.get("parts")
    if not parts:
        return [payload]
    out: list[dict[str, Any]] = []
    for part in parts:
        out.extend(_walk_parts(part))
    return out


class GmailClient:
    """Implements the ``GraphClient`` protocol against the real Gmail REST API."""

    def __init__(
        self,
        auth: GmailAuth,
        *,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._auth = auth
        self._http = http or httpx.Client(base_url=_GMAIL_BASE_URL, timeout=30)
        self._max_attempts = max_attempts

    @classmethod
    def from_settings(cls) -> GmailClient:
        return cls(GmailAuth.from_settings())

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GmailClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _retry_delay(response: httpx.Response) -> float:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return _DEFAULT_RETRY_DELAY
        try:
            return float(int(raw))
        except (ValueError, TypeError):
            return _DEFAULT_RETRY_DELAY

    def _get(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_status: int | None = None
        for attempt in range(self._max_attempts):
            headers = {"Authorization": f"Bearer {self._auth.token()}"}
            response = self._http.get(url, headers=headers, params=params)
            if response.status_code == 404:
                # Gmail's equivalent of Graph's 410: the historyId is older
                # than the account's retention window — caller must resync.
                raise DeltaExpired(url)
            if response.status_code == 429 or response.status_code >= 500:
                last_status = response.status_code
                if attempt + 1 < self._max_attempts:
                    time.sleep(self._retry_delay(response))
                continue
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            return payload
        raise GraphThrottled(
            f"Gmail API kept returning {last_status} after {self._max_attempts} attempts: {url}"
        )

    def fetch_delta(self, delta_link: str | None) -> DeltaResult:
        if delta_link is None:
            return self._full_sync()
        return self._incremental_sync(delta_link)

    def _full_sync(self) -> DeltaResult:
        profile = self._get("/profile")
        history_id = profile.get("historyId")
        if not history_id:
            raise GraphProtocolError("Gmail profile response had no historyId")

        message_ids: list[str] = []
        params: dict[str, Any] = {"labelIds": "INBOX", "maxResults": _LIST_PAGE_SIZE}
        while True:
            payload = self._get("/messages", params=params)
            message_ids.extend(m["id"] for m in payload.get("messages", []))
            next_token = payload.get("nextPageToken")
            if not next_token:
                break
            params["pageToken"] = next_token

        messages = [self._fetch_message(mid) for mid in message_ids]
        return DeltaResult(messages, str(history_id))

    def _incremental_sync(self, start_history_id: str) -> DeltaResult:
        messages: list[GraphMessage] = []
        seen: set[str] = set()
        latest_history_id = start_history_id
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
        }
        while True:
            payload = self._get("/history", params=params)
            for entry in payload.get("history", []):
                for added in entry.get("messagesAdded", []):
                    mid = added.get("message", {}).get("id")
                    if mid and mid not in seen:
                        seen.add(mid)
                        messages.append(self._fetch_message(mid))
                for deleted in entry.get("messagesDeleted", []):
                    mid = deleted.get("message", {}).get("id")
                    if mid:
                        messages.append(_removed_message(mid))
            latest_history_id = str(payload.get("historyId", latest_history_id))
            next_token = payload.get("nextPageToken")
            if not next_token:
                break
            params["pageToken"] = next_token
        return DeltaResult(messages, latest_history_id)

    def _fetch_message(self, message_id: str) -> GraphMessage:
        item = self._get(f"/messages/{message_id}", params={"format": "full"})
        payload = item.get("payload", {})
        headers = payload.get("headers", [])
        parts = _walk_parts(payload)

        body_html = ""
        body_text = ""
        has_attachments = False
        for part in parts:
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            filename = part.get("filename", "")
            if filename and body.get("attachmentId"):
                has_attachments = True
                continue
            data = body.get("data")
            if not data:
                continue
            if mime == "text/html" and not body_html:
                body_html = _b64url_decode(data).decode("utf-8", errors="replace")
            elif mime == "text/plain" and not body_text:
                body_text = _b64url_decode(data).decode("utf-8", errors="replace")

        raw_date = item.get("internalDate")
        received_at = (
            datetime.fromtimestamp(int(raw_date) / 1000, tz=UTC) if raw_date else datetime.now(UTC)
        )

        return GraphMessage(
            id=item["id"],
            internet_message_id=_header(headers, "Message-Id"),
            sender_address=_extract_address(_header(headers, "From")),
            subject=_header(headers, "Subject"),
            received_at=received_at,
            body_html=body_html,
            body_text=body_text or item.get("snippet", ""),
            has_attachments=has_attachments,
        )

    def download_attachments(self, message_id: str) -> list[GraphAttachment]:
        item = self._get(f"/messages/{message_id}", params={"format": "full"})
        parts = _walk_parts(item.get("payload", {}))
        attachments: list[GraphAttachment] = []
        for part in parts:
            body = part.get("body", {})
            filename = part.get("filename", "")
            attachment_id = body.get("attachmentId")
            if not (filename and attachment_id):
                continue
            data = self._get(f"/messages/{message_id}/attachments/{attachment_id}")
            content = _b64url_decode(data.get("data", ""))
            attachments.append(
                GraphAttachment(
                    name=filename,
                    content_type=part.get("mimeType", "application/octet-stream"),
                    size=int(data.get("size", len(content))),
                    content=content,
                )
            )
        return attachments


def _removed_message(message_id: str) -> GraphMessage:
    return GraphMessage(
        id=message_id,
        internet_message_id="",
        sender_address="",
        subject="",
        received_at=datetime.now(UTC),
        body_html="",
        body_text="",
        has_attachments=False,
        removed=True,
    )


def _extract_address(from_header: str) -> str:
    """``'Name <a@b.com>'`` or ``'a@b.com'`` -> ``'a@b.com'``."""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<", 1)[1].split(">", 1)[0].strip()
    return from_header.strip()
