# Phase 7 — Containerize + Deploy

## What changed

- **`Dockerfile.go`** (new) — the Go image. Three stages:
  1. **C++ builder** — *byte-identical* to the Python `Dockerfile`'s builder
     stage. Compiles the five per-product `office-convert-worker-<fmt>`
     binaries from `vendor/aspose/`. Go shells out to these unchanged.
  2. **Go builder** (`golang:1.26-bookworm`) — `CGO_ENABLED=0 go build
     -trimpath -ldflags="-s -w"` produces a fully static binary. `vendor/`
     (the 4.7 GB Aspose C++ tree) is deliberately **not** copied into this
     stage, so Go never enters vendor mode; `go.sum` drives a reproducible
     build. The dashboard + landing HTML are baked in via `go:embed`.
  3. **Runtime** (`debian:bookworm-slim`) — qpdf + util-linux + LibreOffice +
     fonts + the Aspose `.so` trees + the five worker binaries + the one Go
     binary. **No Python, no uvicorn.** Same non-root user (uid 1000), same
     `/cache`, same `OFFICE_CONVERT_*` ENV defaults, same `EXPOSE 8080`.
     `CMD ["/usr/local/bin/office-convert-orchestrator"]` (the binary handles
     SIGTERM with a graceful shutdown, so no process manager is needed).
- **`Makefile`** — `make build-go` (build the image) and `make test-go` (run
  the Go suite in a golang container; installs `util-linux` for the prlimit
  worker tests). `IMAGE_GO ?= office-convert:go`.
- The Python `Dockerfile` is **kept as-is** so the two images can run
  side-by-side during cutover (Phase 8).

## Helm: no chart change needed

`deploy/helm/.../api-deployment.yaml` references the image as
`{{ .Values.image.repository }}:{{ .Values.image.tag }}`. The Go image has the
**same contract** (port 8080, `GET /health`, the `OFFICE_CONVERT_*` env, the
ConfigMap/Secret mounts, the license at `/aspose/license.lic`). So switching to
Go is a pure **image-only roll** — push the Go image, bump `image.tag` (or
`kubectl set image`). The ConfigMap, probes, resource limits, topology spread,
and `memswap`/RAM posture all apply unchanged.

The container memory ceiling carries over verbatim: `mem_limit 4g` +
`memswap_limit 6g` + `OFFICE_CONVERT_WORKER_RAM_BYTES=6 GiB` (the compose/Helm
value; the Dockerfile's 4 GiB default is overridden at deploy). Fork-after-load
is unchanged — it lives entirely in the C++ workers, which Go spawns identically.

## Build notes / gotchas

- **`-mod=mod` is required for any host-side Go command in this repo.** The
  repo's `vendor/` (Aspose C++ libs) trips Go's vendor-mode auto-detect. The
  Dockerfile's Go stage sidesteps it by not copying `vendor/`; host builds and
  `make test-go` set `GOFLAGS=-mod=mod`. (`go env -w GOFLAGS=-mod=mod` was set
  in the dev environment.)
- **Build context is 4.7 GB** (the Aspose vendor tree, intentionally included
  per `.dockerignore`). Same as the Python image — unavoidable while the C++
  workers are vendored.
- **Footprint reality** (confirms the plan-review finding): the Go binary is a
  ~16 MB static artifact, but the runtime image stays dominated by the Aspose
  `.so` trees (~600 MB+) + LibreOffice. scratch/distroless is impossible
  (LibreOffice + Aspose need a full glibc userland). Net image saving over the
  Python image ≈ the interpreter + wheel layer only, **not** a 10× shrink.

## Validation status

- ✅ Go builder stage builds in `golang:1.26-bookworm` (static binary, embed
  resolves) — validated in a minimal context.
- ⏳ Full `make build-go` (C++ compile + runtime assembly) — runs on a box with
  Docker + `vendor/aspose/` + the license; ~15 min (dominated by the Aspose C++
  compile, identical to the Python image's builder).
- The **golden-fixture parity diff** (Phase 6 exit criterion) should run against
  this image once built — it provides the qpdf + worker binaries the host lacks.

## Cutover handoff (Phase 8)

Build + push `office-convert:go` to ECR, then **shadow-traffic or hard-swap on
dev05 — never a live load-balanced A/B** (the in-memory observability + recent
stores are per-process and would split-brain across the two images; the
dashboard would flicker and cross-service jobs would intermittently vanish).
Run the golden-fixture diff before flipping. Roll back = re-point `image.tag` at
the last Python tag (the chart is identical).
