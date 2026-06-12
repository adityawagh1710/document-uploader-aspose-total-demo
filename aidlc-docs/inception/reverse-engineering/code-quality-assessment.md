# Code Quality Assessment

> Reverse-engineered 2026-06-12. Assessment is from static inspection of the code, tests, CI
> config, and the AI-DLC state/audit history — not from a fresh test run in this session.

## Test Coverage

- **Overall**: **Good.** Python suite ~218 tests across unit/integration/property/e2e with an
  enforced **80% coverage gate** (`--cov-fail-under=80`, `server.py` omitted from coverage). Go
  port mirrors the suite (unit + rapid PBT + fake-worker integration + httptest + golden gate).
- **Unit Tests**: Good — both languages cover planner, cache, probe, config, rate-limit, license,
  qpdf, csvinput, orchestrator, s3 helpers.
- **Integration Tests**: Good — Python `tests/integration/` exercises `/v1/convert` (all paths),
  health, conversions+stats, rate-limit, S3 flow via `TestClient`; Go uses `httptest`.
- **Property-Based Tests**: Present in both — Hypothesis (Python) and `rapid` (Go) assert the
  planner's complete-cover / no-overlap / subdivision-halving / SHA-determinism invariants.
- **E2E Tests**: Present but **license-gated** — `tests/e2e/` (testcontainers) and `make test-e2e`
  require the real Aspose binaries + license, so they are skipped in CI. Real end-to-end
  conversion (all six formats) has been validated manually on the built images (per state log).
- **Parity gate**: Python→Go **golden-fixture diff is green (14/14)** and runs in CI's `go-test`
  job using committed fixtures (no Python/qpdf needed). This is a notable quality asset — it
  pins wire equivalence across the migration and already caught one real divergence
  (`last_touched` leakage).

## Code Quality Indicators

- **Linting**: Configured and CI-enforced. Python: `ruff check .` + `ruff format --check .`
  (CI runs whole-tree, not file-scoped). Go: `go vet ./...`. C++: `.clang-format` (Google base).
- **Type checking**: `mypy --strict` on `office_convert` (CI-enforced); Go is statically typed.
- **Code Style**: Consistent within each language. Files are generally focused; the two notable
  large files are `office_convert_ui/app.py` (~2.8k lines) and `server.py` — both have documented
  ruff per-file ignores.
- **Documentation**: Strong. Extensive AI-DLC artifacts (`aidlc-docs/`), a large `README.md`,
  `INTEGRATIONS.md`, an operations topology doc, and a detailed `aidlc-state.md`/`audit.md`
  decision trail. Inline rationale comments are dense in the load-bearing spots (CMake ABI fix,
  fork-after-load, XLSX Cells lifecycle).
- **CI/CD**: Three-job CI (qa / go-test / helm-lint) + a security workflow (Trivy fs blocking +
  config informational, weekly + PR) + grouped Dependabot. No automated deploy (manual
  `make deploy-dev`).
- **Security posture**: non-root containers, `cap_drop: ALL`, no-privilege-escalation, read-only
  rootfs (local), tmpfs scratch, S3 bucket allowlists, per-IP rate limiting, RLIMIT_AS memory
  capping, ALB CIDR allowlist. Known residual: ECR images carry upstream-unfixed CVEs (curl,
  expat, libxml2, nss, krb5) deemed unreachable in the threat model; UI runs as root (TODO).

## Technical Debt

- **Transitional dual-orchestrator duplication** — Python and Go implement the same backend.
  Intentional and time-boxed (resolved at Phase 8 cutover + Phase 9 Python retirement), but it is
  real maintenance surface until then (every behavior change must land in both, or be deferred).
- **In-memory, per-process observability stores** (recent/progress/heartbeats/timings) — by
  design single-replica/single-worker. **Tripwire**: any move to multi-replica or multi-uvicorn-
  worker silently splits the dashboard/recent-feed state. This is the load-bearing scaling
  constraint to revisit before horizontal scale-out.
- **XLSX worker is reload-per-render in pool mode** — Cells accumulates render state in a held
  `Workbook`, so each chunk reloads from disk (~24 s setup/worker). Acceptable only while render
  compute dominates; a perf cliff on many-small-chunk workbooks.
- **No swap on EKS** vs 2 GiB swap cushion locally — large inputs (> ~250 MB PPTX-class) can OOM
  in cluster where they survive locally. Environment-dependent behavior.
- **`email` worker pool stubs throw** — pool mode unsupported for email; fine today (email is
  outside the chunking path) but a latent trap if email is ever chunked.
- **Deferred doc drift (per state log)** — some construction-era docs still reference the old
  single-tarball Aspose SDK path / `make verify-sdk`. These RE artifacts supersede them for the
  current-code view.
- **Stale AI-DLC `Project Type: Greenfield`** — corrected by this Reverse Engineering pass
  (workspace is brownfield; the state header predates all the code).

## Patterns and Anti-patterns

- **Good Patterns**:
  - Probe→plan→render→merge pipeline with OOM subdivision — the core resilience design.
  - Per-product worker-binary isolation (the CMake ABI fix) — structurally prevents the
    CodePorting collision rather than papering over it.
  - Fork-after-load COW pooling — keeps peak RAM ≈ 1× the loaded document.
  - Canonical, wire-stable `FailureClass` → HTTP-status mapping shared across languages.
  - Frozen/value domain types; immutability honored (matches the coding-style rule).
  - Explicit `sync.Mutex` in Go obs stores — correct GIL→lock translation, documented in-package.
  - Streaming with deferred status — pre-stream errors still return JSON, not a half-200.
  - Golden-fixture parity gate — high-signal guard for the migration.
- **Anti-patterns / risks**:
  - Shared mutable in-memory stores with a single-process assumption (see debt above).
  - Two very large files (`app.py`, `server.py`) exceeding the 800-line guideline (mitigated by
    documented ignores, but candidates for extraction).
  - Manual deploy path (no GitOps/CD) — deploy correctness rides on `make deploy-dev` discipline.
  - License expiry awareness lives only in the Python/Go layer; the C++ workers will silently
    watermark on expiry (mitigated by the health-check pre-gate).

## Overall

A mature, well-tested, well-documented service with a clear and defensible architecture. The
dominant quality concern is **temporal**: the dual-orchestrator overlap and the single-process
observability assumption are both deliberate and tracked, but they are the two things a new
contributor must understand before changing behavior or scaling out.
