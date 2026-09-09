from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from ar_pipeline.config import get_settings
from ar_pipeline.ingest.auth import GraphAuth
from ar_pipeline.ingest.types import DeltaResult, GraphAttachment, GraphMessage


class DeltaExpired(Exception):
    """The stored delta link was rejected (HTTP 410). Caller must resync."""


class GraphClient(Protocol):
    def fetch_delta(self, delta_link: str | None) -> DeltaResult: ...

    def download_attachments(self, message_id: str) -> list[GraphAttachment]: ...


_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_DELTA_SELECT = "id,internetMessageId,from,subject,receivedDateTime,body,bodyPreview,hasAttachments"
_FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"


class HttpGraphClient:
    """httpx-backed Microsoft Graph client implementing the ``GraphClient`` protocol."""

    def __init__(
        self,
        auth: GraphAuth,
        mailbox: str,
        *,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ) -> None:
        self._auth = auth
        self._mailbox = mailbox
        self._http = http or httpx.Client(base_url=_GRAPH_BASE_URL, timeout=30)
        self._max_retries = max_retries

    @classmethod
    def from_settings(cls) -> HttpGraphClient:
        return cls(GraphAuth.from_settings(), get_settings().shared_mailbox)

    def _get(self, url: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._auth.token()}"}
        for _attempt in range(self._max_retries):
            response = self._http.get(url, headers=headers)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "2"))
                time.sleep(retry_after)
                continue
            if response.status_code == 410:
                raise DeltaExpired(url)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            return payload
        raise RuntimeError(
            f"Graph API kept returning 429 after {self._max_retries} attempts: {url}"
        )

    def fetch_delta(self, delta_link: str | None) -> DeltaResult:
        if delta_link is None:
            url = f"/users/{self._mailbox}/mailFolders/inbox/messages/delta?$select={_DELTA_SELECT}"
        else:
            url = delta_link

        messages: list[GraphMessage] = []
        new_delta_link = ""
        while True:
            payload = self._get(url)
            messages.extend(self._parse_message(item) for item in payload.get("value", []))
            next_link = payload.get("@odata.nextLink")
            if next_link:
                url = next_link
                continue
            new_delta_link = payload.get("@odata.deltaLink", "")
            break

        return DeltaResult(messages, new_delta_link)

    def _parse_message(self, item: dict[str, Any]) -> GraphMessage:
        if "@removed" in item:
            return GraphMessage(
                id=item["id"],
                internet_message_id="",
                sender_address="",
                subject="",
                received_at=datetime.now(UTC),
                body_html="",
                body_text="",
                has_attachments=False,
                removed=True,
            )

        body = item.get("body") or {}
        content_type = body.get("contentType", "")
        content = body.get("content", "")
        sender = ((item.get("from") or {}).get("emailAddress") or {}).get("address", "")
        received_at = datetime.fromisoformat(item["receivedDateTime"].replace("Z", "+00:00"))

        return GraphMessage(
            id=item["id"],
            internet_message_id=item.get("internetMessageId", ""),
            sender_address=sender,
            subject=item.get("subject", ""),
            received_at=received_at,
            body_html=content if content_type == "html" else "",
            body_text=content if content_type == "text" else item.get("bodyPreview", ""),
            has_attachments=bool(item.get("hasAttachments", False)),
        )

    def download_attachments(self, message_id: str) -> list[GraphAttachment]:
        url = f"/users/{self._mailbox}/messages/{message_id}/attachments"
        attachments: list[GraphAttachment] = []
        while True:
            payload = self._get(url)
            for item in payload.get("value", []):
                if item.get("@odata.type") != _FILE_ATTACHMENT_TYPE:
                    continue
                attachments.append(
                    GraphAttachment(
                        name=item.get("name", ""),
                        content_type=item.get("contentType", ""),
                        size=int(item.get("size", 0)),
                        content=base64.b64decode(item["contentBytes"]),
                    )
                )
            next_link = payload.get("@odata.nextLink")
            if not next_link:
                break
            url = next_link
        return attachments
