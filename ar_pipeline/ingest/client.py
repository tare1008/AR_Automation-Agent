from __future__ import annotations

from typing import Protocol

from ar_pipeline.ingest.types import DeltaResult, GraphAttachment


class DeltaExpired(Exception):
    """The stored delta link was rejected (HTTP 410). Caller must resync."""


class GraphClient(Protocol):
    def fetch_delta(self, delta_link: str | None) -> DeltaResult: ...

    def download_attachments(self, message_id: str) -> list[GraphAttachment]: ...
