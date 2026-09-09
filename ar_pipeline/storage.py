from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from ar_pipeline.config import get_settings


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def sha256(self, data: bytes) -> str: ...


class LocalBlobStore:
    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        p = (self._root / key).resolve()
        if not p.is_relative_to(self._root.resolve()):
            raise ValueError(f"key escapes storage root: {key!r}")
        return p

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file://{path.resolve()}"

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def sha256(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


def get_blob_store() -> BlobStore:
    return LocalBlobStore(get_settings().blob_dir)
