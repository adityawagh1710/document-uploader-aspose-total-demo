"""Per-request job progress tracking — feeds the UI progress bar.

The store carries enough state for the UI to render a weighted progress bar
that climbs smoothly across the three phases of a pool-mode conversion:

  load (30%)  →  render N/M chunks (65%)  →  merge (5%)

Updated by the orchestrator at each phase boundary and by the C++-side load
progress callback (DOCX only — see worker_cpp/formats/docx.cpp). For formats
without an Aspose load-progress callback, load_progress stays at 0 until
fork_pool_loaded / pool_worker_loaded flips it to 1.0 — the bar's load
segment is then a single jump, but render and merge stay smooth.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field

_TTL_SECONDS = 1800


@dataclass
class JobProgress:
    total_chunks: int = 0
    chunks_rendered: int = 0
    # init → probing → planning → loading → rendering → merging → complete
    phase: str = "init"
    load_progress: float = 0.0  # 0.0 .. 1.0
    merge_done: float = 0.0  # 0.0 .. 1.0
    started_at: float = field(default_factory=time.time)
    last_touched: float = field(default_factory=time.monotonic)

    def weighted_percent(self) -> float:
        if self.total_chunks <= 0:
            chunk_pct = 0.0
        else:
            chunk_pct = min(1.0, self.chunks_rendered / self.total_chunks)
        pct = 0.30 * self.load_progress + 0.65 * chunk_pct + 0.05 * self.merge_done
        if self.phase == "complete":
            return 1.0
        return min(0.999, max(0.0, pct))

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = asdict(self)
        d["weighted_percent"] = self.weighted_percent()
        d["elapsed_s"] = max(0.0, time.time() - self.started_at)
        return d


class JobProgressStore:
    """Thread-safe per-request progress state. Lazy TTL eviction on read/write."""

    def __init__(self, ttl_seconds: float = _TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[str, JobProgress] = {}

    def _evict_locked(self, now: float) -> None:
        expired = [rid for rid, jp in self._store.items() if now - jp.last_touched > self._ttl]
        for rid in expired:
            self._store.pop(rid, None)

    def _get_or_create_locked(self, rid: str) -> JobProgress:
        jp = self._store.get(rid)
        if jp is None:
            jp = JobProgress()
            self._store[rid] = jp
        return jp

    def update(
        self,
        rid: str,
        *,
        total_chunks: int | None = None,
        phase: str | None = None,
        load_progress: float | None = None,
        merge_done: float | None = None,
        increment_chunks: int = 0,
    ) -> None:
        if not rid or rid == "-":
            return
        now = time.monotonic()
        with self._lock:
            self._evict_locked(now)
            jp = self._get_or_create_locked(rid)
            if total_chunks is not None:
                jp.total_chunks = total_chunks
            if phase is not None:
                jp.phase = phase
            # Monotonic: callbacks fire frequently; never regress load_progress.
            if load_progress is not None and load_progress > jp.load_progress:
                jp.load_progress = max(0.0, min(1.0, load_progress))
            if merge_done is not None:
                jp.merge_done = max(0.0, min(1.0, merge_done))
            if increment_chunks:
                jp.chunks_rendered += increment_chunks
            jp.last_touched = now

    def get(self, rid: str) -> JobProgress | None:
        if not rid:
            return None
        with self._lock:
            self._evict_locked(time.monotonic())
            jp = self._store.get(rid)
            if jp is None:
                return None
            # Return a copy so the caller doesn't see mutations mid-read
            return JobProgress(**asdict(jp))

    def forget(self, rid: str) -> None:
        with self._lock:
            self._store.pop(rid, None)


_singleton: JobProgressStore | None = None
_singleton_lock = threading.Lock()


def job_progress_store() -> JobProgressStore:
    global _singleton  # noqa: PLW0603 — intentional double-checked-lock singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = JobProgressStore()
    return _singleton
