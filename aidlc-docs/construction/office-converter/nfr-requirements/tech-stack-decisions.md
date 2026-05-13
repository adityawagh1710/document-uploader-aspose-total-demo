# Tech Stack Decisions — office-converter (Local v1)

Each entry: chosen technology, version pin, role, alternatives
considered, why chosen.

Reflects the Application Design Q1 = B pivot: native C++ worker
linking Aspose.Total C++, invoked as a subprocess from a Python
orchestrator.

## Orchestrator (Python)

### Python 3.11

- **Role**: Implementation language for the orchestrator, HTTP
  server, chunk planner, qpdf wrapper, cache, license parsing,
  logging, types.
- **Version pin**: 3.11 (specific minor version).
- **Alternatives considered**: 3.12 (newer, marginal improvements
  but stick with the broadly-tested 3.11), Go (better p99 but
  verbose; no advantage for I/O-bound orchestrator).
- **Why**: Confirmed in Functional Design Q11c. Mature ecosystem
  match for FastAPI, pydantic, Hypothesis. Stable.

### FastAPI

- **Version pin**: `fastapi==0.115.*`.
- **Role**: HTTP routing, request validation, response streaming.
- **Alternatives considered**: Starlette (lower-level), Flask
  (sync-only), Litestar (newer).
- **Why**: Type-hint-driven, integrates cleanly with the
  qpdf async byte-generator via `StreamingResponse`, `TestClient`
  enables in-process integration tests.

### Uvicorn

- **Version pin**: `uvicorn==0.32.*`.
- **Role**: ASGI server runs `office_convert.server:app` in the
  Docker `CMD`.
- **Alternatives considered**: Hypercorn, Daphne.
- **Why**: Standard FastAPI pairing.

### pydantic-settings v2

- **Version pin**: `pydantic-settings==2.6.*`.
- **Role**: Loads `OFFICE_CONVERT_*` env vars into the `Settings`
  model with validation.
- **Alternatives considered**: `python-decouple`, stdlib
  `os.environ`, `dynaconf`.
- **Why**: Validation rules from `business-rules.md §12` map
  cleanly to Pydantic field constraints.

### asyncio (stdlib)

- **Role**: `asyncio.create_subprocess_exec` for worker and qpdf
  invocation; `asyncio.Semaphore` for concurrency budgets.
- **Why**: No external dependency; matches FastAPI's async-handler
  model.

### Hypothesis

- **Version pin**: `hypothesis==6.*`.
- **Role**: Property-based testing.
- **Configuration**: 500 examples for chunk planner; 100 for
  everything else.

### pytest, pytest-cov, FastAPI TestClient

- **Versions**: `pytest==8.3.*`, `pytest-cov==5.*`.
- **Roles**: Test runner, coverage, in-process HTTP test client.

### Testcontainers (added 2026-05-11)

- **Version pin**: `testcontainers==4.8.*`.
- **Role**: End-to-end test fixtures — bring up the real Docker
  image once per test session, bind-mount a real Aspose license,
  exercise `/convert` over real HTTP via `httpx`.
- **Gating**: tests skipped unless `OFFICE_CONVERT_E2E_LICENSE`
  points at a real `.lic` file. Operator installs via
  `uv pip install -e .[dev,e2e]`.
- **Alternatives considered**: docker-py directly (more manual
  lifecycle code), pytest-docker (less mature).
- **Why**: Fills the coverage gap between in-process integration
  tests (fake worker, no Docker) and manual smoke. Validates the
  Dockerfile, the C++ worker binary linkage, the real Aspose
  license activation, and `prlimit` behavior — none of which
  in-process tests can reach.

### Quality Tooling

- **ruff** (`==0.7.*`) — linting and formatting (one tool).
- **mypy** (`==1.13.*`) — strict-mode type checking.
- **Why**: Modern Python ecosystem standards in 2026; minimal
  tool count.

### uv

- **Version pin**: `uv==0.5.*` (latest stable).
- **Role**: Dependency resolution, lockfile, virtualenv (dev).
- **Why**: Fast (10–100× pip), lockfile-first. `uv.lock` committed.

## Worker (C++)

### C++17

- **Standard**: C++17 (default `-std=c++17`).
- **Role**: Implementation language for `office-convert-worker`
  binary.
- **Alternatives considered**: C++20 (well-supported by gcc-12 but
  unnecessary; C++17 is broadly compatible with Aspose.Total C++
  API examples).
- **Why**: Safest standard for Aspose.Total C++ compatibility;
  modern features (structured bindings, optional, filesystem)
  available; no need for C++20-only features.

### gcc 12 (Debian Bookworm)

- **Version**: whatever `apt install gcc-12 g++-12` ships on
  Debian Bookworm (currently 12.2.x).
- **Role**: Compiles the worker binary in the C++ builder stage.
- **Alternatives considered**: clang (Aspose's official examples
  more frequently demonstrate gcc; either works).
- **Why**: Default on Debian Bookworm, well-tested, broad ABI
  compatibility with Aspose's redistributable `.so`.

### CMake 3.25+

- **Role**: Build system for `worker_cpp/`.
- **Why**: De facto standard for C++ projects; cross-compiler
  support; integrates with most IDEs; Aspose's documentation
  uses CMake examples.

### Aspose.Total C++

- **SKU**: Aspose.Total C++ (the native C++ library SKU; NOT the
  Python-via-.NET distribution).
- **License**: Aspose Temporary License (Aspose.Total C++ scope),
  bind-mounted at runtime per `requirements.md` FR-8.
- **Distribution**: Aspose tarball baked into the build context
  (Q5 default in NFR plan); builder stage extracts headers and
  `.so` to `/opt/aspose-sdk/`; runtime stage receives only the
  `.so` files copied into `/usr/local/lib/aspose/`.
- **Linkage**: dynamic. Worker binary built with
  `-L /opt/aspose-sdk/lib -laspose-words -laspose-slides
  -laspose-cells -laspose-pdf` (or whatever the SDK's library
  names are; verified during Code Generation), with RPATH set
  to `$ORIGIN/../lib/aspose` so the runtime can locate the `.so`
  files without needing `LD_LIBRARY_PATH` (though we set
  `LD_LIBRARY_PATH=/usr/local/lib/aspose` anyway for defense in
  depth).

### GoogleTest (optional)

- **Role**: Unit tests for C++ worker internals (format dispatch,
  license parsing, exit-code translation) if value warrants.
- **Why**: De facto standard for C++ testing; only added if v1 has
  enough non-trivial worker-internal logic to justify it.
  Otherwise integration tests against the running worker binary
  are sufficient.

## Native Binaries (apt-installed in runtime image)

### qpdf

- **Version pin**: whatever Debian Bookworm ships (currently 11.x).
- **Role**: Streaming PDF concatenation per
  `business-logic-model.md §5`.
- **Alternatives considered**: pikepdf (buffers full PDF in
  memory — violates NFR-1), pdftk (older).
- **Why**: Streaming stdout enables chunked-transfer response
  without buffering.

### prlimit (util-linux)

- **Version pin**: whatever Debian Bookworm ships.
- **Role**: Applied via `prlimit --as=2147483648 --` before exec-
  ing the worker binary.
- **Why**: Kernel-enforced 2 GB virtual address space ceiling.

## Container

### Multi-Stage Dockerfile

**Stage 1 — C++ builder**:

- Base: `debian:bookworm`
- apt: `gcc-12 g++-12 cmake make build-essential`
- Operator's Aspose tarball: `COPY aspose-total-cpp.tar.gz` from
  build context, extracted to `/opt/aspose-sdk/`
- C++ source: `COPY worker_cpp/`
- Build: `cmake -S worker_cpp -B build -DASPOSE_SDK=/opt/aspose-sdk &&
  cmake --build build --target office-convert-worker`
- Output: `/build/office-convert-worker` (binary)

**Stage 2 — Runtime**:

- Base: `python:3.11-slim-bookworm`
- apt: `qpdf util-linux libstdc++6 libgcc-s1` (minimal C++
  runtime deps for the worker binary; everything else gets
  COPY-ed from the builder)
- User: `appuser:appgroup` (non-root)
- `uv pip install --frozen --requirement uv.lock` for Python deps
- `COPY --from=builder /build/office-convert-worker → /usr/local/bin/`
- `COPY --from=builder /opt/aspose-sdk/lib/*.so → /usr/local/lib/aspose/`
- `COPY office_convert/ → /app/office_convert/` (Python package)
- `ENV LD_LIBRARY_PATH=/usr/local/lib/aspose:$LD_LIBRARY_PATH`
- `ENV OFFICE_CONVERT_*` defaults
- `USER appuser`
- `CMD ["uvicorn", "office_convert.server:app", "--host", "0.0.0.0", "--port", "8080"]`

**Alternatives considered for Aspose acquisition**:

- A) Download from Aspose CDN at build time — risky for CI/CD
  without credentials story.
- C) Try local tarball first, fall back to download — Dockerfile
  complexity for marginal benefit.
- **B (chosen)**: Bake-in from build context — operator drops the
  tarball next to the Dockerfile. Reproducible builds without
  network calls.

**Why multi-stage**: The C++ compiler, headers, CMake, and Aspose
build dependencies never enter the runtime image. Final image
ships only the worker binary + Aspose `.so` files + Python deps,
keeping it minimal.

## Aspose License Distribution

- **Type**: Aspose Temporary License (Aspose.Total C++ scope),
  per Functional Design Q9.
- **Distribution**: bind-mounted at runtime via
  `-v /path/to/license.lic:/aspose/license.lic`.
- **Path in container**: `ASPOSE_LICENSE_PATH` env var (default
  `/aspose/license.lic`).
- **NOT baked into image**, NOT committed to source. Image works
  with any valid Aspose.Total C++ license file at the mount path.
- **Operator workflow**: request a temp license at
  `purchase.aspose.com/temporary-license`, specifying
  "Aspose.Total for C++". Place the resulting `.lic` file
  somewhere accessible to the container runtime, bind-mount on
  every `docker run`.

## Document Fixture Generation

### Synthetic Corpus (AI-generated)

- **Mechanism**: Generation scripts in `tests/corpus/_generate.py`
  using python-docx, python-pptx, openpyxl, ReportLab. Fixtures
  generated once and checked into source.
- **Why**: User does not need to supply documents; corpus is
  reproducible and version-controllable.
- **Resolution**: Q11a in Functional Design.

## Summary Table

| Layer                         | Choice                                                |
| ----------------------------- | ----------------------------------------------------- |
| Orchestrator language         | Python 3.11                                           |
| Worker language               | C++17                                                 |
| HTTP framework                | FastAPI                                               |
| ASGI server                   | Uvicorn                                               |
| Settings                      | pydantic-settings v2                                  |
| Subprocess                    | asyncio (stdlib)                                      |
| Aspose binding                | Aspose.Total C++ (dynamic-linked `.so`)               |
| Aspose acquisition            | Bake-in via build context (operator tarball)          |
| Aspose .so path               | `/usr/local/lib/aspose/` (RPATH + LD_LIBRARY_PATH)    |
| C++ compiler                  | gcc-12 (Debian Bookworm)                              |
| C++ build system              | CMake 3.25+                                           |
| C++ unit tests                | GoogleTest (optional)                                 |
| PDF concat                    | qpdf binary                                           |
| RAM limit                     | prlimit (util-linux)                                  |
| Python test runner            | pytest                                                |
| PBT                           | Hypothesis                                            |
| HTTP test client              | FastAPI TestClient (in-process) + httpx (e2e)         |
| Coverage                      | pytest-cov                                            |
| E2E test fixtures             | Testcontainers (Docker-driven, gated)                 |
| Python linter+formatter       | ruff                                                  |
| Python type checker           | mypy (strict)                                         |
| Logging                       | stdlib logging + custom JsonFormatter                 |
| Python package manager        | uv                                                    |
| Container builder stage       | debian:bookworm                                       |
| Container runtime stage       | python:3.11-slim-bookworm                             |
| License type                  | Aspose Temporary License (Aspose.Total C++ scope)     |
