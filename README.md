# Document Uploader Office Convert Service (Aspose.Total)

[![go](https://img.shields.io/badge/go-1.26-00ADD8.svg?logo=go&logoColor=white)](https://go.dev/)
[![chi](https://img.shields.io/badge/router-go--chi%2Fv5-00ADD8.svg)](https://github.com/go-chi/chi)
[![nextjs](https://img.shields.io/badge/next.js-15-000000.svg?logo=next.js&logoColor=white)](https://nextjs.org/)
[![typescript](https://img.shields.io/badge/typescript-5.7-3178C6.svg?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![aspose.total](https://img.shields.io/badge/aspose.total-c%2B%2B%2026.4-ff4081.svg)](https://products.aspose.com/total/cpp/)
[![qpdf](https://img.shields.io/badge/qpdf-streaming%20merge-orange.svg)](https://qpdf.sourceforge.io/)
[![gotenberg](https://img.shields.io/badge/gotenberg-8-2496ED.svg)](https://gotenberg.dev/)
[![docker](https://img.shields.io/badge/docker-required-2496ED.svg?logo=docker&logoColor=white)](https://docs.docker.com/)
[![status](https://img.shields.io/badge/status-v1%20local%20PoC-yellow.svg)](#)
[![AI-DLC](https://img.shields.io/badge/AI--DLC-powered-9C27B0.svg)](https://github.com/aws-samples/aws-aidlc-rule-details)

**v1.0 — local PoC** · Chunked Office document → PDF conversion service.

Converts DOCX, PPTX, XLSX, PDF, legacy DOC/XLS/PPT, ODT/ODS/ODP/ODG,
RTF, CSV, **raster + vector images** (PNG, JPG, TIFF, GIF, BMP, WEBP,
SVG), **email** (EML), and **HTML** to PDF over a local HTTP API. Office docs
render via **Aspose.Total C++** with RAM-isolated subprocess workers
and **qpdf** streaming merge; ODG + images route through the bundled
**LibreOffice** fallback (soffice `--convert-to pdf` picks the right
importer per input extension); EML goes through a two-stage
**Aspose.Email → MHTML → Aspose.Words → PDF** pipeline so the two
products' CodePorting frameworks stay process-isolated; HTML renders
through **two interchangeable engines** (Gotenberg/Chromium for JS
fidelity, Aspose.Words for static markup) on separate endpoints for
head-to-head comparison.

```
┌────────────────────── Docker container (4GB RAM + 2GB swap) ──────────────────────┐
│                                                                                    │
│   net/http + go-chi/chi (Go orchestrator)                                          │
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

The **Go orchestrator** (`cmd/` + `internal/`) is the production backend. The
**Next.js operator dashboard** (`ui/`) is a separate container. HTML conversions
additionally route to a **Gotenberg** sidecar (headless Chromium) for the
JS-capable engine. For detailed Mermaid diagrams (request flow, format detection,
pool mode), see `aidlc-docs/construction/office-converter/architecture-diagram.md`.

---

## TL;DR — Quickstart

**Host requirements:** Docker (with `docker compose` v2) + `curl`. Optionally
`make` for the convenience targets, and Node 22 for host-side UI development.
**Nothing else** runs on the host — the backend, workers, and UI all build and
run inside containers.

### Path A — Docker Compose (canonical)

```bash
# 1. Place your Aspose license in the project root (vendor/aspose/ must
#    already be populated — see "Prerequisites").
ls Aspose.TotalforC++.lic

# 2. Build and start the full stack (Go API + Next.js UI + Gotenberg + LocalStack)
docker compose up -d --build

# 3. Hit the endpoints via URL
curl http://localhost:8080/health
curl -X POST http://localhost:8080/v1/convert \
     -F "file=@testdata/corpus/simple.pdf" -o output.pdf

# 4. Open the operator dashboard
open http://localhost:8501

# 5. Stop when done
docker compose down
```

### Path B — Makefile (same thing, with helpers)

```bash
make test-go             # run the Go suite (unit + property + golden parity gate)
make build               # build the API image (needs vendor/aspose/ + license)
make up                  # docker compose up -d --build; waits for /health
make demo                # URL-based smoke: /health + bad-format + real convert + docs URLs
make convert FILE=testdata/corpus/simple.pdf
make down                # docker compose down
```

`make up` delegates to `docker compose up -d --build`, then waits for `/health`
to respond and prints all the URLs. `make help` shows the full target list.

### UI development (hot reload, host-side)

```bash
make ui-install          # npm ci in ui/
make ui-dev              # Next.js dev server on http://localhost:3000 (proxies /api/* to API_URL)
make ui-lint             # ESLint + tsc type check
make ui-build            # production standalone build
```

Need the SDK or license?

- **Aspose product libraries**: <https://releases.aspose.com/total/cpp/>
  (populate `vendor/aspose/` — see "Prerequisites").
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

Full layout, scripts, and the ALB Ingress setup (with corp-CIDR-allowlisted
internet-facing URL) are documented in [`deploy/README.md`](deploy/README.md)
and [`aidlc-docs/operations/dev-deployment-topology.md`](aidlc-docs/operations/dev-deployment-topology.md).

---

## Why chunked

Aspose accumulates rendered content in memory; a single large document
can exceed available RAM. This service breaks inputs into bounded chunks
(adaptive, RAM-aware), renders each chunk in a subprocess capped via
`prlimit RLIMIT_AS`, and streams the resulting chunk PDFs through
`qpdf --empty --pages ... -- -` directly into the HTTP response. The full
output PDF is never buffered.

If a chunk OOMs, the orchestrator **subdivides** the page range
(10 → 5 → 2 → 1 page) and retries. Single-page failures dead-letter
with a structured diagnostic rather than silently producing partial
output.

---

## Tech stack

| Layer | Technology |
| ----- | ---------- |
| **Orchestrator / HTTP API** | Go (stdlib `net/http` + `go-chi/chi/v5`, `log/slog`, AWS SDK v2) |
| **Document workers** | C++17, Aspose.Total for C++ (5 per-product binaries) |
| **PDF merge** | qpdf (streaming, never buffers full output) |
| **HTML / Chromium engine** | Gotenberg 8 (sidecar container) |
| **Operator dashboard** | Next.js 15 (App Router, TypeScript, Tailwind, SWR, recharts) |
| **Local AWS emulation** | LocalStack (S3) |
| **Tests** | Go `testify` + `go-cmp` + `pgregory.net/rapid` (property-based); UI `vitest` + Testing Library |

There is **no Python** in the repo. The orchestrator was originally a Python
(FastAPI) service; it was reimplemented in Go and the Python implementation +
Streamlit UI + pytest suite were retired. The Go↔original wire contract is
frozen as committed golden fixtures (see "Golden-fixture parity gate").

---

## Go orchestrator

The orchestrator (`cmd/` + `internal/`) is a Go service: stdlib `net/http`
routing via `go-chi/chi/v5`, `log/slog` logging, AWS SDK v2. It shells out to
the five per-product C++ Aspose worker binaries (`worker_cpp/`) over a
JSON-stdio protocol, merges chunk PDFs with qpdf, and streams the result.

**Run / test it:**

```bash
make up                # build + run the full stack (API + UI + Gotenberg + LocalStack)
make test-go           # full Go suite (unit + property + golden parity gate)
make golden-verify     # replay the Go server against the committed golden fixtures
make down              # tear down
```

**Golden-fixture parity gate.** `make golden-verify` (and CI's `go-test` job)
runs `TestGoldenParity`, which replays a set of frozen HTTP responses against the
Go server and diffs them — the safety net that pins the wire contract. Fixtures
are committed under `internal/server/testdata/golden/` (14 cases). They were
originally captured from the retired Python oracle and are now maintained as
static contract snapshots. Design docs: `aidlc-docs/construction/go-orchestrator/`.

---

## Prerequisites

1. **Aspose.Total C++ Temporary License** (`.lic` file). Request at
   <https://purchase.aspose.com/temporary-license>. Specify "Aspose.Total
   **for C++**" — the Python-via-.NET license is NOT compatible.
2. **Aspose product libraries** under `vendor/aspose/`. The build consumes five
   per-product trees (`Words`, `Cells`, `Slides`, `PDF`, `Email`) — see
   `make verify-vendor` for the expected layout and the Linux x86_64 `.so`
   checks. (Words 26.3 + Cells/Slides/PDF/Email 26.4; the per-product split
   keeps each product's CodePorting framework ABI-isolated.)
3. **Docker** (or any OCI-compatible runtime).
4. **x86_64 host**. Aspose.Total C++ is x86_64-Linux only. Apple Silicon
   hosts work via Docker Desktop's amd64 emulation (slower; dev only).

---

## Docker-First Workflow

Two paths to the same outcome:

| Path | When to use |
| ---- | ----------- |
| **Docker Compose** (`docker compose up -d`) | Canonical. Standard tooling everyone recognizes. CI-friendly. |
| **Makefile** (`make up`, `make demo`, …) | Convenience: organized help (`make help`), waits for `/health`, URL-based smoke (`make demo`), variable overrides on the command line. Delegates to compose for `up`/`down`/`logs`/`restart`. |

Both read the same `compose.yaml`. Pick whichever feels natural.

### Compose services

`compose.yaml` brings up four services:

| Service | Image | Purpose |
| ------- | ----- | ------- |
| `office-convert` | `office-convert:go` (`Dockerfile`) | Go orchestrator + C++ workers + Aspose `.so`. **Requires `vendor/aspose/` + license.** |
| `ui` | `office-convert-ui:dev` (`ui/Dockerfile`) | Next.js operator dashboard on `:8501`. |
| `gotenberg` | `gotenberg/gotenberg:8` | Headless-Chromium HTML→PDF engine (internal only). |
| `localstack` | `localstack/localstack:3.8` | Local S3 emulation for the S3 source/sink integration. |

### Common workflows

**Run the Go tests** (no Aspose dependency):

```bash
make test-go          # Go unit + property + golden parity gate (runs in a golang container)
make golden-verify    # just the parity gate
```

**Full production build + URL-based smoke test** (needs Aspose libs + license):

```bash
# Pre-flight: populate vendor/aspose/ and place the license in the project root
make verify-vendor              # checks all 5 product trees are present + Linux x86_64
ls Aspose.TotalforC++.lic       # license should already exist

make build            # builds C++ workers + Go binary into the runtime image
make up               # starts the full stack; waits for /health to respond

# Now test via URLs (all run from your shell, hitting localhost)
make health           # GET /health
make demo             # health + bad-format + convert + docs URLs
make convert FILE=testdata/corpus/simple.pdf   # POST a document
```

**Cleanup**:

```bash
make clean            # stops services; removes API + UI images
make clean-all        # also removes ui/.next + ui/node_modules
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
  - HTML is **not** accepted here — use the engine-specific routes below.
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
| 500 | `subdivision_floor_exceeded` | Single page OOMs even at the RAM ceiling |
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

### `POST /v1/convert/html/{gotenberg|aspose}` — HTML dual-engine

HTML is deliberately **not** accepted by the generic `/v1/convert` route.
Instead, two engine-specific endpoints exist so the engines can be benchmarked
head-to-head (latency AND fidelity):

| Endpoint | Engine | JavaScript |
| --- | --- | --- |
| `POST /v1/convert/html/gotenberg` | Gotenberg 8 (headless Chromium, separate container) | **Executed** — SPAs, charts, dynamic content render |
| `POST /v1/convert/html/aspose` | Aspose.Words via `worker-docx` (single-shot, no chunking) | **None** — static markup + CSS subset only |

```bash
curl -X POST http://localhost:8080/v1/convert/html/gotenberg \
    -F "file=@page.html" \
    -F "waitDelay=2s" \
    -F "waitForExpression=window.status === 'ready'" \
    -o out-gotenberg.pdf

curl -X POST http://localhost:8080/v1/convert/html/aspose \
    -F "file=@page.html" -o out-aspose.pdf
```

- `waitDelay` (≤ 30s) and `waitForExpression` (≤ 1024 chars) give JavaScript
  time to finish before Chromium snapshots the page — **Gotenberg endpoint
  only**; the Aspose endpoint rejects them with 422 (it has no JS to wait for).
- Input cap: `OFFICE_CONVERT_HTML_MAX_BYTES` (default 10 MiB), separate from
  the office-format ceiling.
- Both engines render to identical page geometry (US Letter, 0.5in margins)
  for a fair comparison.
- **SSRF guard**: external resources referenced by the HTML are fetched only
  from public hosts. Loopback, RFC1918, link-local/metadata (169.254.0.0/16),
  IPv6-private, and single-label in-cluster hostnames are denied on BOTH
  engines (Gotenberg `--chromium-deny-list` / Aspose resource-loading
  callback — canonical policy in `internal/netpolicy`).
- Gotenberg down or unconfigured → `503 engine_unavailable`. The Gotenberg
  container ships in `compose.yaml`; the API reaches it via
  `OFFICE_CONVERT_GOTENBERG_URL` (default `http://gotenberg:3000`).
- Telemetry: HTML `ConversionRecord`s carry `engine`, and
  `GET /v1/conversions/stats` adds a `per_engine_html` block
  (count / avg_ms / p95_ms per engine) once HTML conversions exist. The
  Next.js UI's "HTML → PDF · engine comparison" panel fires both endpoints
  in parallel and shows the results side by side.

### `GET /v1/downloads/presign`

Mints a short-TTL presigned GET URL for an output object. The service owns the
S3 credentials (IRSA on EKS); clients only ever receive a time-boxed URL. The
output-bucket allowlist is enforced before signing.

```bash
curl "http://localhost:8080/v1/downloads/presign?bucket=office-convert-out&key=pdf/abc123.pdf"
# → {"download_url":"https://…X-Amz-Signature=…","bucket":"…","key":"…",
#    "expires_in_seconds":900,"expires_at":"2026-05-27T…Z"}
```

A fresh URL is minted per call (presigned URLs expire), so the UI's Conversion
History calls this on demand for its "☁️ Download from S3" link.

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

## Operator dashboard (Next.js)

The dashboard at **http://localhost:8501** (the `ui` service) is a Next.js 15
App Router app. It is a thin client over the API — there is no client-side
state store; everything is read from the API so cross-service conversions
(e.g. curl, the classification fanout) appear in history by construction.

- **Single-origin proxy**: the browser only ever talks to the UI's own origin.
  `next.config.ts` rewrites `/api/:path*` → the Go API server-side, so there's
  no CORS surface and no API URL baked into client JS (except the
  `/v1/dashboard` iframe, the one deliberate browser-direct URL).
- **Panels**: health/KPI tiles, single-file convert, HTML engine comparison
  (fires both engines in parallel), conversion history (API-truth, cursor
  paginated), per-format / per-engine performance charts, and the embedded
  live `/v1/dashboard`.
- **Security headers**: a CSP (plus `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Permissions-Policy`) is set in `next.config.ts`.

Develop it with hot reload via `make ui-dev` (host Node 22), or build the
container with `docker build -t office-convert-ui:dev ui/`.

---

## Configuration

All runtime config via `OFFICE_CONVERT_*` environment variables:

| Variable | Default | Notes |
| -------- | ------- | ----- |
| `OFFICE_CONVERT_MAX_JOBS` | `1` | Concurrent HTTP requests served. Excess → 503 busy. |
| `OFFICE_CONVERT_PARALLEL` | `4` | Concurrent chunk renders inside one request (DOCX/PPTX/PDF use fork-after-load, so peak RAM ≈ 1× loaded doc; XLSX legacy pool independently loads per worker — see `XLSX_MAX_POOL_SIZE` row). |
| `OFFICE_CONVERT_XLSX_MAX_POOL_SIZE` | `4` | Per-format cap on workers for XLSX. Aspose.Cells is fork-unsafe, so each XLSX worker independently loads the workbook — large files (>50 MB) can OOM with `parallel=4` on a 4 GiB pod. Cap to `2` on swap-less environments (EKS chart default). |
| `OFFICE_CONVERT_GOTENBERG_URL` | (unset) | Chromium HTML engine endpoint. Unset → `/v1/convert/html/gotenberg` returns 503 `engine_unavailable`. Compose sets `http://gotenberg:3000`. |
| `OFFICE_CONVERT_HTML_MAX_BYTES` | `10485760` | 10 MiB cap on HTML uploads, separate from the office-format ceiling. |
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
| `OFFICE_CONVERT_POOL_MIN_CHUNKS` | `1` | Pool mode activates when the chunk plan has at least this many chunks. Compose default `1` forces pool mode on every conversion (so the heartbeat dashboard populates even for small files). |
| `OFFICE_CONVERT_HEARTBEAT_MS` | `2000` | Per-process heartbeat cadence in ms while a load or render is in flight. `0` disables. Heartbeats are emitted at DEBUG. |
| `OFFICE_CONVERT_FORK_AFTER_LOAD` | `1` | Fork-after-load: one leader loads the document then `fork()`s N children that share the loaded `Document` via copy-on-write — eliminates the N× parallel-parse contention on large DOCX. XLSX is auto-excluded (Aspose.Cells is fork-unsafe). Set `0` for a global kill switch. |

### Live heartbeat dashboard

The UI shows a per-worker heartbeat table during pool-mode conversions:

- Phase (load / render), elapsed-in-phase, **RSS in MB, Swap in MB** (highlighted
  when non-zero — indicates the worker is paging out under `memswap_limit`),
  CPU jiffies, time since last heartbeat.
- Each conversion generates a UUID sent as `X-Request-ID`; the panel polls
  `GET /v1/jobs/{request_id}/heartbeats` and correlates 1:1 with the in-flight
  upload.

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

The C++ worker is optimized for the subprocess pattern:

- **Lazy product activation** — each per-product binary links exactly one Aspose
  product, so `SetLicense()` + static init touch only that product. Saves
  ~150–600 ms of overhead per worker invocation.
- **Compiler/linker optimizations** — Release builds use
  `-O2 -flto -fvisibility=hidden -fvisibility-inlines-hidden
  -fdata-sections -ffunction-sections` + `-Wl,--gc-sections -Wl,-s`.
  10–30% smaller binary; ~30–100 ms faster dynamic-loader resolution
  per spawn.
- **Fork-after-load** (DOCX/PPTX/PDF) — load the document once, fork
  copy-on-write child renderers, keeping peak RAM at ~1× the loaded document.

Combined: large-DOCX loads that previously timed out under N× parallel parsing
now complete in seconds. Aspose render time itself is unchanged — that's still
the dominant cost.

---

## Troubleshooting

| Symptom | Likely cause | Action |
| ------- | ------------ | ------ |
| `docker build` fails copying `vendor/aspose/...` | Aspose product trees not populated | Run `make verify-vendor`; extract the 5 product libraries into `vendor/aspose/{Words,Cells,Slides,PDF,Email}/` |
| `docker build` fails with linker errors `undefined reference to Aspose::...` | Product `.so` names don't match the CMake link targets | Inspect the extracted trees; verify each product's `lib/` against `worker_cpp/CMakeLists.txt` |
| Container starts but `/health` 503 with `qpdf_missing` | qpdf not in runtime image | Already in Dockerfile; check `apt-get` step succeeded |
| `/health` 503 with `worker_binary_missing` | C++ workers didn't build | Re-check builder-stage logs; verify vendor trees extracted |
| `/health` 503 with `license_path_missing` | Bind-mount missing | Add `-v ./license.lic:/aspose/license.lic:ro` |
| `/health` 503 with `license_expired` | License past expiry date | Renew the temp license |
| 503 `engine_unavailable` on `/v1/convert/html/gotenberg` | Gotenberg sidecar down or `OFFICE_CONVERT_GOTENBERG_URL` unset | Ensure the `gotenberg` compose service is up |
| 500 `subdivision_floor_exceeded` | A single page exceeds the RAM ceiling (e.g. PPTX with huge embedded media) | Reduce input complexity; documented v1 limitation |
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
- **NG-8**: Output is generic PDF only. No PDF/A, no linearization,
  no digital signing.
- **NG-9**: x86_64 Linux only (Aspose.Total C++ constraint).

---

## Development

The recommended path is **Docker-first via `make`** (see the workflow section
above).

### Go orchestrator

```bash
make test-go          # unit + property + golden parity gate (runs in a golang:1.26 container)
make golden-verify    # just the parity gate
```

To run the Go suite directly on the host (needs Go 1.26 + `util-linux` for the
worker tests' `prlimit`):

```bash
go build ./... && go vet ./...
go test ./...
```

### Next.js UI

```bash
make ui-install       # npm ci
make ui-dev           # dev server on :3000 (set API_URL to a running API)
make ui-lint          # ESLint + tsc --noEmit
make ui-build         # production standalone build
npm --prefix ui test  # vitest component tests
```

In-process Go integration tests use a fake worker stand-in, so the entire suite
passes without the real Aspose SDK present.

---

## Security Posture

The image and recommended `docker run` flags support:

- **Non-root user** — image runs without root
- **Read-only root filesystem** — compatible with `--read-only --tmpfs /tmp --tmpfs /var/run`
- **Dropped Linux capabilities** — compatible with `--cap-drop=ALL`

License file is **never baked into the image**, never committed to source,
never logged. Document content is never logged (only metadata: size,
format, page count, `request_id`).

### Vulnerability scanning

| Layer | What it covers | Where |
|---|---|---|
| ECR scan-on-push (BASIC) | OS packages in the pushed image | AWS console / `aws ecr describe-image-scan-findings` |
| `apt-get upgrade -y` in the Dockerfile apt stages | Base-image-inherited CVEs at build time | `Dockerfile` |
| Trivy filesystem + config scan in CI | Dependencies + Dockerfile / Helm / k8s misconfig | `.github/workflows/security.yml`, SARIF → GitHub code scanning |

Dependabot (`.github/dependabot.yml`) opens weekly Monday PRs for `npm` (the
`ui/` dependencies), `docker` (base images), and `github-actions` ecosystems.

For the full security testing matrix, see
`aidlc-docs/construction/build-and-test/security-test-instructions.md`.

---

## Project Structure

```
cmd/ + internal/       Go orchestrator. net/http + chi server, planner,
                       worker pool, qpdf, cache, observability, S3. Tests use
                       testify + go-cmp + rapid; the golden parity gate lives
                       in internal/server/testdata/golden/ (14 cases).
worker_cpp/            C++17 worker — main, error/license/render/probe
                       coordinators, per-format dispatch (DOCX/PPTX/XLSX/
                       PDF/Email), CMakeLists.txt with per-product Aspose linkage.
ui/                    Next.js 15 operator dashboard (App Router, TypeScript,
                       Tailwind, SWR, recharts). app/ components/ lib/ + vitest
                       component tests. Built via ui/Dockerfile.
testdata/corpus/       Static document fixtures for manual / acceptance testing.
smoke_test/            Aspose.Words license + .so smoke test (pre-integration).
vendor/                Go module vendor tree + Aspose product libraries
                       (vendor/aspose/{Words,Cells,Slides,PDF,Email}).

Dockerfile             Multi-stage production build: C++ worker builder →
                       Go builder → Python-free runtime (the only backend image).
compose.yaml           Docker Compose definition for the full stack (API + UI +
                       Gotenberg + LocalStack). Canonical entrypoint.
Makefile               Docker-first workflow orchestrator (run `make help`).
go.mod / go.sum        Go module definition.
README.md              You are here.
.gitignore             Defensive secrets + build artifact exclusions.
.dockerignore          Same, for the Docker build context.
aidlc-docs/            Full AI-DLC documentation (requirements, design, stories,
                       plans, build/test instructions, audit log).
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
| How is the Go orchestrator structured? | `aidlc-docs/construction/go-orchestrator/` |
| How do I build, test, and ship this? | `aidlc-docs/construction/build-and-test/*.md` |
| What were the actual decisions made? | `aidlc-docs/audit.md` (full ISO-timestamped log) |
| What's deferred to v2 (cloud)? | `aidlc-docs/inception/requirements/requirement-verification-questions.md` |

---

## License of this code

The Go orchestrator, the Next.js UI, and the C++ worker scaffolding are provided
as example code. **Aspose.Total C++ itself requires a separate commercial
license from Aspose**, not included here.
