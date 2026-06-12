# Python Retirement + Next.js UI — Requirement Verification Questions

**Stage**: Requirements Analysis · **Date**: 2026-06-12
**Feature/unit**: `python-retirement-nextjs-ui` — remove ALL Python from the repo (orchestrator
AND UI) and rewrite the operator UI in Next.js (TypeScript).

Already established (not re-asked): backend retirement mechanics follow the existing Phase 9
plan (`construction/go-orchestrator/python-retirement-plan.md`): delete `office_convert/` +
Python `tests/` + `Dockerfile`/`Dockerfile.test` + `pyproject.toml` + `capture_golden.py`
(golden fixtures kept frozen, Go-only verify); fold `go.Dockerfile`→`Dockerfile`,
`compose.go.yaml`→`compose.yaml`; canonical Make targets; CI drops the `qa` job; Dependabot
pip→gomod; README Go-first; `office-convert:go` tag kept for the classification-service
consumer. NEW scope on top: `office_convert_ui/` (Streamlit, ~3k lines) is deleted and
replaced by a Next.js app.

Please fill in the `[Answer]:` tags (e.g. `[Answer]: A`).

---

## Q1 — UI parity scope for the Next.js rewrite

The Streamlit app has ~10 surfaces: convert flow, HTML engine-comparison panel, conversion
history (re-run, presign, filters), KPI tiles, CPU/RAM gauges + sparklines, live heartbeat
tables, RAM/timing/Gantt charts, events feed, per-format perf, embedded `/v1/dashboard` iframe.

A) **Core-first** (recommended): v1 ships convert flow + HTML engine comparison + history
   (download/presign) + health/stats tiles + per-format & per-engine perf + dashboard iframe.
   The deep live-telemetry panels (heartbeat tables, RAM/timing/Gantt charts, events feed)
   land as a follow-up iteration — the embedded `/v1/dashboard` (served by Go) already covers
   much of that live view meanwhile.
B) **Full parity in one go** — every Streamlit surface reproduced before Python is deleted.
   Larger and slower, but no temporary capability dip.
X) Other (please describe after the [Answer]: tag below)

[Answer]: A

## Q2 — Next.js deployment shape & API origin strategy

A) **Node container + Next rewrites proxy** (recommended): the Next.js server proxies
   `/api/*` to the Go API (single origin, NO CORS changes to Go, mirrors today's separate
   UI Deployment/Ingress on EKS; `API_URL` env survives as the proxy target).
B) **Static export (`output: 'export'`) served by nginx** + CORS headers added to the Go API.
   Lighter runtime, but touches the Go API contract (CORS) and loses any server-side bits.
C) **Static export embedded into the Go binary** (`go:embed`): ONE container total, UI served
   by the API at `/ui`. Most elegant ops story, but couples UI and API releases and changes
   the EKS topology (no separate UI pod).
X) Other (please describe after the [Answer]: tag below)

[Answer]: A

## Q3 — Phase 8 gate override (retire before the dev05 Go cutover soak?)

The original Phase 9 plan gated retirement on the dev05 Go cutover HOLDING first. That soak is
currently impossible anyway: the real Aspose license expired 2026-06-08, so no environment can
prove conversions until it's renewed.

A) **Retire now** (recommended given the blocked soak): rollback story = git tag
   `last-python-backend` on the final Python-bearing commit + the existing Python images in
   ECR (dev05 keeps running its current image until someone deploys). Accepts that Go has not
   soaked in dev05.
B) **Wait**: renew the license, do the Phase 8 dev05 cutover, soak N days, then retire.
X) Other (please describe after the [Answer]: tag below)

[Answer]: A

## Q4 — Sequencing vs the open `feat/html-conversion` PR

A) **Merge `feat/html-conversion` to main first** (recommended), then branch
   `feat/python-retirement-nextjs-ui` from main — clean history, no cross-branch conflicts
   (the retirement folds `compose.go.yaml` which that PR just touched).
B) Stack the retirement branch on top of `feat/html-conversion` (don't wait for the merge).
X) Other (please describe after the [Answer]: tag below)

[Answer]: B (user override 2026-06-12: "Approve & Continue but everything in feat/html-conversion branch" — stack on the open branch, single PR)
