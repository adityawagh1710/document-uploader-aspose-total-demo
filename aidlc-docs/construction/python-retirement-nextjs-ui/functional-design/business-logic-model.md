# Business Logic Model — unit `python-retirement-nextjs-ui`

## Flow 1 — UI data flows (Next.js)

```
Browser ──/api/*──> Next server (rewrites) ──> Go API :8080
   │
   └─ iframe src ──────────────────────────> Go API /v1/dashboard (direct, browser-facing URL)

Convert:        File → POST /api/v1/convert (multipart) → blob | Diagnostic → download/error card
Compare:        File → allSettled[ POST /api/v1/convert/html/gotenberg (+wait fields),
                                   POST /api/v1/convert/html/aspose ]
                → EngineCard ×2 + recharts latency bar + per_engine_html cumulative row
History:        SWR GET /api/v1/conversions?cursor… → table (engine chip) → presign per click
Tiles/Perf:     SWR GET /api/{health,v1/stats,v1/workers,v1/conversions/stats}
```

All five surfaces consume ONLY the documented 16-endpoint contract — no new API surface, no
Go changes (NFR-4).

## Flow 2 — Retirement sequencing (the safety logic)

```
0. git tag last-python-backend (annotated, at HEAD)        ← rollback anchor
1. ADD ui/ (Next.js app) … verify against live Go stack (Streamlit still present)
2. compose: ui service → Next image … re-verify stack
3. DELETE python inventory (BR-R1) … go test ./... + golden 14/14 must stay green
4. CONSOLIDATE (BR-R2: Dockerfile, compose merge, Make canonical)
5. CI/docs (BR-R3, README)
6. Full Build & Test (acceptance criteria 1–6)
```

Invariant at every step: `GOFLAGS=-mod=mod go test ./...` green and golden gate 14/14 —
the Go side never changes in this unit, so any red is a broken consolidation, caught
immediately at the step that caused it.

## Flow 3 — Engine comparison (port of the html-conversion panel)

Identical semantics to the Streamlit panel (frontend-components.md of the html-conversion
unit): parallel fire, independent failure rendering, wait fields Gotenberg-only, JS-fidelity
hint, cumulative per-engine stats from the API. One improvement: results survive page reload
only via the API history (no process-wide store — D5 honesty).

## Testable Properties (PBT-01)

| Component | Property analysis | PBT? |
|---|---|---|
| Next.js UI (all components) | Rendering/interaction; no algorithms, no transformations with invariants — formatting helpers (`bytes`, `ms`) are trivially example-testable | **No PBT properties identified** — rationale documented; vitest component tests cover ComparePanel + HistoryPanel behavior |
| Retirement sweep | Not code — verified by acceptance criteria (zero `.py`, suites green) | N/A |
| Go orchestrator | Unchanged; existing rapid PBT suites (planner, probe sniffer, netpolicy) continue to run | Already covered |
