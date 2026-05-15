"""Per-request stage-timing ring buffer.

Pool-mode C++ workers emit `{"type":"timing", ...}` JSON to stderr around
each load / pagination / save stage (see worker_cpp/formats/xlsx.cpp). The
Python tailer in worker_pool.py forwards them here, keyed by the in-flight
request_id, so the Streamlit dashboard can plot a stage-timing breakdown
per worker without re-parsing logs.

Mirrors `heartbeats.py` in shape (bounded deque per request, TTL-evicted).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_MAX_PER_REQUEST = 1000  # ~ 6 stages × dozens of workers — plenty
_TTL_SECONDS = 1800  # 30 min, matches heartbeats


class TimingStore:
    def __init__(
        self,
        max_per_request: int = _MAX_PER_REQUEST,
        ttl_seconds: float = _TTL_SECONDS,
    ) -> None:
        self._max = max_per_request
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[str, deque[dict[str, Any]]] = {}
        self._last_touched: dict[str, float] = {}

    def record(self, request_id: str, ev: dict[str, Any]) -> None:
        if not request_id or request_id == "-":
            return
        now = time.monotonic()
        with self._lock:
            buf = self._store.get(request_id)
            if buf is None:
                buf = deque(maxlen=self._max)
                self._store[request_id] = buf
            buf.append({**ev, "received_at": now})
            self._last_touched[request_id] = now
            self._evict_locked(now)

    def get(self, request_id: str) -> list[dict[str, Any]]:
        if not request_id:
            return []
        with self._lock:
            self._evict_locked(time.monotonic())
            buf = self._store.get(request_id)
            if buf is None:
                return []
            return list(buf)

    def forget(self, request_id: str) -> None:
        with self._lock:
            self._store.pop(request_id, None)
            self._last_touched.pop(request_id, None)

    def _evict_locked(self, now: float) -> None:
        if not self._last_touched:
            return
        expired = [rid for rid, touched in self._last_touched.items() if now - touched > self._ttl]
        for rid in expired:
            self._store.pop(rid, None)
            self._last_touched.pop(rid, None)


_singleton: TimingStore | None = None
_singleton_lock = threading.Lock()


def timing_store() -> TimingStore:
    global _singleton  # noqa: PLW0603 — intentional double-checked-lock singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = TimingStore()
    return _singleton
