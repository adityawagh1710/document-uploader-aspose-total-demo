# Technology Stack

> Reverse-engineered 2026-06-12. Versions from `pyproject.toml`, `go.mod`, the Dockerfiles, and
> `worker_cpp/CMakeLists.txt`.

## Programming Languages

- **Python** — 3.12 (`requires-python = ">=3.12,<3.13"`) — orchestrator (prod) + Streamlit UI +
  test corpus + golden-capture script.
- **Go** — 1.26 (`go.mod`; runtime image `golang:1.26-bookworm`) — orchestrator port.
- **C++** — C++17 (gcc-12/g++-12) — Aspose worker binaries.
- **Bash** — deploy/ops scripts (Route53, port-forward, LocalStack init).

## Frameworks

- **FastAPI** `>=0.115,<0.116` (pinned tight to avoid Starlette drift) + **Starlette** + **uvicorn[standard]** `>=0.32,<0.49` — Python HTTP.
- **pydantic** `>=2.9,<2.14` + **pydantic-settings** `>=2.6,<2.15` — models + env config.
- **go-chi/chi** v5.3.0 — Go HTTP router.
- **Streamlit** 1.57.* (+ **plotly** 6.7.*, **pandas** 3.0.*, **requests** 2.34.*) — UI.
- **Aspose.Total for C++** — render engine: Words 26.3, Cells 26.4, Slides 26.4, PDF 26.4,
  Email 25.12 (each + CodePorting Cs2Cpp framework, except Cells which is plain C++).

## Infrastructure

- **Docker** — multi-stage images (C++ builder + Python or Go runtime); `Dockerfile`,
  `go.Dockerfile`, `Dockerfile.test`, `Dockerfile.ui`.
- **Docker Compose** — local stack (`compose.yaml` + `compose.go.yaml` overlay): api, localstack,
  test-ui, tests.
- **Kubernetes / EKS + Helm** (chart v0.1.0) — API + UI Deployments, shared ALB ingress
  (`aws-load-balancer-controller`), ACM HTTPS, IRSA for S3.
- **AWS S3** — optional input/output; **LocalStack** `3.8` locally (host port 4567 → 4566).
- **cgroup v1/v2 + `/proc`** — resource telemetry (no external metrics backend).
- **Base images** — `python:3.12-slim-bookworm` (Python runtime + builders),
  `debian:bookworm` (C++ builder), `debian:bookworm-slim` (Go runtime), `golang:1.26-bookworm`.

## Build Tools

- **hatchling** — Python build backend (`pyproject.toml`).
- **uv** 0.5.* — fast pip installs inside images.
- **Go toolchain** 1.26 — `go build`/`vet`/`test`; **requires `GOFLAGS=-mod=mod`** (vendor/ collision).
- **CMake** ≥3.25 + **gcc-12/g++-12** — C++ workers; Release LTO + `--gc-sections` + strip.
- **GNU Make** — task runner (`Makefile`, ~40k chars).
- **qpdf** (system) — PDF merge + `--show-npages`.
- **LibreOffice** (`libreoffice-core-nogui` + `libreoffice-draw-nogui`) — image/ODG fallback.
- **prlimit** (`util-linux`) — RLIMIT_AS enforcement on workers.

## Testing Tools

- **pytest** 8.3.* (+ **pytest-asyncio** `asyncio_mode=auto`, **pytest-xdist** `-n auto`,
  **pytest-cov** with `--cov-fail-under=80`) — Python tests; `filterwarnings = error`.
- **Hypothesis** 6.* — Python property-based planner tests.
- **moto[s3]** `>=5.0,<6` + **httpx** — S3 mocking + HTTP test client.
- **testcontainers** 4.8.* — Python E2E (real container, license-gated).
- **testify** v1.11.1 (assert/require), **go-cmp** v0.7.0 (semantic JSON diff in golden test),
  **pgregory.net/rapid** v1.3.0 (Go PBT) — Go tests.
- **reportlab** `>=4.4,<4.6` — fake-PDF generation in the test fake-worker shim.

## QA / Security Tooling

- **ruff** 0.7.* — lint (`E,F,I,B,UP,RUF,SIM,PIE,PL`) + format; `target-version py311`, line-length 100.
- **mypy** 1.13.* — `--strict`, scope `office_convert` only.
- **clang-format** — Google base, C++17, 4-space, Aspose-aware include ordering.
- **Trivy** (`aquasecurity/trivy-action@v0.36.0`) — fs scan (CRITICAL/HIGH, blocking,
  ignore-unfixed) + config scan (informational); weekly + PR; SARIF to GitHub code scanning.
- **Dependabot** — pip + docker + github-actions, weekly, grouped.

## Cloud / Runtime Dependencies (Go orchestrator)

- **aws-sdk-go-v2** (core v1.41.9, config, `feature/s3/manager`, `service/s3`) + **smithy-go** v1.26.0.
- Everything else stdlib (`net/http` via chi, `log/slog`, `encoding/json`, `os/exec`).
