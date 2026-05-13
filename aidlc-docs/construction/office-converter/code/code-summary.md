# Code Generation Summary — office-converter (Local v1)

## File Inventory

### Root-level

- `pyproject.toml` — Python package + tool config (Python 3.11, FastAPI,
  Uvicorn, pydantic-settings, Hypothesis, pytest, ruff, mypy).
- `ruff.toml` — Linter + formatter rules. `select = ["E", "F", "I", "B",
  "UP", "RUF", "SIM", "PIE", "PL"]`.
- `.gitignore` — Excludes `.lic`, `aspose-total-cpp*.tar.gz`, cache, scratch,
  build artifacts.
- `.dockerignore` — Excludes test corpus, build artifacts, license files,
  aidlc-docs from the image context.
- `Dockerfile` — Multi-stage: debian:bookworm builder (gcc-12 + cmake +
  Aspose tarball + worker binary build) → python:3.11-slim-bookworm
  runtime (qpdf + util-linux + libstdc++ + uv-installed Python deps +
  COPY --from=builder for worker binary and Aspose `.so` to
  `/usr/local/lib/aspose` + appuser non-root + LD_LIBRARY_PATH +
  CMD uvicorn).
- `README.md` — Operator-facing docs.

### Python package `office_convert/`

| File             | Lines (~) | FR/NFR              | Purpose                                    |
| ---------------- | --------: | ------------------- | ------------------------------------------ |
| `__init__.py`    |        5  | —                   | Package marker + version                   |
| `types.py`       |       80  | FR-5, FR-9          | Frozen dataclasses + enums                 |
| `errors.py`      |      120  | FR-5                | Exception hierarchy (10 classes)           |
| `config.py`      |       60  | NFR-8               | pydantic-settings `Settings`               |
| `logging.py`     |      100  | FR-10               | JSON formatter + request_id ContextVar     |
| `license.py`     |       90  | FR-8                | License XML parser + state classification  |
| `chunk_planner.py`|     220  | FR-3, FR-4, NFR-5   | Pure: adaptive sizing, plan, subdivide, chunk hash |
| `cache.py`       |      100  | FR-7                | CacheManager + atomic_write                |
| `qpdf.py`        |       70  | FR-3, NFR-1         | Streaming concat async generator           |
| `probe.py`       |      120  | FR-1, FR-3, NFR-3   | Magic-byte detect + probe invocation       |
| `probe_lite.py`  |       80  | FR-3, NFR-3         | Metadata-only probe (no Aspose load)       |
| `aspose_worker.py`|     140  | FR-4, FR-6, NFR-1   | Subprocess wrapper, exit-code translation  |
| `worker_pool.py` |      200  | FR-6, NFR-1         | Persistent worker pool (document loaded once) |
| `orchestrator.py`|      260  | FR-3 to FR-7, FR-9  | Per-request async generator pipeline       |
| `server.py`      |      200  | FR-1, FR-2, FR-5    | FastAPI app, routes, middleware            |

### C++ worker `worker_cpp/`

| File                     | Purpose                                            |
| ------------------------ | -------------------------------------------------- |
| `CMakeLists.txt`         | CMake 3.25+ build; per-product `add_aspose_worker()` emitting 4 binaries |
| `error.h` / `error.cpp`  | Exit codes + exception translation                 |
| `license.h`              | `apply_license()` declaration (per-binary definition in formats/) |
| `render.h`               | `RenderArgs` struct + `dispatch_render()` declaration |
| `probe.h`                | `ProbeArgs` struct + `dispatch_probe()` declaration |
| `probe_util.h`           | Header-only: `emit_probe_json()` + `file_size_bytes()` |
| `pool.h`                 | Pool mode: `pool_load()` / `pool_render()` / `pool_loop()` declarations |
| `pool.cpp`               | Pool mode: stdin/stdout JSON event loop + minimal JSON parser |
| `main.cpp`               | argv parsing, mode dispatch (render/probe/pool), exit code mapping |
| `formats/docx.cpp`       | Aspose.Words: render + probe + pool_load/pool_render |
| `formats/pptx.cpp`       | Aspose.Slides: render + probe + pool_load/pool_render |
| `formats/xlsx.cpp`       | Aspose.Cells: render + probe + pool_load/pool_render |
| `formats/pdf.cpp`        | Aspose.Pdf: render + probe + pool_load/pool_render |

### Tests `tests/`

| File                                          | Type            |
| --------------------------------------------- | --------------- |
| `conftest.py`                                 | Fixtures        |
| `unit/test_config.py`                         | Unit            |
| `unit/test_logging.py`                        | Unit            |
| `unit/test_license.py`                        | Unit            |
| `unit/test_chunk_planner.py`                  | Unit            |
| `unit/test_cache.py`                          | Unit            |
| `unit/test_qpdf.py`                           | Unit (real qpdf)|
| `unit/test_probe.py`                          | Unit            |
| `unit/test_aspose_worker.py`                  | Unit (fake bin) |
| `unit/test_orchestrator.py`                   | Unit (mocked)   |
| `property/test_chunk_planner_pbt.py`          | PBT (500)       |
| `property/test_subdivision_pbt.py`            | PBT (100)       |
| `property/test_qpdf_concat_pbt.py`            | PBT (100, real qpdf) |
| `property/test_format_detection_pbt.py`       | PBT (100)       |
| `integration/test_convert_endpoint.py`        | Integration (TestClient) |
| `integration/test_health_endpoint.py`         | Integration     |
| `e2e/conftest.py`                             | Testcontainers fixtures |
| `e2e/test_real_conversion.py`                 | E2E (real container, gated) |
| `corpus/_generate.py`                         | Generator script |
| `corpus/README.md`                            | Generator docs  |
| `corpus/simple.pdf`                           | Fixture (generated) |

OOXML fixtures (small.docx, medium.docx, simple.pptx, complex.pptx,
single_sheet.xlsx, multi_sheet.xlsx) are produced by
`python -m tests.corpus._generate` after `uv sync`.

## Story → File Traceability

| Story    | Implementing files                                              |
| -------- | --------------------------------------------------------------- |
| US-PD-01 | server.py, orchestrator.py, qpdf.py, aspose_worker.py            |
| US-PD-02 | errors.py, server.py (exception handler)                         |
| US-PD-03 | logging.py, server.py (request_id middleware)                    |
| US-PD-04 | server.py (options parsing), orchestrator.py (cache skip)        |
| US-PD-05 | server.py (incremental size check), qpdf.py (streaming)          |
| US-PD-06 | probe.py (detect_format), server.py (head-bytes read)            |
| US-PD-07 | orchestrator.py (subdivide retry), errors.py (SubdivisionFloorError) |
| US-OP-01 | server.py (HealthChecker)                                        |
| US-OP-02 | license.py (classify), server.py (health snapshot)               |
| US-OP-03 | license.py (refresh; per-request re-read)                        |
| US-OP-04 | config.py, server.py (semaphores)                                |
| US-OP-05 | logging.py (event vocabulary), errors.py                         |
| US-OP-06 | Dockerfile (USER appuser, --read-only compat, no caps)           |
| US-OP-07 | Dockerfile (multi-stage with build-context tarball)              |
| US-OP-08 | cache.py, config.py                                              |

## Key Design Decisions Realized in Code

1. **Pure cores, impure shells**: `chunk_planner.py` has no imports of
   I/O modules. PBT tests run against it without a single mock.
2. **Subprocess isolation for memory**: `aspose_worker.PRLIMIT_AS_BYTES`
   is the kernel-enforced ceiling. `prlimit` is invoked externally
   because asyncio's subprocess API has no `preexec_fn`.
3. **Streaming everywhere on the output path**: `qpdf.concat_streaming`
   is an async generator yielding 64 KB blocks. `server.convert` returns
   a `StreamingResponse` that consumes it directly. No intermediate
   buffer.
4. **Tee-to-cache during streaming**: `qpdf.concat_streaming` accepts an
   optional `cache_temp_path`. Bytes flow both upstream (response) and
   to disk (cache temp file). Cache is finalized via atomic rename only
   on successful generator completion.
5. **Failure translation at the worker boundary**: `aspose_worker._map_exit_code`
   is the single function that turns exit codes into Python exceptions.
   The C++ worker's exception classes never appear in Python.
6. **Two-level concurrency**: server-level `asyncio.Semaphore(max_jobs)`
   with non-blocking acquire (503 on contention); per-job
   `asyncio.Semaphore(parallel)` regular `async with`. Peak Aspose RAM
   bounded by `max_jobs × parallel × 2 GB`.
7. **Hybrid health probing**: static checks (worker binary, qpdf, scratch
   dir) memoized at server construction. License live every call (cheap;
   important for 30-day temp).
