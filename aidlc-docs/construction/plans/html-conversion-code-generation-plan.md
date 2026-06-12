# Code Generation Plan — unit `html-conversion`

**Date**: 2026-06-12 · **THIS PLAN IS THE SINGLE SOURCE OF TRUTH for Code Generation.**
**Inputs**: approved functional design (`construction/html-conversion/functional-design/`),
execution plan, requirements (FR-1…7 / NFR-1…6, decisions Q1–Q7 + D1–D4).

## Unit Context

- **Unit**: `html-conversion` — dual-engine HTML→PDF endpoints + UI comparison panel.
- **Backend**: Go orchestrator ONLY (`cmd/` + `internal/`). Python `office_convert/` untouched.
- **Dependencies**: existing `worker-docx` binary (Aspose.Words), new `gotenberg/gotenberg:8`
  compose service, existing bypass-pattern plumbing (`internal/server/convert.go` finishStream),
  existing obs stores.
- **Interfaces produced**: `POST /v1/convert/html/gotenberg`, `POST /v1/convert/html/aspose`,
  `engine` field in conversions feed, `per_engine_html` in stats.
- **Requirement traceability** noted per step as [FR-x/BR-x/NFR-x].
- **Branch/PR**: all work on feature branch `feat/html-conversion` (main is PR-only).

## Generation Steps

### Module 1 — C++ worker (image rebuild required)

- [x] **Step 1**: `worker_cpp/formats/docx.cpp` — (a) relax format guard to accept `"html"`
      alongside `"docx"` (render mode; probe stays docx-only); (b) add
      `IResourceLoadingCallback` implementing the BR-4 deny table (loopback, RFC1918,
      169.254/16, IPv6 private, single-label hosts, non-http(s) schemes ⇒ skip + log hostname
      to stderr); install it via `LoadOptions` for html loads; (c) after html load, force
      `PageSetup` Letter 8.5×11in + 0.5in margins on all sections [FR-1, BR-4, BR-7, NFR-1].

### Module 2 — Go orchestrator

- [x] **Step 2**: `internal/types/types.go` — add `DispatchHTML DispatchFormat = "html"`
      constant (NOT in `FormatName`/`AsposeFormats`) [FR-2].
- [x] **Step 3**: `internal/oerrors/` — add `FailureClass "engine_unavailable"` → HTTP 503 +
      `EngineUnavailableError{Engine, URL, Cause}` following the existing error pattern
      [FR-6, BR-5].
- [x] **Step 4**: `internal/config/config.go` — add `GotenbergURL` (default
      `http://gotenberg:3000`), `GotenbergTimeoutSeconds` (120), `HTMLMaxBytes` (10485760)
      parsed from `OFFICE_CONVERT_*` envs [FR-7, NFR-2, NFR-3].
- [x] **Step 5**: `internal/probe/probe.go` — HTML sniff per BR-1 (1024-byte window, BOM +
      whitespace strip, case-insensitive `<!doctype html`/`<html`), positioned after image
      check / before EML; extension fallback `.html`/`.htm`; extend `AcceptedUploadFormats`;
      extend `extHintFormats` in `internal/server/server.go` [FR-2, BR-1].
- [x] **Step 6**: NEW package `internal/netpolicy/` — pure `Denied(rawURL) (bool, reason)`
      matcher implementing BR-4 + exported `ChromiumDenyListRegex` constant (the compose flag
      value, kept adjacent so both enforcement points stay in sync) [BR-4, NFR-1].
- [x] **Step 7**: NEW package `internal/gotenberg/` — HTTP client: `ConvertHTML(ctx, htmlPath,
      waitOpts) (pdfPath, error)`; multipart per business-logic-model Flow A (file as
      `index.html`, Letter geometry fields, wait fields); bounded timeout; response
      classification per BR-5 (connect-fail→EngineUnavailable, 4xx→InputUnprocessable,
      5xx→RenderFailed); `%PDF-` validation [FR-1, FR-3, BR-5, BR-6, BR-8].
- [x] **Step 8**: `internal/server/` — register the two routes; new `convert_html.go` handler:
      shared pre-processing (multipart, `HTMLMaxBytes` cap, BR-1 content validation,
      scratch dir), wait-field parsing/validation (≤30s, ≤1024 chars; reject on aspose
      endpoint per D4), engine dispatch (gotenberg client | one-shot `worker-docx --format
      html` via existing `internal/worker.RunWorker`), stream via existing
      `streamWriter`/`finishStream`; generic `/v1/convert` D1 rejection for detected HTML
      [FR-1, FR-3, BR-2, BR-3, D1, D4].
- [x] **Step 9**: `internal/obs/` — `ConversionRecord.Engine` field (additive JSON) +
      `per_engine_html` aggregation in the stats payload; handlers tag engine on record
      [FR-4, BR-9].

### Module 2 tests (PBT extension enforced)

- [x] **Step 10**: Unit + PBT tests — `internal/probe`: sniffer example tests + **rapid PBT**
      (BOM/whitespace/case prefix permutations always detect; random non-HTML bytes never);
      `internal/netpolicy`: deny-table example tests + **rapid PBT** (generated
      private-range IPs/single-label hosts always denied, generated public IPv4 allowed);
      wait-validation round-trip/bound tests; gotenberg response-mapping table test
      [NFR-6, PBT-01/02/03].
- [x] **Step 11**: Integration tests — `internal/server`: httptest fake-Gotenberg
      (200/4xx/5xx/conn-refused/timeout) + existing fake-worker harness for the aspose path;
      engine tagging + stats assertions; D1 generic-endpoint rejection; testdata
      `sample.html` + `sample-js.html` (also copied to `tests/corpus/` for manual E2E)
      [NFR-6, acceptance 1–5].

### Module 3 — Compose / deploy (local only, Q6:A)

- [x] **Step 12**: `compose.go.yaml` — add `gotenberg` service (`gotenberg/gotenberg:8`,
      command includes `--chromium-deny-list` from `internal/netpolicy` constant, non-root,
      no AWS creds, mem limit 768m) + `OFFICE_CONVERT_GOTENBERG_URL` env on the Go api
      service + `depends_on`. Python `compose.yaml` untouched [FR-7, NFR-1].

### Module 4 — UI

- [x] **Step 13**: `office_convert_ui/app.py` — "HTML → PDF · Engine Comparison" panel per
      `frontend-components.md`: uploader, wait-control inputs, **Convert with both**
      (parallel via ThreadPoolExecutor) + single-engine buttons, side-by-side result cards,
      latency bar chart, `engine` column in history, per-engine perf split from
      `per_engine_html`, JS-fidelity hint text [FR-5, Q7:A].

### Module 5 — Docs & traceability

- [x] **Step 14**: Parity-divergence note — add a short section to
      `aidlc-docs/construction/go-orchestrator/parity-testing.md` recording the two Go-only
      routes as the first deliberate divergence (golden gate unchanged, still 14/14)
      [NFR-5].
- [x] **Step 15**: `README.md` — document the two endpoints, wait fields, deny policy
      summary, gotenberg service, new env vars; `aidlc-docs/construction/html-conversion/code/code-summary.md`
      — generated-code summary (modified vs created) [FR-1…7].

## Expected file inventory

| Action | Path |
|---|---|
| Modify | `worker_cpp/formats/docx.cpp` |
| Modify | `internal/types/types.go`, `internal/oerrors/*`, `internal/config/config.go`, `internal/probe/probe.go`, `internal/server/server.go`, `internal/server/convert.go`, `internal/obs/*` |
| Create | `internal/netpolicy/{netpolicy.go,netpolicy_test.go,pbt_test.go}` |
| Create | `internal/gotenberg/{gotenberg.go,gotenberg_test.go}` |
| Create | `internal/server/convert_html.go` (+ `convert_html_test.go`, testdata) |
| Modify | `internal/probe/probe_test.go` (+ PBT), `compose.go.yaml`, `office_convert_ui/app.py`, `README.md`, `aidlc-docs/construction/go-orchestrator/parity-testing.md` |
| Create | `tests/corpus/sample.html`, `tests/corpus/sample-js.html`, `aidlc-docs/construction/html-conversion/code/code-summary.md` |

**Not touched**: `office_convert/` (Python), `deploy/helm/` (Q6:A), golden fixtures
(`internal/server/testdata/golden/` — additive routes don't affect replay), `Dockerfile`
(Python image), `compose.yaml`.

## Verification per module (executed in Build & Test stage)
- Module 1: worker image rebuild (`make build-go` path builds workers too via go.Dockerfile).
- Module 2: `GOFLAGS=-mod=mod go vet ./... && go test ./...` (incl. new PBT).
- Module 3: `docker compose -f compose.yaml -f compose.go.yaml up` → acceptance criteria 1–4.
- Module 4: UI manual check (criterion 5) on the live local stack.
