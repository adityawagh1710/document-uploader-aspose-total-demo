# Document Uploader Office Convert Service (Aspose.Total)

[![python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![fastapi](https://img.shields.io/badge/fastapi-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![aspose.total](https://img.shields.io/badge/aspose.total-c%2B%2B%2026.4-ff4081.svg)](https://products.aspose.com/total/cpp/)
[![qpdf](https://img.shields.io/badge/qpdf-streaming%20merge-orange.svg)](https://qpdf.sourceforge.io/)
[![docker](https://img.shields.io/badge/docker-required-2496ED.svg?logo=docker&logoColor=white)](https://docs.docker.com/)
[![tests](https://img.shields.io/badge/tests-238-brightgreen.svg)](#tldr--quickstart)
[![type checked](https://img.shields.io/badge/type%20checked-mypy%20strict-1f5082.svg)](http://mypy-lang.org/)
[![lint](https://img.shields.io/badge/lint-ruff-D7FF64.svg)](https://github.com/astral-sh/ruff)
[![status](https://img.shields.io/badge/status-v1%20local%20PoC-yellow.svg)](#)
[![last commit](https://img.shields.io/badge/last%20commit-may%202026-blue.svg)](#)
[![contributors](https://img.shields.io/badge/contributors-1-orange.svg)](#)
[![repo](https://img.shields.io/badge/repo-internal-lightgrey.svg)](#)
[![AI-DLC](https://img.shields.io/badge/AI--DLC-powered-9C27B0.svg)](https://github.com/aws-samples/aws-aidlc-rule-details)

**v1.0 — local PoC** · Chunked Office document → PDF conversion service.

Converts DOCX, PPTX, XLSX, PDF, legacy DOC/XLS/PPT, ODT/ODS/ODP/ODG,
RTF, CSV, **raster + vector images** (PNG, JPG, TIFF, GIF, BMP, WEBP,
SVG), **and email** (EML) to PDF over a local HTTP API. Office docs
render via **Aspose.Total C++** with RAM-isolated subprocess workers
and **qpdf** streaming merge; ODG + images route through the bundled
**LibreOffice** fallback (soffice `--convert-to pdf` picks the right
importer per input extension); EML goes through a two-stage
**Aspose.Email → MHTML → Aspose.Words → PDF** pipeline so the two
products' CodePorting frameworks stay process-isolated.

```
┌────────────────────── Docker container (4GB RAM + 2GB swap) ──────────────────────┐
│                                                                                    │
│   FastAPI / Uvicorn (Python) → net/http + chi (Go orchestrator)                    │
│   ┌─────────────────────────────────────────────────────────────────────────┐      │
│   │ 1. Format Detection (magic bytes + OLE2 streams, format retry)          │      │
│   │ 2. Probe Lite (ZIP metadata / size estimate — instant)                  │      │
│   │ 3. Adaptive Chunk Planner (RAM-aware, parallelism-aware)                │      │
│   │ 4. Worker Pool (persistent processes, document loaded once)             │      │
│   │ 5. Auto Re-plan (actual page count from workers → corrected chunks)     │      │
│   └─────────────────────────────────────────────────────────────────────────┘      │
│       │                                                                            │
│       │ Pool mode: load once → render N chunks (stdin/stdout JSON protocol)        │
│       ▼                                                                            │
│   ┌──────────────────────────────────────────────────────────────────────┐         │
│   │ C++ Worker Pool (5 per-product binaries, prlimit RLIMIT_AS=6GB)      │         │
│   │ ├─ office-convert-worker-docx  (Aspose.Words, PageSet slicing)       │         │
│   │ ├─ office-convert-worker-pptx  (Aspose.Slides, slide-index array)    │         │
│   │ ├─ office-convert-worker-xlsx  (Aspose.Cells, PageIndex/PageCount)   │         │
│   │ ├─ office-convert-worker-pdf   (Aspose.PDF, page Delete + Save)      │         │
│   │ └─ office-convert-worker-email (Aspose.Email, EML → MHTML; the       │         │
│   │                                 docx worker then renders MHTML→PDF)  │         │
│   └──────────────────────────────────────────────────────────────────────┘         │
│       │                                                                            │
│       │ chunk PDFs                                                                 │
│       ▼                                                                            │
│   qpdf --empty --pages ... -- -  ═══ streaming bytes ═══▶  HTTP response           │
│   (never buffers full PDF in memory)                                               │
└────────────────────────────────────────────────────────────────────────────────────┘
```

> **Orchestrator box:** the Go re-implementation (`net/http` + `chi`) now lives on
> `main` alongside the Python (FastAPI) orchestrator and is the **only** box that
> changes at cutover — the C++ workers, JSON-stdio protocol, qpdf merge, and HTTP
> wire contract are identical. **Python is still the deployed backend until the
> Phase 8 dev05 cutover** (see the "Go orchestrator" section below).

For detailed Mermaid diagrams (request flow, format detection, pool mode),
see `aidlc-docs/construction/office-converter/architecture-diagram.md`.

---

## TL;DR — Quickstart

**Host requirements:** Docker (with `docker compose` v2) + `curl`. Optionally
`make` for the convenience targets. **Nothing else** — no Python, no uv, no
qpdf, no pytest needed on the host. Everything runs inside containers.

### Path A — Docker Compose (canonical)

```bash
# 1. Place the two Aspose files in the project root
cp /path/to/Aspose.Total.for.C++_*.tar.gz aspose-total-cpp.tar.gz
# (Aspose.TotalforC++.lic should already exist here)

# 2. Build and start the service
docker compose up -d --build

# 3. Hit the endpoints via URL
curl http://localhost:8080/health
curl -X POST http://localhost:8080/v1/convert \
     -F "file=@tests/corpus/simple.pdf" -o output.pdf

# 4. Run the test suite (separate test image; no Aspose SDK needed)
docker compose --profile test run --rm tests

# 5. Stop when done
docker compose down
```

### Path B — Makefile (same thing, with helpers)

```bash
make build-test          # build the test image (no Aspose dep)
make test                # run unit + property + integration suites
make build               # build the production image (needs Aspose tarball + license)
make up                  # docker compose up -d --build; waits for /health
make demo                # URL-based smoke: /health + bad-format + real convert + docs URLs
make convert FILE=tests/corpus/simple.pdf
make down                # docker compose down
```

`make up` delegates to `docker compose up -d --build`, then waits for `/health`
to respond and prints all the URLs. `make help` shows the full target list.

Need the SDK or license?

- **SDK tarball**: <https://releases.aspose.com/total/cpp/>
- **Temporary license**: <https://purchase.aspose.com/temporary-license>
  (specify "Aspose.Total **for C++**" — the Python-via-.NET license is NOT
  compatible with this build).

### Path C — EKS dev cluster (Helm)

A working dev deployment lives under `deploy/` for shared dogfooding /
multi-operator testing. Same image, same orchestrator — running on
`DEV05-EKS-CLUSTER` in namespace `office-convert-dev`. Host requirements:
`kubectl`, `helm`, `aws` CLI, corp VPN.

```bash
# Push images to ECR, then:
AWS_ACCOUNT_ID=537462380503 AWS_REGION=eu-west-1 IMAGE_TAG=$(git rev-parse --short HEAD) \
    make deploy-dev          # full undeploy + redeploy; logs to deploy/logs/

# Access via port-forward (NLBs are internal; corp VPN doesn't peer with VPC):
./deploy/scripts/portforward.sh start
# → API on http://localhost:18080, UI on http://localhost:8501

# Tear down:
make undeploy-dev
```

Full layout, scripts, and the in-flight ALB Ingress migration plan
(with corp-CIDR-allowlisted internet-facing URL) are documented in
[`deploy/README.md`](deploy/README.md) and
[`aidlc-docs/operations/dev-deployment-topology.md`](aidlc-docs/operations/dev-deployment-topology.md).

---

## Why chunked

Aspose accumulates rendered content in memory; a single large document
can exceed available RAM. This service breaks inputs into bounded chunks
(default: **10 pages or 50 MB** of estimated rendered size, whichever is
smaller), renders each chunk in a fresh subprocess capped at **2 GB**
virtual memory via `prlimit RLIMIT_AS`, and streams the resulting chunk
PDFs through `qpdf --empty --pages ... -- -` directly into the HTTP
response. The full output PDF is never buffered.

If a chunk OOMs, the orchestrator **subdivides** the page range
(10 → 5 → 2 → 1 page) and retries. Single-page failures dead-letter
with a structured diagnostic rather than silently producing partial
output.

---

## v1 Status & What's In The Box

The conversion engine is a **single-container service** — the canonical
deployment is Docker Compose on a single host (Path A / Path B above), and
that's still the v1 PoC the AI-DLC workflow produced. A working **EKS dev
deployment** (Path C) runs the same image on `DEV05-EKS-CLUSTER` for
shared dogfooding; see `aidlc-docs/operations/dev-deployment-topology.md`
for the current shape and the in-flight ALB Ingress migration.

The full **v1-cloud target** (queue-driven via per-tenant SQS, no public
HTTP, DynamoDB job state, multi-tenant isolation) is explicitly deferred
— `aidlc-docs/operations/eks-production-topology.md` captures that
production design, and the original 25-question requirement set lives at
`aidlc-docs/inception/requirements/requirement-verification-questions.md`.

**Generated and complete** (run as-is):

- Python orchestrator + HTTP server (FastAPI on Uvicorn)
- Chunk planner (pure logic + property-based tests; 500 Hypothesis
  examples covering coverage, monotonicity, balance, determinism)
- qpdf streaming merge wrapper (real qpdf binary, async byte iterator)
- Cache layer (content-addressable, atomic writes)
- License XML parser + expiry state machine
- Structured JSON-lines logging with `request_id` ContextVar propagation
- Multi-stage Dockerfile with non-root user + read-only-root-compatible
  + cap-drop-compatible posture
- 100+ test cases across unit / property / integration / e2e /
  security layers

**Scaffolded — operator must wire in real Aspose calls before production**:

- `worker_cpp/formats/{docx,pptx,xlsx,pdf,email}.cpp` — Aspose API calls are
  documented in comments but commented out, so the C++ compiles cleanly
  without the SDK headers. After placing the Aspose tarball in the
  build context, uncomment the real calls per the inline comment
  blocks, then rebuild.
- `worker_cpp/probe.cpp` — same pattern for metadata extraction.
- `worker_cpp/license.cpp` — same pattern for `SetLicense()` per
  format.

The Dockerfile sets up the entire build pipeline; when both the SDK
tarball is present AND the Aspose calls are uncommented, the C++ worker
builds and links cleanly. Otherwise the worker compiles but throws
`RenderException("SDK not linked")` at runtime — useful for verifying
the Dockerfile and HTTP plumbing before paying for an Aspose license.

---

## Go orchestrator (migration — backend-only, pre-cutover)

The Python orchestrator described throughout this README is the **current
production backend**. A complete **Go re-implementation** of the orchestrator
lives alongside it in `cmd/` + `internal/` on `main` (merged from
`feat/go-orchestrator`, now deleted) and is validated end-to-end but **not yet
cut over** — Python remains the deployed backend until the Phase 8 dev05 cutover.

This is **transitional duplication, not a hybrid**: one backend reimplemented
two ways. Everything *around* the orchestrator is shared and unchanged — the C++
Aspose worker binaries (`worker_cpp/`), the JSON-stdio worker protocol, the
Streamlit UI (`office_convert_ui/`), and the Helm chart (`deploy/helm/`). The Go
orchestrator (`cmd/` + `internal/`) shells out to the same five per-product
worker binaries over the same protocol, so the cutover is a pure image swap
(same `repository:tag` contract).

**Stack:** Go stdlib + `net/http` routing via `go-chi/chi/v5`; `log/slog`
logging; AWS SDK v2; tests use `testify` + `go-cmp` + `pgregory.net/rapid`
(property-based). The HTTP wire contract is identical to the Python service.

**Run / test it:**

```bash
make up-go             # build + run the Go stack (backend + UI) via compose.go.yaml
make test-go           # full Go suite (unit + property + golden parity gate)
make golden-verify     # replay the Go server against the captured Python oracle
make golden-capture    # (re)generate the golden fixtures from the live Python oracle
make down-go           # tear down
```

**Golden-fixture parity gate.** `make golden-verify` (and CI's `go-test` job)
runs `TestGoldenParity`, which replays the Python orchestrator's frozen HTTP
responses against the Go server and diffs them — the safety net that proves the
two backends are interchangeable before the cutover. Fixtures are committed under
`internal/server/testdata/golden/`; regenerate them with `make golden-capture`
(needs Python; no Aspose/qpdf). Built with `go.Dockerfile` (not the production
`Dockerfile`). Design docs: `aidlc-docs/construction/go-orchestrator/`.

---

## Prerequisites

1. **Aspose.Total C++ Temporary License** (`.lic` file). Request at
   <https://purchase.aspose.com/temporary-license>. Specify "Aspose.Total
   **for C++**" — the Python-via-.NET license is NOT compatible.
2. **Aspose.Total C++ SDK tarball** (`aspose-total-cpp.tar.gz`). The
   headers + shared objects needed to build the worker. Save next to
   the Dockerfile.
3. **Docker** (or any OCI-compatible runtime).
4. **x86_64 host**. Aspose.Total C++ is x86_64-Linux only. Apple Silicon
   hosts work via Docker Desktop's amd64 emulation (slower; dev only).

---

## Docker-First Workflow

Two paths to the same outcome:

| Path | When to use |
| ---- | ----------- |
| **Docker Compose** (`docker compose up -d`, `--profile test run …`) | Canonical. Standard tooling everyone recognizes. CI-friendly. |
| **Makefile** (`make up`, `make test`, `make demo`, …) | Convenience: organized help (`make help`), waits for `/health`, URL-based smoke (`make demo`), variable overrides on the command line. Delegates to compose under the hood for `up`/`down`/`logs`/`restart`. |

Both read the same `compose.yaml`. Pick whichever feels natural.

### Two images

The `compose.yaml` defines two services and the corresponding images:

| Image | Built by | Purpose |
| ----- | -------- | ------- |
| `office-convert:test` | `Dockerfile.test` | Python + dev deps + qpdf. Runs unit/property/integration tests. **No Aspose SDK needed.** |
| `office-convert:dev` | `Dockerfile` (multi-stage) | Production image with C++ worker + Aspose `.so` + Python orchestrator. **Requires `aspose-total-cpp.tar.gz` in build context.** |

### Common workflows

**Just run the tests** (no Aspose dependency):

```bash
make build-test       # ~30 sec; only needed once unless you change deps
make test             # ~60 sec; full unit + property + integration suite
make test-coverage    # also enforces ≥ 80% line coverage
make qa               # lint + format-check + typecheck + test
```

**Full production build + URL-based smoke test** (needs Aspose SDK + license):

```bash
# Pre-flight: place the two Aspose files in the project root
cp /path/to/Aspose.Total.for.C++_*.tar.gz aspose-total-cpp.tar.gz
ls Aspose.TotalforC++.lic       # license should already exist

make build            # ~5 min cold; builds C++ worker + multi-stage image
make up               # starts service; waits for /health to respond

# Now test via URLs (all run from your shell, hitting localhost)
make health           # GET /health
make demo             # health + bad-format + convert + docs URLs
make convert FILE=tests/corpus/simple.pdf   # POST a document
make docs             # print Swagger / ReDoc / OpenAPI URLs

make logs             # tail container logs
make shell            # interactive shell in the container
make down             # stop and remove
```

**End-to-end tests via Testcontainers** (programmatic e2e against the running container):

```bash
make test-e2e         # mounts Docker socket; spawns prod container; runs pytest
```

**Cleanup**:

```bash
make clean            # stops service; removes both images
make clean-all        # also removes .pytest_cache, .ruff_cache, etc.
```

### Override configuration on the command line

Any variable defined at the top of the `Makefile` can be overridden:

```bash
make up PORT=9090                                          # use a different host port
make convert FILE=~/Documents/big.docx                     # convert a doc outside the repo
make build IMAGE_PROD=myregistry/office-convert:0.2.0      # tag for a registry push
make up LICENSE_FILE=/etc/secrets/aspose.lic               # license at a non-default path
```

---

## Manual Docker commands (without `make`)

If you prefer the underlying Docker commands or are scripting around them:

```bash
cp /path/to/aspose-total-cpp.tar.gz .
docker build -t office-convert:dev .
```

The image is multi-stage:

- **Stage 1** (`debian:bookworm`): gcc-12 + CMake + Aspose tarball → builds
  the C++ worker binary with `-O2 -flto -fvisibility=hidden -fdata-sections
  -ffunction-sections` and `--gc-sections` + strip for minimal binary size.
- **Stage 2** (`python:3.11-slim-bookworm`): qpdf + util-linux + Python
  deps + non-root `appuser`; copies the worker binary and Aspose `.so`
  files from Stage 1 into `/usr/local/bin/` and `/usr/local/lib/aspose/`
  with `LD_LIBRARY_PATH` set.

The C++ compiler, headers, and build tools never enter the runtime image.

Build will fail at the `COPY aspose-total-cpp.tar.gz` step if the tarball
is missing from the build context.

---

## Run

**Recommended (localhost-only, defense-in-depth):**

```bash
docker run --rm \
    -p 127.0.0.1:8080:8080 \
    -v $(pwd)/license.lic:/aspose/license.lic:ro \
    --cap-drop=ALL \
    --read-only \
    --tmpfs /tmp \
    --tmpfs /var/run \
    office-convert:dev
```

**With cache (greatly speeds up repeat conversions of the same input):**

```bash
docker run ... \
    -v $(pwd)/cache:/cache \
    -e OFFICE_CONVERT_CACHE_DIR=/cache \
    office-convert:dev
```

---

## HTTP API

All application endpoints are versioned under `/v1/`. Only `/health` stays
unversioned (orchestrator probe convention).

### `POST /v1/convert`

```bash
curl -X POST http://localhost:8080/v1/convert \
    -F "file=@document.docx" \
    -F 'options={"cache":true}' \
    -o output.pdf
```

**Request**: `multipart/form-data` with:

- `file` — binary input. Accepted (magic-byte detected, not by extension):
  - **Office / documents**: DOCX, PPTX, XLSX, PDF, legacy DOC/XLS/PPT,
    ODT, ODS, ODP, ODG, RTF, CSV
  - **Images** (routed to LibreOffice): PNG, JPG, JPEG, TIFF, GIF, BMP,
    WEBP, SVG
- `options` (optional JSON): `{"cache": <bool>, "log_level": "<level>"}`
- `s3_input` (optional, requires `OFFICE_CONVERT_S3_ENABLED=1`) — `s3://bucket/key`
  to convert *instead of* uploading `file`. Exactly one of `file` / `s3_input`.
- `s3_output` (optional, requires S3 enabled) — `s3://bucket[/key]`. The PDF is
  streamed back to the caller AND teed to this S3 location. A bucket-only URL
  uses `s3_output_key_template` (`pdf/{request_id}.pdf`). Works in both input modes.

**Response on success**: HTTP 200, `Content-Type: application/pdf`,
chunked transfer encoding. Headers:

- `X-Request-ID` — UUID for log correlation
- `Content-Type: application/pdf`
- `X-S3-Output-Bucket` / `X-S3-Output-Key` — present only when `s3_output` was set

**Response on failure**: structured JSON body
`{request_id, failure_class, detail}`:

| Status | failure_class | When |
| ------ | ------------- | ---- |
| 400 | `unsupported_format` | Magic bytes don't match an accepted type |
| 400 | `missing_file` | Multipart `file` field absent or empty |
| 400 | `input_too_large` | Input exceeds 1 GB (`OFFICE_CONVERT_MAX_INPUT_BYTES`) |
| 422 | `input_unprocessable` | Aspose can't parse (corrupt, encrypted) |
| 500 | `render_failed` | Transient render error |
| 500 | `subdivision_floor_exceeded` | Single page OOMs even at 2 GB RAM |
| 500 | `merge_failed` | qpdf concat failed |
| 429 | `rate_limited` | Per-IP token bucket exhausted (`Retry-After` + `X-RateLimit-*` headers set) |
| 503 | `license_expired` | Aspose license past expiry |
| 503 | `busy` | At `max_jobs` capacity (`Retry-After` header set) |
| 400 | `input_source_conflict` | Both `file` and `s3_input` supplied |
| 400 | `s3_disabled` | `s3_input`/`s3_output`/presign used while S3 is off |
| 400 | `s3_invalid_url` | Malformed `s3://…` URL |
| 400 | `s3_input_forbidden` / `s3_output_forbidden` | Bucket not in the allowlist |
| 404 | `s3_input_not_found` | `s3_input` object does not exist |
| 500 | `s3_output_upload_failed` | S3 PUT failed (surfaced post-stream; logged) |

### `GET /v1/downloads/presign`

Mints a short-TTL presigned GET URL for an output object. The service owns the
S3 credentials (IRSA on EKS); clients only ever receive a time-boxed URL. The
output-bucket allowlist is enforced before signing.

```bash
curl "http://localhost:8080/v1/downloads/presign?bucket=office-convert-out&key=pdf/abc123.pdf"
# → {"download_url":"https://…X-Amz-Signature=…","bucket":"…","key":"…",
#    "expires_in_seconds":900,"expires_at":"2026-05-27T…Z"}
```

A fresh URL is minted per call (presigned URLs expire), so the Streamlit UI's
Conversion History calls this on demand for its "☁️ Download from S3" link.

### S3 integration — local vs EKS

| Concern | Local (compose) | EKS (Helm) |
|---|---|---|
| S3 endpoint | LocalStack (`AWS_ENDPOINT_URL_S3=http://localstack:4566`) | real AWS |
| Auth | `test`/`test` env creds | IRSA — ServiceAccount → OIDC → IAM role |
| Buckets | `office-convert-{in,out}` (auto-created by the localstack init hook) | operator-created |
| Enable | on by default in `compose.yaml` | `--set s3.enabled=true …` (see `deploy/iam/README.md`) |

### `GET /health`

```bash
curl http://localhost:8080/health
```

```json
{
  "ready": true,
  "license_days_remaining": 23,
  "active_jobs": 0,
  "max_jobs": 1,
  "problems": []
}
```

`200` if ready; `503` otherwise. `problems` carries failure-class strings
when not ready (`license_expired`, `qpdf_missing`, `worker_binary_missing`,
`scratch_dir_unwritable`, `aspose_so_unloadable`, `license_path_missing`,
`license_invalid`).

---

## Configuration

All runtime config via `OFFICE_CONVERT_*` environment variables:

| Variable | Default | Notes |
| -------- | ------- | ----- |
| `OFFICE_CONVERT_MAX_JOBS` | `1` | Concurrent HTTP requests served. Excess → 503 busy. |
| `OFFICE_CONVERT_PARALLEL` | `4` | Concurrent chunk renders inside one request (DOCX/PPTX/PDF use fork-after-load, so peak RAM ≈ 1× loaded doc; XLSX legacy pool independently loads per worker — see `XLSX_MAX_POOL_SIZE` row). |
| `OFFICE_CONVERT_XLSX_MAX_POOL_SIZE` | `4` | Per-format cap on workers for XLSX. Aspose.Cells is fork-unsafe, so each XLSX worker independently loads the workbook — large files (>50 MB) can OOM with `parallel=4` on a 4 GiB pod. Cap to `2` on swap-less environments (EKS chart default). |
| `OFFICE_CONVERT_CACHE_DIR` | (unset) | If set, enables content-addressable filesystem cache. Bind-mount a directory. |
| `OFFICE_CONVERT_LICENSE_PATH` | `/aspose/license.lic` | Path inside the container. Bind-mount the operator's `.lic` here. |
| `OFFICE_CONVERT_SCRATCH_DIR` | `/tmp/office-convert` | Per-request scratch directory. tmpfs recommended. |
| `OFFICE_CONVERT_LOG_FORMAT` | `json` | `json` (default) or `human`. |
| `OFFICE_CONVERT_LOG_LEVEL` | `info` | `debug` / `info` / `warn` / `error`. |
| `OFFICE_CONVERT_CHUNK_TIMEOUT_SECONDS` | `300` | Per-chunk render timeout. Hung renders killed and treated as render failures. |
| `OFFICE_CONVERT_MAX_INPUT_BYTES` | `1073741824` | 1 GB. Inputs above this are rejected at ingest. |
| `OFFICE_CONVERT_RATE_LIMIT_ENABLED` | `1` | Per-IP rate limit on `POST /v1/convert` (token bucket). Set to `0` to disable. |
| `OFFICE_CONVERT_RATE_LIMIT_PER_IP_RPM` | `30` | Sustained requests/minute/IP ceiling. Refill rate = `rpm / 60` tokens/sec. |
| `OFFICE_CONVERT_RATE_LIMIT_BURST` | `5` | Token-bucket capacity. Lets short bursts through before throttling kicks in. |
| `OFFICE_CONVERT_RATE_LIMIT_MAX_KEYS` | `10000` | LRU cap on the per-IP bucket dictionary (bounded memory). |
| `OFFICE_CONVERT_RATE_LIMIT_TRUST_XFF` | `1` | Use the first IP in `X-Forwarded-For` as the client identifier (ALB pattern). Set to `0` if exposed without a proxy — the header is spoofable. |

### Pool mode & observability knobs

These control the pool-mode workers and the live heartbeat dashboard.
All shell-overridable (`OFFICE_CONVERT_VAR=value docker compose up -d`).

| Variable | Default | Notes |
| -------- | ------- | ----- |
| `OFFICE_CONVERT_POOL_MODE` | `1` | Pool mode (persistent workers, load-once-render-many). Set to `0` to force per-chunk one-shot subprocesses. |
| `OFFICE_CONVERT_POOL_MIN_CHUNKS` | `2` | Pool mode activates when the chunk plan has at least this many chunks. Single-chunk plans use one-shot. Set to `1` to force pool mode on every conversion (useful for exercising the heartbeat dashboard on small files). |
| `OFFICE_CONVERT_HEARTBEAT_MS` | `2000` | Per-process heartbeat cadence in ms while a load or render is in flight. `0` disables. Heartbeats are emitted at DEBUG; flip `OFFICE_CONVERT_LOG_LEVEL=debug` to see them in stdout logs. |
| `OFFICE_CONVERT_FORK_AFTER_LOAD` | `0` | Fork-after-load mode: one leader process loads the document then `fork()`s N children that share the loaded `Document` via copy-on-write. Eliminates the N× parallel-parse contention that times out large-DOCX loads in default pool mode. Off by default until broader format/file-shape testing; flip per-workload when you have large files. |

### Live heartbeat dashboard

The Streamlit test UI at **http://localhost:8501** (`test-ui` service in
`compose.yaml`) shows a per-worker heartbeat table during pool-mode
conversions:

- Phase (load / render), elapsed-in-phase, **RSS in MB, Swap in MB** (orange
  when non-zero — indicates the worker is paging out under `memswap_limit`),
  CPU jiffies, time since last heartbeat.
- Green dot if heartbeat is ≤ 6 s old; orange if stale (worker likely hung).
- Each conversion generates a UUID sent as `X-Request-ID`; the panel polls
  `GET /v1/jobs/{request_id}/heartbeats` every 1 s and correlates 1:1 with the
  in-flight upload.

Heartbeats are also retrievable programmatically:
```bash
curl http://localhost:8080/v1/jobs/<request_id>/heartbeats
```
Returns the last 5000 heartbeats per request (30-min TTL).

Caller-side timeouts: configure your HTTP client to **at least 15 minutes**
for safety. Default client timeouts (30 s) will abort large conversions.

---

## License lifecycle

The Aspose Temporary License expires after 30 days. The service surfaces
expiry progressively (per `aidlc-docs/construction/office-converter/functional-design/business-rules.md` §4):

| Days remaining | Behavior |
| -------------- | -------- |
| `> 7` | Silent. |
| `4–7` | WARN log per `/v1/convert` request. |
| `1–3` | ERROR log per `/v1/convert` request. |
| `0` | `/health` flips to `ready: false`. `/v1/convert` still works today. |
| `< 0` | `/v1/convert` returns 503 `license_expired`. **No silent fallback to evaluation mode (no watermarked PDFs).** |

**To renew**: request a new temp license from Aspose, replace the
bind-mounted `.lic` in place. The service re-reads on every request — **no
container restart required**.

---

## Performance Tier-1 Optimizations

The C++ worker is optimized for the per-chunk subprocess pattern:

- **Lazy product activation** — `apply_license()` activates only the Aspose
  product matching `--format`, not all four. Saves ~150–600 ms of static
  init + SetLicense overhead per worker invocation.
- **Compiler/linker optimizations** — Release builds use
  `-O2 -flto -fvisibility=hidden -fvisibility-inlines-hidden
  -fdata-sections -ffunction-sections` + `-Wl,--gc-sections -Wl,-s`.
  10–30% smaller binary; ~30–100 ms faster dynamic-loader resolution
  per spawn.

Combined saving: **~200–700 ms per chunk render** of startup overhead.
Aspose render time itself is unchanged — that's still the dominant cost.

---

## Troubleshooting

| Symptom | Likely cause | Action |
| ------- | ------------ | ------ |
| `docker build` fails at `COPY aspose-total-cpp.tar.gz` | Tarball not in build context | `cp /path/to/aspose-total-cpp.tar.gz .` then rebuild |
| `docker build` fails with linker errors `undefined reference to Aspose::...` | SDK library names don't match `target_link_libraries()` in `worker_cpp/CMakeLists.txt` | Inspect the extracted tarball at `aspose-sdk/lib/`; update CMake `aspose_words` / `aspose_slides` / `aspose_cells` / `aspose_pdf` to match real `.so` filenames |
| Container starts but `/health` 503 with `qpdf_missing` | qpdf not in runtime image | Already in Dockerfile; check `apt-get` step succeeded |
| `/health` 503 with `worker_binary_missing` | C++ worker didn't build | Re-check builder-stage logs; verify the SDK extracted to `/opt/aspose-sdk/` |
| `/health` 503 with `license_path_missing` | Bind-mount missing | Add `-v ./license.lic:/aspose/license.lic:ro` |
| `/health` 503 with `license_expired` | License past expiry date | Renew the temp license |
| All `/v1/convert` requests return 500 `render_failed` with "SDK not linked" | Real Aspose calls still commented out in `worker_cpp/formats/*.cpp` | Uncomment the real Aspose API calls per the inline comment blocks; rebuild |
| 500 `subdivision_floor_exceeded` | A single page exceeds 2 GB RAM (e.g. PPTX with huge embedded media) | Reduce input complexity; documented v1 limitation |
| 503 `busy` on every request | `max_jobs` exhausted | Raise `OFFICE_CONVERT_MAX_JOBS` (mind host RAM headroom; XLSX is fork-unsafe so each worker independently loads the workbook — see `OFFICE_CONVERT_XLSX_MAX_POOL_SIZE` cap) |
| HTTP client times out at ~30 s on large conversions | Default client timeout too short | Set client timeout to ≥ 15 minutes |

---

## Known v1 Limitations

See `aidlc-docs/inception/user-stories/stories.md` § "Explicit Non-Goals"
for the full list with rationale:

- **NG-1**: No application-layer authentication or authorization. Anyone
  with network access to the listening port can submit jobs.
  Recommended: bind to `127.0.0.1` only (`--publish 127.0.0.1:8080:8080`).
- **NG-2**: No per-tenant or per-caller quotas (global `max_jobs` only).
- **NG-3**: No `/metrics` endpoint. Operators derive metrics from
  structured logs.
- **NG-4**: No automatic cache eviction. Operator deletes when full.
- **NG-5**: No hot-reload of config. Env-var changes require restart.
  Exception: the license file IS re-read per request.
- **NG-6**: No HA, replication, or failover.
- **NG-7**: No committed SLO (best-effort).
- **NG-8**: Output is generic PDF 1.7 only. No PDF/A, no linearization,
  no digital signing.
- **NG-9**: x86_64 Linux only (Aspose.Total C++ constraint).

---

## Development

The recommended path is **Docker-first via `make`** (see the workflow section
above). The same commands inside the test image, for the Makefile-curious:

| What | Make target | Underlying command |
| ---- | ----------- | ------------------ |
| Build test image | `make build-test` | `docker build -t office-convert:test -f Dockerfile.test .` |
| Run all tests | `make test` | `docker run --rm office-convert:test pytest tests/unit tests/property tests/integration -v` |
| Coverage gate | `make test-coverage` | `docker run --rm office-convert:test pytest --cov=office_convert --cov-fail-under=80 ...` |
| Lint | `make lint` | `docker run --rm office-convert:test ruff check .` |
| Format check | `make format-check` | `docker run --rm office-convert:test ruff format --check .` |
| Type-check | `make typecheck` | `docker run --rm office-convert:test mypy office_convert` |
| Generate corpus | `make corpus` | `docker run --rm -v .../corpus:... office-convert:test python -m tests.corpus._generate` |
| All gates | `make qa` | lint + format-check + typecheck + test |

In-process integration tests use a fake worker stand-in
(`tests/conftest.py::fake_worker_script`), so the entire suite passes
without the real Aspose SDK present.

### Host-side Python development (alternative, not preferred)

If you prefer working with Python directly on the host (e.g., editor
integration with the venv):

```bash
uv sync                                    # creates .venv with [dev] extras
source .venv/bin/activate
python -m tests.corpus._generate           # generate corpus fixtures
pytest                                     # run tests
ruff check . && ruff format --check . && mypy office_convert
```

Host needs Python 3.11, `uv`, and `qpdf` installed. Docker-first
workflow above avoids these prerequisites.

### End-to-end tests (Docker + real Aspose)

A separate test suite under `tests/e2e/` uses
[Testcontainers](https://testcontainers.com/) to bring up the real container
and exercise `/v1/convert` over real HTTP. These catch what in-process tests
cannot: Dockerfile bugs, real Aspose linkage, real qpdf concat at real
sizes, real `prlimit` enforcement.

**Prerequisites**:

1. Build the image with a real Aspose SDK in the build context
   (see Build section above).
2. Obtain an Aspose.Total **C++** scope Temporary License.
3. Install the e2e extras:
   ```bash
   uv pip install -e .[dev,e2e]
   ```

**Run via Makefile (recommended — runs inside container, mounts Docker socket)**:

```bash
make test-e2e
```

**Or manually** (requires `pytest` + `testcontainers` installed on the host):

```bash
OFFICE_CONVERT_E2E_LICENSE=/path/to/license.lic \
OFFICE_CONVERT_E2E_IMAGE=office-convert:dev \
pytest tests/e2e -m e2e
```

The suite is **skipped by default** (when `OFFICE_CONVERT_E2E_LICENSE` is
unset), so CI without an Aspose license runs only the in-process tests.

**Dual-mode design**: rendering tests accept either HTTP 200 (real Aspose
linked) or 500 `render_failed` (scaffolded worker without Aspose SDK
calls uncommented). The Docker plumbing is verified even before Aspose
is fully wired in; once you uncomment the API calls and rebuild, the
same tests transition to verifying real conversion fidelity without
code changes.

---

## Security Posture

The image and recommended `docker run` flags support:

- **Non-root user** (`appuser:appgroup`, uid 1000) — image runs without root
- **Read-only root filesystem** — compatible with `--read-only --tmpfs /tmp --tmpfs /var/run`
- **Dropped Linux capabilities** — compatible with `--cap-drop=ALL`

License file is **never baked into the image**, never committed to source,
never logged. Document content is never logged (only metadata: size,
format, page count, `request_id`).

### Vulnerability scanning

Three layers, all wired up:

| Layer | What it covers | Where |
|---|---|---|
| ECR scan-on-push (BASIC) | OS packages in the pushed image | AWS console / `aws ecr describe-image-scan-findings` |
| `apt-get upgrade -y` in every Dockerfile apt stage | Base-image-inherited CVEs at build time | `Dockerfile`, `Dockerfile.ui`, `Dockerfile.test` |
| Trivy filesystem + config scan in CI | Python deps + Dockerfile / Helm / k8s misconfig | `.github/workflows/security.yml`, SARIF → GitHub code scanning |

Dependabot (`.github/dependabot.yml`) opens weekly Monday PRs for `pip`,
`docker` (base images), and `github-actions` ecosystems.

For the full security testing matrix, see
`aidlc-docs/construction/build-and-test/security-test-instructions.md`.

---

## Project Structure

```
office_convert/        Python package — orchestrator, HTTP server, chunk
├── py.typed             PEP 561 marker (this package ships type hints)
├── types.py errors.py config.py logging.py license.py
├── chunk_planner.py cache.py qpdf.py probe.py
├── aspose_worker.py orchestrator.py server.py
cmd/ + internal/       Go orchestrator (migration; backend-only, pre-cutover
                       — see "Go orchestrator" above). net/http+chi server,
                       planner, worker pool, qpdf, cache, obs, s3. Tests use
                       testify + go-cmp + rapid; the golden parity gate lives
                       in internal/server/testdata/golden/.
worker_cpp/            C++17 worker — main, error/license/render/probe
                       coordinators, per-format dispatch (DOCX/PPTX/
                       XLSX/PDF), CMakeLists.txt with Aspose linkage
tests/
├── unit/              ~80 unit tests
├── property/          4 PBT files (Hypothesis: 500 examples for chunk
                       planner, 100 elsewhere)
├── integration/       In-process integration via FastAPI TestClient
                       + fake worker
├── e2e/               Testcontainers + real Docker container
                       (gated by OFFICE_CONVERT_E2E_LICENSE)
└── corpus/            Synthetic document fixtures + generator script

Dockerfile             Multi-stage production build (debian:bookworm
                       builder → python:3.11-slim-bookworm runtime)
Dockerfile.test        Test runner image (Python + dev deps; no Aspose)
compose.yaml           Docker Compose definition for the prod service +
                       opt-in test profile. Canonical entrypoint.
go.Dockerfile          Go orchestrator image (C++ builder + Go builder +
                       Python-free runtime). Build via make build-go.
compose.go.yaml        Compose override swapping in the Go backend
                       (make up-go); inherits everything else from compose.yaml.
Makefile               Docker-first workflow orchestrator (delegates to
                       compose; run `make help`)
pyproject.toml         Python deps + tool config (PEP 621, PEP 561)
ruff.toml              Linter + formatter rules
README.md              You are here
.gitignore             Defensive secrets + build artifact exclusions
.dockerignore          Same, for the Docker build context
aidlc-docs/            Full AI-DLC documentation (requirements,
                       design, stories, plans, build/test instructions,
                       audit log)
```

---

## Documentation

This README covers the operator-facing surface. For deeper material:

| Question | Doc |
| -------- | --- |
| Why does this exist? What requirements? | `aidlc-docs/inception/requirements/requirements.md` |
| What does each component do? | `aidlc-docs/inception/application-design/application-design.md` |
| Who uses this and how? | `aidlc-docs/inception/user-stories/stories.md` |
| How does the algorithm work? | `aidlc-docs/construction/office-converter/functional-design/business-logic-model.md` |
| What are the concrete business rules? | `aidlc-docs/construction/office-converter/functional-design/business-rules.md` |
| What are the non-functional commitments? | `aidlc-docs/construction/office-converter/nfr-requirements/nfr-requirements.md` |
| What's the tech stack? Why these choices? | `aidlc-docs/construction/office-converter/nfr-requirements/tech-stack-decisions.md` |
| How do specific patterns work in code? | `aidlc-docs/construction/office-converter/nfr-design/nfr-design-patterns.md` |
| How do I build, test, and ship this? | `aidlc-docs/construction/build-and-test/*.md` |
| What were the actual decisions made? | `aidlc-docs/audit.md` (full ISO-timestamped log) |
| What's deferred to v2 (cloud)? | `aidlc-docs/inception/requirements/requirement-verification-questions.md` |

---

## License of this code

The Python orchestrator and the C++ worker scaffolding are provided as
example code. **Aspose.Total C++ itself requires a separate commercial
license from Aspose**, not included here.
