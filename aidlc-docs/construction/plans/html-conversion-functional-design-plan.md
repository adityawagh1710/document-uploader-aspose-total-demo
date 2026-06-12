# Functional Design Plan — unit `html-conversion`

**Date**: 2026-06-12 · **Inputs**: approved `inception/requirements/html-conversion-requirements.md`,
`inception/plans/html-conversion-execution-plan.md`, RE artifacts.

## Plan

- [x] Analyze unit context (requirements FR-1…7 / NFR-1…6; integration-point trace; existing
      bypass-pattern precedents `internal/libreoffice`, `internal/email`)
- [x] Identify open design decisions; resolve with documented defaults (no user questions —
      all business-level ambiguity was resolved in the requirements Q&A; the four defaults below
      are surfaced for review at the stage approval gate)
- [x] business-logic-model.md — engine flows, shared pre-processing, Gotenberg/worker contracts,
      telemetry, **Testable Properties (PBT-01)**
- [x] business-rules.md — detection, validation, deny-list policy (canonical), failure mapping,
      timeouts, page geometry
- [x] domain-entities.md — new/extended types and settings
- [x] frontend-components.md — UI comparison panel (unit includes frontend)

## Design decisions taken by default (review at approval gate)

| # | Decision | Default taken | Rationale |
|---|---|---|---|
| D1 | HTML uploaded to the GENERIC `/v1/convert` | Reject with `unsupported_format` (400), diagnostic detail names the two engine endpoints | Never silently pick an engine in a benchmarking feature; keeps the comparison explicit |
| D2 | Page geometry | Letter 8.5×11 in, 0.5 in margins, enforced identically on BOTH engines | Apples-to-apples fidelity comparison; matches the sibling design's Gotenberg config |
| D3 | `waitDelay` bound | ≤ 30 s (reject larger, 400) | Keeps the 120 s Gotenberg client timeout safe with headroom |
| D4 | Wait fields sent to the Aspose endpoint | Reject with 400 (`input_unprocessable`, explanatory detail) | Explicit beats silent-ignore for a comparison tool (no JS engine ⇒ the option is meaningless) |
