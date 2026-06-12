# Execution Plan — Python Retirement + Next.js UI (unit `python-retirement-nextjs-ui`)

**Date**: 2026-06-12 · **Requirements**: [`../requirements/python-retirement-nextjs-ui-requirements.md`](../requirements/python-retirement-nextjs-ui-requirements.md)
**Branch**: `feat/html-conversion` (Q4:B user override — single PR for html feature + retirement + UI).

## Analysis Summary

- **Transformation type**: System-wide migration — delete one of two backends + the UI app;
  replace UI; consolidate build/run/CI. Mostly mechanical deletion + consolidation; the new
  engineering is the Next.js app (`ui/`).
- **Impact**: API wire contract UNTOUCHED (golden 14/14 is the tripwire). User-facing: new UI.
  Structural: single-backend repo. Consumers: classification-service unaffected
  (`office-convert:go` tag kept).
- **Risk**: Medium-high (large deletion), mitigated by: `last-python-backend` git tag before
  deletion, ECR images surviving, additive-first ordering (build UI → verify → delete Python),
  and the Go suite + golden fixtures as the regression net.
- **Rollback**: revert PR or redeploy tagged/ECR images. Easy.

## Workflow (text)

```
INCEPTION:    Requirements (APPROVED, Q4 overridden to B) -> Workflow Planning (this doc)
              -> User Stories SKIP -> Application Design SKIP -> Units Generation SKIP
CONSTRUCTION: Functional Design EXECUTE -> NFR Req/Design SKIP -> Infra Design SKIP
              -> Code Generation EXECUTE (plan -> approval -> generate)
              -> Build and Test EXECUTE
```

### Stage determinations
- **User Stories — SKIP**: single operator persona; acceptance criteria concrete.
- **Application Design — SKIP**: backend deletion needs no design; the Next.js component
  architecture lands in Functional Design's frontend-components artifact (same treatment the
  html-conversion unit got).
- **Functional Design — EXECUTE**: Next.js app structure (pages, components, polling hooks,
  proxy config, security headers), retirement inventory (exact file list + consolidation
  mapping), PBT-01 properties analysis (UI: none — documented rationale).
- **NFR Requirements / NFR Design — SKIP**: NFR-1…5 already enumerated and approved.
- **Infrastructure Design — SKIP**: compose/Helm deltas folded into Code Generation.
- **Code Generation / Build and Test — EXECUTE** (always).

## Module sequence (ordering is the safety mechanism: ADD before DELETE)

| Order | Work | Why this order |
|---|---|---|
| 0 | Tag `last-python-backend` at current HEAD | rollback anchor BEFORE any deletion |
| 1 | Build `ui/` (Next.js app, 5 core surfaces) + Dockerfile.ui (node, non-root) | new UI proven against the live Go stack while Streamlit still exists |
| 2 | Compose: swap test-ui service to the Next image; verify stack | UI cutover, reversible |
| 3 | Retirement sweep: delete `office_convert/`, `tests/` (move corpus → `testdata/corpus/`), `office_convert_ui/`, `pyproject.toml`, `ruff.toml`, old `Dockerfile`/`Dockerfile.test`, `scripts/capture_golden.py` | only after the replacement works |
| 4 | Consolidation: `go.Dockerfile`→`Dockerfile`, `compose.go.yaml`→`compose.yaml`, canonical Make targets, healthcheck notes | single-backend repo shape |
| 5 | CI: drop `qa`, add `ui-test`; Dependabot pip→npm; README Go+TS rewrite; Helm ui image | plumbing follows the code |
| 6 | Build & Test: `make build && make test && make up`, acceptance criteria 1–6, golden 14/14 | full verification |

## Success criteria
Zero `.py` files; canonical Make targets green; UI serves the 5 core surfaces against the live
stack (Gotenberg engine verified; Aspose side still license-blocked); golden 14/14; CI matrix
(go-test, ui-test, helm-lint, security) green; `last-python-backend` tag pushed.

## Estimated timeline
2 working sessions (session 1: UI build + verify; session 2: retirement sweep + consolidation
+ CI + full verification).
