# Requirements — Python Retirement + Next.js UI Rewrite

**Date**: 2026-06-12 · **Stage**: Requirements Analysis (standard depth) · Unit: `python-retirement-nextjs-ui`
**Question record**: [`python-retirement-nextjs-ui-questions.md`](python-retirement-nextjs-ui-questions.md) (Q1:A, Q2:A, Q3:A, Q4:A — 2026-06-12).
**Builds on**: the Phase 9 plan [`../../construction/go-orchestrator/python-retirement-plan.md`](../../construction/go-orchestrator/python-retirement-plan.md) (backend mechanics) — this doc extends it with the UI rewrite and supersedes its open UI question (answer: rewrite in Next.js).

## Intent Analysis

- **User request**: "Can we remove python entirly from this repo ?????? … Remove python and
  rewrite the UI too in next js … all recommended."
- **Request type**: Migration (Python → Go/TypeScript) + Refactoring (consolidation).
- **Scope estimate**: System-wide — deletes one of two backends, deletes the UI app, replaces
  it, consolidates Docker/compose/Make/CI.
- **Complexity estimate**: Complex (large surface, but mostly mechanical on the backend side;
  the new engineering is the Next.js app).
- **End-state**: repo languages = **Go (orchestrator) + C++17 (workers) + TypeScript/Next.js
  (UI)**. Zero `.py` files.

## Functional Requirements

### FR-1 Remove the Python orchestrator (per Phase 9 plan)
Delete `office_convert/` (~25 modules), Python `tests/` (unit/integration/property/e2e —
237 tests; the HTML corpus samples under `tests/corpus/` move to Go testdata or `testdata/`),
`pyproject.toml`, `ruff.toml`, `Dockerfile` (Python prod), `Dockerfile.test`,
`scripts/capture_golden.py`, `smoke_test/` Python bits if any. Golden fixtures under
`internal/server/testdata/golden/` are **kept frozen** as Go-only regression anchors.

### FR-2 Remove the Streamlit UI; replace with a Next.js app (Q1:A core-first)
Delete `office_convert_ui/` + `Dockerfile.ui`. New app `ui/` (Next.js 15+, TypeScript, App
Router) shipping these **core surfaces** in v1:
1. **Convert** — upload any accepted format → `POST /v1/convert` → download PDF; error
   Diagnostics rendered (failure_class + detail); S3-output checkbox when enabled.
2. **HTML engine comparison** — the dual-engine panel (parallel convert-with-both, wait
   controls for Gotenberg, side-by-side latency/size/status cards, latency bar, download).
3. **Conversion history** — API-backed via `/v1/conversions` (cursor pagination, filters,
   `engine` column, presigned S3 download via `/v1/downloads/presign`).
4. **Health & stats tiles** — `/health`, `/v1/stats`, `/v1/workers`; per-format +
   `per_engine_html` perf from `/v1/conversions/stats`.
5. **Live dashboard** — embedded `/v1/dashboard` iframe (Go-served; unchanged).

**Deferred to a follow-up iteration** (explicitly OUT of v1): heartbeat tables, RAM/timing/
Gantt charts, events feed, CPU/RAM gauges+sparklines (the iframe covers the live view).

### FR-3 API access via Next.js rewrites proxy (Q2:A)
The Next.js server proxies `/api/*` → `${API_URL}` (Go API). Single browser origin; **no CORS
changes to the Go API**; `X-Request-ID` passed through. `API_URL` env var keeps today's
semantics; `PUBLIC_API_URL` remains only for the dashboard-iframe src.

### FR-4 Consolidate build/run to Go-canonical
`go.Dockerfile` → `Dockerfile`; `compose.go.yaml` merged into `compose.yaml` (incl. the
gotenberg service); UI service swaps to the Next.js image (same `office-convert-ui:dev` local
tag convention — classification-service consumer of `office-convert:go` unaffected, tag kept).
Make targets renamed canonical: `build-go/test-go/run-go/up-go` → `build/test/run/up`; new
`ui-build`/`ui-dev` targets; vendor checks unchanged.

### FR-5 CI / Dependabot / docs
CI: drop the Python `qa` job; keep `go-test` (golden 14/14) + `helm-lint` + Trivy; add a
`ui-test` job (npm ci, eslint, `tsc --noEmit`, `next build`). Dependabot: `pip` → `npm` +
keep `gomod`/`docker`/`github-actions`. README rewritten Go-first (Python references removed);
Helm `ui-deployment` continues to exist, its image now the Next.js container.

### FR-6 Rollback anchor (Q3:A)
Git tag **`last-python-backend`** on the final Python-bearing commit (post-html-conversion
merge). ECR keeps the existing Python images; dev05 keeps running its current image until a
deliberate deploy. The Phase 8 soak gate is consciously overridden (license-blocked anyway).

### FR-7 Sequencing (Q4:B — user override at approval, 2026-06-12)
All retirement + Next.js work lands **on the existing `feat/html-conversion` branch** —
one branch, one PR carrying the HTML feature, the Python retirement, and the new UI together.
The `last-python-backend` rollback tag (FR-6) is placed on the current branch HEAD (last
commit where the Python backend + Streamlit UI still exist) before deletion begins.

## Non-Functional Requirements

### NFR-1 Security **[SECURITY-04 now APPLIES — HTML-serving app]**
The Next.js app sets security headers (next.config headers()): restrictive CSP
(`default-src 'self'`; `frame-src` allowing the API origin for the dashboard iframe),
`X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy:
strict-origin-when-cross-origin`. No secrets in the UI (API_URL only, server-side); no
`dangerouslySetInnerHTML` with API data; Diagnostics rendered as text. Container runs
non-root (fixes the Streamlit-ran-as-root TODO).

### NFR-2 Quality gates
TypeScript strict; eslint (next/core-web-vitals); `next build` must pass in CI. Component
tests for the two non-trivial client components (engine-comparison, history pagination) via
vitest + testing-library; a license-gated Playwright smoke (upload → PDF) mirroring the old
testcontainers e2e is a stretch goal, not a v1 gate.

### NFR-3 Performance (operator dashboard, internal)
Polling intervals match the Streamlit cadence (health/stats ~2–3s, history on demand);
App-page JS budget ≤ 300 KB gz (web perf rules); charts via a single lightweight lib
(recharts) — no plotly (heavy) in v1.

### NFR-4 Parity & consumers
Go API wire contract untouched (golden gate stays 14/14). `office-convert:go` tag preserved.
The UI consumes only documented endpoints — anything the UI needs that isn't in the contract
is a contract change, not a UI hack.

### NFR-5 PBT extension
Go rapid suites unchanged (planner, probe, netpolicy) — PBT coverage survives the loss of the
Hypothesis suite. Frontend: no PBT properties identified (UI rendering — rationale per
PBT-01); the deleted Python PBT tests' invariants are already mirrored in Go.

## Out of Scope
- Deep telemetry panels (heartbeats/timing/Gantt/events) — follow-up iteration.
- Helm manifest for Gotenberg (still deferred from the html-conversion unit).
- License-expiry parser fix (`LicenseExpiry` vs `SubscriptionExpiry`) — separate follow-up.
- dev05 redeploy/cutover — operational action after merge, not part of this unit.

## Acceptance Criteria
1. `find . -name '*.py' -not -path './.git/*'` → **0 files**; `grep -ri streamlit --include='*.yaml' --include='Makefile' .` → 0.
2. `make build && make test && make up` (canonical names) brings up Go API + gotenberg +
   localstack + Next.js UI; `make qa`-equivalent = `go vet/test` + `ui-test` job commands.
3. UI serves all five FR-2 core surfaces against the live stack; engine-comparison works
   end-to-end (Gotenberg side; Aspose side pending license renewal).
4. Golden parity gate still 14/14; classification-service consumer needs no change.
5. CI green with the new job matrix (go-test, ui-test, helm-lint, security).
6. Tag `last-python-backend` exists and is pushed.

## Extension Compliance (Requirements stage)
| Rule | Status | Note |
|---|---|---|
| SECURITY-01/02 | N/A | No new data store / network intermediary |
| SECURITY-03 | Captured | Go structured logging unchanged; Next server logs to stdout (compose/K8s collected) |
| SECURITY-04 | **Captured (now applicable)** | NFR-1 — headers on the HTML-serving Next.js app |
| PBT-01 | Captured | NFR-5 — Go PBT retained; UI marked no-properties with rationale |
