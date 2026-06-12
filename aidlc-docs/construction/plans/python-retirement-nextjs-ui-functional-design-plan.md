# Functional Design Plan — unit `python-retirement-nextjs-ui`

**Date**: 2026-06-12 · **Inputs**: approved requirements + execution plan; html-conversion
artifacts (the engine-comparison panel spec carries over); RE artifacts.

## Plan

- [x] Analyze unit context (5 core surfaces ↔ 16-endpoint contract; retirement inventory from
      the Phase 9 plan + current tree)
- [x] Resolve open design choices as documented defaults D1–D7 (no user questions — business
      decisions were settled in the requirements Q&A; defaults reviewable at this gate)
- [x] business-logic-model.md — UI data flows, retirement sequencing flow, PBT-01 analysis
- [x] business-rules.md — retirement inventory (exact delete/move/consolidate lists), UI
      behavior rules, security headers
- [x] domain-entities.md — TypeScript API types (wire mirrors), env/config surface
- [x] frontend-components.md — Next.js architecture: layout, components, hooks, proxy

## Defaults taken (review at approval gate)

| # | Decision | Default | Rationale |
|---|---|---|---|
| D1 | Next.js shape | Next 15.x, App Router, TS strict, `ui/` root with `app/` dir, standalone output | Current LTS conventions; standalone = slim Docker runtime |
| D2 | Charts | recharts only | NFR-3; plotly is ~3 MB and v1 has one bar chart + small sparklines |
| D3 | Data fetching | SWR with polling intervals | Lightest battle-tested polling lib; matches "server state via SWR" rule |
| D4 | Corpus relocation | `tests/corpus/*` → `testdata/corpus/` (repo root) | Go-conventional dir name; referenced by docs + manual acceptance commands |
| D5 | History | Purely API-backed (`/v1/conversions` + cursor) — the Streamlit process-wide store is gone | FIXES the known UI-local-state pitfall (cross-service conversions now always visible) |
| D6 | UI port | container 3000, host stays `127.0.0.1:8501`; Helm service port unchanged | operator muscle memory + no ingress change |
| D7 | Re-run | available only for files uploaded in the current browser session (client memory) | API history stores no input bytes; honest scope cut vs Streamlit's server-side store |
