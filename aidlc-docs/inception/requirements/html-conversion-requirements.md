# Requirements — HTML → PDF Conversion (Dual-Engine, Go-Only)

**Date**: 2026-06-12 · **Stage**: Requirements Analysis (standard depth)
**Feature requirements only** — the original v1 service requirements remain in
[`requirements.md`](requirements.md) / [`local-v1-scope.md`](local-v1-scope.md).
**Question record**: [`html-conversion-requirement-questions.md`](html-conversion-requirement-questions.md)
(all 7 answered 2026-06-12).

## Intent Analysis

- **User request**: "I want to impliment HTML Conversion using both Gotenberg and Aspose …
  I want both of them but on different end points so that we can analyze performance.
  Make sure we adjust this UI as well. … Using AIDLC start with Go only."
- **Request type**: New Feature (brownfield).
- **Scope estimate**: Multiple Components — Go orchestrator (`cmd/` + `internal/`), C++ worker
  format guard (`worker_cpp/formats/docx.cpp`), local compose, Streamlit UI. Python orchestrator
  explicitly **untouched**.
- **Complexity estimate**: Moderate.
- **Business goal**: a head-to-head **performance AND fidelity comparison** of two HTML→PDF
  engines, with JavaScript handling as the known fault line (Gotenberg/Chromium executes JS;
  Aspose.Words does not).

## Functional Requirements

### FR-1 Two engine-specific endpoints (Go orchestrator only)
- `POST /v1/convert/html/gotenberg` — renders via the Gotenberg (Chromium) service.
- `POST /v1/convert/html/aspose` — renders via the existing `worker-docx` binary (Aspose.Words
  loads HTML via `LoadFormat::Auto`), single-shot, no chunk planner.
- Both accept `multipart/form-data` with a `file` field (HTML) and return `200 application/pdf`
  (streamed) with the existing `X-Request-ID` header. Existing error envelope (`Diagnostic`
  JSON) applies on failure.
- Both routes bypass the probe→plan→render→merge pipeline (bypass pattern, modeled on the
  LibreOffice/EML paths in `internal/server/convert.go`).

### FR-2 HTML input acceptance & detection
- Accept `.html` / `.htm` uploads. Detection: content sniff (`<!doctype html` / `<html`, after
  BOM/whitespace strip, case-insensitive) with filename-extension fallback.
- `"html"` joins `DispatchFormat` (Go `internal/types`) — NOT `FormatName`/`AsposeFormats`
  (the closed Aspose-chunking set).
- Non-HTML content uploaded to these endpoints → `input_unprocessable` (422).

### FR-3 JavaScript wait controls — Gotenberg endpoint (Q1:A)
- Optional multipart form fields passed through to Gotenberg:
  - `waitDelay` — fixed pause (duration, e.g. `2s`) before snapshot.
  - `waitForExpression` — JS expression polled until truthy (e.g. `window.status === 'ready'`).
- Both unset → Gotenberg default behavior. Invalid values → 400 with `Diagnostic`.
- The Aspose endpoint ignores/rejects these fields (no JS engine) — documented in the API help.

### FR-4 Engine tagging & comparison telemetry
- `ConversionRecord` gains an `engine` field (`"gotenberg"` | `"aspose"`; empty/`"aspose-chunked"`
  not required for existing office paths — existing records unchanged).
- `/v1/conversions` entries expose `engine`; `/v1/conversions/stats` adds a per-engine breakdown
  for HTML (count / avg_ms / p95_ms per engine) alongside the per-format stats.

### FR-5 Streamlit UI — engine comparison panel (Q7:A)
- New "HTML → PDF · engine comparison" panel: upload `.html`, **Convert with both** fires both
  endpoints in parallel; side-by-side result cards (engine · latency ms · output size · status ·
  download/preview) + a latency bar chart.
- Single-engine convert buttons also available; JS wait-control inputs exposed for the Gotenberg
  run.
- Conversion history gains an `engine` column; per-format perf panel splits HTML by engine.

### FR-6 Failure taxonomy (Q5:A)
- New wire-stable failure class **`engine_unavailable`** → HTTP 503, returned when the Gotenberg
  service is down/unreachable/timing out at connect.
- All other failures map to existing classes: `render_failed` (500), `input_unprocessable` (422),
  `input_too_large` (400), `unsupported_format` (400), `busy`/`rate_limited` unchanged.

### FR-7 Gotenberg service (local) (Q6:A)
- `gotenberg/gotenberg:8` added as a compose service (shared default network); Go API reaches it
  at `http://gotenberg:3000` via new setting `OFFICE_CONVERT_GOTENBERG_URL`.
- Conversion call: `POST {url}/forms/chromium/convert/html`, file uploaded as `index.html`
  (Chromium contract), wait fields forwarded.
- Helm/EKS manifests are **out of scope this iteration** (deferred follow-up).

## Non-Functional Requirements

### NFR-1 Security — SSRF / external resource policy (Q2:A, Q3:B) **[SECURITY]**
- **Both engines enforce the same network posture**: deny private/internal ranges
  (RFC1918, link-local/metadata `169.254.0.0/16`, loopback, and in-cluster service names),
  allow public internet (CDN assets, fonts).
- Gotenberg: enforced via `--chromium-deny-list` (regex over URLs) on the service container.
- Aspose: enforced via Aspose.Words `IResourceLoadingCallback` in the worker — resolve/inspect
  the resource URL, skip loads matching the deny policy. Policy must be defined once and kept
  textually identical in both configurations (documented side-by-side).
- Gotenberg container runs non-root (upstream default uid 1001), no AWS credentials, no S3
  access — the Go API mediates all I/O.

### NFR-2 Input ceiling (Q4:B)
- HTML-specific upload cap, default **10 MB** (`OFFICE_CONVERT_HTML_MAX_BYTES`), enforced before
  dispatch; exceeding → `input_too_large` (400). Independent of the global office-format ceiling.

### NFR-3 Timeouts
- Gotenberg HTTP call: bounded client timeout (default 120 s, configurable), must exceed any
  caller-supplied `waitDelay`; connect-phase failure → `engine_unavailable`.
- Aspose path: reuse the existing worker one-shot timeout (`chunk_timeout` equivalent in Go
  settings).

### NFR-4 Observability **[SECURITY-03 conformant]**
- Both endpoints emit the existing structured-log events (request received/completed, duration,
  engine, failure class) with request-id correlation; no document content or URLs from inside
  the HTML are logged beyond hostnames of denied fetches (for SSRF audit).
- Heartbeats/progress stores: not required (single-shot conversions); records land in the
  recent-conversions ring buffer as today.

### NFR-5 Parity-gate scoping (Go-only consequence)
- The two new routes exist ONLY in the Go orchestrator. The Python→Go golden-parity gate
  (14/14) remains green and **excludes** the new routes (no Python capture). A short note in the
  parity docs records this as the first deliberate divergence, resolved by Phase 9 retirement.
- Until Phase 8 cutover, the deployed Python prod backend does not serve these endpoints —
  benchmarking happens on the local Go stack (`compose.go.yaml`).

### NFR-6 Testing **[PBT extension]**
- Go unit tests: HTML detection (doctype/tag/BOM/whitespace/extension-fallback), deny-list
  policy matcher, failure-class mapping, options parsing (wait fields).
- **PBT (rapid)**: HTML sniffer properties (e.g. any byte-prefix permutation of
  whitespace/BOM + `<!doctype html` detects as html; random non-HTML bytes never do) — joins the
  existing `internal/probe` PBT suite. Deny-list matcher: generated URLs/IPs in private ranges
  are always denied (invariant).
- Integration: httptest with a fake Gotenberg server (200/4xx/503/timeout cases) + fake worker
  for the Aspose path; `tests`-equivalent corpus gains `sample.html` (static) and
  `sample-js.html` (JS-rendered content, for manual/E2E fidelity check).
- E2E (license-gated, manual): convert both samples through both engines on the live local
  stack; verify the JS sample renders content under Gotenberg and lacks it under Aspose.

## Out of Scope (this iteration)
- Helm/EKS deployment of Gotenberg (Q6:A — follow-up).
- Python orchestrator changes of any kind.
- Chunked/paginated Aspose HTML rendering (single-shot only).
- URL-to-PDF (only uploaded HTML files; remote-page conversion is a different SSRF posture).
- Automated visual-diff scoring of fidelity (the comparison surfaces latency/size/status;
  fidelity judgment is human via preview/download).

## Acceptance Criteria (summary)
1. `curl -F file=@page.html :8080/v1/convert/html/gotenberg` and `…/aspose` both return valid
   PDFs on the local Go stack.
2. A JS-rendered HTML (with `waitForExpression`) shows its dynamic content in the Gotenberg PDF
   and not in the Aspose PDF.
3. HTML referencing `http://169.254.169.254/` or `http://localstack:4566/` renders with those
   fetches denied on BOTH engines.
4. Gotenberg container stopped → Gotenberg endpoint returns 503 `engine_unavailable`; Aspose
   endpoint unaffected.
5. `/v1/conversions/stats` shows per-engine HTML stats; UI panel shows side-by-side results.
6. `go vet`/`go test ./...` green including new PBT; golden-parity gate still 14/14.

## Extension Compliance (Requirements stage)

| Rule | Status | Note |
|---|---|---|
| SECURITY-01 (encryption at rest/in transit) | N/A | No new data store; S3 not in the HTML path this iteration |
| SECURITY-02 (LB/gateway access logs) | N/A | No new network intermediary (local compose only) |
| SECURITY-03 (structured app logging) | Captured | NFR-4 requires existing structured logging on both endpoints |
| SECURITY-04 (HTTP security headers) | N/A | New endpoints serve PDF/JSON, not HTML pages |
| SSRF posture (baseline spirit) | Captured | NFR-1 — deny-internal policy on both engines |
| PBT-01 (property identification) | Captured | NFR-6 names sniffer + deny-list properties; full analysis lands in Functional Design |
