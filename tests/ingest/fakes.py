from __future__ import annotations

from ar_pipeline.ingest.client import DeltaExpired, GraphClient
from ar_pipeline.ingest.types import DeltaResult, GraphAttachment, GraphMessage


class FakeGraphClient(GraphClient):
    """In-memory GraphClient. Each fetch_delta returns messages appended
    since the caller's delta_link; delta_link is the message count so far
    encoded as a string."""

    def __init__(
        self,
        messages: list[GraphMessage],
        attachments: dict[str, list[GraphAttachment]] | None = None,
    ) -> None:
        self._messages = list(messages)
        self._attachments = attachments or {}
        self._issued: set[str] = set()
        self._redeliver = False

    def add_message(self, message: GraphMessage) -> None:
        self._messages.append(message)

    def redeliver_all(self) -> None:
        """Test hook (I7): make the next ``fetch_delta`` of a known link return
        every message again, so the poller's dedup paths see a re-delivery."""
        self._redeliver = True

    def fetch_delta(self, delta_link: str | None) -> DeltaResult:
        if delta_link is None:
            start = 0
        elif delta_link in self._issued:
            start = 0 if self._redeliver else int(delta_link.split(":")[1])
        else:
            raise DeltaExpired(delta_link)
        self._redeliver = False
        batch = self._messages[start:]
        new_link = f"delta:{len(self._messages)}"
        self._issued.add(new_link)
        return DeltaResult(messages=batch, delta_link=new_link)

    def download_attachments(self, message_id: str) -> list[GraphAttachment]:
        return list(self._attachments.get(message_id, []))
