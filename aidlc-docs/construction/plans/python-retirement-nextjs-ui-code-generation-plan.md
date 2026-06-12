# Code Generation Plan — unit `python-retirement-nextjs-ui`

**Branch**: `feat/html-conversion`  
**Safety ordering**: ADD → VERIFY → DELETE → CONSOLIDATE → CI/DOCS → VERIFY  
**Invariant**: `GOFLAGS=-mod=mod go test ./...` green + golden gate 14/14 at every step

---

## Module 0: Rollback Anchor

- [x] **Step 0.1** — Create annotated git tag `last-python-backend` at current HEAD on `feat/html-conversion`
  ```
  git tag -a last-python-backend -m "Last commit containing Python orchestrator + Streamlit UI + pytest suite before Next.js rewrite and Python retirement"
  ```
  *This is the rollback point — never delete; push with the branch (`git push origin last-python-backend`).*

---

## Module 1: Next.js App (`ui/`)

*All steps are ADDITIVE — existing Python/Streamlit untouched until Module 3.*

### Step 1.1 — Scaffold `ui/`
Create the project foundation:
- [x] `ui/package.json` — Next.js 15, TypeScript, Tailwind, SWR, recharts, eslint, prettier, vitest, @testing-library/react
- [x] `ui/tsconfig.json` — strict, path aliases (`@/*` → `./`)
- [x] `ui/next.config.ts` — rewrites (`/api/:path*` → `API_URL/:path*`), security headers (BR-UI-7 CSP set)
- [x] `ui/tailwind.config.ts` — content: `app/**/*.tsx`, `components/**/*.tsx`, `lib/**/*.ts`; slate dark theme palette
- [x] `ui/postcss.config.js`
- [x] `ui/.eslintrc.json` — extends `next/core-web-vitals`, TypeScript strict
- [x] `ui/.dockerignore` — excludes `node_modules`, `.next`, `*.md`

### Step 1.2 — Types and utilities
- [x] `ui/lib/types.ts` — full wire-type mirror from domain-entities.md (`FailureClass`, `ConversionRecord`, `ConversionsPage`, `ConversionsStats`, `Health`, `ContainerStats`, `WorkerProc`, `Presigned`, `EngineRunResult`)
- [x] `ui/lib/api.ts` — typed fetch helpers; all calls to `/api/*` proxy paths; error parsing to `Diagnostic`
- [x] `ui/lib/format.ts` — `formatBytes(n)`, `formatMs(n)`, `formatDate(ts)` helpers

### Step 1.3 — App shell
- [x] `ui/app/globals.css` — Tailwind base/components/utilities; CSS tokens (slate-900 bg, cyan/violet accents, `--radius`)
- [x] `ui/app/layout.tsx` — root layout; dark slate theme; header with health pill (SWR `/api/health` 3 s); `<main>` landmark
- [x] `ui/app/page.tsx` — single-page dashboard; sections: Convert, Compare, History, Performance, Live Dashboard

### Step 1.4 — Shared UI primitives
- [x] `ui/components/ui/Badge.tsx` — `failure_class` chip, engine tag chip (`gotenberg`/`aspose`)
- [x] `ui/components/ui/Card.tsx` — surface card (dark bg, rounded, border)
- [x] `ui/components/ui/Spinner.tsx` — animated loading indicator
- [x] `ui/components/ui/ErrorDiagnostic.tsx` — renders `Diagnostic` as `failure_class` chip + `detail` key-value table; never `dangerouslySetInnerHTML`

### Step 1.5 — ConvertPanel
- [x] `ui/components/convert/ConvertPanel.tsx`
  - File picker (all supported formats; BR-UI-3: client-side 10 MiB guard for HTML uploads)
  - S3-output checkbox (visible only when `NEXT_PUBLIC_S3_ENABLED=true`)
  - Submit → `POST /api/v1/convert` (multipart); blob download on 200; `ErrorDiagnostic` on non-200
  - `data-testid="convert-file-input"`, `data-testid="convert-submit-button"`

### Step 1.6 — ComparePanel + EngineCard
- [x] `ui/components/html-compare/EngineCard.tsx` — per-engine result card: engine name, latency, PDF size, download link / error display
- [x] `ui/components/html-compare/ComparePanel.tsx`
  - HTML file picker (10 MiB client-side cap)
  - Wait controls: `waitDelay` (≤ 30 s, pattern-checked) + `waitForExpression`; JS-fidelity hint text; disabled on Aspose run (BR-UI-3)
  - "Convert with BOTH" → `Promise.allSettled([ POST gotenberg, POST aspose ])` in parallel
  - Side-by-side `EngineCard ×2`; recharts latency bar; cumulative `per_engine_html` stats row from SWR
  - `data-testid="compare-file-input"`, `data-testid="compare-both-button"`

### Step 1.7 — HistoryPanel + PresignButton
- [x] `ui/components/history/PresignButton.tsx` — `GET /api/v1/downloads/presign?…`; mints URL per click; never caches
- [x] `ui/components/history/HistoryPanel.tsx`
  - SWR 5 s on `/api/v1/conversions?cursor=&limit=20`; cursor pagination; stale-cursor toast
  - Engine chip column (visible for rows with `engine` field)
  - `data-testid="history-filter-input"`, `data-testid="history-table"`

### Step 1.8 — Stats / Perf panels
- [x] `ui/components/stats/HealthTiles.tsx` — SWR 3 s on `/api/health` + `/api/v1/stats` + `/api/v1/workers`; KPI tiles: ready, license days, active/max jobs, worker count, cgroup CPU%/mem
- [x] `ui/components/stats/PerfPanel.tsx` — SWR 5 s on `/api/v1/conversions/stats`; recharts `BarChart` per_format + per_engine_html (avg_ms, p95_ms, count)

### Step 1.9 — DashboardFrame
- [x] `ui/components/dashboard/DashboardFrame.tsx` — `<iframe src={NEXT_PUBLIC_DASHBOARD_URL}>` in a Card; `title="Live conversion dashboard"` for a11y

### Step 1.10 — ui/ Dockerfile
- [x] `ui/Dockerfile` — multi-stage:
  - Stage 1 `deps`: `node:22-alpine AS deps`; `npm ci --omit=dev`
  - Stage 2 `builder`: copy source + `node_modules`; `npm run build`
  - Stage 3 `runner`: `node:22-alpine`; `USER node`; copy `.next/standalone` + `.next/static` + `public`; `PORT=3000 EXPOSE 3000`; `CMD ["node", "server.js"]`
  - `output: 'standalone'` wired in next.config.ts; non-root node user; no-new-privileges

### Step 1.11 — Component tests (vitest)
- [x] `ui/components/html-compare/__tests__/ComparePanel.test.tsx` — tests: wait-field disabled on aspose, allSettled independent failure rendering, `per_engine_html` row visible when data present
- [x] `ui/components/history/__tests__/HistoryPanel.test.tsx` — tests: engine chip visible on html records, stale-cursor reset

---

## Module 2: Compose Swap (additive then swap)

### Step 2.1 — Add Next.js UI to `compose.go.yaml` (additive)
- [ ] Add `ui` service to `compose.go.yaml`:
  ```yaml
  ui:
    build:
      context: .
      dockerfile: ui/Dockerfile
    image: office-convert-ui:dev
    ports:
      - "127.0.0.1:8501:3000"
    environment:
      API_URL: "http://office-convert:8080"
      NEXT_PUBLIC_DASHBOARD_URL: "http://localhost:8080/v1/dashboard"
      NEXT_PUBLIC_S3_ENABLED: "${OFFICE_CONVERT_S3_ENABLED:-true}"
      PORT: "3000"
    depends_on:
      - office-convert
  ```
  *Keep `test-ui` (Streamlit) in compose.yaml for now — Streamlit stays until verified.*

### Step 2.2 — Verify compose config
- [ ] `docker compose -f compose.yaml -f compose.go.yaml config --quiet` exits 0

---

## Module 3: Python Deletion Sweep (BR-R1)

*Only execute after Next.js UI is verified reachable at `http://localhost:8501`.*

### Step 3.1 — Move corpus
- [x] `mkdir -p testdata/corpus && cp tests/corpus/* testdata/corpus/` (copy first; delete later)
- [x] Update corpus references in README, Makefile corpus target, build-and-test docs

### Step 3.2 — Delete Python inventory (exact BR-R1 list)
Delete in one commit (all or nothing):
- [x] `office_convert/` (entire directory — ~25 modules)
- [x] `office_convert_ui/` (Streamlit UI + `__pycache__`)
- [x] `tests/` (pytest suite — unit/integration/property/e2e/corpus; corpus already moved)
- [x] `pyproject.toml`
- [x] `ruff.toml`
- [x] `Dockerfile` (Python prod image — superseded by go.Dockerfile→Dockerfile)
- [x] `Dockerfile.test` (Python test runner)
- [x] `Dockerfile.ui` (Streamlit image — replaced by ui/Dockerfile)
- [x] `scripts/capture_golden.py` (golden capture oracle — Go fixtures kept)
- [x] `scripts/` (delete directory if empty after capture_golden.py removal)

**KEEP**: `internal/server/testdata/golden/` · `testdata/corpus/` · `smoke_test/` · `vendor/`

### Step 3.3 — Verify golden gate green
- [x] `docker run --rm -v $(PWD):/src -w /src golang:1.26-bookworm sh -c "GOFLAGS=-mod=mod go test ./internal/... ./cmd/... -count=1"` → green
- [x] Golden gate: 14/14 pass

---

## Module 4: Consolidation (BR-R2)

### Step 4.1 — Rename `go.Dockerfile` → `Dockerfile`
- [x] `mv go.Dockerfile Dockerfile` — now the ONLY backend Dockerfile
- [x] Update Makefile `build-go` → `build` target to use default `Dockerfile` (drop `-f go.Dockerfile`)
- [x] Update `.dockerignore` if it references `go.Dockerfile`

### Step 4.2 — Merge compose files into single `compose.yaml`
Merge `compose.go.yaml` content INTO `compose.yaml`:
- [x] `office-convert` service: use new `Dockerfile` (default); add `OFFICE_CONVERT_GOTENBERG_URL` env; override healthcheck to `CMD /usr/local/bin/office-convert-orchestrator healthcheck`; add `depends_on: gotenberg: condition: service_started`; remove Python `CMD` healthcheck
- [x] Add `gotenberg` service (from compose.go.yaml) with full `--chromium-deny-list` command
- [x] `test-ui` → `ui` service: `ui/Dockerfile` image; port `127.0.0.1:8501:3000`; Next.js env vars
- [x] Delete `compose.go.yaml`

### Step 4.3 — Canonicalize Makefile targets
- [x] Rename `build-go` → `build`, keep `test-go`, `up-go` → `up`, `down-go` → `down`, `logs-go` → `logs`
- [x] Remove `COMPOSE_GO` variable (now just `docker compose`)
- [x] Remove Python-only targets: `lint`, `format-check`, `format`, `typecheck`, `qa`, `update-test-badge`, `build-test`, `test`, `test-unit`, `test-property`, `test-integration`, `test-coverage`, `corpus`, `test-e2e`
- [x] Add `ui-install`: `npm --prefix ui ci`
- [x] Add `ui-dev`: `npm --prefix ui run dev`
- [x] Add `ui-build`: `npm --prefix ui run build`
- [x] Add `ui-lint`: `npm --prefix ui run lint`
- [x] Update `help` header and `BUILD/TEST/RUN` references

---

## Module 5: CI + Dependabot + Docs (BR-R3)

### Step 5.1 — Update `.github/workflows/ci.yml`
- [x] Delete the `qa` job (lint + typecheck + pytest — Python-only)
- [x] Keep `go-test` job unchanged (golden gate 14/14 is the contract tripwire)
- [x] Keep `helm-lint` job unchanged
- [x] Add `ui-test` job:
  ```yaml
  ui-test:
    name: Next.js lint + typecheck + build
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v4
        with:
          node-version: '22'
          cache: 'npm'
          cache-dependency-path: ui/package-lock.json
      - run: npm --prefix ui ci
      - run: npm --prefix ui run lint
      - run: npm --prefix ui run tsc -- --noEmit
      - run: npm --prefix ui run build
  ```

### Step 5.2 — Update `.github/dependabot.yml`
- [x] Delete the `pip` ecosystem block
- [x] Add `npm` ecosystem block for `/ui` directory (weekly, Monday, 07:00 UTC, groups: `nextjs-stack`, `test-tooling`)
- [x] Keep `docker`, `github-actions` blocks unchanged

### Step 5.3 — Update `README.md`
- [x] Language badges: remove Python, keep Go + C++ + TypeScript (add)
- [x] Quick start: replace `make build-test && make test` with `make test-go`; add `make ui-install && make ui-dev` for UI development
- [x] Remove "Python orchestrator", "pytest", "mypy", "ruff" references
- [x] Update "Tech stack" table: Go (orchestrator), C++ (Aspose workers), TypeScript/Next.js (UI), no Python row
- [x] Corrected stale Aspose tarball references → `vendor/aspose/` 5-libs path (was a pre-existing doc gap)

---

## Module 6: Final Verification

- [ ] **Step 6.1** — `docker compose config --quiet` exits 0 on the merged `compose.yaml`
- [ ] **Step 6.2** — `GOFLAGS=-mod=mod go test ./internal/... ./cmd/... -count=1` → all green, golden 14/14
- [ ] **Step 6.3** — `npm --prefix ui run lint` → 0 errors
- [ ] **Step 6.4** — `npm --prefix ui run tsc -- --noEmit` → 0 errors
- [ ] **Step 6.5** — `npm --prefix ui run build` → successful standalone output
- [ ] **Step 6.6** — `docker compose build` with new unified compose builds both API and UI images
- [ ] **Step 6.7** — Zero `.py` files remain (verify: `find . -name '*.py' -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/aidlc-docs/*'` → empty)
- [ ] **Step 6.8** — Update `aidlc-docs/aidlc-state.md` stage progress (python-retirement-nextjs-ui Code Generation → COMPLETE)
- [ ] **Step 6.9** — Append code generation summary to `aidlc-docs/audit.md`

---

## Defaults applied (D1–D7)

| Default | Decision |
|---|---|
| D1 SWR | Used over React Query — simpler, smaller footprint for polling-only use case |
| D2 recharts | Used over plotly — consistent with the Go API's JSON shape; lighter bundle |
| D3 API-backed history | History fetched from `/v1/conversions` — fixes the known UI-local-state pitfall |
| D4 host port 8501 | `127.0.0.1:8501:3000` matches existing browser bookmarks and operator muscle memory |
| D5 no persistence | Session re-run limited to files uploaded in the current browser session |
| D6 Next.js standalone | `output: 'standalone'` in next.config.ts; minimal runtime image footprint |
| D7 component tests | vitest + testing-library for ComparePanel + HistoryPanel logic |

---

## Acceptance criteria mapping

| Criterion | Covered by |
|---|---|
| AC-1: UI accessible at `:8501` showing 5 surfaces | Step 2.2 verify + Module 6 |
| AC-2: Convert panel converts a file | Step 6.6 smoke |
| AC-3: Compare panel fires both engines | Step 6.6 smoke |
| AC-4: History shows engine chip for html rows | Step 1.7 + Step 6.2 |
| AC-5: Zero `.py` files | Step 6.7 |
| AC-6: `go test` golden 14/14 green | Steps 3.3 + 6.2 |
