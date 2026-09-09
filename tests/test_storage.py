import hashlib

from ar_pipeline.storage import LocalBlobStore


def test_put_then_get_roundtrips(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    url = store.put("vendor/msg1/settlement.xlsx", b"binary-bytes")
    assert url.startswith("file://")
    assert store.get("vendor/msg1/settlement.xlsx") == b"binary-bytes"


def test_sha256_matches_hashlib():
    store = LocalBlobStore("/tmp")
    assert store.sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_get_missing_key_raises(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    try:
        store.get("nope")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
