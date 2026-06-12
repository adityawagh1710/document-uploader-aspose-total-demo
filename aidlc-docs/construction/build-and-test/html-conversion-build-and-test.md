# Build and Test — unit `html-conversion` (HTML dual-engine endpoints)

**Date**: 2026-06-12 · **Branch**: `feat/html-conversion` · Go backend only.
Feature-scoped supplement to the v1 docs in this directory (which remain valid for the office
paths). Acceptance criteria referenced below are §Acceptance Criteria of
[`../../inception/requirements/html-conversion-requirements.md`](../../inception/requirements/html-conversion-requirements.md).

## 1. Build

```bash
# Rebuilds the C++ workers (incl. the docx.cpp html path) against the real
# vendored Aspose SDK, then the Go orchestrator, into office-convert:go.
make build-go
```

- **Expected**: image `office-convert:go` builds cleanly; the C++ builder stage compiles
  `worker_cpp/formats/docx.cpp` (the html changes: IResourceLoadingCallback, PageSetup
  geometry, LoadFormat::Html) without errors.
- **Failure here = Aspose API mismatch** in the new docx.cpp code — fix in `worker_cpp/`
  and rebuild.

## 2. Unit + integration tests (host, no Docker)

```bash
GOFLAGS=-mod=mod go vet ./...
GOFLAGS=-mod=mod go test ./...           # all packages, incl. golden parity gate (must stay 14/14)
GOFLAGS=-mod=mod go test -race ./internal/server/ ./internal/gotenberg/ ./internal/netpolicy/ ./internal/probe/
```

- **Covers**: BR-1 sniffer (examples + rapid PBT), BR-4 deny matcher (examples + rapid PBT incl.
  the matcher↔Chromium-regex consistency oracle), BR-3 wait validation, BR-5 classification
  (fake Gotenberg httptest: 200/4xx/5xx/refused), endpoint handlers (fake worker script),
  engine telemetry, D1 generic-route rejection, caps.
- **Expected**: all green; `internal/server` includes `TestGoldenParity` — 14/14.

## 3. Python QA (UI changes)

```bash
make qa   # dockerized ruff check + ruff format --check + mypy + pytest (CI parity)
```

- The UI panel touched `office_convert_ui/app.py` only; mypy scope (`office_convert/`) is
  unaffected; ruff runs whole-tree.

## 4. Live-stack acceptance (compose, Go overlay)

```bash
export COMPOSE_FILE=compose.yaml:compose.go.yaml
docker compose up -d --build          # api (Go) + gotenberg + localstack + test-ui
docker compose ps                     # all Up; gotenberg listening internally on :3000
```

| # | Criterion | Command | Expected |
|---|---|---|---|
| 1 | Both engines convert | `curl -fsS -X POST localhost:8080/v1/convert/html/gotenberg -F file=@tests/corpus/sample.html -o /tmp/g.pdf && curl -fsS -X POST localhost:8080/v1/convert/html/aspose -F file=@tests/corpus/sample.html -o /tmp/a.pdf && file /tmp/{g,a}.pdf` | two valid PDFs |
| 2 | JS fidelity differs | `curl -fsS -X POST localhost:8080/v1/convert/html/gotenberg -F file=@tests/corpus/sample-js.html -F "waitForExpression=window.status === 'ready'" -o /tmp/g-js.pdf; curl -fsS -X POST localhost:8080/v1/convert/html/aspose -F file=@tests/corpus/sample-js.html -o /tmp/a-js.pdf` | Gotenberg PDF shows the dynamic table; Aspose PDF shows the "JavaScript did NOT run" placeholder (visual check via `pdftotext` or the UI preview) |
| 3 | SSRF deny on both | convert an HTML referencing `http://169.254.169.254/` and `http://localstack:4566/` | both PDFs render (resource skipped); `resource_denied` line in api logs (aspose) / no fetch hits localstack (gotenberg) |
| 4 | Engine down → 503 | `docker compose stop gotenberg; curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8080/v1/convert/html/gotenberg -F file=@tests/corpus/sample.html` | `503` + `engine_unavailable`; aspose endpoint still converts; `docker compose start gotenberg` |
| 5 | Telemetry + UI | `curl -s localhost:8080/v1/conversions/stats \| jq .per_engine_html` + open http://localhost:8501 → "HTML → PDF · Engine Comparison" → Convert with both | per-engine count/avg/p95; side-by-side cards + latency bar |
| 6 | Suite green | step 2 above | all ok, golden 14/14 |

Wait-control validation spot-checks: `-F waitDelay=31s` → 422; `-F waitDelay=2s` on the
**aspose** endpoint → 422; HTML upload to the generic `/v1/convert` → 400 `unsupported_format`
whose detail names the engine endpoints.

## 5. Performance comparison (the feature's purpose)

No fixed SLO — the endpoints exist to MEASURE. Methodology: convert the same file via both
endpoints N≥10 times (UI "Convert with both" or a curl loop), read
`/v1/conversions/stats.per_engine_html` (count/avg/p95 per engine). Expect Aspose to win on
latency for static HTML and lose on fidelity for JS pages — that asymmetry is the deliverable.

## 6. Cleanup

```bash
docker compose down        # never blanket-prune — sibling projects share this Docker host
```

---

## EXECUTED RESULTS — 2026-06-12 (local Go stack, branch feat/html-conversion)

### Builds
| Step | Result |
|---|---|
| `make build-go` (C++ workers vs real Aspose SDK + Go image) | ✅ exit 0 — **docx.cpp html path compiles against real Aspose headers** |
| `make qa` (ruff + format-check + mypy + pytest) | ✅ 237 passed / 1 skipped |
| `go vet` + `go test ./...` (incl. golden 14/14) + `-race` | ✅ all green (run during Code Generation, unchanged since) |

### Acceptance criteria
| # | Criterion | Result |
|---|---|---|
| 1 | Both engines convert sample.html | **Gotenberg ✅** (200, valid PDF 27 KB, 4.4 s cold / 0.15 s warm). **Aspose ⛔ BLOCKED (env)** — see blocker below |
| 2 | JS fidelity | **Gotenberg ✅** — `waitForExpression=window.status === 'ready'` captured the dynamic table ("JavaScript RAN" + data rows present via pdftotext). Aspose side ⛔ blocked |
| 3 | SSRF deny on both | **Gotenberg ✅** — sample-ssrf.html (metadata/localstack/loopback/RFC1918 imgs) converted 200; **localstack logs show 0 fetches** (deny-list effective). Aspose side ⛔ blocked (callback compiled; runtime unverified) |
| 4 | Engine down → 503 | ✅ stop gotenberg → `503 engine_unavailable` with cause detail; restarted cleanly |
| 5 | Telemetry + UI | ✅ `per_engine_html` {gotenberg: count 3, avg 1661 ms, p95 4368 ms}; `engine` field on conversion records (absent on non-engine records, per parity rule). UI panel up at :8501 (manual visual check available — stack left running) |
| 6 | Suite green | ✅ |

### Validation spot-checks
| Check | Result |
|---|---|
| `waitDelay=31s` → 422 | ✅ `input_unprocessable` "waitDelay must be <= 30s" |
| `waitDelay` on aspose endpoint → 422 (D4) | ✅ with explanatory detail pointing at the gotenberg endpoint |
| HTML upload on generic `/v1/convert` → 400 (D1) | ✅ `unsupported_format`, reason names both engine endpoints, accepted-list unchanged (parity) |
| non-HTML upload on engine endpoint → 422 | ✅ |

### ⛔ Environmental blocker (NOT a feature regression)
The Aspose render path fails with worker exit 2: `Aspose::Words SetLicense: The license has
expired.` Root cause: `Aspose.TotalforC++.lic` carries `SubscriptionExpiry: 2027-05-08` (what
BOTH orchestrators' health checks parse → "330 days remaining") but **`LicenseExpiry:
2026-06-08`** — the temporary license hard-expired 4 days before this test run. **The baseline
DOCX conversion fails identically on this image**, proving the blocker pre-exists the feature.
- Operator action: obtain a renewed Aspose temporary license, re-run acceptance 1–3 aspose-side
  (commands above).
- Pre-existing observability gap surfaced: `license.{py,go}` parse only `SubscriptionExpiry`;
  the SDK enforces `LicenseExpiry`. Health reports healthy while every render 503s. Worth a
  follow-up fix (parse min of both) — OUT of this unit's scope.
