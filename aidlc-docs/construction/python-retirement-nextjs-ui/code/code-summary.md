# Code Generation Summary — unit `python-retirement-nextjs-ui`

**Branch**: `feat/html-conversion`
**Completed**: 2026-06-12
**Outcome**: Python orchestrator + Streamlit UI + pytest suite fully retired;
Next.js 15 operator dashboard built; infrastructure consolidated to a single
`Dockerfile` + `compose.yaml`; CI/dependabot/README migrated to the Go + C++ +
TypeScript stack.

**End-state languages**: Go (orchestrator) · C++ (Aspose workers) · TypeScript/Next.js (UI).
There is no Python in the codebase (only vendored Aspose Qt-Creator debug helpers
under `vendor/aspose/.../misc/` remain — third-party, out of scope).

---

## Safety ordering (the load-bearing mechanism)

`ADD → VERIFY → DELETE → CONSOLIDATE → CI/DOCS → VERIFY`, with the invariant
that `GOFLAGS=-mod=mod go test ./...` + the 14/14 golden parity gate stayed green
after **every** step. The Go side never changed in this unit, so any red would
have been a broken consolidation caught immediately. It never went red.

Rollback anchor: annotated tag `last-python-backend` at `a00df4d` (last commit
containing the Python backend), pushed with the branch.

---

## Module 1 — Next.js app (`ui/`)

Next.js 15 App Router, TypeScript strict, Tailwind (dark slate), SWR polling,
recharts, vitest + Testing Library. Key decisions:

- **Single-origin proxy (BR-UI-1)**: `next.config.ts` rewrites `/api/:path*` →
  `API_URL/:path*` server-side. The browser only talks to the UI origin — no CORS
  change to the Go API. The dashboard iframe is the one deliberate browser-direct URL.
- **Security headers (BR-UI-7 / SECURITY-04)**: CSP + `X-Content-Type-Options` +
  `X-Frame-Options` + `Referrer-Policy` + `Permissions-Policy` in `next.config.ts`.
- **API-truth history (BR-UI-5 / D3)**: history fetched from `/v1/conversions` with
  cursor pagination + stale-cursor handling — no client-side store, so cross-service
  conversions appear by construction (fixes the known Streamlit UI-local-state pitfall).
- **HTML engine comparison**: `Promise.all([gotenberg, aspose])` fires both engines in
  parallel; one failing never hides the other. Wait fields (`waitDelay`/`waitForExpression`)
  are sent to Gotenberg ONLY (D4); client-side mirrors of the 10 MiB cap + 30 s waitDelay
  bound (BR-UI-3) give feedback while the server stays authoritative.
- **Runtime env (not build-time)**: `app/page.tsx` is `force-dynamic` so
  `NEXT_PUBLIC_*` env reads happen per request, not baked at build.
- **Image**: multi-stage `ui/Dockerfile`, `output: 'standalone'`, non-root `node` user.

## Module 2 — Compose (additive, then merged in Mod 4)

UI service added to the compose overlay first (additive), verified, then folded
into the unified `compose.yaml`.

## Module 3 — Python deletion sweep (BR-R1) — commit `717fb4a`

Deleted `office_convert/` (~25 modules), `office_convert_ui/`, `tests/` (unit/
property/integration/e2e/corpus), `pyproject.toml`, `ruff.toml`, `Dockerfile`
(Python prod), `Dockerfile.test`, `Dockerfile.ui`, `scripts/capture_golden.py`.
Moved `tests/corpus/` → `testdata/corpus/` (corpus fixtures kept; Python generator
deleted). Kept `internal/server/testdata/golden/`, `smoke_test/`, `vendor/`.

## Module 4 — Consolidation (BR-R2) — commit `27dae67`

- `go.Dockerfile` → `Dockerfile` (the only backend image).
- Merged `compose.go.yaml` into `compose.yaml`: Go healthcheck (`office-convert-orchestrator
  healthcheck`), `gotenberg` service (full `--chromium-deny-list`), Next.js `ui` service
  (host `8501` → container `3000`), removed the Python `tests` + Streamlit `test-ui` services.
- Rewrote `Makefile`: removed all Python-only targets (`test`, `test-*`, `lint`,
  `format*`, `typecheck`, `qa`, `corpus`, `build-test`, `update-test-badge`);
  `build-go`→`build`, `up-go`→`up`, `down-go`→`down`; added `ui-install/ui-dev/ui-build/ui-lint`.

## Module 5 — CI + dependabot + docs (BR-R3) — commit `6235f68`

- `ci.yml`: removed the Python `qa` job; added `ui-test` (npm ci → lint → typecheck →
  build); `go-test` (golden gate) + `helm-lint` unchanged.
- `dependabot.yml`: removed `pip`; added `npm` for `/ui` (`nextjs-stack` + `test-tooling`
  groups); `docker` now tracks the Go/Node/Gotenberg base images.
- `README.md`: rewrote framing/build/test/structure for the post-retirement stack;
  corrected the pre-existing stale Aspose-tarball references to the actual `vendor/aspose/`
  5-libs path; `testdata/corpus/` paths.

## Module 6 — Final verification (all green)

| Gate | Result |
|---|---|
| `docker compose config --quiet` | exit 0 |
| `go test ./internal/... ./cmd/...` | all packages green |
| golden parity gate | 14/14 |
| `npm run lint` | 0 errors (next-lint deprecation note only) |
| `npm run typecheck` (`tsc --noEmit`) | 0 errors |
| `npm run build` | standalone OK; `/` dynamic, 213 kB First Load JS |
| `docker compose build ui` / `office-convert` | both images built (239 MB / 5.17 GB) |
| zero `.py` (our codebase) | confirmed (vendored Aspose helpers excluded) |

---

## Open items

- **PR not yet opened** for `feat/html-conversion` — by the Q4-override-to-B decision,
  the HTML feature + Python retirement + Next.js UI ship as a single PR.
- **Orphaned Python images** (`office-convert:dev`, `office-convert:test`) left in the
  local Docker store — not pruned (sibling `classification-pipeline/office-convert:local`
  shares the namespace; never blanket-prune).
- **Aspose-side HTML acceptance** still blocked on the expired real Aspose license
  (`LicenseExpiry 2026-06-08`; health reads only `SubscriptionExpiry`) — pre-existing,
  unrelated to this unit.
- **`next lint` deprecation**: removed in Next.js 16; migrate `ui-test`/`ui-lint` to the
  ESLint CLI before that bump (dependabot will surface it).
