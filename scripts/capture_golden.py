#!/usr/bin/env python3
"""Golden-fixture capture — the Python oracle half of the Go/Python parity gate.

Phase 6 exit criterion (see aidlc-docs/construction/go-orchestrator/parity-testing.md):
freeze the *live Python* orchestrator's HTTP responses so the Go port can be
diffed against them before the Phase 8 cutover.

Run this where the Python service + its deps live (the capture image — see
scripts/golden.Dockerfile / `make golden-capture`). It uses Starlette's
in-process TestClient with a fake worker, so it needs NO Aspose binaries. qpdf is
only needed if a case actually converts; the cases here exercise GET shapes +
error bodies + the recent-conversions cursor, all of which are produced by
seeding the in-process stores directly — no worker/qpdf round-trip required.

Output (into <out_dir>, default internal/server/testdata/golden/):
  manifest.json        — the case + seed definitions the Go test replays
  <case-name>.json     — the captured Python response {status, headers, body}

The Go side (internal/server/golden_test.go) seeds the SAME records, replays the
SAME requests, and compares semantically (numeric-aware; see that file for why a
byte diff is wrong: Go renders whole-valued floats as `1` where Python renders
`1.0`, and the base64 cursor token inherits that — identical to any JSON parser,
different bytes).
"""

from __future__ import annotations

import base64
import json
import stat
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from office_convert import job_progress, recent
from office_convert.config import Settings
from office_convert.server import create_app

# Worker set mirrors production's per-product split (docx/pptx/xlsx/pdf/email).
WORKER_FORMATS = ("docx", "pptx", "xlsx", "pdf", "email")

# A fixed far-ish license expiry keeps the file well-formed; the resulting
# license_days_remaining is date-relative and therefore normalized by both sides.
LICENSE_DAYS = 3650

# ---- Shared seed dataset (the Go test seeds byte-identical records) ----
# completion_ts values are deliberately fractional: a whole-number float would
# expose the Go/Python rendering split (1700000000 vs 1700000000.0) in a way
# that's cosmetic but noisy. The cursor + completion_ts are compared numerically.
SEED_CONVERSIONS: list[dict[str, Any]] = [
    {
        "request_id": "seed-rid-0001",
        "completion_ts": 1700000001.5,
        "source": "ui",
        "input_filename": "report.docx",
        "format": "docx",
        "page_count": 4,
        "duration_ms": 1234,
        "status": "success",
        "error_code": None,
        "output_s3_uri": None,
        "output_size_bytes": 20480,
    },
    {
        "request_id": "seed-rid-0002",
        "completion_ts": 1700000002.5,
        "source": "cross",
        "input_filename": None,
        "format": "xlsx",
        "page_count": 12,
        "duration_ms": 5678,
        "status": "success",
        "error_code": None,
        "output_s3_uri": "s3://out/seed-rid-0002/output.pdf",
        "output_size_bytes": 51200,
    },
    {
        "request_id": "seed-rid-0003",
        "completion_ts": 1700000003.5,
        "source": "ui",
        "input_filename": "broken.pptx",
        "format": "pptx",
        "page_count": None,
        "duration_ms": 321,
        "status": "failed",
        "error_code": "render_failed",
        "output_s3_uri": None,
        "output_size_bytes": None,
    },
]

# Progress entries: started_at + elapsed_s are wall-clock derived and normalized
# on both sides; weighted_percent/load_progress/merge_done are deterministic.
SEED_PROGRESS: list[dict[str, Any]] = [
    {
        "request_id": "seed-rid-job1",
        "phase": "rendering",
        "total_chunks": 4,
        "chunks_rendered": 2,
        "load_progress": 1.0,
        "merge_done": 0.0,
    },
]

# A fixed multipart body (stable boundary + bytes) so Go and Python POST the
# exact same wire bytes and the magic-byte detector yields the same diagnostic.
_BOUNDARY = "GOLDENBOUNDARY0123456789"
_GARBAGE = b"\x00\x01\x02\x03GARBAGE-not-an-office-file"
_UNSUPPORTED_BODY = (
    (
        f"--{_BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="x.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    + _GARBAGE
    + f"\r\n--{_BOUNDARY}--\r\n".encode()
)
_EMPTY_MULTIPART = f"--{_BOUNDARY}--\r\n".encode()

# Normalization paths shared with the Go test. Dotted body paths; "[]" means
# "for each list element". headers: case-insensitive header names.
NORM_DEFAULT_HEADERS = ["X-RateLimit-Reset"]

CASES: list[dict[str, Any]] = [
    {
        "name": "health",
        "method": "GET",
        "path": "/health",
        "request_id": "rid-health",
        "seed": False,
        # ready/problems/license_days_remaining are environment-coupled (qpdf
        # presence + worker-binary presence differ between the capture image and
        # the Go test host); we assert the key SET but normalize the volatile
        # values. NOTE: health *status* (200 vs 503) is also env-coupled — run
        # capture + replay in the same image (CI) for a meaningful health diff.
        "normalize_body": ["ready", "problems", "license_days_remaining"],
    },
    {
        "name": "conversions_empty",
        "method": "GET",
        "path": "/v1/conversions",
        "request_id": "rid-conv-empty",
        "seed": False,
    },
    {
        "name": "conversions_seeded_page1",
        "method": "GET",
        "path": "/v1/conversions?limit=2",
        "request_id": "rid-conv-p1",
        "seed": True,
        # next_cursor is compared by decoding (base64-JSON {ts,id}), not raw
        # string — see golden_test.go cursorEqual.
        "cursor_field": "next_cursor",
    },
    {
        "name": "conversions_filter_failed",
        "method": "GET",
        "path": "/v1/conversions?filter=failed",
        "request_id": "rid-conv-failed",
        "seed": True,
    },
    {
        "name": "conversions_stats_seeded",
        "method": "GET",
        "path": "/v1/conversions/stats",
        "request_id": "rid-conv-stats",
        "seed": True,
    },
    {
        "name": "jobs_active_seeded",
        "method": "GET",
        "path": "/v1/jobs/active",
        "request_id": "rid-jobs-active",
        "seed": True,
        "normalize_body": ["jobs[].started_at", "jobs[].elapsed_s"],
    },
    {
        "name": "progress_known",
        "method": "GET",
        "path": "/v1/jobs/seed-rid-job1/progress",
        "request_id": "rid-prog-known",
        "seed": True,
        "normalize_body": ["started_at", "elapsed_s"],
    },
    {
        "name": "progress_unknown",
        "method": "GET",
        "path": "/v1/jobs/does-not-exist/progress",
        "request_id": "rid-prog-unknown",
        "seed": False,
    },
    {
        "name": "heartbeats_empty",
        "method": "GET",
        "path": "/v1/jobs/does-not-exist/heartbeats",
        "request_id": "rid-hb",
        "seed": False,
    },
    {
        "name": "timings_empty",
        "method": "GET",
        "path": "/v1/jobs/does-not-exist/timings",
        "request_id": "rid-tm",
        "seed": False,
    },
    # ---- error bodies (the Diagnostic envelope, one per reachable class) ----
    {
        "name": "err_s3_disabled",
        "method": "GET",
        "path": "/v1/downloads/presign?bucket=b&key=k",
        "request_id": "rid-s3-disabled",
        "seed": False,
    },
    {
        "name": "err_missing_file",
        "method": "POST",
        "path": "/v1/convert",
        "request_id": "rid-missing-file",
        "seed": False,
        "body_b64": base64.b64encode(_EMPTY_MULTIPART).decode(),
        "content_type": f"multipart/form-data; boundary={_BOUNDARY}",
    },
    {
        "name": "err_unsupported_format",
        "method": "POST",
        "path": "/v1/convert",
        "request_id": "rid-unsupported",
        "seed": False,
        "body_b64": base64.b64encode(_UNSUPPORTED_BODY).decode(),
        "content_type": f"multipart/form-data; boundary={_BOUNDARY}",
    },
    {
        "name": "err_rate_limited",
        "method": "POST",
        "path": "/v1/convert",
        "request_id": "rid-rate-limited",
        "seed": False,
        "body_b64": base64.b64encode(_EMPTY_MULTIPART).decode(),
        "content_type": f"multipart/form-data; boundary={_BOUNDARY}",
        # rpm=1/burst=1: first POST passes the limiter (then fails missing_file),
        # the second trips rate_limited. Capture the second.
        "config": {"rate_limit_enabled": True, "rate_limit_per_ip_rpm": 1, "rate_limit_burst": 1},
        "repeat": 2,
        "normalize_headers": ["X-RateLimit-Reset"],
    },
]

CONTRACT_HEADERS = [
    "X-Request-ID",
    "Content-Type",
    "Content-Disposition",
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
]


def _write_fake_worker(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "# Minimal: probe -> fixed JSON; render -> touch output. Existence is\n"
        "# all health checks; these cases never actually render.\n"
        'if "--mode=probe" in sys.argv or "probe" in sys.argv:\n'
        '    out = {"page_count": 4, "format": "x", '
        '"natural_seams": [], "size_bytes": 1000}\n'
        "    sys.stdout.write(json.dumps(out))\n"
        "sys.exit(0)\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_settings(tmp: Path, overrides: dict[str, Any]) -> Settings:
    prefix = tmp / "fake-worker"
    for fmt in WORKER_FORMATS:
        _write_fake_worker(prefix.with_name(f"{prefix.name}-{fmt}"))
    lic = tmp / "license.lic"
    expiry = (datetime.now(UTC).date() + timedelta(days=LICENSE_DAYS)).strftime("%Y%m%d")
    lic.write_text(
        f"<License><Data><SubscriptionExpiry>{expiry}</SubscriptionExpiry></Data></License>",
        encoding="utf-8",
    )
    kwargs: dict[str, Any] = {
        "worker_binary_prefix": prefix,
        "license_path": lic,
        "scratch_dir": tmp / "scratch",
        "cache_dir": None,
        "max_jobs": 2,
        "chunk_timeout_seconds": 60,
        "aspose_version": "golden",
        "rate_limit_enabled": False,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _seed_stores() -> None:
    recent.default_store().clear()
    store = job_progress.job_progress_store()
    # Clear any prior progress (no public clear; forget every known rid).
    for rid in list(getattr(store, "_store", {}).keys()):
        store.forget(rid)
    for rec in SEED_CONVERSIONS:
        recent.default_store().record(recent.ConversionRecord(**rec))
    for p in SEED_PROGRESS:
        store.update(
            p["request_id"],
            phase=p["phase"],
            total_chunks=p["total_chunks"],
            load_progress=p["load_progress"],
            merge_done=p["merge_done"],
            increment_chunks=p["chunks_rendered"],
        )


def _capture_one(case: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        settings = _make_settings(tmp, case.get("config", {}))
        app = create_app(settings)
        if case.get("seed"):
            _seed_stores()
        else:
            recent.default_store().clear()
        with TestClient(app) as client:
            headers = {"X-Request-ID": case["request_id"]}
            content = None
            if "body_b64" in case:
                content = base64.b64decode(case["body_b64"])
                headers["Content-Type"] = case["content_type"]
            resp = None
            for _ in range(case.get("repeat", 1)):
                resp = client.request(
                    case["method"], case["path"], headers=headers, content=content
                )
            assert resp is not None
            return _response_to_golden(resp)


def _norm_path(v: Any, segs: list[str]) -> Any:
    """Replace the leaf at a dotted path with a sentinel. "field[]" descends
    into each list element. Mirrors normPath in golden_test.go so the stored
    golden is stable across captures and matches what the Go side normalizes."""
    if not segs:
        return "<NORM>"
    seg = segs[0]
    if seg.endswith("[]"):
        key = seg[:-2]
        target = v.get(key) if isinstance(v, dict) else v
        if isinstance(target, list):
            for i, el in enumerate(target):
                target[i] = _norm_path(el, segs[1:])
        return v
    if isinstance(v, dict) and seg in v:
        v[seg] = _norm_path(v[seg], segs[1:])
    return v


def _normalize(golden: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    for p in case.get("normalize_body", []):
        golden["body"] = _norm_path(golden["body"], p.split("."))
    for h in NORM_DEFAULT_HEADERS + case.get("normalize_headers", []):
        if h in golden["headers"]:
            golden["headers"][h] = "<NORM>"
    return golden


def _response_to_golden(resp: Any) -> dict[str, Any]:
    hdrs = {}
    for h in CONTRACT_HEADERS:
        if h in resp.headers:
            hdrs[h] = resp.headers[h]
    body: Any
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        body = resp.json()
    elif ctype.startswith("text/html"):
        body = {"_html_len": len(resp.text)}  # HTML compared by length only
    else:
        body = {"_text": resp.text}
    return {"status": resp.status_code, "headers": hdrs, "body": body}


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("internal/server/testdata/golden")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_cases = []
    for case in CASES:
        golden = _normalize(_capture_one(case), case)
        (out_dir / f"{case['name']}.json").write_text(
            json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest_cases.append(case)
        print(f"  captured {case['name']:28s} -> {golden['status']}")
    manifest = {
        "_comment": "Generated by scripts/capture_golden.py — do not hand-edit. "
        "Replayed by internal/server/golden_test.go.",
        "license_days": LICENSE_DAYS,
        "worker_formats": list(WORKER_FORMATS),
        "norm_default_headers": NORM_DEFAULT_HEADERS,
        "seed": {"conversions": SEED_CONVERSIONS, "progress": SEED_PROGRESS},
        "contract_headers": CONTRACT_HEADERS,
        "cases": manifest_cases,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"\nWrote {len(CASES)} golden files + manifest.json to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
