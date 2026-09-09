from __future__ import annotations

import base64
import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ar_pipeline.config import get_settings
from ar_pipeline.ingest.auth import GraphAuth
from ar_pipeline.ingest.types import DeltaResult, GraphAttachment, GraphMessage

log = logging.getLogger(__name__)


class DeltaExpired(Exception):
    """The stored delta link was rejected (HTTP 410). Caller must resync."""


class GraphThrottled(RuntimeError):
    """Graph kept returning 429/5xx until the retry budget was exhausted."""


class GraphProtocolError(RuntimeError):
    """Graph returned a 200 response that violates the documented contract."""


class GraphClient(Protocol):
    def fetch_delta(self, delta_link: str | None) -> DeltaResult: ...

    def download_attachments(self, message_id: str) -> list[GraphAttachment]: ...


_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_DELTA_SELECT = "id,internetMessageId,from,subject,receivedDateTime,body,bodyPreview,hasAttachments"
_FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"
_DEFAULT_RETRY_DELAY = 2.0


class HttpGraphClient:
    """httpx-backed Microsoft Graph client implementing the ``GraphClient`` protocol."""

    def __init__(
        self,
        auth: GraphAuth,
        mailbox: str,
        *,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._auth = auth
        self._mailbox = mailbox
        self._http = http or httpx.Client(base_url=_GRAPH_BASE_URL, timeout=30)
        self._max_attempts = max_attempts

    @classmethod
    def from_settings(cls) -> HttpGraphClient:
        return cls(GraphAuth.from_settings(), get_settings().shared_mailbox)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HttpGraphClient:
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

    def _get(self, url: str) -> dict[str, Any]:
        last_status: int | None = None
        for attempt in range(self._max_attempts):
            headers = {"Authorization": f"Bearer {self._auth.token()}"}
            response = self._http.get(url, headers=headers)
            if response.status_code == 410:
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
            f"Graph API kept returning {last_status} after {self._max_attempts} attempts: {url}"
        )

    def fetch_delta(self, delta_link: str | None) -> DeltaResult:
        if delta_link is None:
            mailbox = quote(self._mailbox, safe="@")
            url = f"/users/{mailbox}/mailFolders/inbox/messages/delta?$select={_DELTA_SELECT}"
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

        if not new_delta_link:
            raise GraphProtocolError("Graph delta response had no @odata.deltaLink")

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
        raw_received = item.get("receivedDateTime")
        if raw_received:
            received_at = datetime.fromisoformat(raw_received.replace("Z", "+00:00"))
        else:
            received_at = datetime.now(UTC)

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
        mailbox = quote(self._mailbox, safe="@")
        message = quote(message_id, safe="")
        url = f"/users/{mailbox}/messages/{message}/attachments"
        attachments: list[GraphAttachment] = []
        while True:
            payload = self._get(url)
            for item in payload.get("value", []):
                if item.get("@odata.type") != _FILE_ATTACHMENT_TYPE:
                    log.info("skipping non-file attachment: %s", item.get("@odata.type"))
                    continue
                raw_content = item.get("contentBytes", "")
                if not raw_content:
                    log.info(
                        "skipping fileAttachment without contentBytes: %s",
                        item.get("name"),
                    )
                    continue
                attachments.append(
                    GraphAttachment(
                        name=item.get("name", ""),
                        content_type=item.get("contentType", ""),
                        size=int(item.get("size", 0)),
                        content=base64.b64decode(raw_content),
                    )
                )
            next_link = payload.get("@odata.nextLink")
            if not next_link:
                break
            url = next_link
        return attachments
