"""Compatibility controls for security upgrades of cache locks and serialization."""

import pytest


def test_hub_cache_lock_acquires_and_releases(tmp_path):
    pytest.importorskip("huggingface_hub")
    from huggingface_hub.utils._fixes import WeakFileLock
    from filelock import FileLock
    path = tmp_path / 'download.lock'
    with WeakFileLock(path, timeout=2):
        assert path.is_file()
    lock = FileLock(path, timeout=2)
    with lock:
        assert lock.is_locked
    assert not lock.is_locked


def test_messagepack_preserves_nested_metadata():
    msgpack = pytest.importorskip("msgpack")
    value = {'session': 'retained', 'messages': [1, 2], 'payload': b'bytes'}
    assert msgpack.unpackb(msgpack.packb(value), raw=False) == value


def test_brotli_roundtrip():
    brotlicffi = pytest.importorskip("brotlicffi")
    content = b'retained attachment' * 128
    assert brotlicffi.decompress(brotlicffi.compress(content)) == content
