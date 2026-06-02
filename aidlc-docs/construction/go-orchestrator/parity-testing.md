# Phase 6 — Parity Testing

**Status**: In-repo parity tests COMPLETE for everything provable without the
Aspose C++ workers + qpdf + a running Python instance. The golden-fixture diff
against live Python is **scaffolded + specified here**, deferred to an
environment that has qpdf + the Python service (the dev box / CI image).

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

1. **Golden-fixture diff vs live Python** — the canonical cross-impl parity
   gate. Requires a running Python `office-convert` (with its own fake worker)
   to capture reference responses, then a Go test that diffs Go responses
   byte-for-byte. See the harness spec below.
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
