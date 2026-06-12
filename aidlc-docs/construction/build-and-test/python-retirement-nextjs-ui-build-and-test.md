# Build and Test — unit `python-retirement-nextjs-ui`

**Executed**: 2026-06-12 · **Branch**: `feat/html-conversion`
**Outcome**: ✅ All build and test gates GREEN. Stack verified end-to-end on the
consolidated single-`compose.yaml` topology (Go API + Next.js UI + Gotenberg + LocalStack).

Unlike the historical `office-converter` summary (instructions only — no SDK in that
environment), this unit's gates were **actually run** in this environment.

---

## 1. Build Status

| Artifact | Tool | Result |
|---|---|---|
| `office-convert:go` (API) | `docker build` via `Dockerfile` (C++ worker builder → Go builder → Python-free runtime) | ✅ Built — **5.17 GB** |
| `office-convert-ui:dev` (UI) | `docker build ui/` (Next.js multi-stage standalone, non-root node) | ✅ Built — **239 MB** |
| `docker compose config` | compose v2 | ✅ exit 0 on the merged `compose.yaml` |

Prereqs present in this environment: `vendor/aspose/` (5 product trees) + `Aspose.TotalforC++.lic`.

---

## 2. Unit / Component Tests

### Go suite (`make test-go` equivalent)
`GOFLAGS=-mod=mod go test ./internal/... ./cmd/...` — **all packages green**.

- Golden parity gate `TestGoldenParity`: **14/14** (the wire-contract tripwire; the
  Go side is unchanged by this unit, so this proves the retirement didn't disturb it).
- planner / worker / qpdf / netpolicy / license / probe / csvinput / gotenberg / obs / server: pass.

### Next.js UI (`make ui-lint` + `ui-build`)
- `npm run lint` (ESLint via `next lint`): **0 errors** (only a `next lint` deprecation notice — removed in Next.js 16; migrate to ESLint CLI before that bump).
- `npm run typecheck` (`tsc --noEmit`): **0 errors**.
- `npm run build`: **standalone output OK**. `/` is `ƒ` (dynamic, server-rendered per request — correct for runtime env reads), **213 kB First Load JS** (within the 300 kB app-page budget).
- vitest component tests (ComparePanel, HistoryPanel): **9/9 pass** (`npm --prefix ui test`; now run in CI's `ui-test` job too).

### Browser E2E (Playwright) — added 2026-06-12
`@playwright/test` + `ui/e2e/` (config runs the production standalone server with
`/api/**` stubbed; CI-friendly, no backend needed). Live specs (`E2E_LIVE=1`) hit
the real `:8501` stack.
- **Mocked suite: 12/12 pass** — dashboard (5 sections, health tiles, iframe, CSP
  header), HTML compare (run-gating, engine independence with aspose 503,
  client-side waitDelay bound, per-engine stats), history (engine chips, failed
  error codes, stale-cursor reset, filters).
- **Live suite: 2/2 pass** against the rebuilt stack — UI hydrates + health tiles
  READY; **real browser-driven Gotenberg HTML→PDF via the `/api` proxy**.
- CI: new `ui-e2e` job (installs Chromium, runs the mocked suite, uploads the report).

### 🐞 Production bug found AND fixed by the E2E (commit `afaaf19`)
The E2E surfaced a real shipped bug the curl-only smoke could not: the CSP was
`script-src 'self'` (static, in `next.config.ts`). Next.js App Router streams its
hydration/RSC payload via **inline `<script>` tags in production**, so that policy
**blocked Next's own bootstrap → the UI rendered SSR HTML but never hydrated**
(dead panels, no SWR, no `/api` calls) for every real browser user. Fixed with a
**per-request nonce CSP in `ui/middleware.ts`** (`'self' 'nonce-…' 'strict-dynamic'`),
matching the project web-security rules; verified live (the rebuilt container serves
the nonce CSP and the browser E2E now hydrates + converts).

---

## 3. Integration Tests (executed against the running stack)

Stack: `docker compose up -d` → 4 containers healthy (`office-convert` healthy,
`gotenberg` up, `localstack` healthy, `ui` up). All probes from the host.

| # | Scenario | Expected | Result |
|---|---|---|---|
| 1 | `GET /health` (direct) | 200, `ready:true` | ✅ `{"ready":true,"license_days_remaining":330,"max_jobs":2,...}` |
| 2 | `GET /api/health` **via Next.js UI proxy** (BR-UI-1) | byte-identical to direct `/health` | ✅ identical JSON — single-origin rewrite proxy works end-to-end |
| 3 | UI `GET /` at `:8501` | 200 (Next.js HTML) | ✅ HTTP 200 |
| 4 | UI security headers (BR-UI-7 / SECURITY-04) | CSP + nosniff + frame + referrer | ✅ all present; `frame-src http://localhost:8080` for the dashboard iframe |
| 5 | `POST /v1/convert` truly-unsupported bytes | 400 `unsupported_format`, HTML absent from `accepted` | ✅ 400; `accepted` list has no `html` (golden parity preserved) |
| 6 | `POST /v1/convert/html/gotenberg` (license-independent) | 200, valid PDF | ✅ 200, **27688-byte PDF v1.4**, `X-Request-Id` set |
| 7 | Gotenberg **via UI proxy** `/api/v1/convert/html/gotenberg` | 200, identical PDF | ✅ 200, identical 27688 bytes — full browser→UI→API→Gotenberg→PDF path |
| 8 | `GET /v1/conversions` (BR-UI-5 API-truth history) | entries carry `engine`, `source` | ✅ both Gotenberg runs shown: `engine:"gotenberg"`, `format:"html"`, `status:"success"`, `source:"ui"` |
| 9 | `GET /v1/conversions/stats` per-engine block | `per_engine_html.gotenberg` | ✅ `{count:2, avg_ms:2864, p95_ms:5593}` |
| 10 | History via UI proxy `/api/v1/conversions` | 200 | ✅ HTTP 200 |
| 11 | `POST /v1/convert/html/aspose` | 503 `license_expired` (expired real license) | ✅ 503 `license_expired` — **expected**, see note below |

**Aspose-side note (not a regression):** the `/v1/convert` path validates against
`LicenseExpiry` (hard-expired 2026-06-08) and correctly returns 503 `license_expired`,
while `/health` reads `SubscriptionExpiry` (2027, hence `ready:true`). This is the
pre-existing license-field gap documented for the `html-conversion` unit — the only
blocker for the Aspose render engine is license renewal, not code. Gotenberg (Chromium,
no Aspose) is fully verified.

Cleanup: `docker compose down` — clean.

---

## 4. Performance

Not a load-test target for this unit (UI rewrite + retirement, no algorithm change).
Observed during the smoke: Gotenberg HTML render ~136 ms–5.6 s (cold Chromium first
hit dominates the p95); UI First Load JS 213 kB. The conversion engine's performance
profile is unchanged from the `office-converter` / `html-conversion` units.

---

## 5. Security

- **CSP + hardening headers** on the Next.js app verified live (test 4).
- **Single-origin proxy** (no API URL in client JS except the dashboard iframe) verified (tests 2, 7, 10).
- **SSRF deny policy** unchanged (Go `internal/netpolicy` + Gotenberg `--chromium-deny-list`, both retained in the merged compose).
- **No secrets in source**; license bind-mounted read-only; non-root UI + API runtimes.
- CI security: Trivy job retained; dependabot now tracks `npm` (`/ui`) + `docker` bases.

---

## 6. Overall

| Gate | Status |
|---|---|
| Build (API + UI images) | ✅ |
| Go unit + golden 14/14 | ✅ |
| UI lint + typecheck + build | ✅ |
| Integration smoke (11 scenarios) | ✅ (Aspose path 503 expected — license) |
| Security headers + proxy | ✅ |
| **Ready for Operations** | ✅ (with the standing license-renewal item for the Aspose HTML engine) |

**Open items** (carried, not blockers): open the `feat/html-conversion` PR (single PR =
HTML feature + retirement + Next.js UI); renew the Aspose license to unblock Aspose-side
HTML acceptance; migrate `next lint` → ESLint CLI before Next.js 16.
