"""Filesystem cache for final and per-chunk PDFs.

Implements FR-7. Keys include Aspose version so version upgrades naturally
invalidate cached entries. Atomic write protocol per
business-rules.md §7 and nfr-design-patterns.md §6.

When `cache_dir` is None the cache is disabled (all get/put are no-ops).
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from contextlib import suppress
from pathlib import Path

log = logging.getLogger(__name__)


class CacheManager:
    """Content-addressable cache. Keyed by `<aspose_version>/<final|chunks>/<sha>.pdf`."""

    def __init__(self, cache_dir: Path | None, aspose_version: str) -> None:
        self.cache_dir = cache_dir
        self.aspose_version = aspose_version
        if cache_dir is not None:
            (cache_dir / aspose_version / "final").mkdir(parents=True, exist_ok=True)
            (cache_dir / aspose_version / "chunks").mkdir(parents=True, exist_ok=True)

    def enabled(self) -> bool:
        return self.cache_dir is not None

    def _final_path(self, source_sha256: str) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / self.aspose_version / "final" / f"{source_sha256}.pdf"

    def _chunk_path(self, chunk_sha256: str) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / self.aspose_version / "chunks" / f"{chunk_sha256}.pdf"

    def get_final(self, source_sha256: str) -> Path | None:
        if not self.enabled():
            return None
        path = self._final_path(source_sha256)
        return path if path.exists() else None

    def put_final(self, source_sha256: str, pdf_path: Path) -> None:
        if not self.enabled():
            return
        atomic_write(self._final_path(source_sha256), pdf_path)

    def get_chunk(self, chunk_sha256: str) -> Path | None:
        if not self.enabled():
            return None
        path = self._chunk_path(chunk_sha256)
        return path if path.exists() else None

    def put_chunk(self, chunk_sha256: str, pdf_path: Path) -> None:
        if not self.enabled():
            return
        atomic_write(self._chunk_path(chunk_sha256), pdf_path)

    def final_temp_path(self, source_sha256: str) -> Path | None:
        """Return a temp path for tee-to-cache during streaming merge.

        Caller writes to this path while streaming; caller then calls
        `finalize_final` on success. None if cache disabled. The parent
        directory is ensured-existing here so qpdf.concat_streaming can
        open the returned path for write without a FileNotFoundError —
        matches the contract that atomic_write() already enforces for
        chunk writes at line ~101.
        """
        if not self.enabled():
            return None
        target = self._final_path(source_sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.with_suffix(target.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")

    def finalize_final(self, source_sha256: str, temp_path: Path) -> None:
        """Atomically rename a successfully-written temp file into the cache."""
        if not self.enabled():
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                log.warning("failed to remove temp file %s", temp_path)
            return
        target = self._final_path(source_sha256)
        # Ensure durability before the visibility-flipping rename
        try:
            with temp_path.open("rb") as f:
                os.fsync(f.fileno())
        except OSError:
            log.warning("fsync failed on %s", temp_path)
        os.replace(temp_path, target)


def atomic_write(target: Path, source: Path) -> None:
    """Copy `source` to `target` atomically via temp file + rename.

    POSIX rename is atomic within a single filesystem; readers see either the
    old file or the new file, never a partial one. `os.replace` works on
    Windows too.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        shutil.copyfile(source, tmp)
        with tmp.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
