# Phase 6 — Parity Testing

**Status**: ✅ **COMPLETE — golden-fixture diff implemented + green (2026-06-03).**
In-repo parity tests cover everything provable without the Aspose workers; the
golden-fixture diff against the live Python oracle now runs as
`TestGoldenParity` (14/14) and is wired as `make golden-capture` /
`make golden-verify`. This is the Phase 6 exit criterion and the Phase 8 cutover
gate — both satisfied. See **"Golden-fixture diff — as built"** below.

## What runs in-repo today (no Aspose, no qpdf binary needed)

| Test | Package | Proves |
| ---- | ------- | ------ |
| `TestProp_*` (rapid PBT) | `planner` | Chunk-plan invariants: complete non-overlapping cover, `maxPages` respected, subdivision halving + single-page floor, `ChunkSHA256` determinism + page-range sensitivity. Re-expresses the Python `hypothesis` properties (nfr-requirements §9). |
| `TestForkedWorkerPoolRendersConcurrently`, `TestWorkerPoolLegacyRenders`, `TestForkedPoolMapsOOMError` | `worker` | The load-bearing concurrency code, against a **fake worker binary** (`testdata/fakeworker`) speaking the real JSON-stdio protocol: `ForkedPoolLeader` seq-demux, `WorkerPool` channel-checkout, `prlimit` spawn, stderr heartbeat tailing into the mutex-guarded store, and exit-137 → OOM error mapping. |
| `TestConcatStreaming*` | `qpdf` | The streaming-concat wrapper against a **fake qpdf**: streams stdout to the writer, tees to the cache temp, maps non-zero exit → `MergeError`, cleans up the partial tee, rejects empty chunk lists. |
| `Test*` (httptest) | `server` | HTTP contract: health JSON shape + `X-Request-ID` echo, dashboard/landing served, `/v1/conversions` pagination shape, presign→`s3_disabled`, convert→`missing_file`, unknown-job progress. |
| unit tests | `cache`, `csvinput`, `license`, `obs`, `probe` | Pure-logic behavior (atomic cache, CSV→XLSX structure, license thresholds, store weighting/pagination, magic-byte detection). |

The fake worker (`internal/worker/testdata/fakeworker`) is the key enabler: it
lets the **real** orchestrator/worker/pool/server code run end-to-end (spawn →
load → render → respond) without the Aspose SDK. It is the Go analogue of the
Python suite's `conftest.py` fake worker.

## What is deferred (needs infra this environment lacks)

1. ~~**Golden-fixture diff vs live Python**~~ — **DONE 2026-06-03.** Implemented;
   see "Golden-fixture diff — as built" below. (Originally deferred to an env
   with Python + qpdf; the as-built capture needs neither qpdf nor Aspose
   because it seeds the stores directly.)
2. **Full `ConvertJob` e2e through real qpdf** — this box has no `qpdf` binary,
   so the merge step can't run for real. The wrapper logic is covered with a
   fake qpdf; the real merge is exercised in the container (Phase 7) where qpdf
   is installed.
3. **Testcontainers e2e with the real Aspose workers** — needs the licensed C++
   binaries + image; runs in CI/dev only, gated like the Python `tests/e2e/`.

## Golden-fixture harness spec (to run where Python + qpdf exist)

1. **Capture** (one-time, against live Python): for each GET endpoint and a
   representative `/v1/convert` (success headers + one error body per
   `FailureClass`), save the JSON/headers under
   `internal/server/testdata/golden/<name>.json`. A small script hitting the
   Python service with its fake worker produces these.
2. **Diff** (Go test): start the Go server with the same fake-worker config,
   replay the captured requests, and assert the responses match the golden
   files. Normalize the known-variable fields (`request_id`, timestamps,
   `duration_ms`, `sampled_at`) before comparison.
3. **Wire contracts to assert explicitly** (the silent-break risks): the
   `/v1/conversions` base64-JSON cursor format; the `Diagnostic` body shape per
   failure class; the `X-RateLimit-*` / `Retry-After` headers; the
   `/v1/jobs/active` + `/v1/conversions` shapes the Streamlit UI depends on.

Until step 1 runs against Python, behavioral parity is "structurally verified +
contract-tested," not "byte-diffed against the oracle." That final diff is the
Phase 6 exit criterion and should run in CI before the Phase 8 cutover.

## Golden-fixture diff — as built (2026-06-03)

Implemented as a capture script (the Python oracle) + a data-driven Go test (the
diff), sharing a generated manifest so the case + seed definitions live in one
place:

| Artifact | Role |
| --- | --- |
| `scripts/capture_golden.py` | Boots the in-process Python service (Starlette `TestClient` + fake worker), seeds the recent/progress stores with a **fixed dataset**, hits 14 cases, and freezes each response to `internal/server/testdata/golden/<name>.json` + a `manifest.json`. No Aspose; no qpdf (cases seed stores directly instead of converting). |
| `internal/server/golden_test.go` (`TestGoldenParity`) | Reads the manifest, seeds the **same** records into the Go stores, replays the same requests against the Go `httptest` server, and diffs. Skips cleanly if fixtures are absent. |
| `make golden-capture` / `make golden-verify` | Capture in `python:3.12-slim`; verify in `golang:1.26-bookworm`. Both run **qpdf-less** so the env-coupled health status (200-with-qpdf vs 503-without) agrees on both sides. |

**Cases captured (14):** `health`; `/v1/conversions` empty + seeded-page1 (cursor) + filter=failed; `/v1/conversions/stats`; `/v1/jobs/active`; `/v1/jobs/{id}/progress` known + unknown; heartbeats/timings empty; and the Diagnostic envelope for `s3_disabled`, `missing_file`, `unsupported_format` (freezes `detected_magic` + the full `accepted` list), and `rate_limited` (freezes the `X-RateLimit-*` + `Retry-After` headers).

### Why semantic comparison, not a byte diff

The spec said "byte-for-byte," but capture proved that wrong in two concrete,
benign ways — so the comparator decodes both sides to JSON values and compares
by value (numbers unify as float64; cursors decode before comparison):

1. **Whole-valued floats.** Python's `json` renders `1.0`/`0.0`; Go's
   `encoding/json` renders `1`/`0`. Identical to any JSON parser, different
   bytes. Affects `load_progress`, `merge_done`, `weighted_percent`,
   `completion_ts`, etc.
2. **Cursor tokens.** `/v1/conversions` `next_cursor` is `base64(JSON{ts,id})`;
   its embedded float `ts` inherits (1), so the **token bytes differ** while
   decoding to the same `{ts,id}`. The Go test decodes the cursor and compares
   structurally — and pagination still interoperates across a Python↔Go rollover
   because both sides *parse* the token as floats.

Volatile fields (`license_days_remaining`, `started_at`, `elapsed_s`,
`X-RateLimit-Reset`) are normalized to a sentinel before comparison;
deterministic-by-seed fields (`completion_ts`, cursor `ts`) are kept and compared
numerically. `X-Request-ID` is *not* normalized — a fixed request-header value is
sent so the echo is asserted exactly.

### Divergence found + resolved

The gate caught one real wire-contract divergence on its first run:
Python's `JobProgress.to_dict()` did `asdict(self)`, which **leaked the internal
`last_touched` bookkeeping field** (a `time.monotonic()` value, meaningless to
clients) onto `/v1/jobs/{id}/progress` and `/v1/jobs/active`. The Go port never
emitted it. No consumer (incl. the Streamlit UI) reads it. **Resolution
(operator decision):** strip it from Python — `to_dict()` now `pop`s
`last_touched`, cleaning the contract and making Go the reference. Python suite
re-run green (237 passed / 1 skipped); gate now 14/14.

This is precisely the class of silent divergence the golden diff exists to catch
(cf. the earlier XLSX `pool_index=0` dashboard collapse).

### Environment notes / limits

- **health** is environment-coupled (status + `problems` depend on qpdf +
  worker-binary presence). The body's `ready`/`problems`/`license_days_remaining`
  are normalized; capture + verify must share a qpdf-posture for the *status* to
  agree (the make targets keep both qpdf-less, so they agree at 503).
- The **`/v1/convert` success path** (200 + `application/pdf` headers) is still
  exercised by the in-repo fake-worker/fake-qpdf tests, not the golden diff —
  adding it needs real qpdf in the capture image and is the one remaining
  enrichment if a stronger success-header diff is wanted.
