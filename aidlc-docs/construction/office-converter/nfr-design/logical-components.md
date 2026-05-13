# Logical Components — office-converter (Local v1)

Runtime components seen through the NFR lens — what they enforce,
what they observe, and where they sit in the request lifecycle.
Complements `application-design/components.md` (which is the
domain view) by naming the infrastructure-level objects that
realize the NFRs.

## Component Inventory

| Logical component                | Module / artifact                            | NFR concern                                        |
| -------------------------------- | -------------------------------------------- | -------------------------------------------------- |
| Server concurrency gate          | `asyncio.Semaphore(max_jobs)` in `server.py` | Performance + reliability                          |
| Job-level concurrency gate       | `asyncio.Semaphore(parallel)` in `orchestrator.py` | Performance                                  |
| Subprocess RAM enforcer          | external `prlimit` binary, wrapped in `aspose_worker.py` | Memory ceiling (hard)                  |
| Streaming response generator     | `async generator` in `orchestrator.py` / `qpdf.py` | Memory (no full PDF buffering)               |
| Tee-to-cache writer              | combined into the streaming generator         | Reliability of cache + atomic write              |
| Request scratch manager          | `scratch_dir / <request_id>` pattern         | Resource cleanup, security (per-request isolation) |
| Atomic-write helper              | `atomic_write()` in `cache.py`               | Reliability (no partial cache files)               |
| Health checker                   | `HealthChecker` in `server.py` (or module)   | Operability                                       |
| Request-ID context propagator    | `ContextVar` + `logging.Filter`              | Observability                                      |
| Structured log formatter         | custom `JsonFormatter` in `logging.py`       | Observability                                      |
| Failure-class translator         | exit-code → exception map in `aspose_worker.py` | Resilience, observability                       |
| License lifecycle monitor        | `LicenseManager` in `license.py`             | Operability + reliability                          |
| Settings loader / validator      | pydantic-settings `Settings` in `config.py`  | Reliability (fail-fast on bad config)              |
| C++ worker (subprocess)          | `office-convert-worker` binary               | Memory isolation, format dispatch                  |
| qpdf streaming process           | `qpdf` binary subprocess                     | Performance (no buffering), correctness            |

## Per-Component Detail

### Server Concurrency Gate

- **What**: `asyncio.Semaphore(settings.max_jobs)` instantiated once
  at server startup; lifetime = server lifetime.
- **How it enforces**: each `POST /convert` attempts
  `wait_for(server_sem.acquire(), timeout=0.001)`. On timeout →
  raise `BusyError`. On success → request proceeds; release in
  `finally`.
- **Why non-blocking**: queuing requests at the server level
  grows the connection backlog and the orchestrator's in-flight
  memory. Failing fast with 503 + `Retry-After` shifts queueing
  to the client (or a load balancer above), which is the right
  place.
- **Observability**: emits `request_rejected_busy` log event with
  current `active_jobs` vs `max_jobs`.

### Job-Level Concurrency Gate

- **What**: a `asyncio.Semaphore(settings.parallel)` instantiated
  per request inside the orchestrator.
- **How it enforces**: chunk-render coroutines acquire it with
  `async with`. When the semaphore is full, additional chunks
  queue (this is fine — they're within one request, bounded by
  the chunk count).
- **Why per-request**: scoping it per-request makes the budget
  composable (no cross-request contention) and the cleanup
  trivial (semaphore goes out of scope when the request ends).
- **Observability**: emits `chunk_render_start` and
  `chunk_complete` events with `parallel_in_use` field.

### Subprocess RAM Enforcer

- **What**: invocation of the external `prlimit` binary, wrapping
  the worker binary path in `aspose_worker.render_chunk`.
- **How it enforces**: `prlimit --as=2147483648 -- <worker-binary>
  <args>`. `RLIMIT_AS` applies after fork/before exec, so the C++
  worker inherits the limit. Kernel SIGKILLs on overrun.
- **Why external prlimit**: asyncio's subprocess API has no
  `preexec_fn` hook. `prlimit` is part of `util-linux`, always
  present on Debian.
- **Observability**: not visible until exit code 137 is observed;
  then logged as `oom_subdivide` event.

### Streaming Response Generator

- **What**: async generator function wrapping the qpdf subprocess.
- **How it enforces NFR-1 (no output buffering)**: yields fixed
  64 KB byte blocks straight from qpdf's stdout pipe; never
  accumulates the full PDF.
- **Wiring**: returned as the iterable parameter of FastAPI's
  `StreamingResponse(content=..., media_type="application/pdf")`.
  FastAPI handles chunked transfer encoding automatically.
- **Backpressure**: HTTP client reading slowly → FastAPI's send
  blocks → generator's yield blocks → qpdf's stdout-write blocks
  → qpdf pauses. End-to-end TCP backpressure.

### Tee-to-Cache Writer

- **What**: a side branch of the streaming generator: each block
  yielded upstream is also written to a per-request temp file.
- **How**: temp file opened at start of generator; written in
  parallel with the yield; closed and fsynced on EOF.
- **Atomicity link**: on successful generator completion, the
  temp file's path is passed to `atomic_write()` which renames
  it into the cache path. On any exception, the temp file is
  unlinked instead.

### Request Scratch Manager

- **What**: a directory `settings.scratch_dir / request_id`
  created at the start of every request, deleted at the end
  (success or failure).
- **How**: standard `Path.mkdir(parents=True, exist_ok=False)`
  on entry; `shutil.rmtree(scratch_dir, ignore_errors=True)` in
  the cleanup `finally` block.
- **Security**: per-request isolation — one request cannot read
  another's scratch contents.

### Atomic-Write Helper

- **What**: `cache.atomic_write(target, source)` function.
- **How**: copy to `<target>.tmp.<pid>.<uuid>`, `fsync`,
  `os.replace()` → target. Cleanup of the tmp file on exception.
- **Properties**: readers never see partial files; orphan tmp
  files survive process kills but are operator-cleaned by cron
  (not implemented in v1).

### Health Checker

- **What**: `HealthChecker` object instantiated at server startup.
- **Static checks (memoized)**: worker binary present, qpdf binary
  present, Aspose `.so` loadable, scratch dir writable.
- **Live checks (every call)**: license expiry from
  `LicenseManager.days_remaining()` and `is_expired()`.
- **Output**: `HealthResponse` dataclass with `ready`,
  `license_days_remaining`, `active_jobs`, `max_jobs`,
  `problems` (list of failure-class strings if any).

### Request-ID Context Propagator

- **What**: `current_request_id: ContextVar[str]` in `logging.py`.
- **How it propagates**: FastAPI middleware sets the ContextVar
  on request entry, resets on exit. asyncio's automatic context
  copying ensures every coroutine launched via `gather`,
  `create_task`, etc. inherits the value.
- **How it surfaces**: `RequestIdFilter(logging.Filter)` reads the
  ContextVar and injects `record.request_id` into every emitted
  log record. The JSON formatter (next) emits it as a field.

### Structured Log Formatter

- **What**: `JsonFormatter(logging.Formatter)` in `logging.py`.
- **How**: `format(record)` returns a single JSON line containing
  `timestamp`, `level`, `event`, `request_id`, and any extra
  fields passed via `logger.info("name", extra={...})`.
- **Why custom**: log schema is small and stable (16 events per
  `business-rules.md §8`); a third-party library like `structlog`
  would add a dependency for marginal gain.

### Failure-Class Translator

- **What**: function `_map_exit_code(rc, stderr_bytes, chunk) ->
  Exception | Path` in `aspose_worker.py`.
- **How**: switch on `rc` returning the typed exception from the
  `domain-entities.md` hierarchy, or returning the chunk PDF path
  on `rc == 0`.
- **Why centralized**: the orchestrator never sees raw exit codes;
  every other module deals in typed exceptions. Aspose's exception
  vocabulary never enters Python.

### License Lifecycle Monitor

- **What**: `LicenseManager` Python object in `license.py`.
- **What it parses**: Aspose `.lic` XML file at the configured
  path. Extracts expiry date from the XML.
- **What it exposes**: `days_remaining()`, `is_expired()`,
  `refresh()` (re-read from disk for hot-swap support).
- **What it does NOT do**: call Aspose's `SetLicense()`. That
  happens inside the C++ worker subprocess (which has Aspose
  loaded; Python does not).

### Settings Loader / Validator

- **What**: pydantic-settings `Settings` model in `config.py`.
- **How it fails fast**: on server startup, `get_settings()` is
  called inside `app.on_event("startup")`; Pydantic validation
  errors abort the process before the first request is served.
- **What it validates**: every constraint in
  `business-rules.md §12` (numeric ranges, path-must-exist,
  enum values).

### C++ Worker (Subprocess)

- **What**: native binary `/usr/local/bin/office-convert-worker`.
- **NFR concerns it owns**:
  - **Memory isolation**: combined with `prlimit`, enforces the
    2 GB ceiling per render. Aspose's `OutOfMemoryException`
    becomes exit 137.
  - **Format dispatch**: knows about DOCX, PPTX, XLSX, PDF and
    routes to the right Aspose namespace.
  - **License application (lazy, per format)**: calls
    `Aspose::<Product>::License::SetLicense()` only for the
    namespace matching `--format`. Saves ~150–600 ms of static
    init + license-validate overhead per invocation vs activating
    all four (see `nfr-design-patterns.md §12`).
  - **Error translation**: catches Aspose-side exceptions and
    exits with the documented codes (1/2/3).
- **Build-time NFR concerns** (see `nfr-design-patterns.md §13`):
  smaller binary via `-O2 -flto -fvisibility=hidden
  -fdata-sections -ffunction-sections` + `--gc-sections` + strip,
  ~30–100 ms faster dynamic-loader resolution per spawn.

### qpdf Streaming Process

- **What**: native binary `qpdf` invoked via
  `subprocess` per merge.
- **NFR concerns it owns**:
  - **Memory**: qpdf's internal implementation is documented to
    stream PDF concatenation; we rely on this to satisfy NFR-1.
  - **Correctness**: page-count round-trip and order preservation
    are verified by PBT (`nfr-requirements.md §9`).

## Cross-Component Interaction (Per-Request Lifecycle)

```
Request arrives
  ├─ middleware sets request_id ContextVar
  ├─ Server Concurrency Gate acquires (non-blocking; 503 on contention)
  ├─ Request Scratch Manager creates scratch/<req_id>/
  ├─ Format detect → buffer input to scratch (1 MB chunks, size enforced)
  ├─ License Lifecycle Monitor.is_expired() — fail-fast → 503
  ├─ Cache get_final → hit → stream from cache, exit
  ├─ Probe via C++ worker (RAM-enforced subprocess)
  ├─ chunk_planner.plan_chunks → ChunkPlan
  ├─ Job Concurrency Gate enforces parallel limit on chunk renders
  │   ├─ for each chunk:
  │   │   ├─ Cache get_chunk → hit → use cached PDF
  │   │   └─ miss → Subprocess RAM Enforcer + C++ Worker
  │   │       └─ on exit 137 → Failure-Class Translator raises OOMError
  │   │           → orchestrator subdivides, recurses
  │   │           → on floor → SubdivisionFloorError
  │   │       on exit 0 → Atomic-Write Helper → cache_chunk
  ├─ Streaming Response Generator + Tee-to-Cache Writer
  │   ├─ yields 64 KB blocks → FastAPI StreamingResponse → HTTP chunked
  │   └─ tees to <scratch>/output.pdf temp file
  ├─ On success: Atomic-Write Helper renames temp → cache final path
  ├─ Request Scratch Manager removes scratch/<req_id>/
  ├─ Server Concurrency Gate releases
  ├─ Structured Log Formatter emits request_complete
  └─ middleware resets request_id ContextVar
```

## Lifecycle of Each Logical Component

| Component                         | Lifetime                                        |
| --------------------------------- | ----------------------------------------------- |
| Settings loader                   | Construction once at startup, immutable after   |
| Server Concurrency Gate           | Server process lifetime                          |
| Health Checker                    | Server process lifetime (static checks at startup, live license per call) |
| License Lifecycle Monitor         | Server process lifetime (file re-read per call) |
| Logging system                    | Server process lifetime                          |
| Structured Log Formatter          | Server process lifetime                          |
| Request-ID Context Propagator     | Per-request via ContextVar (asyncio-scoped)     |
| Job Concurrency Gate              | Per-request (semaphore object scoped to request)|
| Request Scratch Manager           | Per-request (directory created and cleaned up)  |
| Streaming Response Generator      | Per-request (async generator)                   |
| Tee-to-Cache Writer               | Per-request (combined with generator)           |
| Atomic-Write Helper               | Stateless function — called as needed           |
| Failure-Class Translator          | Stateless function — called per chunk           |
| Subprocess RAM Enforcer           | Per chunk (prlimit invocation)                  |
| C++ Worker                        | Per chunk (subprocess, ≤ render duration)       |
| qpdf Streaming Process            | Per request (subprocess, ≤ merge duration)      |
