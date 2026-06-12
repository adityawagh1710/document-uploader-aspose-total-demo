# Component Inventory

> Reverse-engineered 2026-06-12.

## Application Packages

- **`office_convert/`** (Python, ~6.3k LOC, 25 modules) — FastAPI orchestrator. **Current
  production backend.** Owns HTTP contract + conversion pipeline.
- **`cmd/orchestrator` + `internal/*`** (Go, ~6.1k LOC, 18 internal packages) — chi orchestrator.
  Behavior-parity **port of `office_convert/`, merged to `main`, pre-cutover** (Phase 8 pending).
- **`worker_cpp/`** (C++17) — the render engine. Five per-product Aspose worker binaries
  (`office-convert-worker-{docx,pptx,xlsx,pdf,email}`) sharing `main.cpp`/`pool.cpp`/`error.cpp`.
- **`office_convert_ui/`** (Python/Streamlit, `app.py` ~2.8k lines) — operator/demo front-end,
  backend-agnostic (talks only to the HTTP contract).

## Infrastructure Packages

- **`deploy/helm/office-convert/`** (Helm v2 chart, version 0.1.0) — API + UI Deployments,
  Services, two Ingress (shared ALB), ConfigMap (`OFFICE_CONVERT_*`), ServiceAccount (IRSA).
  Overlay `values-classification-fanout.yaml` enables cross-service S3 fanout.
- **`deploy/iam/`** — IRSA role policy + trust policy JSON for S3 access.
- **`deploy/localstack/init-buckets.sh`** — creates `office-convert-in` / `office-convert-out`.
- **`deploy/scripts/`** — Route53 upsert/delete, port-forward, EKS VPN routes, resource tagging.
- **`Dockerfile`** (Python prod), **`go.Dockerfile`** (Go prod), **`Dockerfile.ui`** (Streamlit),
  **`Dockerfile.test`** (test runner).
- **`compose.yaml`** + **`compose.go.yaml`** — local stack (api / localstack / test-ui / tests).
- **`Makefile`** — build/test/qa/run/deploy automation incl. the 8-step EKS pipeline.
- **`.github/workflows/`** — `ci.yml` (qa + go-test + helm-lint), `security.yml` (Trivy fs+config);
  `.github/dependabot.yml` (pip / docker / github-actions, weekly, grouped).

## Shared Packages

- **`vendor/aspose/{Words,Cells,Slides,PDF,Email}/`** — vendored Aspose C++ product `.so` trees +
  CMake configs (the render-engine binaries; not Go's vendor dir).
- **C++ shared TUs**: `worker_cpp/{main,pool,error}.cpp` + headers (`render.h`, `probe.h`,
  `probe_util.h`, `timing_util.h`, `license.h`) compiled into every worker binary.
- **`Aspose.TotalforC++.lic`** — umbrella license unlocking all five products (bind-mounted at runtime).
- **`internal/types` & `internal/oerrors` (Go) / `types.py` & `errors.py` (Python)** — the shared
  domain-type + failure-taxonomy contract bridging orchestrator and workers.

## Test Packages

- **`tests/unit/`** (Python, ~155 tests) — module-level unit tests (planner, cache, probe, config,
  rate_limit, license, qpdf, orchestrator, s3_client, ...).
- **`tests/integration/`** (Python, ~45 tests) — HTTP route tests via `TestClient` incl. S3 flow.
- **`tests/property/`** (Python, ~13 tests, Hypothesis) — planner invariants, format detection,
  qpdf concat, subdivision.
- **`tests/e2e/`** (Python, ~5 tests, testcontainers) — real conversion against a live container
  (license-gated, skipped in CI without binaries).
- **`tests/corpus/`** + **`tests/conftest.py`** — fixture generation + the reportlab fake-worker shim.
- **Go tests** (`*_test.go` across `internal/*` + `cmd/`) — unit + `rapid` PBT (`planner/pbt_test.go`),
  fake-worker integration (`worker/pool_test.go` + `worker/testdata/fakeworker`), httptest server
  tests, and the **golden parity gate** (`server/golden_test.go` + `testdata/golden/`).
- **`scripts/capture_golden.py`** — freezes Python HTTP responses into Go golden fixtures.
- **`smoke_test/`** — Aspose.Words license/SDK smoke (`Dockerfile.smoke`, `words_smoke.cpp`).

## Total Count

- **Total Packages/Components**: 4 application + ~8 infra units + shared (Aspose vendor + C++
  shared TUs + domain contract) + 6 test groupings.
- **Application**: 4 (Python orchestrator, Go orchestrator, C++ workers, Streamlit UI).
- **Infrastructure**: Helm chart, 4 Dockerfiles, 2 compose files, Makefile, 2 GH workflows +
  dependabot, IAM, LocalStack init, deploy scripts.
- **Shared**: Aspose vendor trees + license, C++ shared TUs/headers, the types+errors contract.
- **Test**: unit, integration, property, e2e, corpus, Go test suite (+ golden gate), smoke.

> **Note on duplication**: the Python and Go orchestrators are **transitional duplication, not a
> hybrid** — one backend implemented twice. End-state (Phase 8 cutover + Phase 9 retirement) is
> Go-only; `office_convert/` is then deleted (or kept briefly as a tagged rollback oracle).
