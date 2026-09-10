from __future__ import annotations

import contextlib
from collections.abc import Iterator

from ar_pipeline.deliver.backend_client import DeliveryResult


class FakeBackendClient:
    """Returns queued DeliveryResults (or repeats the last one). Records calls."""

    def __init__(self, results: list[DeliveryResult] | None = None) -> None:
        self._results = list(results or [DeliveryResult("ok", 201, "")])
        self._it: Iterator[DeliveryResult] = iter(self._results)
        self._last = self._results[-1]
        self.calls: list[tuple[dict, str]] = []

    def send(self, payload: dict, idempotency_key: str) -> DeliveryResult:
        self.calls.append((payload, idempotency_key))
        with contextlib.suppress(StopIteration):
            self._last = next(self._it)
        return self._last
