# Code Summary — unit `html-conversion`

**Generated**: 2026-06-12 · **Branch**: `feat/html-conversion` · Backend scope: **Go only**.
Plan: [`../../plans/html-conversion-code-generation-plan.md`](../../plans/html-conversion-code-generation-plan.md)

## Modified files

| File | Change |
|---|---|
| `worker_cpp/formats/docx.cpp` | Accepts `--format html` (render only); `render_html()` full-document path with `DenyPolicyResourceCallback` (BR-4 table, skip + stderr audit line) and `force_letter_geometry()` (BR-7, 612×792pt / 36pt margins); `--page-range` deliberately ignored for html |
| `internal/types/types.go` | `DispatchHTML` constant; `EngineUnavailable` FailureClass |
| `internal/oerrors/errors.go` | `NewEngineUnavailable(engine, url, cause)` → 503 |
| `internal/config/config.go` | `GotenbergURL` (default `http://gotenberg:3000`), `GotenbergTimeoutSeconds` (120, min 31 > waitDelay cap), `HTMLMaxBytes` (10 MiB) |
| `internal/probe/probe.go` | `LooksLikeHTML` (BR-1 sniff), `IsHTMLUpload`, detection inserted after image/before EML, `.html/.htm` extension fallback. `AcceptedUploadFormats` deliberately UNCHANGED (golden-parity: legacy error body stays byte-identical — the gate caught this) |
| `internal/server/server.go` | 2 new routes; `extHintFormats` += html/htm; `dispatchInfo.engine`; `recordToDict` engine (conditional key); `conversionsStats` → `per_engine_html` (only when non-empty) + extracted `summarizeTimes` |
| `internal/server/convert.go` | D1: generic `/v1/convert` rejects detected HTML with pointer to engine endpoints |
| `internal/worker/worker.go` | `RunWorker` core extracted to `runWorkerBin` (binary ≠ format arg); `RenderHTMLOneShot` (worker-docx + `--format html`, placeholder page range) |
| `internal/obs/recent.go` | `ConversionRecord.Engine` (`json:"engine,omitempty"`) |
| `internal/server/server_test.go` | test Settings gain `GotenbergTimeoutSeconds`/`HTMLMaxBytes` |
| `compose.go.yaml` | `gotenberg` service (gotenberg/gotenberg:8, `--chromium-deny-list` = netpolicy regex, `--api-timeout=130s`, 768m, no-new-privileges, internal-only) + `OFFICE_CONVERT_GOTENBERG_URL` + depends_on on the API |
| `office_convert_ui/app.py` | "🌐 HTML → PDF · Engine Comparison" panel (parallel convert-with-both via ThreadPoolExecutor, side-by-side cards, latency bar, API-wide `per_engine_html` metrics); engine tag in history rows; engine-tagged entries join shared history |
| `README.md` | New endpoint section (usage, wait controls, SSRF guard, env vars) |
| `aidlc-docs/construction/go-orchestrator/parity-testing.md` | Deliberate Go-only divergence note |

## Created files

| File | Purpose |
|---|---|
| `internal/netpolicy/netpolicy.go` | Canonical BR-4 deny policy: `Denied(rawURL)` + `ChromiumDenyListRegex` |
| `internal/netpolicy/{netpolicy_test.go,pbt_test.go}` | Deny-table examples + rapid PBT incl. the matcher↔regex consistency oracle |
| `internal/gotenberg/gotenberg.go` | Gotenberg client (multipart `index.html`, BR-7 geometry, wait fields, BR-5 classification, BR-8 %PDF- check) |
| `internal/gotenberg/gotenberg_test.go` | httptest: success/fields, classification table, unreachable, unconfigured |
| `internal/server/convert_html.go` | The two endpoint handlers: shared pre-processing, BR-3 wait validation (D4), engine dispatch, finishStream reuse |
| `internal/server/convert_html_test.go` | Endpoint integration tests (fake Gotenberg + fake worker script), telemetry, D1, caps |
| `internal/probe/html_test.go` | BR-1 examples + rapid PBT (prefix permutations / no false positives) |
| `tests/corpus/sample.html` | Static control sample (engines should match) |
| `tests/corpus/sample-js.html` | JS fidelity sample (`window.status==='ready'` pattern; engines should differ) |

## Deviations from plan (documented)

1. **`AcceptedUploadFormats` NOT extended** — the golden parity gate failed when
   html/htm were added (the list feeds the legacy route's error body). Reverted;
   recorded in the parity note. The engine endpoints don't use that list.
2. Probe tests landed in a new `internal/probe/html_test.go` instead of editing
   `probe_test.go` (same package, cleaner diff).
3. BR-3's prose said "400" for invalid wait fields; the canonical
   `input_unprocessable` class maps to **422** — implementation uses 422
   (business-rules.md corrected).

## Verification status (Code Generation stage)

- `GOFLAGS=-mod=mod go build ./... && go vet ./...` — clean.
- `go test ./...` — **all packages green, golden gate 14/14**.
- `go test -race` on the 4 touched/new packages — green.
- `python3 -m py_compile office_convert_ui/app.py` — OK.
- `docker compose -f compose.yaml -f compose.go.yaml config` — valid.
- NOT yet done (Build & Test stage): worker image rebuild (real Aspose HTML
  render), live-stack acceptance criteria 1–5, ruff/CI run.
