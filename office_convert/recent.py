"""In-memory ring buffer of recently-completed conversions.

Populated at the `/v1/convert` terminal-state path (server.py — three
`stream*()` generators each call `record()` in their `finally:` block).
Read by `GET /v1/conversions` to power the dashboard's "Recent Conversions"
panel.

Bounded `deque` (default 200 entries). Lost on pod restart. Single source
of truth across UI-initiated and cross-service (s3_input) conversions —
both reach the same capture site. No new IAM, no S3 calls in the hot path.

Thread-safety: single-event-loop coroutine safety under asyncio +
single-worker uvicorn. `deque.append` and `list(deque)` snapshots are
bytecode-atomic under CPython; there is no preemption between yield points.
Multi-worker uvicorn or multi-replica deploy invalidate this design — the
dashboard would see only the records that landed on whichever process
answered the GET. Track as a tripwire; don't fix preemptively.
"""

from __future__ import annotations

import base64
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Final, Literal

# ---------- Constants ----------

DEFAULT_MAX_RECENT: Final[int] = 200

# ---------- Types ----------

Source = Literal["ui", "cross"]
Status = Literal["success", "failed"]
Filter = Literal["all", "ui", "cross", "failed"]


# ---------- Data model ----------


@dataclass(frozen=True)
class ConversionRecord:
    """One terminal-state conversion. Both `success` and `failed` are
    captured; in-flight visibility lives in /v1/jobs/{id}/progress."""

    request_id: str
    completion_ts: float  # unix epoch seconds (time.time())
    source: Source
    input_filename: str | None  # None for s3_input requests (no original name)
    format: str  # "docx" | "pptx" | "xlsx" | "pdf" | "eml" | ...
    page_count: int | None
    duration_ms: int  # monotonic delta from route entry to terminal state
    status: Status
    error_code: str | None  # short code from server.py error taxonomy
    output_s3_uri: str | None  # s3://bucket/key, or None for response-body output
    output_size_bytes: int | None  # streamed bytes, None on failure

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# ---------- Store ----------


class RecentStore:
    """Process-wide bounded deque of completed conversions.

    `append_left()` newest-first → entries flow naturally for chronological
    pagination (most recent at index 0). `snapshot()` is the safe iteration
    primitive."""

    def __init__(self, maxlen: int = DEFAULT_MAX_RECENT) -> None:
        self._buf: deque[ConversionRecord] = deque(maxlen=maxlen)
        self._maxlen = maxlen

    def record(self, rec: ConversionRecord) -> None:
        self._buf.appendleft(rec)

    def snapshot(self) -> list[ConversionRecord]:
        """Atomic copy for safe iteration during pagination."""
        return list(self._buf)

    def size(self) -> int:
        return len(self._buf)

    def maxlen(self) -> int:
        return self._maxlen

    def clear(self) -> None:
        """Test-only helper. Not exposed via any endpoint."""
        self._buf.clear()


# Module-level singleton. server.py wires its `recent_store` reference to
# this for the capture path; tests instantiate fresh RecentStore() locally.
_default_store = RecentStore()


def default_store() -> RecentStore:
    """Return the module-level default store. Lives for the process lifetime."""
    return _default_store


# ---------- Filter ----------


def matches(rec: ConversionRecord, flt: Filter) -> bool:
    if flt == "all":
        return True
    if flt == "ui":
        return rec.source == "ui"
    if flt == "cross":
        return rec.source == "cross"
    if flt == "failed":
        return rec.status == "failed"
    return False


# ---------- Cursor ----------


@dataclass(frozen=True)
class Cursor:
    """Opaque pagination anchor. Encodes `(completion_ts, request_id)` of
    the last entry on the previous page; the next page returns entries
    strictly older than that anchor."""

    ts: float
    id: str

    def encode(self) -> str:
        payload = json.dumps({"ts": self.ts, "id": self.id}, separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode()).decode()

    @classmethod
    def decode(cls, raw: str) -> Cursor | None:
        """Returns None on any malformed input — callers treat as 'no cursor'."""
        try:
            data = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
            return cls(ts=float(data["ts"]), id=str(data["id"]))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None


@dataclass(frozen=True)
class Page:
    entries: list[ConversionRecord]
    next_cursor: str | None
    has_more: bool
    stale_cursor: bool
    buffer_size: int


def paginate(
    items: Iterable[ConversionRecord],
    cursor: Cursor | None,
    limit: int,
    *,
    buffer_size: int,
) -> Page:
    """Apply cursor + limit. Returns a Page with the next cursor + flags.

    Cursor semantics: entries strictly older than `(cursor.ts, cursor.id)`
    using Python tuple comparison. If the cursor's exact (ts, id) doesn't
    exist in the snapshot, the entry has aged out — set `stale_cursor` and
    return the newest `limit` entries (caller-side UX: reset cursor + toast).
    """
    items_list = list(items)

    if cursor is None:
        entries = items_list[:limit]
        has_more = len(items_list) > limit
        next_cur = _make_cursor(entries) if has_more else None
        return Page(
            entries=entries,
            next_cursor=next_cur,
            has_more=has_more,
            stale_cursor=False,
            buffer_size=buffer_size,
        )

    # Cursor-not-found detection: anchor entry must exist somewhere in the
    # snapshot for the cursor to be valid.
    anchor_present = any(
        r.completion_ts == cursor.ts and r.request_id == cursor.id for r in items_list
    )
    if not anchor_present:
        # Stale cursor — fall back to newest, signal to caller.
        entries = items_list[:limit]
        has_more = len(items_list) > limit
        next_cur = _make_cursor(entries) if has_more else None
        return Page(
            entries=entries,
            next_cursor=next_cur,
            has_more=has_more,
            stale_cursor=True,
            buffer_size=buffer_size,
        )

    filtered = [r for r in items_list if (r.completion_ts, r.request_id) < (cursor.ts, cursor.id)]
    entries = filtered[:limit]
    has_more = len(filtered) > limit
    next_cur = _make_cursor(entries) if has_more else None
    return Page(
        entries=entries,
        next_cursor=next_cur,
        has_more=has_more,
        stale_cursor=False,
        buffer_size=buffer_size,
    )


def _make_cursor(entries: list[ConversionRecord]) -> str | None:
    if not entries:
        return None
    last = entries[-1]
    return Cursor(ts=last.completion_ts, id=last.request_id).encode()
