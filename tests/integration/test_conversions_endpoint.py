"""Tests for /v1/conversions + /v1/jobs/active dashboard endpoints."""

from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from office_convert.config import Settings
from office_convert.job_progress import job_progress_store
from office_convert.recent import ConversionRecord, default_store
from office_convert.server import create_app


def _build_settings(tmp_path: Path) -> Settings:
    # Mirror tests/integration/test_s3_convert.py: create a fake worker
    # binary + valid temp license. We don't run conversions here — these
    # endpoints don't touch the worker — but create_app() validates both
    # at startup.
    prefix = tmp_path / "fake-worker"
    for fmt in ("docx", "pptx", "xlsx", "pdf", "email"):
        p = prefix.with_name(f"{prefix.name}-{fmt}")
        p.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    lic = tmp_path / "license.lic"
    expiry = (datetime.now(UTC).date() + timedelta(days=30)).strftime("%Y%m%d")
    lic.write_text(
        f"<License><Data><SubscriptionExpiry>{expiry}</SubscriptionExpiry></Data></License>"
    )
    base: dict[str, Any] = {
        "worker_binary_prefix": prefix,
        "license_path": lic,
        "scratch_dir": tmp_path / "scratch",
        "cache_dir": None,
        "chunk_timeout_seconds": 60,
        "max_pages_per_chunk": 10,
        "max_mb_per_chunk": 50,
        "aspose_version": "test",
        "rate_limit_enabled": False,
        "s3_enabled": False,
    }
    return Settings(**base)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    s = _build_settings(tmp_path)
    return TestClient(create_app(s))


@pytest.fixture(autouse=True)
def _clear_stores() -> None:
    """Each test starts with empty ring + empty job_progress store."""
    default_store().clear()
    # job_progress_store has no clear(); we forget by rid in tests that use it
    yield
    default_store().clear()


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


# ---- /v1/conversions --------------------------------------------------------


def test_conversions_empty_buffer(client: TestClient) -> None:
    r = client.get("/v1/conversions")
    assert r.status_code == 200
    data = r.json()
    assert data["entries"] == []
    assert data["next_cursor"] is None
    assert data["has_more"] is False
    assert data["stale_cursor"] is False
    assert data["buffer_size"] == 0


def test_conversions_returns_recorded_entries(client: TestClient) -> None:
    store = default_store()
    store.record(_rec("r1", 100.0))
    store.record(_rec("r2", 200.0))
    store.record(_rec("r3", 300.0))
    r = client.get("/v1/conversions")
    assert r.status_code == 200
    data = r.json()
    # Newest first (record() uses appendleft)
    assert [e["request_id"] for e in data["entries"]] == ["r3", "r2", "r1"]
    assert data["buffer_size"] == 3
    assert data["has_more"] is False
    assert data["next_cursor"] is None


def test_conversions_paginates(client: TestClient) -> None:
    store = default_store()
    for i in range(25):
        store.record(_rec(f"r{i:02d}", float(i)))
    r = client.get("/v1/conversions?limit=10")
    assert r.status_code == 200
    data = r.json()
    assert len(data["entries"]) == 10
    assert data["has_more"] is True
    assert data["next_cursor"] is not None

    # Page 2
    r2 = client.get(f"/v1/conversions?limit=10&cursor={data['next_cursor']}")
    data2 = r2.json()
    assert len(data2["entries"]) == 10
    assert data2["entries"][0]["request_id"] != data["entries"][0]["request_id"]


def test_conversions_filter_ui_only(client: TestClient) -> None:
    store = default_store()
    store.record(_rec("ui1", 100.0, source="ui"))
    store.record(_rec("cx1", 200.0, source="cross"))
    store.record(_rec("ui2", 300.0, source="ui"))
    r = client.get("/v1/conversions?filter=ui")
    data = r.json()
    assert [e["request_id"] for e in data["entries"]] == ["ui2", "ui1"]
    assert all(e["source"] == "ui" for e in data["entries"])


def test_conversions_filter_cross_only(client: TestClient) -> None:
    store = default_store()
    store.record(_rec("ui1", 100.0, source="ui"))
    store.record(_rec("cx1", 200.0, source="cross"))
    r = client.get("/v1/conversions?filter=cross")
    data = r.json()
    assert [e["request_id"] for e in data["entries"]] == ["cx1"]


def test_conversions_filter_failed(client: TestClient) -> None:
    store = default_store()
    store.record(_rec("ok1", 100.0, status="success"))
    store.record(_rec("fail1", 200.0, status="failed"))
    store.record(_rec("ok2", 300.0, status="success"))
    r = client.get("/v1/conversions?filter=failed")
    data = r.json()
    assert [e["request_id"] for e in data["entries"]] == ["fail1"]
    assert data["entries"][0]["status"] == "failed"
    assert data["entries"][0]["error_code"] == "subdivision_floor"


def test_conversions_invalid_filter_falls_back_to_all(client: TestClient) -> None:
    store = default_store()
    store.record(_rec("r1", 100.0, source="ui"))
    store.record(_rec("r2", 200.0, source="cross"))
    r = client.get("/v1/conversions?filter=garbage")
    data = r.json()
    # 'garbage' coerced to 'all' silently — both entries returned
    assert len(data["entries"]) == 2


def test_conversions_limit_clamped(client: TestClient) -> None:
    store = default_store()
    for i in range(150):
        store.record(_rec(f"r{i}", float(i)))
    # Asking for 500 — should be clamped to 100
    r = client.get("/v1/conversions?limit=500")
    data = r.json()
    assert len(data["entries"]) == 100

    # Asking for 0 / negative — clamped to 1
    r2 = client.get("/v1/conversions?limit=0")
    data2 = r2.json()
    assert len(data2["entries"]) == 1


def test_conversions_stale_cursor_flag(client: TestClient) -> None:
    import base64
    import json as _j

    store = default_store()
    for i in range(5):
        store.record(_rec(f"r{i}", float(i)))
    # Construct a cursor pointing at a request_id NOT in the buffer
    bogus_cursor = base64.urlsafe_b64encode(
        _j.dumps({"ts": 999.0, "id": "does_not_exist"}).encode()
    ).decode()
    r = client.get(f"/v1/conversions?cursor={bogus_cursor}")
    data = r.json()
    assert data["stale_cursor"] is True
    assert len(data["entries"]) == 5  # falls back to newest


def test_conversions_malformed_cursor_treated_as_no_cursor(client: TestClient) -> None:
    store = default_store()
    store.record(_rec("r1", 100.0))
    r = client.get("/v1/conversions?cursor=this-is-not-base64-!!")
    assert r.status_code == 200
    data = r.json()
    assert len(data["entries"]) == 1
    assert data["stale_cursor"] is False


# ---- /v1/jobs/active --------------------------------------------------------


def test_active_jobs_empty(client: TestClient) -> None:
    r = client.get("/v1/jobs/active")
    assert r.status_code == 200
    assert r.json() == {"jobs": []}


def test_active_jobs_returns_non_complete_only(client: TestClient) -> None:
    store = job_progress_store()
    store.update("req_a", total_chunks=10, phase="rendering")
    store.update("req_b", total_chunks=5, phase="loading")
    store.update("req_done", total_chunks=10, phase="complete")
    try:
        r = client.get("/v1/jobs/active")
        data = r.json()
        rids = {j["request_id"] for j in data["jobs"]}
        assert rids == {"req_a", "req_b"}
        # complete one excluded
        assert "req_done" not in rids
    finally:
        store.forget("req_a")
        store.forget("req_b")
        store.forget("req_done")


def test_active_jobs_includes_progress_fields(client: TestClient) -> None:
    store = job_progress_store()
    store.update("req_x", total_chunks=10, phase="rendering", load_progress=1.0)
    store.update("req_x", increment_chunks=3)
    try:
        r = client.get("/v1/jobs/active")
        jobs = r.json()["jobs"]
        assert len(jobs) == 1
        j = jobs[0]
        assert j["request_id"] == "req_x"
        assert j["phase"] == "rendering"
        assert j["total_chunks"] == 10
        assert j["chunks_rendered"] == 3
        assert "weighted_percent" in j
        assert "elapsed_s" in j
        # Weighted: 0.30 * 1.0 (load) + 0.65 * (3/10) (render) + 0.05 * 0 (merge) = 0.495
        assert 0.49 <= j["weighted_percent"] <= 0.50
    finally:
        store.forget("req_x")
