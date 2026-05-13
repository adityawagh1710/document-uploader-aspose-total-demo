# Components — Office Converter (Local v1)

Flat-module Python package. Each component is a single module file unless
noted. Detailed business logic (chunk-planning algorithm, subdivision
policy) is deferred to Functional Design.

| # | Component         | Module                          | Purpose                                                    |
| - | ----------------- | ------------------------------- | ---------------------------------------------------------- |
| 1 | server            | `office_convert/server.py`      | FastAPI app, HTTP routing, request lifecycle, response streaming |
| 2 | config            | `office_convert/config.py`      | Pydantic-settings model loading `OFFICE_CONVERT_*` env vars |
| 3 | orchestrator      | `office_convert/orchestrator.py`| Per-request job logic: probe → plan → dispatch → merge     |
| 4 | chunk_planner     | `office_convert/chunk_planner.py`| Pure logic: produce ChunkPlan from ProbeResult; subdivide  |
| 5 | aspose_worker     | `office_convert/aspose_worker.py`| Orchestrator-side wrapper that spawns worker subprocess with `prlimit` |
| 6 | worker (C++)      | `worker_cpp/` → built binary `/usr/local/bin/office-convert-worker` | Native C++ render binary linking Aspose.Total C++; spawned by `aspose_worker` as subprocess |
| 7 | qpdf              | `office_convert/qpdf.py`        | Wrapper for streaming PDF concat via `qpdf` binary         |
| 8 | cache             | `office_convert/cache.py`       | Get/put for final + per-chunk PDFs, keyed by SHA-256 and Aspose version |
| 9 | license           | `office_convert/license.py`     | Load `.lic`, expose expiry helpers, apply to Aspose runtime |
| 10| probe             | `office_convert/probe.py`       | Aspose-based input introspection: page count, format, natural seams |
| 11| logging           | `office_convert/logging.py`     | JSON-lines + human formatters; `request_id` context propagation |
| 12| types             | `office_convert/types.py`       | Shared dataclasses: ChunkPlan, Chunk, ProbeResult, ConversionResult, Diagnostic |

## Component Responsibilities

### 1. server

- Define FastAPI `app` with two routes: `POST /convert`, `GET /health`.
- Parse multipart upload, buffer input to a per-request scratch path.
- Enforce `--max-jobs` semaphore across concurrent requests.
- Stream the response body from the orchestrator's async generator
  output directly into the HTTP chunked-transfer response.
- Map orchestrator exceptions to HTTP status codes per FR-5.
- Generate `request_id` UUID on request arrival, set as response
  header `X-Request-ID`, bind to logging context.

### 2. config

- Define a single `Settings` Pydantic-settings model with all runtime
  configuration: `max_jobs`, `parallel`, `cache_dir`, `license_path`,
  `scratch_dir`, `log_format`, `log_level`, `aspose_version`.
- Reads `OFFICE_CONVERT_*` env vars; provides validation and defaults.
- Exposed via `get_settings()` (LRU-cached).

### 3. orchestrator

- Top-level async coroutine that owns a single conversion request.
- Coordinates: cache lookup → probe → chunk planning → per-chunk
  render dispatch (parallel within `--parallel`) → subdivision retry
  on OOM → qpdf streaming concat → cache write → completion logging.
- Emits structured progress events to the logging component.
- Yields PDF bytes via async generator (consumed by the server's
  streaming response).

### 4. chunk_planner

- **Pure functions only** (no I/O, no subprocess, no Aspose calls).
- `plan_chunks(probe_result, max_pages, max_mb) -> ChunkPlan`:
  produce deterministic chunk plan using hybrid natural-seam-or-
  page-range strategy.
- `subdivide(chunk) -> list[Chunk]`: produce 2 sub-chunks (halving
  page range) or empty list if chunk is at the single-page floor.
- Pure functional design enables high-coverage property-based tests
  (NFR-6).

### 5. aspose_worker

- Orchestrator-side wrapper around the worker subprocess.
- `render_chunk(chunk, input_path, scratch_dir, request_id)`: spawns
  `prlimit --as=2147483648 -- office-convert-worker --input ...` via
  `asyncio.create_subprocess_exec`; awaits exit; maps documented exit
  codes to typed exceptions (`OOMError`, `RenderError`, `LicenseError`).
- Returns `Path` to the chunk PDF on success.

### 6. worker (C++ binary `office-convert-worker`)

- **Compiled C++ binary** built in a Dockerfile builder stage,
  linking the Aspose.Total C++ shared library. Shipped at
  `/usr/local/bin/office-convert-worker` in the runtime image.
- Source lives in `worker_cpp/` (CMake project): `main.cpp`,
  `render.cpp`, `probe.cpp`, `license.cpp`, format-specific
  dispatch.
- Parses argv: `--input`, `--page-range`, `--output`, `--format`,
  `--license-path`, `--mode={render|probe}`.
- Calls Aspose.Total C++ `Aspose::<Product>::License::SetLicense()`,
  **lazily for the requested format only** (saves ~150–600 ms of
  static init + SetLicense overhead per invocation; see
  `nfr-design-patterns.md §12`).
- Renders the requested page range to a chunk PDF via the
  format-specific Aspose namespace (`Aspose::Words`,
  `Aspose::Slides`, `Aspose::Cells`, `Aspose::Pdf`).
- Catches Aspose `OutOfMemoryException` → exits with code 137.
  Kernel SIGKILL on RLIMIT_AS overflow also produces exit
  status 137 — both cases handled identically by the
  orchestrator (subdivide).
- Catches other render exceptions → exits with code 1 and writes
  diagnostic JSON to stderr.
- License-invalid → exits with code 2.
- Input unprocessable (corrupt, encrypted) → exits with code 3.
- Successful render → exits 0. In `--mode=probe`, writes
  `ProbeResult` JSON to stdout instead of a PDF, exits 0.

**No Python is loaded inside the worker.** The C++ binary's only
runtime dependencies are: glibc, the Aspose.Total C++ shared
library, libstdc++.

### 7. qpdf

- `concat_streaming(chunk_paths)`: async generator that spawns
  `qpdf --empty --pages <list> -- -`; pipes stdout to async byte
  iterator; awaits process exit and raises on non-zero return.
- The merged PDF is NEVER materialized in memory or on disk on the
  orchestrator side; bytes flow directly from qpdf stdout to the
  HTTP response body.

### 8. cache

- `CacheManager` class instantiated with `cache_dir: Path | None` and
  `aspose_version: str`.
- `cache_dir = None` → all get/put are no-ops (cache disabled).
- Keys include Aspose version: `<cache_dir>/<aspose_version>/final/<sha>.pdf`
  and `<cache_dir>/<aspose_version>/chunks/<sha>.pdf`.
- `get_final(sha) / put_final(sha, path)`,
  `get_chunk(sha) / put_chunk(sha, path)`.
- No automatic eviction in v1 (operator deletes when too large).

### 9. license

- `LicenseManager` Python class instantiated with `license_path: Path`.
- `days_remaining() -> int | None`: parse `.lic` XML, compute days
  to expiry. None if license file format doesn't expose expiry.
- `is_expired() -> bool`.
- Python's `LicenseManager` ONLY parses XML for expiry checking; it
  does NOT call any Aspose API (Aspose isn't loaded in the Python
  process). The C++ worker binary applies the license itself via
  Aspose's C++ `SetLicense()` API at every subprocess start.

### 10. probe

- `probe(input_path, format, scratch_dir) -> ProbeResult`: runs the
  worker binary in `--probe` mode (re-uses subprocess isolation +
  prlimit). Worker opens the document with Aspose using
  `LoadOptions::TempFolder` + `MemoryOptimization`, reads page count
  and natural seams, exits with JSON metadata on stdout.

### 11. logging

- `configure(format, level)`: install root handler emitting JSON-lines
  by default; human-readable when `format="human"`.
- `request_context(request_id)`: context manager binding `request_id`
  to a `contextvars.ContextVar` so it appears in every subsequent log
  line in the same async task.
- Standard event vocabulary: `server_start`, `request_received`,
  `chunk_complete`, `subdivision_retry`, `cache_hit`, `cache_miss`,
  `request_complete`, `request_failed`.

### 12. types

- Shared dataclasses used across modules:
  - `Chunk(index: int, page_range: tuple[int, int], natural_seam: bool)`
  - `ChunkPlan(chunks: list[Chunk], total_pages: int, estimated_mb: int)`
  - `ProbeResult(page_count: int, format: str, natural_seams: list[tuple[int, int]], size_bytes: int)`
  - `ConversionOptions(cache: bool, log_level: str)`
  - `Diagnostic(request_id: str, failure_class: str, detail: dict)`
  - `ConversionResult(chunks_rendered: int, subdivision_retries: int, cache_hits: int, duration_seconds: float)`

## Component Interfaces (boundary contracts)

| Component       | Exposes                                                       | Depends on (modules)                                  |
| --------------- | ------------------------------------------------------------- | ----------------------------------------------------- |
| server          | FastAPI `app`                                                 | orchestrator, config, logging, license (read-only)    |
| config          | `Settings`, `get_settings()`                                  | (stdlib + pydantic-settings)                          |
| orchestrator    | `convert_job(...)` async generator                            | probe, chunk_planner, aspose_worker, qpdf, cache, logging, types |
| chunk_planner   | `plan_chunks(...)`, `subdivide(...)` pure functions           | types                                                 |
| aspose_worker   | `render_chunk(...)`                                           | types, logging, config                                |
| worker (C++)    | binary `/usr/local/bin/office-convert-worker`                 | Aspose.Total C++ shared library, glibc, libstdc++ (no Python in the worker process) |
| qpdf            | `concat_streaming(...)`                                       | (none other than asyncio + stdlib)                    |
| cache           | `CacheManager`                                                | config, types                                         |
| license         | `LicenseManager`                                              | (stdlib XML parsing)                                  |
| probe           | `probe(...)`                                                  | aspose_worker (re-uses worker subprocess), types      |
| logging         | `configure(...)`, `request_context(...)`                      | config                                                |
| types           | dataclasses                                                   | (stdlib only)                                         |
