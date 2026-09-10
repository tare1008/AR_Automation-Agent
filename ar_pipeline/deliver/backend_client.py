"""POST a canonical remittance payload to the backend REST API.

``BackendClient`` wraps an injectable ``httpx.Client`` (tests pass an
``httpx.MockTransport`` / ``ASGITransport``) and classifies each POST as
``ok`` / ``permanent_fail`` / ``transient_fail`` so ``deliverer`` can decide
between marking the row delivered, failing it, or scheduling a retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from ar_pipeline.config import get_settings

DeliveryOutcome = Literal["ok", "permanent_fail", "transient_fail"]

_DETAIL_CAP = 500
_TIMEOUT = httpx.Timeout(30.0)


@dataclass(frozen=True)
class DeliveryResult:
    outcome: DeliveryOutcome
    status_code: int | None
    detail: str


def _bounded(s: str) -> str:
    return s if len(s) <= _DETAIL_CAP else s[: _DETAIL_CAP - 1] + "…"


class BackendClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth_header: str = "",
        http: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._auth_header = auth_header
        self._http = http or httpx.Client(base_url=self._base, timeout=_TIMEOUT)

    def send(self, payload: dict, idempotency_key: str) -> DeliveryResult:
        headers = {"Idempotency-Key": idempotency_key}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        try:
            resp = self._http.post(f"{self._base}/remittances", json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return DeliveryResult("transient_fail", None, _bounded(repr(exc)))

        code = resp.status_code
        if 200 <= code < 300:
            return DeliveryResult("ok", code, _bounded(resp.text))
        if code == 429 or code >= 500:
            return DeliveryResult("transient_fail", code, _bounded(resp.text))
        return DeliveryResult("permanent_fail", code, _bounded(f"{code} {resp.text}"))


def get_backend_client() -> BackendClient:
    s = get_settings()
    return BackendClient(
        base_url=s.backend_url,
        auth_header=s.backend_auth_header.get_secret_value(),
    )
