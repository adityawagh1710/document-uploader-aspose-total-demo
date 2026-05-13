# Local v1 Scope — Office Converter

**Scope statement**: Prove the chunk-render-merge pipeline works on a single
Linux box inside a 2 GB RAM ceiling per render. No cloud infrastructure.

**Out of scope for v1**: EKS, SQS, DynamoDB, S3, multi-tenancy, auth,
autoscaling, observability stack, presigned URLs, CloudWatch metrics.
Those concerns live in `requirement-verification-questions.md` and are
deferred until v1 is working.

**What survives from the cloud-scope discussion** (the algorithm itself):

- 2 GB RAM ceiling per render process
- Conservative chunk policy: 10 pages or 50 MB estimated rendered size
- Hybrid split: natural seams (Word sections, PPT slide ranges, Excel
  sheets, PDF page ranges) when balanced; fall back to page-range
- Subdivision-on-OOM retry: 10 → 5 → 2 → 1 page; single-page floor
- qpdf streaming merge (NOT Aspose merge — Aspose accumulates the
  full output DOM in memory)
- Aspose.Total C++ (x86_64 Linux only)
- Swap as OOM cushion (whatever the host OS provides; no special
  configuration in v1)

---

## Q1 — Invocation surface

How is the converter invoked?

A) CLI: `office-convert input.docx output.pdf` — simplest, scriptable
B) Python library: `from office_convert import convert; convert(...)`
C) Local HTTP server on localhost:8080 — closest to the eventual cloud shape
D) Both A and B

[Answer]: **C — Local HTTP server.** (Confirmed by user 2026-05-11, revised
from D.)

**Rationale:** Chosen by the user after explicit discussion of trade-offs.
HTTP gives a language-agnostic entry point that any local caller can hit
(browser, Postman, curl, Node, Go, internal web app). The operational
consequences (server lifecycle, concurrency budgeting, large-body
streaming, timeout configuration) are accepted as v1 cost.

**Server invocation:**

The Docker image's `CMD` starts the server:

```
docker run -p 8080:8080 -v ./io:/io -v ./license.lic:/aspose/license.lic \
  aspose-converter
```

For non-Docker dev: `uvicorn office_convert.server:app --host 0.0.0.0 --port 8080`.

**HTTP API (one endpoint + one health probe):**

```
POST /convert
  Content-Type: multipart/form-data
    file: <binary>          # the Office document to convert
    options: <JSON>         # optional: {"cache": false, "log_level": "info"}
  Response:
    200 OK
      Content-Type: application/pdf
      Body: <streamed PDF bytes from qpdf concat>
    400 Bad Request   — unsupported format, missing file
    422 Unprocessable — input rejected by Aspose (corrupt, encrypted)
    500 Internal      — render or merge failed; body carries diagnostic JSON
    503 Service Unavailable — license expired or worker pool exhausted

GET /health
  200 OK
    {"ready": true, "license_days_remaining": 23, "active_jobs": 1, "max_jobs": 1}
```

**Framework: FastAPI + Uvicorn.** Type-hint-driven, async-native, streams
multipart and response bodies cleanly, auto-generates OpenAPI at `/docs`
for free, well-supported. No real alternative worth considering at this
size.

**Concurrency budget (this becomes the load-bearing design question
under C; clarifies Q4):**

- **Server-level**: `--max-jobs N` (default 1). A semaphore caps
  concurrent conversion requests. Excess requests return 503 with
  `Retry-After`. Default N=1 keeps the peak-RAM math obvious: at most
  one job × 2 chunks parallel × 2 GB = 4 GB peak, fits any reasonable
  dev host.
- **Per-job-level**: Q4's parallel-chunk-renders setting still applies
  inside a single job; default still 2.
- These two together: at default settings, the server uses at most
  4 GB of Aspose worker RAM regardless of incoming request rate.
- Users on bigger hosts can raise `--max-jobs` to trade RAM for
  throughput.

**Synchronous response semantics:**

The PDF is the response body. Caller holds the HTTP connection for the
full job duration (potentially up to 15 min on large inputs). This is
the simplest model — no job state, no polling, no callback URLs. Two
operational notes:

- Default uvicorn/Starlette request timeouts are way under 15 min.
  Server is configured with `--timeout-keep-alive 60` and explicit
  `--timeout-graceful-shutdown 900`. Callers must configure their
  HTTP client timeouts to ≥ 15 min for large inputs.
- The response body streams from qpdf's stdout directly to the HTTP
  response (chunked transfer encoding). The server never buffers the
  full output PDF in memory — critical given some outputs may be
  hundreds of MB.

**Error model:**

`POST /convert` returns one of:

- `200` + PDF body on success
- `4xx` for caller-fixable issues, JSON body with `{error, detail}`
- `5xx` for service issues, JSON body with the same structured
  diagnostic the CLI would have written to stderr in the A/B variants

Subdivision-floor failures are `500` with `diagnostic.failure_class:
"subdivision_floor_exceeded"`. License expiry is `503` with
`diagnostic.failure_class: "license_expired"` and the date in detail.

**What this drops vs the previous D pick:**

- No CLI `office-convert input.docx output.pdf` one-shot. To convert
  locally, you start the server and `curl` to it.
- No public `from office_convert import convert` Python library. The
  HTTP handler internally calls a private `_convert_internal()`
  function but it is not the public surface.
- Tests now bring the server up in fixtures (FastAPI `TestClient`
  handles this cleanly — no real network port needed for tests).

**What this keeps:**

- Subprocess-per-chunk Aspose model (Q2 = A) unchanged. Server forks
  Aspose worker processes per chunk.
- 2 GB ceiling enforcement via `prlimit` (Q3 = A) unchanged.
- Docker packaging (Q8 = B) unchanged. Image just runs `uvicorn`
  instead of `office-convert` CLI.
- Bind-mounted temp license (Q9 = A) unchanged.

---

## Q2 — Aspose integration mechanism

How does the orchestrator invoke Aspose.Total C++ to render a chunk?

A) Subprocess per chunk render — spawn a fresh `aspose-render-chunk` binary,
   pass page range as args, receive PDF on stdout or written to a file path
B) Long-lived worker process with a local queue (mini-SQS in-memory)
C) Direct C++ library call from a single process (FFI / pybind11)

[Recommended Answer]: **A — Subprocess per chunk render**

**Rationale:** Critical for the 2 GB RAM ceiling (Q3 below). Each chunk
render gets a fresh process with `prlimit RLIMIT_AS=2G` applied before
exec; on OOM the process is killed cleanly without taking the
orchestrator down. Subprocess overhead (~50 ms fork+exec, library load)
is negligible against multi-second renders. B (long-lived worker) would
require Aspose to reliably free all memory between renders — Aspose's
allocator behavior under sustained use is the kind of thing we don't
want to discover in production. C (FFI) inherits memory state forever
and any leak compounds.

---

## Q3 — 2 GB ceiling enforcement mechanism

How is the per-render 2 GB ceiling actually enforced?

A) `prlimit --as=2147483648 -- aspose-render-chunk ...` (Linux address-
   space limit, set before exec)
B) Cgroups v2 unit per render (`systemd-run --scope -p MemoryMax=2G`)
C) Docker container per render (`docker run --memory=2g`)
D) Rely on application-level Aspose memory tuning, no OS-level limit

[Recommended Answer]: **A — `prlimit` address-space limit**

**Rationale:** Simplest mechanism that works on plain Linux without root,
without systemd, without Docker-in-Docker. `RLIMIT_AS` is enforced by
the kernel — when the process's virtual address space hits 2 GB, the
next `mmap`/`brk` returns ENOMEM and Aspose's allocator surfaces it as
an exception (which the render binary catches → exits with a specific
"OOM" exit code → orchestrator subdivides). B (cgroups v2) is more
flexible but requires systemd-run or direct cgroup writes — overkill
for v1. C (Docker) works but adds container-runtime overhead per chunk
and complicates filesystem sharing for temp folders. D is what the
design doc rejected ("the 2 GB ceiling is hard"); no.

**Caveat:** `RLIMIT_AS` limits virtual memory, not RSS. Aspose may
allocate large sparse mappings that pass `RLIMIT_AS` but never touch
physical RAM. In practice this is fine — modern allocators page-fault
on use, so the effective bound matches RSS closely. If we see false
OOMs from virtual-address fragmentation in testing, switch to cgroups.

---

## Q4 — Concurrency model

How many chunk renders run in parallel?

A) Serial — one render at a time. Simplest.
B) Fixed pool of N concurrent subprocess renders (configurable, default 2)
C) Auto-detect: `min(CPU_count, available_RAM_GB / 2.5)` to avoid OOM-on-host

[Recommended Answer]: **B — Fixed pool, default N=2** (per-job).

**Rationale:** Serial (A) is too conservative — even a laptop has 16 GB
RAM, so running 2 renders × 2 GB each = 4 GB peak with headroom for
swap. B gives meaningful wall-time wins on multi-chunk documents at
zero correctness cost. C (auto-detect) is clever but introduces a
heuristic we'd need to test — start with B's fixed default and add
auto-detection later if the user runs into "no, my laptop only has 4
GB" or "I have 64 cores, use them all" cases.

**Cascade from Q1 = C (HTTP server):** there are now TWO concurrency
budgets stacked:

| Setting | Default | Meaning |
| --- | --- | --- |
| `--max-jobs` (server-level) | 1 | concurrent HTTP requests being served |
| `--parallel` / N (per-job) | 2 | concurrent chunk renders inside one job |

Peak Aspose-worker RAM = `max-jobs × parallel × 2 GB`. At defaults:
`1 × 2 × 2 GB = 4 GB`. Raise `--max-jobs` to trade RAM for inter-request
throughput; raise `--parallel` to trade RAM for per-job wall time.

---

## Q5 — Cache

Is there a cache, and where?

A) No cache — every invocation re-renders. Simplest.
B) Optional local directory cache, enabled with `--cache-dir DIR`. Keyed
   by content SHA-256.
C) Mandatory cache at a fixed location (`~/.cache/office-convert/`).

[Recommended Answer]: **B — Optional local directory cache**

**Rationale:** A is too austere — even local development benefits from
not re-rendering on repeat invocations. C is too opinionated — users
running this in CI or in temporary environments don't want a default
cache leaking into surprising paths. B is the right middle: off by
default, easy to enable, trivially auditable (`ls $CACHE_DIR`).
Caches both final outputs and per-chunk PDFs (mirrors the cloud-scope
Q13 = B picks).

**Cascade from Q1 = C (HTTP server):** the configuration surface is no
longer a CLI flag. Cache is configured at two levels:

1. **Server start (operator):** `OFFICE_CONVERT_CACHE_DIR` env var
   (Docker bind-mount the volume at the path it points to). If unset,
   no cache. If set, cache is active for every request.
2. **Per-request (caller):** `options.cache: false` in the multipart
   `options` JSON field bypasses the cache for that specific request
   (mirrors the cloud-scope Q14 `nocache` semantics).

**TTL: "until operator deletes"** — no automatic expiry in v1. Known
limitation documented in the operator guide. Long-lived server +
unbounded cache growth = future problem; flag for v2.

---

## Q6 — Failure handling

What happens when subdivision reaches the single-page floor and still OOMs?

A) Exit non-zero with a diagnostic JSON written to stderr describing
   the failing page range, file format, last attempted chunk plan
B) Same as A, plus copy the input file and chunk plan to a configurable
   `--quarantine-dir` for later forensic inspection
C) Best-effort: skip the failing pages, complete the rest, emit a
   warning

[Recommended Answer]: **A — Fail loudly with structured diagnostic**

**Rationale:** Simplest "failed loudly, with information" model. C
(skip) silently produces incorrect output, which violates the
principle that "the floor is well-defined and observable" (Q25 of
the cloud-scope discussion). B (quarantine dir) doesn't apply — the
HTTP caller still has the input file in their hands, no need for the
server to copy it anywhere.

**Cascade from Q1 = C (HTTP server):** "exit non-zero + stderr" maps
to HTTP error responses:

| Failure | Status | Body                                                       |
| ------- | ------ | ---------------------------------------------------------- |
| Bad input format          | 400 | `{error: "unsupported_format", detail: "..."}`     |
| Encrypted / corrupt input | 422 | `{error: "input_unprocessable", detail: "..."}`    |
| Render failed (transient) | 500 | `{error: "render_failed", diagnostic: {...}}`      |
| Subdivision-floor OOM     | 500 | `{error: "subdivision_floor_exceeded", diagnostic: {failing_page_range, format, attempts}}` |
| License expired           | 503 | `{error: "license_expired", expired_on: "..."}`    |
| Server at max-jobs        | 503 | `{error: "busy", retry_after: 60}` + `Retry-After: 60` header |

Diagnostic JSON always includes a `request_id` (UUID assigned at
request arrival, also returned in `X-Request-ID` response header) so
caller-side and server-side logs can be correlated. Server-side logs
emit the full diagnostic at ERROR level; the client sees the same
structure in the response body.

Successful conversions: response body IS the PDF (no JSON wrapping);
metadata travels in response headers (`X-Request-ID`,
`X-Chunks-Rendered`, `X-Subdivision-Retries`, `X-Duration-Seconds`,
`X-Cache-Hits`).

---

## Q7 — Logging

How does the converter log progress?

A) stderr, human-readable, level via `--log-level {debug,info,warn,error}`
B) stderr, JSON-lines, level via flag
C) Both: human-readable by default, JSON via `--log-format json`

[Recommended Answer]: **C — Both formats, default JSON-lines** (revised
default for server context).

**Rationale:** Adding both formats is one logging helper away — cheap.
Cascade from Q1 = C: a long-lived server in a container normally has
its stdout/stderr captured by Docker / a log aggregator, where
JSON-lines is the lingua franca. Default flips from "human" (sensible
for one-shot CLI) to "json" (sensible for a server). Human form
stays available via `--log-format human` for interactive dev runs.

**Logging shape:**

- **JSON-lines (default)**: one event per line, each event has
  `timestamp`, `level`, `request_id`, `event`, plus event-specific
  fields. Examples:
  - `{"event": "server_start", "license_days_remaining": 23, ...}`
  - `{"event": "request_received", "request_id": "...", "format": "docx", "input_size_bytes": 5242880}`
  - `{"event": "chunk_complete", "request_id": "...", "chunk_index": 4, "chunks_total": 10, "duration_seconds": 3.2}`
  - `{"event": "request_complete", "request_id": "...", "status": 200, "duration_seconds": 24.5}`
  - `{"event": "subdivision_retry", "request_id": "...", "chunk_index": 7, "page_range_before": [60, 70], "page_range_after": [60, 65]}`
- **Human (opt-in)**: same data, single-line readable: `2026-05-11
  12:34:56 INFO [req_abc123] chunk 4/10 complete in 3.2s`
- **Level**: configurable via env var `OFFICE_CONVERT_LOG_LEVEL`
  (debug / info / warn / error) and `OFFICE_CONVERT_LOG_FORMAT`
  (json / human).
- **No remote sink in v1.** Logs go to stdout/stderr; aggregation
  is the operator's problem (Docker logs, fluent-bit sidecar, etc.).

---

## Q8 — Packaging

How is the converter distributed?

A) Plain Python package (`pip install office-convert`) — but Aspose.Total
   C++ is a separately-licensed C++ binary that doesn't fit a Python
   wheel cleanly
B) Docker image with Aspose + Python orchestrator + qpdf preinstalled,
   single `docker run` invocation
C) Tarball with all binaries + a wrapper script. No Docker.

[Recommended Answer]: **B — Docker image**

**Rationale:** Aspose.Total C++ has specific runtime dependencies
(specific glibc versions, fontconfig, freetype, often a system Java
runtime for some operations) that are a nightmare to install ad-hoc
on every developer machine. Docker bundles all of that into a known-
good environment. A single `docker run -v $PWD:/io office-convert
/io/input.docx /io/output.pdf` is the v1 happy path. The image is also
what eventually deploys to EKS unchanged, so this is forward-compatible
without making us write a Helm chart today.

**Caveat:** Aspose.Total C++ is x86_64-only. Docker on M-series Macs
runs amd64 images via emulation (slow but functional) — acceptable
for v1 development; production hosts run on amd64.

---

## Q9 — License provisioning

How is the Aspose license file made available to the converter?

A) Mount via Docker bind: `-v ./license.lic:/aspose/license.lic`,
   path passed via env var `ASPOSE_LICENSE_PATH`
B) Bake the license into the image at build time
C) Trial / evaluation mode only for now (output watermarked)

[Answer]: **A — Bind-mounted license file.** License type:
**Aspose Temporary License (30-day, Aspose.Total scope).** (Confirmed
by user 2026-05-11.)

**Rationale:** Standard provisioning pattern. Keeps the license file out
of the image (which would leak it if the image is ever pushed to a
public registry). Same env var works in v1 (Docker bind) and in any
future cloud deployment (Kubernetes Secret mount at the same path). B
leaks the license into the registry. C (eval mode) would watermark
output, which the user has rejected by choosing a real license.

**License type sub-spec (added 2026-05-11):**

- **Kind:** Aspose Temporary License — obtained via
  `purchase.aspose.com/temporary-license`. Full functionality, no
  watermark, 30-day expiry. Suitable for development and PoC; not for
  production.
- **Scope:** Must be an **Aspose.Total** temporary license, not a
  per-product license. The converter uses Aspose.Words (DOCX),
  Aspose.Slides (PPTX), Aspose.Cells (XLSX), and Aspose.PDF. A
  single-product license fails on the other three formats.
- **File format:** `.lic` (XML signed by Aspose). Mounted at the path
  named in `ASPOSE_LICENSE_PATH` (recommend `/aspose/license.lic`).

**Expiry handling (new requirement, not present under permanent license):**

1. **On startup**, the orchestrator calls Aspose's `SetLicense()` and
   reads the license expiry date from the response. Log the expiry
   date at INFO level on every invocation.
2. **Warn-window: ≤ 7 days to expiry** → log a WARN line on every
   invocation with the days-remaining count. Surfaced in CLI output
   too: `WARN: Aspose license expires in 5 days.`
3. **Critical-window: ≤ 1 day to expiry** → log ERROR, but still
   process the job. Surface to caller (CLI exit message + library
   ConversionResult field) so it's visible without grep-ing logs.
4. **Post-expiry**: Aspose's `SetLicense()` will throw on an expired
   license. Catch it cleanly, emit a diagnostic that says explicitly
   "license expired on YYYY-MM-DD; obtain a new temporary license at
   purchase.aspose.com/temporary-license", exit non-zero. Do NOT
   silently fall back to evaluation mode — that would produce
   watermarked output that looks correct but isn't.
5. **`ConversionResult` field:** `license_days_remaining: int | None`
   for programmatic callers (None if license type doesn't expire).
6. **Renewal workflow** documented in the README: where to request a
   new temp license, where to drop the new `.lic` file, no service
   restart needed (the file is read per-invocation, not cached).

**Documentation impact:** README needs a "Getting an Aspose License"
section as a prerequisite step. Don't ship the temp license in the
git repo (it's tied to the requester's email and shouldn't be shared).

---

## Q10 — Testing

What's the v1 test pyramid?

A) Unit tests only
B) Unit + integration (end-to-end conversion of sample documents)
C) Unit + integration + property-based tests on the chunk planner,
   subdivision logic, and qpdf concat round-trip

[Recommended Answer]: **C — Full pyramid including PBT**

**Rationale:** The property-based-testing extension is enabled as a
blocking constraint (set during the gating phase before the scope
pivot). PBT scope shrinks vs the cloud-scope answer because SQS
consumer idempotency and S3 multipart streaming are no longer
applicable, but the core surfaces survive intact.

**Test pyramid under Q1 = C (HTTP server):**

| Layer | Tool | Targets |
| --- | --- | --- |
| Unit (pure logic) | pytest | Chunk planner, subdivision logic, qpdf wrapper, license-expiry helper, options-JSON parser |
| Unit (HTTP handlers) | pytest + FastAPI `TestClient` | Endpoint routing, request validation, error-response mapping, concurrency semaphore behavior |
| Property-based | Hypothesis | (see invariants below) |
| Integration | pytest + `TestClient` + real Aspose subprocess | End-to-end conversion on sample documents through the HTTP API |
| Smoke (manual) | `curl` against the running container | Pre-release sanity check |

**PBT invariants (unchanged from earlier draft):**

- **Chunk planner**: total page coverage = input page count;
  non-overlapping ranges; monotonic ordering; chunk size ≤ Q11
  ceiling; subdivision halves page ranges deterministically.
- **qpdf concat wrapper**: page count of concat output = sum of
  input page counts; page-order preserved; concat is associative.
- **Subdivision logic**: subdivide(n pages) eventually reaches
  single-page chunks or returns failure; subdivide is deterministic
  for a given input.

**FastAPI `TestClient` advantage**: integration tests bring the
server up *in-process* with no real network port, no Docker, no
uvicorn — just the ASGI app called directly. Tests are fast and
hermetic. Real-network smoke testing happens at the manual `curl`
layer pre-release.

**Integration test corpus**: small set of sample documents (1-page
PDF, 100-page DOCX, multi-sheet XLSX, PPTX with embedded media) in
`test_corpus/`. Open item below covers whether you supply them or I
generate synthetic ones.

---

## Open items the user should decide before code generation

1. ~~**Aspose.Total C++ license**: do you already have a license file, or
   does the team need to acquire one? Without a license, output is
   watermarked.~~ **Resolved 2026-05-11: Aspose Temporary License,
   Aspose.Total scope. See Q9.**
2. **Sample document corpus**: do you have representative documents for
   integration testing, or should I generate synthetic ones?
3. **Target host environment for v1**: pure Linux container, or does it
   need to run on a developer's macOS via Docker Desktop? (Affects
   how aggressively we depend on x86_64-specific behavior.)
4. **Python version**: 3.11+ assumed. Any constraint pulling you to a
   specific version?

---

**When you're done**, reply "answered" (or call out which questions to
revise), and I'll proceed to Workflow Planning with this scope and
draft a focused implementation plan.
