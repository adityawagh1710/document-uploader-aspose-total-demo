"""Tests for office_convert.recent: ring buffer + cursor pagination + filter."""

from __future__ import annotations

import time

from office_convert.recent import (
    ConversionRecord,
    Cursor,
    RecentStore,
    matches,
    paginate,
)


def _rec(
    rid: str,
    ts: float,
    source: str = "ui",
    status: str = "success",
    fmt: str = "docx",
) -> ConversionRecord:
    return ConversionRecord(
        request_id=rid,
        completion_ts=ts,
        source=source,  # type: ignore[arg-type]
        input_filename=f"{rid}.{fmt}",
        format=fmt,
        page_count=1,
        duration_ms=1000,
        status=status,  # type: ignore[arg-type]
        error_code=None if status == "success" else "subdivision_floor",
        output_s3_uri=None,
        output_size_bytes=4096 if status == "success" else None,
    )


# ---------- RecentStore ----------


def test_store_starts_empty() -> None:
    s = RecentStore(maxlen=10)
    assert s.size() == 0
    assert s.snapshot() == []
    assert s.maxlen() == 10


def test_store_records_newest_first() -> None:
    s = RecentStore()
    s.record(_rec("a", 100.0))
    s.record(_rec("b", 200.0))
    s.record(_rec("c", 300.0))
    snap = s.snapshot()
    assert [r.request_id for r in snap] == ["c", "b", "a"]


def test_store_bounded_by_maxlen() -> None:
    s = RecentStore(maxlen=3)
    for i in range(10):
        s.record(_rec(f"r{i}", float(i)))
    assert s.size() == 3
    # Newest three (r9, r8, r7) — older ones aged out
    assert [r.request_id for r in s.snapshot()] == ["r9", "r8", "r7"]


def test_store_snapshot_is_a_copy() -> None:
    """Mutating the snapshot must not affect the live deque."""
    s = RecentStore()
    s.record(_rec("a", 100.0))
    snap = s.snapshot()
    snap.clear()
    assert s.size() == 1


# ---------- matches() filter ----------


def test_filter_all_matches_everything() -> None:
    r_ui = _rec("a", 1.0, source="ui", status="success")
    r_cross = _rec("b", 2.0, source="cross", status="failed")
    assert matches(r_ui, "all")
    assert matches(r_cross, "all")


def test_filter_ui_only_matches_ui_source() -> None:
    assert matches(_rec("a", 1.0, source="ui"), "ui")
    assert not matches(_rec("b", 2.0, source="cross"), "ui")


def test_filter_cross_only_matches_cross_source() -> None:
    assert matches(_rec("b", 2.0, source="cross"), "cross")
    assert not matches(_rec("a", 1.0, source="ui"), "cross")


def test_filter_failed_matches_failed_regardless_of_source() -> None:
    assert matches(_rec("a", 1.0, source="ui", status="failed"), "failed")
    assert matches(_rec("b", 2.0, source="cross", status="failed"), "failed")
    assert not matches(_rec("c", 3.0, status="success"), "failed")


# ---------- Cursor encode/decode ----------


def test_cursor_roundtrip() -> None:
    c = Cursor(ts=1234567890.123, id="req_abc")
    decoded = Cursor.decode(c.encode())
    assert decoded == c


def test_cursor_decode_handles_garbage() -> None:
    assert Cursor.decode("not-base64-!!") is None
    assert Cursor.decode("aGVsbG8=") is None  # valid base64, not JSON
    assert Cursor.decode("") is None


def test_cursor_decode_handles_missing_keys() -> None:
    import base64 as _b64
    import json as _j

    bad = _b64.urlsafe_b64encode(_j.dumps({"only_ts": 1.0}).encode()).decode()
    assert Cursor.decode(bad) is None


# ---------- paginate() ----------


def _items(n: int) -> list[ConversionRecord]:
    """N records, ts decreasing (newest first), ids r0..r(n-1)."""
    return [_rec(f"r{i}", 1000.0 - float(i)) for i in range(n)]


def test_paginate_no_cursor_returns_first_page() -> None:
    items = _items(25)
    page = paginate(items, cursor=None, limit=10, buffer_size=25)
    assert len(page.entries) == 10
    assert page.entries[0].request_id == "r0"
    assert page.entries[-1].request_id == "r9"
    assert page.has_more is True
    assert page.next_cursor is not None
    assert page.stale_cursor is False
    assert page.buffer_size == 25


def test_paginate_no_cursor_with_smaller_buffer_returns_all() -> None:
    items = _items(5)
    page = paginate(items, cursor=None, limit=10, buffer_size=5)
    assert len(page.entries) == 5
    assert page.has_more is False
    assert page.next_cursor is None


def test_paginate_with_cursor_returns_next_page() -> None:
    items = _items(25)
    # First page: r0..r9, next_cursor pins r9
    first = paginate(items, cursor=None, limit=10, buffer_size=25)
    assert first.next_cursor is not None
    next_cur = Cursor.decode(first.next_cursor)
    assert next_cur is not None

    second = paginate(items, cursor=next_cur, limit=10, buffer_size=25)
    assert len(second.entries) == 10
    assert second.entries[0].request_id == "r10"
    assert second.entries[-1].request_id == "r19"
    assert second.has_more is True
    assert second.stale_cursor is False


def test_paginate_last_page_has_no_more() -> None:
    items = _items(15)
    # Page 1: r0..r9, page 2 starts at r10. Only 5 left → has_more=False
    first = paginate(items, cursor=None, limit=10, buffer_size=15)
    next_cur = Cursor.decode(first.next_cursor or "")
    assert next_cur is not None

    second = paginate(items, cursor=next_cur, limit=10, buffer_size=15)
    assert len(second.entries) == 5
    assert second.has_more is False
    assert second.next_cursor is None


def test_paginate_stale_cursor_falls_back_to_newest() -> None:
    """Cursor anchor doesn't exist in current snapshot → stale=True, return newest."""
    items = _items(15)
    bogus = Cursor(ts=999999.0, id="req_no_such_thing")

    page = paginate(items, cursor=bogus, limit=10, buffer_size=15)
    assert page.stale_cursor is True
    # Falls back to newest 10 entries
    assert len(page.entries) == 10
    assert page.entries[0].request_id == "r0"


def test_paginate_empty_buffer() -> None:
    page = paginate([], cursor=None, limit=10, buffer_size=0)
    assert page.entries == []
    assert page.has_more is False
    assert page.next_cursor is None
    assert page.buffer_size == 0
    assert page.stale_cursor is False


def test_paginate_handles_tie_in_completion_ts() -> None:
    """Two entries with identical completion_ts: request_id breaks the tie."""
    items = [
        _rec("req_zz", 100.0),
        _rec("req_aa", 100.0),  # same ts, lex-smaller id → 'older' per cursor rule
    ]
    # Cursor pinned to req_zz: only req_aa is strictly older (same ts, smaller id)
    cur = Cursor(ts=100.0, id="req_zz")
    page = paginate(items, cursor=cur, limit=10, buffer_size=2)
    assert len(page.entries) == 1
    assert page.entries[0].request_id == "req_aa"
    assert page.stale_cursor is False  # req_zz IS in the buffer


# ---------- to_dict shape ----------


def test_to_dict_includes_all_fields_for_success() -> None:
    r = _rec("req_x", 1717000000.0, source="cross", status="success")
    d = r.to_dict()
    expected_keys = {
        "request_id",
        "completion_ts",
        "source",
        "input_filename",
        "format",
        "page_count",
        "duration_ms",
        "status",
        "error_code",
        "output_s3_uri",
        "output_size_bytes",
    }
    assert set(d.keys()) == expected_keys
    assert d["source"] == "cross"
    assert d["status"] == "success"
    assert d["error_code"] is None


def test_to_dict_carries_error_code_on_failure() -> None:
    r = _rec("req_y", time.time(), status="failed")
    d = r.to_dict()
    assert d["status"] == "failed"
    assert d["error_code"] == "subdivision_floor"
    assert d["output_size_bytes"] is None
