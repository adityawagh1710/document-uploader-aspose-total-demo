# NFR Design Patterns — office-converter (Local v1)

Concrete implementation patterns realizing the NFR requirements
under the Python orchestrator + native C++ worker architecture.

## 1. Subprocess Isolation via `prlimit` Wrapper (Resilience + Security)

**Pattern**: external `prlimit` CLI wraps every worker invocation;
the kernel enforces `RLIMIT_AS` after exec.

**Why this form**: asyncio's `create_subprocess_exec` does not
support `preexec_fn` (only `subprocess.Popen` does, and that's
blocking). The external wrapper is the simplest mechanism
compatible with the async event loop.

**Sizing (revised 2026-05-12)**: `RLIMIT_AS` MUST be ≥
container `memswap_limit` (which is RAM + swap *total*) when
swap is enabled. `RLIMIT_AS` is *virtual* address space, which
includes swapped-out pages — if it's smaller than the cgroup
budget, the worker hits `ENOMEM` from `malloc()` before the
kernel ever pages out, and the swap cushion is dead weight. The
production compose deployment sets `mem_limit: 4g`,
`memswap_limit: 6g`, and `OFFICE_CONVERT_WORKER_RAM_BYTES=6g`
so all three line up (= 4 GiB RAM + 2 GiB swap cushion).

**Implementation sketch**:

```python
async def render_chunk(chunk: Chunk, ...) -> Path:
    argv = [
        "prlimit", f"--as={settings.worker_ram_bytes}", "--",
        "/usr/local/bin/office-convert-worker",
        "--mode", "render",
        "--input", str(input_path),
        "--page-range", f"{chunk.page_range[0]}-{chunk.page_range[1]}",
        "--output", str(output_path),
        "--format", format_name,
        "--license-path", str(settings.license_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_bytes, _ = await asyncio.gather(
        proc.stderr.read(),
        proc.wait(),
    )
    return _map_exit_code(proc.returncode, stderr_bytes, chunk)
```

**Properties**:

- `RLIMIT_AS` applied before exec → C++ worker inherits the cap.
- Kernel SIGKILLs the worker on allocation overrun → exit 137 →
  orchestrator subdivides (per `business-rules.md §2`).
- No Python-side resource limits leak into siblings or parent.

## 2. Streaming Response Generator (Performance + Memory)

**Pattern**: async generator wraps the qpdf subprocess; yields
fixed-size byte blocks; FastAPI's `StreamingResponse` consumes
the generator and emits HTTP/1.1 chunked transfer encoding.

**Tee-to-cache variant** (when `options.cache` is true): the
generator additionally writes each yielded block to a per-request
temp file. On successful generator completion, the temp file is
atomically renamed into the cache.

**Implementation sketch**:

```python
async def concat_streaming(
    chunk_paths: list[Path],
    cache_temp_path: Path | None,
) -> AsyncIterator[bytes]:
    argv = ["qpdf", "--empty", "--pages", *map(str, chunk_paths), "--", "-"]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    cache_handle = cache_temp_path.open("wb") if cache_temp_path else None
    try:
        while True:
            block = await proc.stdout.read(65536)   # 64 KB
            if not block:
                break
            if cache_handle:
                cache_handle.write(block)
            yield block
        if cache_handle:
            cache_handle.flush()
            os.fsync(cache_handle.fileno())
    finally:
        if cache_handle:
            cache_handle.close()

    stderr_bytes = await proc.stderr.read()
    rc = await proc.wait()
    if rc != 0:
        raise MergeError(rc, stderr_bytes[-1024:].decode("utf-8", errors="replace"))
```

**Properties**:

- Memory footprint per request bounded by `65 KB` (the read block
  size) + Python's internal `StreamingResponse` overhead.
- Backpressure: if the HTTP client reads slowly, the qpdf pipe
  fills, qpdf blocks on `write()`, no buffer grows unbounded.
- Cache-on-success: rename happens only if the generator finishes
  cleanly. Partial writes never leak into the cache.

## 3. Multipart Upload Buffering (Memory + Reliability)

**Pattern**: FastAPI's `UploadFile` handles spool-to-disk
automatically. The orchestrator copies the spooled tempfile to
the per-request scratch directory in a single `async` copy.

**Implementation sketch**:

```python
@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    options: str = Form("{}"),
):
    request_id = uuid.uuid4().hex
    scratch_dir = settings.scratch_dir / request_id
    scratch_dir.mkdir(parents=True, exist_ok=False)

    # Magic-byte detection on the first 512 bytes, BEFORE buffering
    head = await file.read(512)
    fmt = detect_format(head)         # raises UnsupportedFormatError
    await file.seek(0)

    input_path = scratch_dir / f"input.{fmt}"
    async with aiofiles.open(input_path, "wb") as dest:
        size = 0
        while chunk := await file.read(1024 * 1024):    # 1 MB chunks
            size += len(chunk)
            if size > settings.max_input_bytes:
                raise InputTooLargeError(size, settings.max_input_bytes)
            await dest.write(chunk)
    ...
```

**Properties**:

- Format check before disk write avoids buffering bad inputs.
- 1 MB read chunks balance syscall count vs memory.
- Size limit enforced incrementally → fail-fast for oversized
  inputs without filling the disk.

## 4. contextvars Propagation Across `asyncio.gather` (Observability)

**Pattern**: a single `ContextVar[str]` (`current_request_id`) is
set at the request entry point. asyncio automatically copies
context into spawned tasks. A custom `logging.Filter` injects the
ContextVar value into every log record.

**Implementation sketch**:

```python
# In logging.py
current_request_id: ContextVar[str] = ContextVar("request_id", default="-")

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id.get()
        return True

# In server.py
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = uuid.uuid4().hex
    token = current_request_id.set(rid)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        current_request_id.reset(token)
```

**Test guarantee**: a Hypothesis test spawns 10 fake chunk-render
coroutines via `asyncio.gather`; asserts every emitted log line
carries the parent context's `request_id`. Catches accidental
context resets in future code.

## 5. Worker stderr Capture (Reliability)

**Pattern**: concurrent drain of stderr via `asyncio.gather` while
awaiting process exit. Avoids the classic pipe-full deadlock.

Already shown in §1 above:

```python
stderr_bytes, _ = await asyncio.gather(
    proc.stderr.read(),
    proc.wait(),
)
```

`proc.stderr.read()` reads until EOF; `proc.wait()` waits for exit.
Both complete when the process exits and closes its stderr.

The captured bytes attach to the `RenderError.stderr_tail` field;
last 1 KB is preserved (full content goes to logs if larger).

## 6. Atomic Cache Write (Reliability)

**Pattern**: write to a temp path in the same directory, `fsync`,
then `os.replace()`. POSIX-atomic on same-filesystem renames; also
works on Windows for portability of the dev environment.

**Implementation sketch**:

```python
def atomic_write(target: Path, source: Path) -> None:
    """Copy source's contents into target atomically."""
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        shutil.copyfile(source, tmp)
        with tmp.open("rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
```

**Properties**:

- Readers see either the old file or the new file, never partial.
- `fsync` before replace gives durability on the underlying disk.
- Cleanup of the tmp file on any exception (KeyboardInterrupt
  included via `BaseException`).
- Orphaned `.tmp.*` files from process kills survive; operator
  cleanup cron is documented but not implemented in v1.

## 7. Hybrid Health Probing (Operational UX)

**Pattern**: filesystem and binary presence checks happen once at
server startup and the result is memoized. License expiry is
recomputed on every `/health` call.

**Implementation sketch**:

```python
class HealthChecker:
    def __init__(self, settings: Settings, license_mgr: LicenseManager):
        self._settings = settings
        self._license = license_mgr
        # Static checks at construction (startup)
        self._static_problems: list[str] = []
        if not Path("/usr/local/bin/office-convert-worker").exists():
            self._static_problems.append("worker_binary_missing")
        if not shutil.which("qpdf"):
            self._static_problems.append("qpdf_missing")
        if not settings.scratch_dir.exists() or not os.access(settings.scratch_dir, os.W_OK):
            self._static_problems.append("scratch_dir_unwritable")
        if not _aspose_so_loadable():
            self._static_problems.append("aspose_so_unloadable")

    def check(self) -> HealthResponse:
        # Live check: license
        days = self._license.days_remaining()
        license_expired = self._license.is_expired()
        problems = list(self._static_problems)
        if license_expired:
            problems.append("license_expired")
        ready = not problems
        return HealthResponse(
            ready=ready,
            license_days_remaining=days,
            problems=problems,
            ...
        )
```

**Trade-off**: if an operator deletes the worker binary while the
server is running, `/health` continues reporting `ready: true`
until restart. Acceptable v1 limitation; documented.

## 8. Concurrency Budget Enforcement (Performance + Reliability)

**Pattern**: two stacked `asyncio.Semaphore` instances. Server-
level acquires non-blocking (returns 503 on contention); per-job
level uses regular `async with` (queues if exhausted).

**Implementation sketch**:

```python
# At server startup
server_sem = asyncio.Semaphore(settings.max_jobs)

@app.post("/convert")
async def convert(...):
    try:
        await asyncio.wait_for(server_sem.acquire(), timeout=0.001)
    except asyncio.TimeoutError:
        raise BusyError(retry_after_seconds=60)
    try:
        job_sem = asyncio.Semaphore(settings.parallel)
        async def render_one(chunk):
            async with job_sem:
                return await render_chunk_with_retry(chunk, ...)
        results = await asyncio.gather(*(render_one(c) for c in plan.chunks))
        ...
    finally:
        server_sem.release()
```

**Properties**:

- Server-level non-blocking acquire turns saturation into 503
  with `Retry-After` rather than queuing (avoids unbounded
  request memory growth).
- Per-job sem uses normal `async with` semantics: chunks queue
  when concurrency is full but ordering is preserved.

## 9. Failure-Class Translation at Worker Boundary (Resilience)

**Pattern**: worker exit codes are the only contract crossing the
process boundary. Orchestrator's `aspose_worker.render_chunk`
translates exit codes to typed Python exceptions; the
`failure_class` taxonomy from `business-rules.md §3` flows from
those exceptions to the HTTP response.

Already implemented as the exit-code map in §1 (above).

**Properties**:

- Aspose's internal exception hierarchy is invisible to the
  Python side. Aspose version changes that rename exceptions
  don't break the orchestrator.
- The C++ worker is the *single* place that touches Aspose
  errors, making it the natural unit for C++ tests (optional
  GoogleTest layer).

## 10. License File Mid-Flight Resilience (Reliability)

**Pattern**: no pre-flight check, no file watcher. The worker
attempts to read the license file at each invocation. If gone,
exits 2. Orchestrator raises `LicenseExpiredError`. Request
returns 503. Next `/health` call surfaces the issue (license
file missing manifests as license_manager raising on parse →
treated as expired).

**Trade-off**: a handful of requests may see 503 between the
operator removing the license and the operator noticing (via
`/health` polling or alerting). Accepted v1 behavior.

## 11. Container Security Layering (Security)

**Pattern**: multi-stage Dockerfile + non-root user + read-only
root + cap-drop. Each layer is independently defensible.

- **Build-time**: compiler, headers, Aspose SDK never enter
  runtime image.
- **Runtime image**: minimal apt deps, `appuser:appgroup`, no
  capabilities granted (operator passes `--cap-drop=ALL`).
- **Filesystem**: image is compatible with `--read-only` root;
  operator passes `--tmpfs /tmp --tmpfs /var/run` for the scratch
  directory and pid file.
- **License**: bind-mounted read-only; never copied into the
  image.

## 12. Lazy Aspose Product Activation (Performance, added 2026-05-11)

**Pattern**: the C++ worker activates only the Aspose product matching
its `--format` argument, not all four. Implemented in
`worker_cpp/license.cpp` as four per-product helpers
(`apply_docx_license`, `apply_pptx_license`, `apply_xlsx_license`,
`apply_pdf_license`) selected by a single switch in
`apply_license(path, format)`.

**Why**: each Aspose product has non-trivial static initialization
plus `SetLicense()` overhead (~50–200 ms each). A worker invoked with
`--format=docx` doesn't need Slides/Cells/Pdf state. Activating only
the needed product saves ~150–600 ms per worker invocation,
compounded across every chunk render.

**Trade-off accepted**: a worker invoked with one format cannot
subsequently render another without re-applying the license. Fine
because workers are one-shot — a single `--format` per invocation by
design (Q2 in app-design).

**Failure-fast pre-check**: `verify_license_file()` does a cheap
`fopen()` on the license path BEFORE touching any Aspose API. A
missing or unreadable license raises `LicenseException` (exit 2)
without paying the cost of loading even one Aspose namespace.

## 13. Compiler/Linker Optimizations (Performance, added 2026-05-11)

**Pattern**: Release builds of the C++ worker use aggressive
size-and-startup optimizations declared in
`worker_cpp/CMakeLists.txt`:

```cmake
add_compile_options(
    $<$<CONFIG:Release>:-O2>
    $<$<CONFIG:Release>:-flto>
    $<$<CONFIG:Release>:-fvisibility=hidden>
    $<$<CONFIG:Release>:-fvisibility-inlines-hidden>
    $<$<CONFIG:Release>:-fdata-sections>
    $<$<CONFIG:Release>:-ffunction-sections>
)
add_link_options(
    $<$<CONFIG:Release>:-flto>
    $<$<CONFIG:Release>:-Wl,--gc-sections>
    $<$<CONFIG:Release>:-Wl,-s>
)
```

**Why**: each chunk render spawns a fresh worker process. A smaller
binary with fewer dynamic symbols resolves faster in the dynamic
loader. Effects:

- `-O2 -flto`: link-time optimization across translation units
- `-fvisibility=hidden`: symbols not in dynamic export table by default
- `-fdata-sections + -ffunction-sections + --gc-sections`: per-symbol
  section assignment + strip unreferenced sections at link time
- `-Wl,-s`: strip debug symbols from the final binary

**Combined effect**: typically 10–30% binary size reduction; ~30–100 ms
saved per dynamic-loader resolution on every spawn.

## 14. End-to-End Test Layer via Testcontainers (Testability, added 2026-05-11)

**Pattern**: a session-scoped Testcontainers fixture brings up the
real Docker image once per test session, bind-mounts a real Aspose
license, exposes `/convert` over real HTTP. In-process tests stay
fast; the e2e suite layers on top for what in-process can't reach.

**Coverage gained over in-process tests**:

- Dockerfile correctness (apt deps, ENV, USER, LD_LIBRARY_PATH)
- Real C++ worker binary linkage (Aspose `.so` symbols resolvable)
- Real Aspose render + license activation
- Real qpdf concat at real PDF sizes
- Real `prlimit RLIMIT_AS=2G` behavior under OOM

**Gating**: `OFFICE_CONVERT_E2E_LICENSE` env var must point at a real
`.lic`. CI without that env var runs only in-process tests.

**Dual-mode design**: rendering tests accept either HTTP 200 (real
Aspose linked) or HTTP 500 `render_failed` (scaffolded worker without
Aspose SDK). The Docker plumbing is verified even before Aspose is
fully wired in; once wired, the same tests transition to verifying
real conversion fidelity without code changes.

## 15. Determinism Verification (Maintainability + Testability)

**Pattern**: PBT properties from `nfr-requirements.md §9` are
codified as test files: one per surface (chunk planner, qpdf
concat, subdivision, format detection). Hypothesis examples
counts configured per surface (500 for chunk planner, 100
elsewhere).

**Configuration sketch**:

```python
# tests/test_chunk_planner.py
@settings(max_examples=500, deadline=timedelta(seconds=5))
@given(probe=probe_strategy(), max_pages=integers(1, 100), max_mb=integers(1, 1000))
def test_chunks_cover_input(probe, max_pages, max_mb):
    plan = plan_chunks(probe, max_pages, max_mb)
    assert sum(c.pages for c in plan.chunks) == probe.page_count
    ...
```

PBT failures are reproducible: Hypothesis seeds and shrinks the
failing input, which the test artifact carries. CI archives the
Hypothesis database so test runs can re-replay failed seeds.

## 16. Adaptive Chunk Sizing (Performance, added 2026-05-13)

**Pattern**: the chunk planner computes optimal pages-per-chunk per
request based on probe data and runtime constraints, rather than using
a static default.

**Why**: a static `max_pages_per_chunk=10` caused excessive subprocess
spawns for large files. Each subprocess pays a fixed cost (process
start + full document load + license validation) regardless of how
many pages it renders. Larger chunks amortize that fixed cost over
more useful work.

**Algorithm** (`chunk_planner.adaptive_max_pages()`):

```python
def adaptive_max_pages(probe, worker_ram_bytes, parallel, ...):
    # 1. Estimate per-page rendered RAM cost
    per_page_rendered_mb = (probe.size_bytes / probe.page_count * AMPLIFICATION[format]) / MB

    # 2. Compute how many pages fit in the per-worker RAM budget
    #    (with 75% safety margin and subprocess overhead subtracted)
    render_budget_mb = worker_ram_bytes * 0.75 - SUBPROCESS_OVERHEAD_MB[format]
    pages_by_ram = render_budget_mb / per_page_rendered_mb

    # 3. Ensure enough chunks to fill parallelism slots
    pages_by_parallelism = probe.page_count / max(MIN_CHUNKS, parallel)

    # 4. Take the smaller, clamp to [min_floor, max_ceiling]
    return clamp(min(pages_by_ram, pages_by_parallelism), 10, 200)
```

**Properties**:

- Small files (< parallel × min_floor pages): 1–2 chunks, minimal
  spawn overhead.
- Large files: chunks sized to fill all parallel slots without
  exceeding RAM budget.
- Per-format overhead constants account for Aspose product init +
  full document load cost.
- Static config value acts as ceiling (operator override).
- OOM subdivision retry remains the safety net for miscalculation.

**Trade-off**: the 75% safety margin is conservative. Files with
unusually high amplification (e.g., PPTX with embedded video) may
still OOM, but the subdivision path catches them.

## 17. Worker Process Pool (Performance, added 2026-05-13)

**Pattern**: persistent worker processes that load the document once
and render multiple page ranges on demand, eliminating per-chunk
spawn + document-load overhead.

**Why**: in the one-shot model, each chunk spawns a new process that
re-loads the entire input file. For a 200 MB DOCX with 10 chunks,
that's 10 × (500 ms spawn + 3 s document load) = 35 seconds of pure
overhead. A persistent worker loads once (3 s) and renders all 10
chunks with only the render cost per chunk.

**Protocol** (line-delimited JSON over stdin/stdout):

```
→ {"cmd": "load", "input": "/path/to/file", "license_path": "/path/to/lic"}
← {"status": "ok", "page_count": N}

→ {"cmd": "render", "page_start": 1, "page_end": 50, "output": "/tmp/chunk-0.pdf"}
← {"status": "ok", "output": "/tmp/chunk-0.pdf"}

→ {"cmd": "quit"}
← (process exits)
```

**Implementation**: `office_convert/worker_pool.py` provides
`WorkerPool` (async context manager) and `PooledWorker`. The
orchestrator uses the pool when `OFFICE_CONVERT_POOL_MODE=1` is set
and falls back to one-shot mode otherwise.

**Properties**:

- Document loaded once per pool (not per chunk).
- Pool is per-request (one document per pool lifetime).
- Pool size = min(parallel, chunk_count) — no idle workers.
- Graceful shutdown via `{"cmd": "quit"}` + timeout + kill.
- Error responses map to the same typed exceptions as one-shot mode.

**Trade-off**: requires C++ worker to implement `--mode=pool` (a
read-eval-render loop). Until then, the one-shot fallback is used.
Pool workers hold the full document in memory for their lifetime
(acceptable because pools are per-request and short-lived).

## 18. Per-Format Page-Range Slicing (Performance + Correctness, completed 2026-05-13)

**Pattern**: all four C++ worker binaries now implement real page-range
subsetting, enabling parallel chunked rendering for every format.

| Format | API used | Notes |
|--------|----------|-------|
| DOCX | `PdfSaveOptions::set_PageSet(PageSet(indices[]))` | 0-based indices; implemented since v1 |
| PPTX | `Presentation::Save(path, slides[], SaveFormat::Pdf, opts)` | 1-based slide indices; added 2026-05-13 |
| XLSX | `PdfSaveOptions::SetPageIndex/SetPageCount` | 0-based; added 2026-05-13 |
| PDF | `Document::get_Pages()->idx_get(i)` + `output->get_Pages()->Add(page)` | 1-based; fast-path for full-range; added 2026-05-13 |

**Properties**:

- The chunk planner no longer forces single-chunk for any format.
- All formats benefit from parallel rendering on multi-core hosts.
- Per-format minimum chunk floors prevent excessive spawns where
  per-subprocess overhead is high (XLSX: 1500 pages, PPTX: 25 slides).


## 19. Fontconfig Cache Pre-generation (Reliability, added 2026-05-13)

**Pattern**: install `fontconfig` + `fonts-dejavu-core` at build time and
run `fc-cache -f` to pre-generate the font cache. At runtime, mount
`/var/cache/fontconfig` as tmpfs and set `HOME=/tmp` so fontconfig can
write its runtime cache updates.

**Why**: Aspose Words/Slides use fontconfig for font resolution during
page layout. With `read_only: true` on the container, fontconfig couldn't
write its cache, causing repeated errors and eventually a heap corruption
crash (SIGABRT exit -6) after 15+ minutes.

**Properties**:

- Font cache pre-generated at build time → first worker spawn is fast
- tmpfs mount allows runtime cache updates without violating read-only root
- `HOME=/tmp` ensures user-level fontconfig cache (`~/.cache/fontconfig`)
  resolves to a writable location
- `fonts-dejavu-core` provides actual font files for Aspose to use

## 20. Exact PPTX Slide Count from ZIP Structure (Performance, added 2026-05-13)

**Pattern**: count `ppt/slides/slide*.xml` entries in the PPTX ZIP
directory listing to get the exact slide count without loading the
presentation into Aspose.

**Why**: `docProps/app.xml` may not contain a `<Slides>` element (common
for files created by non-Microsoft tools). The previous fallback was a
full Aspose probe (loads entire presentation, 15+ minutes for large files).
The ZIP directory listing gives the exact count in microseconds.

**Implementation**:

```python
def _pptx_slide_count_from_zip(path: Path) -> int | None:
    with zipfile.ZipFile(path) as z:
        count = sum(
            1 for name in z.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        return count if count > 0 else None
```

**Probe priority chain for PPTX**:
1. `docProps/app.xml` `<Slides>` element (instant, may be absent)
2. ZIP directory slide count (instant, always accurate)
3. Size-based estimate (instant, conservative fallback)
4. Full Aspose probe (never reached for valid PPTX files)
