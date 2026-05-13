# Component Dependencies — Office Converter (Local v1)

## Dependency Matrix

Rows are consumers; columns are providers. ✓ = direct import dependency.
External dependencies (FastAPI, aspose-python, pydantic-settings, qpdf
binary) are listed in a separate section below.

| Consumer ↓ \ Provider →| server | config | orch | planner | aspw | qpdf | cache | license | probe | logging | types |
| ---------------------- | :----: | :----: | :--: | :-----: | :--: | :--: | :---: | :-----: | :---: | :-----: | :---: |
| server                 |        |   ✓    |  ✓   |         |      |      |       |   ✓     |       |   ✓     |   ✓   |
| config                 |        |        |      |         |      |      |       |         |       |         |       |
| orchestrator           |        |   ✓    |      |   ✓     |  ✓   |  ✓   |   ✓   |         |   ✓   |   ✓     |   ✓   |
| chunk_planner          |        |        |      |         |      |      |       |         |       |         |   ✓   |
| aspose_worker          |        |   ✓    |      |         |      |      |       |         |       |   ✓     |   ✓   |
| qpdf                   |        |        |      |         |      |      |       |         |       |         |       |
| cache                  |        |   ✓    |      |         |      |      |       |         |       |         |   ✓   |
| license                |        |        |      |         |      |      |       |         |       |         |       |
| probe                  |        |        |      |         |  ✓   |      |       |         |       |         |   ✓   |
| logging                |        |   ✓    |      |         |      |      |       |         |       |         |       |
| types                  |        |        |      |         |      |      |       |         |       |         |       |

The **C++ worker binary** is not a Python import dependency — it is
an OS-process invocation target spawned by `aspose_worker` via
`asyncio.create_subprocess_exec`. No row in the import matrix.

### Key observations

- **The C++ worker is a separate executable, not a Python module.**
  It shares only the CLI contract (argv + exit codes + stdout JSON
  in probe mode) with the Python side. No Python is loaded inside
  the worker process.
- **`probe` depends on `aspose_worker`** at the process level —
  probe invokes the C++ worker binary in `--mode=probe` through the
  same subprocess machinery as render.
- **`chunk_planner` is leaf-pure.** Depends only on `types`. No I/O,
  no subprocess, no Aspose. This is what enables high-coverage PBT
  on the chunk-planning algorithm.
- **No cycles.** The Python graph is a DAG; `types` is the only
  universal leaf.

## Communication Patterns

| Pattern                          | Used between                                                          | Notes                                    |
| -------------------------------- | --------------------------------------------------------------------- | ---------------------------------------- |
| Synchronous function call        | All in-process component pairs                                        | Plain Python imports                     |
| `asyncio.create_subprocess_exec` | `aspose_worker` ↔ `worker_main` (separate process)                    | RAM-isolated via `prlimit RLIMIT_AS=2G`  |
| `asyncio.create_subprocess_exec` | `qpdf` ↔ `qpdf` binary                                                | Pipe stdout for streaming merge          |
| Async generator yield            | `orchestrator` → `server` (response stream)                           | Streaming PDF bytes through FastAPI      |
| Async generator yield            | `qpdf.concat_streaming` → `orchestrator`                              | Streaming bytes from qpdf stdout         |
| Filesystem                       | Worker (writes chunk PDF) → orchestrator (reads chunk PDF) → qpdf (reads chunk PDF) | Per-request scratch directory       |
| Filesystem                       | `cache` ↔ filesystem cache directory                                  | Atomic write via temp file + rename      |
| `contextvars.ContextVar`         | `logging` propagation of `request_id` across async tasks              | Survives `await` boundaries              |

## Data Flow

### Forward path (success)

```
HTTP multipart body
    ↓ (server buffers to disk)
scratch/<request_id>/input.<ext>
    ↓ (probe via worker subprocess)
ProbeResult
    ↓ (chunk_planner.plan_chunks, pure)
ChunkPlan
    ↓ (orchestrator iterates chunks under asyncio.Semaphore(parallel))
for each Chunk:
    ↓ (cache.get_chunk → hit → skip render)
    ↓ (cache.get_chunk → miss → aspose_worker.render_chunk)
        ↓ (asyncio.create_subprocess_exec with prlimit)
        worker_main loads Aspose, applies license, renders page range
        ↓ (writes chunk PDF to scratch dir)
        ↓ (exit 0 → orchestrator reads chunk path)
    ↓ (cache.put_chunk → atomic write)
chunk PDFs (list of paths in chunk-order)
    ↓ (qpdf.concat_streaming spawns qpdf binary)
async iterator of bytes
    ↓ (orchestrator yields bytes upward)
async iterator of bytes
    ↓ (FastAPI StreamingResponse chunked transfer encoding)
HTTP response body
```

### Failure paths

```
OOM on chunk render
    ↓ (worker exits 137)
aspose_worker raises OOMError
    ↓ (orchestrator catches)
chunk_planner.subdivide(chunk)
    ↓ (returns 2 sub-chunks or empty)
re-dispatch sub-chunks
    ↓ (recursion bounded by single-page floor)
on floor reached → orchestrator raises SubdivisionFloorError
    ↓ (server maps to HTTP 500 with diagnostic JSON body)
```

```
License expired (detected at startup or per-request)
    ↓ (orchestrator/server checks license.is_expired())
LicenseExpiredError
    ↓ (server maps to HTTP 503)
```

```
Input unprocessable (Aspose throws on open)
    ↓ (worker subprocess exits 3 with stderr diagnostic)
aspose_worker raises InputUnprocessableError
    ↓ (orchestrator propagates)
    ↓ (server maps to HTTP 422)
```

## External Dependencies

| Dependency                       | Provided by                          | Purpose                                                   |
| -------------------------------- | ------------------------------------ | --------------------------------------------------------- |
| Python 3.11                      | Docker runtime base image            | Runtime for orchestrator (the C++ worker has no Python)   |
| FastAPI                          | pip                                  | HTTP framework                                            |
| Uvicorn                          | pip                                  | ASGI server                                               |
| pydantic / pydantic-settings     | pip                                  | Settings model + validation                               |
| **Aspose.Total C++**             | Aspose download tarball, installed in C++ builder stage | Document rendering library (linked into worker) |
| **C++ compiler (gcc/clang)**     | apt-get in C++ builder stage          | Builds `office-convert-worker`                            |
| **C++ standard library** (libstdc++) | Debian base in runtime stage     | Runtime dep of the worker binary                          |
| **CMake**                        | apt-get in C++ builder stage          | Build system                                              |
| Hypothesis                       | pip (dev)                            | Property-based testing                                    |
| pytest                           | pip (dev)                            | Test runner                                               |
| qpdf binary                      | apt-get (Debian/Ubuntu base)         | PDF concatenation, streaming merge                        |
| prlimit binary                   | util-linux (always present on Linux) | RAM ceiling enforcement                                   |

**Removed vs the prior design**: `aspose-words` / `aspose-slides` /
`aspose-cells` / `aspose-pdf` Python packages and the .NET runtime
they require. The C++ worker links against Aspose.Total C++ directly
and is invoked as a subprocess from Python.

## Lifecycle Diagram

```mermaid
sequenceDiagram
    participant Operator
    participant Docker
    participant Server as Server (uvicorn)
    participant Caller
    participant Orch as Orchestrator
    participant W as Worker (subprocess)
    participant Q as qpdf (subprocess)

    Operator->>Docker: docker run -v license.lic:/aspose/license.lic ...
    Docker->>Server: uvicorn office_convert.server:app
    Server->>Server: load Settings, configure logging
    Server->>Server: LicenseManager(license_path) [parses .lic XML for expiry]
    Server->>Server: emit server_start event

    Caller->>Server: POST /convert (multipart)
    Server->>Server: acquire max_jobs semaphore
    Server->>Server: buffer body to scratch/<req_id>/input
    Server->>Orch: convert_job(request_id, input_path, options, settings)
    Orch->>W: spawn /usr/local/bin/office-convert-worker --mode=probe (subprocess + prlimit)
    W->>W: SetLicense(); open via Aspose C++ API; emit metadata
    W-->>Orch: ProbeResult JSON on stdout, exit 0
    Orch->>Orch: chunk_planner.plan_chunks(probe)
    loop for each chunk (up to parallel concurrent)
        Orch->>W: spawn worker --mode=render (subprocess + prlimit)
        W->>W: SetLicense(); render page range; write chunk PDF
        W-->>Orch: chunk PDF path, exit 0 (or 137 → subdivide)
    end
    Orch->>Q: spawn qpdf --empty --pages ... -- -
    Q-->>Orch: streaming PDF bytes
    Orch-->>Server: yield bytes (async generator)
    Server-->>Caller: streamed PDF response (chunked transfer encoding)
    Server->>Server: release max_jobs semaphore
    Server->>Server: cleanup scratch/<req_id>/
    Server->>Server: emit request_complete event
```
