"""Per-request heartbeat ring buffer.

Pool-mode C++ workers emit a JSON heartbeat to stderr every N ms while a load
or render is in flight (see worker_cpp/pool.cpp). The Python tailer in
worker_pool.py records them here, keyed by the in-flight request_id.

The /jobs/{request_id}/heartbeats endpoint surfaces them to clients (e.g. the
Streamlit dashboard) so a 600s pool_load_timeout window is visible in real
time instead of being a black box.

Memory shape: bounded deque per request_id (default 5000 entries — enough for
4 workers * 2s cadence * 600s = ~1200 with headroom), TTL-evicted on read.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_MAX_PER_REQUEST = 5000
_TTL_SECONDS = 1800  # 30 min — long enough for a multi-GB XLSX


class HeartbeatStore:
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

    def record(self, request_id: str, hb: dict[str, Any]) -> None:
        if not request_id or request_id == "-":
            return
        now = time.monotonic()
        with self._lock:
            buf = self._store.get(request_id)
            if buf is None:
                buf = deque(maxlen=self._max)
                self._store[request_id] = buf
            buf.append({**hb, "received_at": now})
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


_singleton: HeartbeatStore | None = None
_singleton_lock = threading.Lock()


def heartbeat_store() -> HeartbeatStore:
    global _singleton  # noqa: PLW0603 — intentional double-checked-lock singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = HeartbeatStore()
    return _singleton
