# Requirements — Office Converter (Local v1)

## Intent Analysis

- **User Request**: "Using AI-DLC can you please understand
  aspose-total/office-converter.md" (subsequently narrowed via scope
  pivot: "keep it simple for now — we'll get it working locally first
  (no EKS).")
- **Request Type**: New Project (greenfield).
- **Scope Estimate**: Multiple Components (HTTP server, chunk planner,
  subprocess Aspose worker, qpdf merge wrapper, cache, license
  lifecycle).
- **Complexity Estimate**: Moderate. The algorithm is non-trivial
  (chunk + subdivision + streaming merge under a hard RAM ceiling) but
  the operational scope is deliberately bounded.

**Source materials**:

- `office-converter.md` — the original design doc (cloud-target).
- `local-v1-scope.md` — v1 question/answer set with picks confirmed by
  the user on 2026-05-11.
- `requirement-verification-questions.md` — preserved 25-question
  cloud-target work; informs *algorithm* decisions inherited into v1
  but explicitly NOT v1 infrastructure requirements.

## Functional Requirements

### FR-1 — HTTP Convert Endpoint

The service exposes one conversion endpoint:

- `POST /convert`
- Content-Type: `multipart/form-data`
- Form fields:
  - `file`: binary input document. Accepted formats: DOCX, PPTX, XLSX,
    PDF.
  - `options` (optional JSON): `{"cache": <bool>, "log_level":
    "<level>"}`.
- Successful response:
  - Status: `200 OK`
  - Content-Type: `application/pdf`
  - Body: the converted PDF, streamed via chunked transfer encoding.
  - Headers carry conversion metadata: `X-Request-ID`,
    `X-Chunks-Rendered`, `X-Subdivision-Retries`, `X-Duration-Seconds`,
    `X-Cache-Hits`.

### FR-2 — Health Endpoint

- `GET /health`
- Status: `200 OK` when the service is ready.
- Body: `{"ready": <bool>, "license_days_remaining": <int>,
  "active_jobs": <int>, "max_jobs": <int>}`.

### FR-3 — Chunked Render Algorithm

For each `POST /convert` request, the service:

1. **Probes** the input to determine page count, format, and natural
   structural boundaries (Word sections, PPT slide ranges, Excel
   sheets, PDF page ranges).
2. **Plans chunks** bounded by both page count and estimated rendered
   memory cost:
   - Default upper bound per chunk: 10 pages OR 50 MB estimated
     rendered size, whichever is smaller.
   - Split strategy: natural seams when they produce balanced chunks
     within the bound; otherwise fall back to page-range splitting.
   - Chunk plan is deterministic for a given input.
3. **Renders each chunk** in an isolated subprocess (see FR-6) using
   Aspose.Total C++.
4. **Merges chunk PDFs** by streaming them through `qpdf
   --empty --pages ... -- -` to stdout. The merged PDF is the
   response body. The merger never holds the full output in memory.

### FR-4 — Subdivision-On-OOM Retry

When a chunk render exits with the documented "OOM" exit code:

- The orchestrator subdivides the failing page range
  (10 → 5 → 2 → 1 page) and re-dispatches.
- Subdivision continues to single-page granularity.
- A single page that still cannot render at 2 GB + swap is
  documented as a "subdivision floor exceeded" failure
  (see FR-5).

### FR-5 — Structured Failure Responses

Failures are returned as HTTP error responses with structured JSON
bodies. The diagnostic always includes a `request_id` (also in the
`X-Request-ID` response header).

| Status | Failure class                  | When                                          |
| ------ | ------------------------------ | --------------------------------------------- |
| 400    | `unsupported_format`           | Input file is not DOCX/PPTX/XLSX/PDF          |
| 400    | `missing_file`                 | `file` field absent from multipart            |
| 422    | `input_unprocessable`          | Aspose cannot parse (corrupt, encrypted, etc.)|
| 500    | `render_failed`                | Transient render error not classifiable below |
| 500    | `subdivision_floor_exceeded`   | Single-page chunk still OOMs                  |
| 500    | `merge_failed`                 | qpdf failure during concat                    |
| 503    | `license_expired`              | Aspose temp license past expiry               |
| 503    | `busy`                         | Server at `--max-jobs` (Retry-After header)   |

### FR-6 — Isolated Subprocess Per Chunk Render

- Each chunk render runs as a fresh subprocess of the Aspose worker
  binary.
- The subprocess inherits an address-space limit of 2 GB via
  `prlimit --as=2147483648` applied before exec.
- The subprocess catches Aspose's out-of-memory exception and exits
  with a documented "OOM" exit code (distinct from generic failure).

### FR-7 — Optional Local Filesystem Cache

- Activated when `OFFICE_CONVERT_CACHE_DIR` env var is set to a
  writable directory.
- Two cache layers, both keyed by content SHA-256:
  - **Final output**: `<cache-dir>/final/<source_sha256>.pdf`
  - **Per-chunk PDFs**: `<cache-dir>/chunks/<chunk_sha256>.pdf`
- Per-request bypass: `options.cache: false` in the submit body.
- No automatic eviction in v1; operator deletes the cache directory
  when it grows too large.

### FR-8 — License Lifecycle Handling

- Aspose Temporary License (`.lic`) bind-mounted at the path named
  in `ASPOSE_LICENSE_PATH` (default `/aspose/license.lic`).
- License must be Aspose.Total scope (covers Words, Slides, Cells,
  PDF — all four are used).
- License expiry date logged at INFO on every request.
- Days-remaining thresholds:
  - **≤7 days**: WARN log per request, surfaced in `/health` body.
  - **≤1 day**: ERROR log per request, surfaced in `/health` body.
  - **Expired**: every `POST /convert` returns 503 with
    `failure_class: license_expired`. The service does NOT silently
    fall back to evaluation mode (which would watermark output).

### FR-9 — Concurrency Control

The service enforces two stacked concurrency budgets:

- `--max-jobs N` (server-level, default 1): concurrent HTTP
  requests served. Excess requests return 503 `busy` with
  `Retry-After`.
- `--parallel N` (per-job, default 2): concurrent chunk renders
  inside a single job.

Peak Aspose worker RAM = `max-jobs × parallel × 2 GB`. At defaults:
4 GB.

### FR-10 — Structured Logging

- Two log formats: JSON-lines (default) and human (opt-in via
  `OFFICE_CONVERT_LOG_FORMAT=human`).
- Each log event carries `timestamp`, `level`, `request_id`, `event`.
- Required events: `server_start`, `request_received`,
  `chunk_complete`, `subdivision_retry`, `request_complete`,
  `request_failed`.
- Output sink: stdout/stderr. No remote sink in v1.

## Non-Functional Requirements

### NFR-1 — Memory Ceiling (Hard)

- Each Aspose worker subprocess MUST NOT exceed 2 GB of address
  space. Enforced by the kernel via `RLIMIT_AS`.
- The full output PDF MUST NOT be buffered in memory at any layer.
  The merge step streams through qpdf; the HTTP response body
  streams via chunked transfer encoding.

### NFR-2 — Swap as Soft Cushion

- The host (Docker container or bare host) SHOULD have swap
  available to act as an OOM cushion for borderline chunks. No
  special configuration is required in v1; whatever swap the OS
  provides is fine.
- Chronic swap usage indicates the chunk planner's MB-bound is
  mis-estimating amplification; this is observable via the
  operator's standard OS tools (no service-specific metrics
  required in v1).

### NFR-3 — Input Size Bound

- v1 accepts inputs up to 1 GB. Larger inputs are rejected at
  ingest (HTTP 400, `failure_class: input_too_large`).
- This is a v1 simplification of the cloud-target 10 GB ceiling.
  Raising it requires only a config change once tested.

### NFR-4 — Wall-Time Targets

- Best-effort, not committed:
  - ≤100 MB input: aim for <5 min wall time.
  - >100 MB input: best-effort.
- No SLO formally committed in v1.
- Caller HTTP timeout MUST be configured to ≥ 15 min for safety;
  documented as a prerequisite.

### NFR-5 — Determinism

- The chunk planner is deterministic: same input → same chunk plan,
  byte-for-byte.
- The qpdf concat is deterministic.
- Aspose render is non-deterministic in practice (rendering
  artifacts at the byte level may differ between runs); this is
  accepted.

### NFR-6 — Testability

- Property-based tests cover (Hypothesis):
  - Chunk planner: full coverage, non-overlap, monotonic ordering,
    bound respected, subdivision determinism.
  - qpdf concat wrapper: page-count round-trip, page-order
    preservation, associativity.
  - Subdivision logic: termination, determinism.
- In-process integration tests cover end-to-end conversion through
  the HTTP API via FastAPI `TestClient` (no Docker required) with
  a fake worker stand-in.
- **End-to-end tests** via Testcontainers cover real Docker image
  behavior (Dockerfile correctness, real C++ worker linkage, real
  Aspose render, real qpdf concat, real `prlimit` enforcement).
  Gated by `OFFICE_CONVERT_E2E_LICENSE` env var; CI without a
  license runs only the in-process suite.
- Unit tests cover HTTP request validation, error mapping,
  concurrency semaphore behavior, license-expiry helper.

### NFR-7 — Packaging

- Single Docker image bundles: Python orchestrator, FastAPI server,
  Aspose.Total C++ runtime + dependencies, qpdf, the Aspose worker
  binary.
- Image is x86_64-only (Aspose.Total C++ constraint).
- License file is NOT baked into the image; it is bind-mounted at
  runtime.

### NFR-8 — Trust Boundary (Security)

- v1 runs locally; trust boundary is the host. No authentication,
  no authorization, no network exposure beyond `localhost` by
  default (operator may bind to `0.0.0.0` if they choose; warned in
  the README).
- License file MUST NOT be checked into source control or embedded
  in the image.
- The orchestrator MUST NOT log the contents of input documents
  (only metadata: size, format, page count).
- Input validation: file format detection by magic bytes (not by
  extension); reject anything outside the four supported formats
  before any further processing.

## Hard Constraints (Load-Bearing)

| Constraint                      | Source        | Notes                                      |
| ------------------------------- | ------------- | ------------------------------------------ |
| 2 GB RAM per render             | User          | Per-pod ceiling in cloud, per-subprocess locally |
| Local-only v1, no cloud         | User          | EKS/SQS/S3/DynamoDB deferred               |
| Aspose Temporary License        | User          | 30-day evaluation, Aspose.Total scope      |
| Single HTTP endpoint surface    | User (Q1=C)   | `/convert` + `/health`                     |
| Property-based testing enabled  | Gating phase  | Blocking constraint                        |
| Security baseline enabled       | Gating phase  | Blocking constraint                        |

## Out of Scope for v1 (Deferred)

The following were debated in `requirement-verification-questions.md`
and explicitly deferred. They are not requirements for v1 but are
preserved for any future cloud scope:

- EKS deployment, Kubernetes manifests, KEDA autoscaling
- SQS-driven async ingest
- DynamoDB job status table
- S3 source/output storage and presigned URLs
- Multi-tenancy (logical or hard isolation)
- AWS IAM authentication / `Attributes.SenderId` validation
- Per-tenant quotas
- CloudWatch / X-Ray observability stack
- Region/DR posture (multi-AZ, active-active)
- Compliance regimes (HIPAA, SOC 2, GDPR)
- Memory tiers beyond 2 GB
- Inputs above 1 GB (raised to 10 GB in cloud scope)
- Tiered SLO (typical / intermediate / best-effort)
- Caller-provided destination S3 URI
- Signing of output PDFs
- PDF/A or linearized output profiles

## Extension Compliance

### Security Baseline (Enabled)

| Concern                                       | v1 status                                                     |
| --------------------------------------------- | ------------------------------------------------------------- |
| Secrets not in source or image                | Aspose `.lic` is bind-mounted, never baked into image (NFR-8) |
| Input validation                              | Magic-byte format check before any further processing (NFR-8) |
| Don't log sensitive content                   | Logs carry only document metadata, not body (NFR-8)           |
| Define trust boundary                         | Local-only, localhost default, operator-aware (NFR-8)         |
| AuthN / AuthZ                                 | N/A — single-tenant local scope, explicit non-requirement     |
| Encryption in transit                         | N/A — localhost; operator deploys TLS terminator if needed    |
| Encryption at rest                            | N/A — local filesystem, operator's host security              |

### Property-Based Testing (Enabled)

Targets enumerated in NFR-6. The PBT-load-bearing surfaces are:

- **Chunk planner**: highest priority. A planner bug under the
  2 GB ceiling produces OOMs → subdivision retries → wall-time hits
  or failures.
- **qpdf concat wrapper**: page-level correctness is critical
  because the output PDF is the externally-visible artifact.
- **Subdivision logic**: must terminate and be deterministic so
  retries don't produce different plans.

Surfaces NOT in scope for PBT (covered by example-based tests):

- Aspose render fidelity (can't generate plausible Office documents)
- License lifecycle (small, example-based vectors)
- HTTP layer (FastAPI's own test coverage + integration tests)

## Open Items (Resolved Before Code Generation)

The following remained open at the close of Requirements Analysis
and should be resolved during Workflow Planning or before Code
Generation:

1. **Sample document corpus**: user supplies representative
   documents, or AI generates synthetic ones for the four formats.
2. **Target host environment**: pure Linux container, or also
   Docker-on-Mac (amd64 emulation acceptable for dev).
3. **Python version**: 3.11+ assumed; confirm or pin a specific
   version.

Resolved:

- Aspose license: Aspose Temporary License, Aspose.Total scope
  (Q9, 2026-05-11).
