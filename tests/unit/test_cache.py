"""Tests for office_convert.cache: atomic writes, key layout, disabled mode."""

from __future__ import annotations

from pathlib import Path

from office_convert.cache import CacheManager, atomic_write


def test_disabled_cache_is_no_op(tmp_path: Path) -> None:
    src = tmp_path / "src.pdf"
    src.write_bytes(b"hello")
    cache = CacheManager(cache_dir=None, aspose_version="24.6")
    assert not cache.enabled()
    assert cache.get_final("anything") is None
    assert cache.get_chunk("anything") is None
    cache.put_final("anything", src)  # no-op, no error
    cache.put_chunk("anything", src)


def test_enabled_cache_directory_layout(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    # CacheManager() is constructed only for the directory-creation side-effect.
    CacheManager(cache_dir=cache_dir, aspose_version="24.6")
    assert (cache_dir / "24.6" / "final").is_dir()
    assert (cache_dir / "24.6" / "chunks").is_dir()


def test_put_then_get_final(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache = CacheManager(cache_dir=cache_dir, aspose_version="24.6")
    src = tmp_path / "src.pdf"
    src.write_bytes(b"final pdf bytes")
    cache.put_final("sha-abc", src)
    cached = cache.get_final("sha-abc")
    assert cached is not None
    assert cached.read_bytes() == b"final pdf bytes"
    # Path layout
    assert cached == cache_dir / "24.6" / "final" / "sha-abc.pdf"


def test_put_then_get_chunk(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache = CacheManager(cache_dir=cache_dir, aspose_version="24.6")
    src = tmp_path / "chunk.pdf"
    src.write_bytes(b"chunk pdf bytes")
    cache.put_chunk("chunk-sha", src)
    cached = cache.get_chunk("chunk-sha")
    assert cached is not None
    assert cached.read_bytes() == b"chunk pdf bytes"


def test_get_miss_returns_none(tmp_path: Path) -> None:
    cache = CacheManager(cache_dir=tmp_path / "cache", aspose_version="24.6")
    assert cache.get_final("never-written") is None
    assert cache.get_chunk("never-written") is None


def test_version_namespacing(tmp_path: Path) -> None:
    src = tmp_path / "src.pdf"
    src.write_bytes(b"x")
    cache_a = CacheManager(cache_dir=tmp_path / "cache", aspose_version="24.6")
    cache_b = CacheManager(cache_dir=tmp_path / "cache", aspose_version="25.1")
    cache_a.put_final("sha-x", src)
    assert cache_a.get_final("sha-x") is not None
    # Different version → different namespace → cache miss
    assert cache_b.get_final("sha-x") is None


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.pdf"
    target.write_bytes(b"old")
    src = tmp_path / "src.pdf"
    src.write_bytes(b"new")
    atomic_write(target, src)
    assert target.read_bytes() == b"new"


def test_atomic_write_no_leftover_tmp_files(tmp_path: Path) -> None:
    target = tmp_path / "out.pdf"
    src = tmp_path / "src.pdf"
    src.write_bytes(b"data")
    atomic_write(target, src)
    leftovers = list(tmp_path.glob("*.tmp.*"))
    assert leftovers == []


def test_tee_finalize_renames_temp(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache = CacheManager(cache_dir=cache_dir, aspose_version="24.6")
    temp = cache.final_temp_path("sha-tee")
    assert temp is not None
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_bytes(b"streamed bytes")
    cache.finalize_final("sha-tee", temp)
    cached = cache.get_final("sha-tee")
    assert cached is not None
    assert cached.read_bytes() == b"streamed bytes"
    assert not temp.exists()
