# Go Orchestrator Migration Plan — office-convert

**Status**: Proposed — NOT approved. Discussion artifact only; no code authored.
**Author**: Aditya Wagh + Claude
**Created**: 2026-05-29
**Pattern**: CONSTRUCTION-phase tech-stack swap (orchestrator language Python → Go).
Requirements unchanged — the HTTP contract, failure taxonomy, and FR/NFR set are
preserved byte-for-byte.

## Goal

Re-implement the Python orchestrator (`office_convert/*.py`, ~9.1k LOC) in Go
**without changing observable behaviour**. The C++ Aspose workers, the JSON-stdio
worker protocol, the Streamlit UI, and the Helm chart stay as-is.

This is explicitly a **language/runtime decision, not a product decision**. It does
NOT touch what the service does, only how the orchestrator layer is built.

## Why this is a clean swap (and why it may not be worth it)

The orchestrator is decoupled from everything expensive:

- **The engine is C++ regardless.** Aspose has no native Go SDK. Go cannot render;
  it shells out to the same `worker_cpp/` binaries over the same protocol Python
  uses today. The 5-binary CodePorting split, fork-unsafe Cells, and scaffolded
  Aspose calls are unchanged — **this migration does nothing about the project's
  actual source of complexity.**
- **The UI is a separate HTTP-client process.** `office_convert_ui/app.py` imports
  zero `office_convert` modules. Honour the endpoint contract and it survives the
  swap untouched (see [§ UI impact](#ui-impact)).
- **The bottleneck is render time, not orchestration.** Per the audit data, renders
  run seconds-to-minutes and probes are <0.01 s; orchestrator overhead is already
  negligible. **Users will not perceive a speedup.** The gains are operational
  (footprint, single binary, concurrency clarity), not latency.

## What does NOT change

- `worker_cpp/` — all 5 per-product binaries, the `--mode pool` / `--pool-size`
  protocol, `prlimit RLIMIT_AS` wrapping.
- `office_convert_ui/` — Streamlit UI stays Python (option 1 below).
- `deploy/helm/office-convert/` — same chart; only the image changes.
- `aidlc-docs/inception/**` — requirements, the 25 Q&A, personas, user stories.
  Behaviour is identical, so intent docs do not move.
- `functional-design/` — `business-rules.md`, `business-logic-model.md`,
  `domain-entities.md` are language-agnostic spec.

## Architecture overview

```
+---------------------------------------------------------------+
|  CLIENT (UI browser / classification-service / curl)         |
+----------------------------+----------------------------------+
                             | HTTP (unchanged /v1 contract)
                             v
+---------------------------------------------------------------+
|  Go orchestrator  (replaces office_convert/*.py)              |
|    net/http router + flushing StreamingResponse               |
|    probe -> plan -> dispatch -> qpdf merge -> stream          |
|    in-memory obs stores (heartbeats/timings/progress/recent)  |
+------------------+--------------------------+-----------------+
                   | exec + prlimit            | exec
                   v  (JSON stdio, UNCHANGED)  v
        +-------------------------+   +--------------------+
        |  worker_cpp (C++)       |   |  qpdf (streaming)  |
        |  5 per-product binaries |   +--------------------+
        |  UNCHANGED              |
        +-------------------------+

  Streamlit UI (Python, UNCHANGED) ---- polls ----> Go /v1/* endpoints
```

## Package layout (mirrors `office_convert/`)

```
cmd/orchestrator/main.go        # uvicorn ... server:app  -> http.ListenAndServe
internal/
  config/    config.go          # pydantic-settings (OFFICE_CONVERT_*) -> env parse + validate
  types/     types.go           # Chunk, ChunkPlan, ProbeResult, FormatName
  oclog/     log.go             # logging.py JSON + request_id  -> log/slog + context
  server/    server.go          # server.py routes, middleware, error->HTTP map, streaming
  probe/     probe.go           # detect_format (magic bytes) + probe_lite (archive/zip, PDF xref)
  planner/   planner.go         # chunk_planner.py — PURE funcs, ~verbatim port
  worker/    pool.go            # aspose_worker.py + worker_pool.py — exec workers, JSON stdio
  qpdf/      qpdf.go            # qpdf.py — streaming concat
  cache/     cache.go           # cache.py — content-addressable, atomic rename
  license/   license.go         # license.py — XML expiry parse
  s3/        s3.go              # s3_client.py — aws-sdk-go-v2
  obs/       ring.go progress.go recent.go   # the 4 observability stores
worker_cpp/                     # UNCHANGED
```

Dependencies: stdlib `net/http`, `golang.org/x/sync/errgroup`, `aws-sdk-go-v2`,
a Go PBT lib (`pgregory.net/rapid`). That is essentially the whole list.

## Phased plan (contract-frozen)

Phase 0 captures the current OpenAPI + JSON shapes as golden fixtures — the live
Python becomes the parity oracle. Phases 1-6 land incrementally; the Go binary is
not deployable until phase 5. Phase 6 runs continuously against the fixtures.

| Phase | Scope | Python source | Difficulty |
| ----- | ----- | ------------- | ---------- |
| 0 Scaffold + freeze contract | Go module, CI, golden OpenAPI/JSON fixtures | — | Low |
| 1 Pure logic | types, planner, detect_format, probe_lite, license, cache, csv->xlsx | `types.py`, `chunk_planner.py`, `probe.py`, `license.py`, `cache.py`, `csv_input.py` | Low |
| 2 Worker layer | one-shot exec + `WorkerPool` + `ForkedPoolLeader` seq-demux + stderr->stores | `aspose_worker.py`, `worker_pool.py` | **High** |
| 3 Merge + orchestrator | qpdf streaming, `convert_job`, OOM subdivide/retry | `qpdf.py`, `orchestrator.py` | Medium |
| 4 Observability stores | 4 stores -> endpoints | `heartbeats/timings/job_progress/recent.py` | Low-Med |
| 5 Server | router, middleware, error map, rate limit, S3, LibreOffice/EML/CSV routing, dashboard+landing HTML | `server.py`, `errors.py`, `rate_limit.py`, `s3_client.py`, `libreoffice_convert.py`, `aspose_email_convert.py` | Medium-High |
| 6 Parity testing | port PBT, integration w/ fake worker, e2e via testcontainers, golden-fixture diff | 235 tests | **High** |
| 7 Containerize + deploy | swap runtime stage (python:3.12-slim -> distroless/scratch + Go binary); keep C++ builder stage, qpdf, LibreOffice, fonts; Helm image swap | `Dockerfile` stage 2, Helm | Medium |
| 8 Cutover | run Go + Python side-by-side, diff, flip, decommission Python | — | Medium |

### Concurrency mapping (the load-bearing parts)

- **Forked-pool seq-demux** (`worker_pool.py::ForkedPoolLeader`): `dict[int, asyncio.Future]`
  + stdout-reader task -> `map[int]chan resp` under a `sync.Mutex` + a reader goroutine;
  `asyncio.wait_for(fut, timeout)` -> `select` on the channel vs `ctx.Done()`. Cleaner in Go.
- **Bounded fan-out** (`orchestrator.py`): `asyncio.Semaphore(parallel)` + `gather`
  -> `errgroup` with `g.SetLimit(parallel)`, results written to an index-keyed slice
  (no post-sort needed). Shared `ctx` gives the same fail-fast.
- **Streaming merge** (`qpdf.py`): async generator yielding 64 KB blocks ->
  `io.Copy(w, stdout)` with an `http.Flusher`-backed writer; cache tee via `io.MultiWriter`.
- **prlimit**: UNCHANGED — Go keeps the `prlimit --as=N -- worker ...` wrapper exec
  (Go's `Setrlimit` applies to the calling process, not the child).

### Observability store mapping

| Python store | Go equivalent | Key change |
| ------------ | ------------- | ---------- |
| `HeartbeatStore` (deque/req, cap 5000, TTL 30m) | generic `RingStore[Heartbeat]` | add `sync.Mutex` |
| `TimingStore` (deque/req, TTL 30m) | `RingStore[Timing]` (same generic) | add `sync.Mutex` |
| `JobProgressStore` (mutable struct, weighted %) | `ProgressStore` + functional `Update(rid, func(*JobProgress))` | mutex; mutator fn instead of kwargs |
| `RecentStore` (deque 200 + cursor pagination) | slice-ring + `Cursor` (base64 JSON, identical wire format) | mutex; pagination ports verbatim |

**Critical correctness note:** Python's stores are lock-free, safe only because of the
GIL + the single-uvicorn-worker assumption (see `project-dashboard-architecture` memory).
Go has true goroutine parallelism, so **every store needs an explicit lock** — that is
what makes the rewrite correct, not just idiomatic. The multi-replica tripwire is
unchanged: >1 replica still needs external persistence (Postgres/Dynamo) regardless of
language; Go merely makes the single-process version genuinely thread-safe.

## UI impact

The Streamlit UI is a separate process coupled only by HTTP. **Recommended: keep it
as-is (option 1).** Go's only obligation is to honour the endpoint contract and serve
the static `/v1/dashboard` + landing HTML (embed via `embed.FS`, byte-identical):

- `POST /v1/convert`, `/health`, `/v1/stats`, `/v1/workers`
- `/v1/jobs/{id}/{heartbeats,timings,progress}`, `/v1/jobs/active`
- `/v1/conversions{,/stats}`, `/v1/downloads/presign`, `DELETE /v1/cache`

Two contracts that MUST be preserved or the UI breaks silently:
- `PUBLIC_API_URL` vs `API_URL` (browser-facing iframe/links vs server-side calls).
- The API-wide fallback signals (`/v1/jobs/active`, `/v1/conversions?limit=1`) the UI
  uses so cross-service (classification-service) conversions still appear — Go must
  keep those populated, including the cross-service path. See `feedback-ui-vs-api-state`.

Alternatives (not recommended): rebuild the UI in Go (templ+htmx — large effort, no
functional gain); or drop Streamlit and rely on the embedded `/v1/dashboard` (loses
upload / per-session history / re-run / S3 download buttons).

## Effort estimate

One experienced Go + infra engineer, to parity **with tests**:

| Phase | Person-weeks |
| ----- | ------------ |
| 0 Scaffold + contract freeze | 0.5 |
| 1 Pure logic | 1.0 |
| 2 Worker layer (forked pool) | 1.5–2.0 |
| 3 Merge + orchestrator | 1.0–1.5 |
| 4 Observability stores | 0.5–1.0 |
| 5 Server | 1.5–2.0 |
| 6 Parity testing | 2.0–3.0 |
| 7 Containerize + deploy | 1.0 |
| 8 Cutover | 0.5–1.0 |
| **Total** | **~9.5–12.5 weeks (≈ 2.5–3 months)** |

The test rebuild (phase 6) is the biggest line item and the easiest to underestimate —
the 235 tests encode hard-won edge cases (OOM subdivision floor, format-mismatch retry,
OOXML EOCD detection, the seq-demux race). Skimping there reintroduces paid-for bugs.

## AI-DLC impact

Re-enters at CONSTRUCTION, not INCEPTION, because requirements are stable.

**Revised (not rewritten):**
- `aidlc-state.md` — decision entry; **Q8 reconsidered (A Python -> Go)**, recorded as
  proposed until approved (mirrors the 2026-05-12 Aspose SKU pivot record).
- `audit.md` — ISO-timestamped decision + approval log entry.
- `nfr-requirements/tech-stack-decisions.md` — the "why Go" rationale + the explicit
  note that it does not address the Aspose C++ pain.
- `nfr-design/nfr-design-patterns.md` — concurrency pattern deltas (goroutines/errgroup;
  the GIL->mutex store rule; `io.Copy`+`Flusher` streaming). prlimit isolation unchanged.
- `application-design/services.md` + `component-methods.md` — tech-stack table; same 12
  components, new language.
- `README.md`, `deploy/README.md`, `Dockerfile` — build/run instructions (no uv/venv).

**Gating extensions to re-satisfy (both blocking):**
- **PBT** — chunk-planner / qpdf-concat / subdivision properties re-expressed in
  `pgregory.net/rapid`.
- **Security baseline** — re-verify non-root / read-only-root / cap-drop / no-secrets.
  Go makes this easier (static binary on distroless/scratch).

## Gains vs costs

**Gains (operational, not latency):**
- Image ~150–250 MB runtime layer -> ~15–35 MB static binary on distroless/scratch;
  smaller Trivy/CVE surface.
- Interpreter-free deploy; ms cold start; deploy = copy one binary.
- Lower orchestrator baseline RAM; no GIL -> true parallel request handling
  (binding constraint is still C++ worker RAM).
- Cleaner concurrency (channels/select/io.Copy vs asyncio futures).
- Genuinely thread-safe single-process stores.

**Costs / non-gains:**
- Zero help on the real pain (Aspose C++ edition). C++ workers unchanged.
- End-to-end latency essentially unchanged (render-bound).
- Rebuild the 235-test safety net; regression risk.
- Polyglot repo (Go backend + Python UI).
- Lose Python ecosystem glue (zip/XML probe, csv->xlsx, moto S3 tests) — re-doable in
  Go stdlib + aws-sdk-go-v2, but rewrite work.

## Recommendation

For a **greenfield** build, Go is a strong default orchestrator. For **this** repo — a
working, 235-test, deployed system whose bottleneck is Aspose C++ rendering — the ROI of
a ~3-month rewrite is **marginal** unless single-binary footprint or interpreter-free
deploys are a stated goal. The higher-leverage lever for "make this project better"
remains the **Aspose engine edition** (C++ -> C#/.NET or Java), which is where the
complexity actually lives. Recorded here as a scoped option, not a recommendation to
proceed.

## Open questions (for approval gate)

1. Is single-binary footprint / interpreter-free deploy an actual stated goal, or
   nice-to-have? (Drives whether this clears the ROI bar.)
2. Keep Streamlit UI (option 1) — confirmed? Or is a Go-native UI in scope (changes the
   effort materially)?
3. Cutover style: side-by-side traffic diff then flip, or hard swap on dev05 first?
4. Does the team have Go depth to own this long-term, or does it trade a Python
   maintenance burden for a Go one?
