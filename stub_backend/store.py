"""In-process store for received remittances, persisted to disk.

This stub stands in for a real backend during demos. Demos routinely
restart this process (or the whole 3-terminal stack) without restarting
the pipeline's own database, so keeping the in-memory dicts alone meant a
fresh process silently forgot everything the pipeline had already marked
"delivered" — looking like a bug rather than a stub-only limitation. The
JSON file below keeps the two in sync across restarts.
"""

from __future__ import annotations

import json
import os
import pathlib

# STUB_BACKEND_STORE_PATH="" (set by tests/conftest.py) disables persistence
# entirely, so test runs never write into the working tree.
_env_path = os.environ.get("STUB_BACKEND_STORE_PATH")
if _env_path is None:
    _STORE_PATH: pathlib.Path | None = (
        pathlib.Path(__file__).resolve().parent.parent / "data" / "stub_backend_store.json"
    )
else:
    _STORE_PATH = pathlib.Path(_env_path) if _env_path else None

RECEIVED: dict[str, dict] = {}
_IDEMPOTENCY: dict[str, str] = {}


def _load() -> None:
    if _STORE_PATH is None or not _STORE_PATH.exists():
        return
    try:
        data = json.loads(_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return
    RECEIVED.update(data.get("received", {}))
    _IDEMPOTENCY.update(data.get("idempotency", {}))


def _save() -> None:
    if _STORE_PATH is None:
        return
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(
        json.dumps({"received": RECEIVED, "idempotency": _IDEMPOTENCY}, indent=2)
    )


def received_list() -> list[dict]:
    return list(RECEIVED.values())


def lookup_idempotency(key: str) -> str | None:
    return _IDEMPOTENCY.get(key)


def record(extraction_id: str, payload: dict, idempotency_key: str | None) -> None:
    RECEIVED[extraction_id] = payload
    if idempotency_key:
        _IDEMPOTENCY[idempotency_key] = extraction_id
    _save()


_load()
