import hashlib

import pytest

from ar_pipeline.storage import LocalBlobStore


def test_put_then_get_roundtrips(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    url = store.put("vendor/msg1/settlement.xlsx", b"binary-bytes")
    assert url.startswith("file://")
    assert store.get("vendor/msg1/settlement.xlsx") == b"binary-bytes"


def test_sha256_matches_hashlib(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    assert store.sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_get_missing_key_raises(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        store.get("nope")


def test_key_escaping_root_rejected(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.put("../evil", b"x")
