# Reverse Engineering Metadata

**Analysis Date**: 2026-06-12T00:00:00Z
**Analyzer**: AI-DLC (Claude) — 4 parallel code-explorer agents + synthesis
**Workspace**: /home/adityawagh/opus2-workspace/office-conversion-service-demo
**Total Files Analyzed**: ~120 source files across Python (`office_convert/`, ~25 modules),
Go (`cmd/` + `internal/`, 18 packages / 41 `.go` files), C++ (`worker_cpp/`, 15 files),
Streamlit UI, Helm chart (8 templates), 4 Dockerfiles, 2 compose files, Makefile, CI workflows.

## Codebase Snapshot

- **Branch analyzed**: `main` (recent HEAD `51fa1e3` — docs: Python→Go arch diagram).
- **Project type**: BROWNFIELD (the recorded `Greenfield` state header is stale and predates all code).
- **Backends present**: Python FastAPI orchestrator (deployed prod) + Go chi orchestrator
  (merged, pre-cutover). C++ Aspose workers, Streamlit UI, and Helm chart shared/unchanged.

## Artifacts Generated

- [x] business-overview.md
- [x] architecture.md
- [x] code-structure.md
- [x] api-documentation.md
- [x] component-inventory.md
- [x] technology-stack.md
- [x] dependencies.md
- [x] code-quality-assessment.md
- [x] reverse-engineering-timestamp.md (this file)

## Staleness Note

These artifacts reflect `main` as of 2026-06-12. Re-run Reverse Engineering if:
- The Phase 8 Python→Go cutover lands (deployed backend changes), or
- The Phase 9 Python retirement removes `office_convert/`, or
- A new format/worker or a new endpoint is added.
