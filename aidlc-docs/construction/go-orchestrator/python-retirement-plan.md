# Phase 9 — Python retirement (Go-only) — PLAN, not yet executed

**Status**: SCOPED 2026-06-03, deferred. **Do NOT execute until the precondition below holds.**
**Goal (operator, verbatim)**: *"Everything must point to Go only, no Python."*

## Context & why "later"

The Go orchestrator ([[../go-orchestrator/...]], branch `feat/go-orchestrator`) is a
complete, validated, backend-only replacement for the Python orchestrator. Today both
coexist on purpose: Python is still the deployed/prod backend and the **rollback path**;
Go is proven but **not yet cut over on dev05** (Phase 8). This plan removes Python once
Go is the running backend in prod, so the repo becomes Go-only and nothing "points to
Python" anymore.

**Hard precondition (gate):** execute Phase 8 first — push `office-convert:go` to ECR,
hard-swap on dev05, and let it hold (≥ a few days, clean). Only then run this plan.
Deleting Python before that removes the rollback with nothing proven in prod.

## Target state — "Go-only", explicitly

After this plan, the repo contains exactly:
- **Orchestrator**: `cmd/` + `internal/` (Go) — the *only* backend. `net/http`+chi, slog,
  testify/go-cmp/rapid. Built by a single `Dockerfile` (the renamed `go.Dockerfile`).
- **Workers**: `worker_cpp/` + `vendor/aspose/` (C++ — unchanged, Go shells out to them).
- **Deploy**: `deploy/helm/` (image swap, unchanged).
- **Frontend**: `office_convert_ui/` (Streamlit) — **see the one open decision below.**
- No `office_convert/`, no Python tests, no Python Dockerfiles, no `pyproject.toml`.

## ⚠️ The one open decision — the Streamlit UI is Python

`office_convert_ui/` is a **Python (Streamlit)** app. It is the *frontend*, not the
orchestrator — it talks to the backend over HTTP and is backend-agnostic. So "no Python"
has two readings; pick one before executing:

- **(A) Keep the UI as-is (recommended).** Retire only the Python *orchestrator/backend*.
  The UI stays Python/Streamlit. "Go-only **backend**", which is the actual migration goal.
  Lowest effort; the UI was never part of the orchestrator rewrite.
- **(B) Truly zero Python — replace the UI too.** Rewrite the Streamlit dashboard as a
  Go-served UI (templates/HTML+JS off the existing `internal/server` embed, or a small SPA).
  This is a **separate, sizeable project** (Streamlit gives a lot for free: upload widget,
  live charts, polling). Scope it on its own; do not fold into this retirement.

This plan assumes **(A)** unless decided otherwise. If (B), add a "UI rewrite" workstream
ahead of the final Python deletion.

## Work breakdown

### 1. Delete (the Python orchestrator + its tooling)
- `office_convert/` — the Python orchestrator package.
- `tests/` — Python unit/property/integration/e2e (Go has its own `internal/**_test.go`).
  - ⚠️ Confirm Go test coverage parity first (it's already broad; the **golden gate** is
    the cross-impl proof). Note any Python test with no Go equivalent and port it before deleting.
- `Dockerfile` (Python prod image) and `Dockerfile.test` (Python test image).
- `scripts/capture_golden.py` — the golden **oracle** is the Python service (see §5).
- `pyproject.toml`, `ruff.toml`, `.python-version`, any `requirements*.txt`.

### 2. Rework so everything points to Go
- **`go.Dockerfile` → `Dockerfile`** (it's the only image now). Update `security.yml`
  (trivy artifact path) and any `-f go.Dockerfile` references.
- **`compose.go.yaml` → fold into `compose.yaml`** (the `office-convert` service builds the
  Go `Dockerfile`, image `office-convert:go`/canonical tag), then delete `compose.go.yaml`.
  Keep the healthcheck = the binary's `healthcheck` subcommand (already Python-free).
- **`Makefile`**: drop the Python targets (`build`, `build-test`, `test`, `test-unit/-property/
  -integration/-e2e`, `lint`, `format`, `format-check`, `typecheck`, `qa`, `corpus`,
  `update-test-badge`, `up`, `down` as Python). Rename the Go targets to canonical names:
  `up-go→up`, `down-go→down`, `build-go→build`, `test-go→test`, `run-go→run`. Make `qa`
  mean Go QA (`go build` + `go vet` + `gofmt -l` + `go test` + `golden-verify`). Keep the
  `chown`-back fix where any container still writes the repo.
- **`.github/workflows/ci.yml`**: remove the `qa` (Python) job; make `go-test` the primary
  job; keep `helm-lint`. (Result: CI = Go + Helm.)
- **`.github/dependabot.yml`**: switch the `pip` ecosystem to `gomod` (+ keep `docker`).
- **`README.md`**: rewrite so the Go service *is* the service (it's currently Python-first
  with an additive Go section). Keep the UI section. Drop the `aspose-total-cpp.tar.gz`
  stale bits while here.
- **`.dockerignore`** / **`.gitignore`**: drop Python-only patterns; the `office-convert:test`
  / pip caches no longer apply.
- **`aidlc-docs/aidlc-state.md`**: update the project tech-stack/“coexistence” notes to
  “Go-only; Python retired (Phase 9)”.

### 3. Keep (verify untouched)
`cmd/`, `internal/`, `worker_cpp/`, `vendor/aspose/`, `go.mod`/`go.sum`, `deploy/helm/`,
`office_convert_ui/` (decision A), `aidlc-docs/` (history).

## 5. Golden parity gate — decide its fate
The gate (`internal/server/golden_test.go` + fixtures) was the migration's cross-impl proof,
and its **oracle is the Python service** (`scripts/capture_golden.py`). Once Python is gone:
- **Keep (recommended for one release):** retain the committed fixtures + `golden_test.go`
  (it runs Go-only, no Python needed) as a frozen regression snapshot of the contract; drop
  only `capture_golden.py` and `make golden-capture` (can no longer regenerate). Document
  that fixtures are frozen at the cutover commit.
- **Retire:** delete the gate entirely — it served its purpose. Cleaner, but you lose the
  contract regression net.

## 6. Cross-repo coordination (image tag)
`classification-service-demo` (`--profile pipeline`) consumes the local tag `office-convert:go`
(`pull_policy: never`). It's already Go, so no functional change — **but** if this plan
renames the canonical tag (e.g. `office-convert:go` → `office-convert:dev`), update
classification's `image:` in lockstep. Recommendation: **keep the `office-convert:go` tag**
to avoid touching the sibling repo.

## 7. Sequencing
1. Phase 8 cutover on dev05 (precondition) — Go holds in prod.
2. Merge `feat/go-orchestrator` → `main` (Go becomes trunk).
3. New branch `chore/retire-python`; execute §1–§2 in reviewable commits
   (delete-python / rework-build-and-ci / docs), each green.
4. PR; verify (§ below); merge.

## 8. Risks & rollback
- **No rollback after deletion.** Mitigation: the gate is Phase 8 holding in prod first;
  the Python backend remains recoverable from git history / the pre-deletion tag.
- **Lost golden oracle** — §5 mitigates (freeze fixtures).
- **Python e2e coverage** (Testcontainers + real Aspose) has no direct Go equivalent yet —
  before deleting `tests/e2e/`, either add a Go e2e or accept manual `make up`/smoke as the
  acceptance check. Track explicitly.
- **UI coupling** — if (B), the UI rewrite must land first or the dashboard breaks.

## 9. Verification (post-execution)
- `grep -ri "python\|office_convert\b\|pyproject\|uvicorn\|fastapi" --include=… .` returns
  only `office_convert_ui/` (decision A) + `aidlc-docs/` history — nothing in build/CI/runtime.
- `make build && make up && make health` → Go backend healthy; UI loads; a real conversion
  per format succeeds.
- `make test` (Go) + `golden-verify` green; CI green (Go + Helm jobs only).
- `deploy-dev` renders + the Helm image is the Go image.
