# Business Logic Model — office-converter (Local v1)

Technology-agnostic description of the algorithms and workflows that
implement the chunk-render-merge pipeline. Reads as a specification:
each section can be implemented in any language. Infrastructure
specifics (asyncio call shapes, FastAPI wiring) belong in NFR Design.

## 1. End-to-End Request Workflow

```
RECEIVE multipart/form-data with file + options JSON
  → DETECT format by magic bytes (first 512 bytes)
  → REJECT (400) if not in {docx, pptx, xlsx, pdf}

ACQUIRE server-level concurrency slot (semaphore over max_jobs)
  → REJECT (503 busy) if exhausted

BUFFER input to scratch_dir/<request_id>/input.<ext>
  → REJECT (400 input_too_large) if size > 1 GB

CHECK license expiry
  → REJECT (503 license_expired) if past expiry date

COMPUTE source_sha256 over the buffered input file

IF options.cache:
  hit = cache.get_final(source_sha256)
  IF hit: STREAM cached PDF to response, RETURN 200

INVOKE worker subprocess in --probe mode → ProbeResult
  → REJECT (422 input_unprocessable) on worker exit code 3

PLAN chunks: chunk_planner.plan_chunks(probe_result) → ChunkPlan

FOR EACH chunk in ChunkPlan (up to `parallel` concurrent):
  chunk_sha = chunk_sha256(chunk, source_sha256)
  IF options.cache AND cache.get_chunk(chunk_sha) exists:
    use cached chunk PDF, record cache hit
  ELSE:
    RENDER via worker subprocess (see §3)
    IF cache: cache.put_chunk(chunk_sha, chunk_pdf)

MERGE: qpdf concat → byte stream
  (tee to <scratch>/<request_id>/output.pdf if cache enabled)

STREAM merged bytes to HTTP response via chunked transfer encoding
  ↓ as each qpdf-stdout chunk arrives, yield to FastAPI response

ON COMPLETION (success):
  IF cache enabled: cache.put_final(source_sha256, output.pdf)
  Cleanup scratch_dir/<request_id>/
  Release server-level concurrency slot
  Emit request_complete event

ON FAILURE (any class):
  Build Diagnostic{request_id, failure_class, detail}
  Map to HTTP status per business-rules.md §3
  Cleanup scratch_dir/<request_id>/
  Release server-level concurrency slot
  Emit request_failed event with diagnostic
```

## 2. Chunk Planning Algorithm

### 2.1 Memory Cost Estimation (`estimate_mb`)

For a chunk covering pages `[start, end]` of a document with
`total_pages` pages, `input_size_bytes` bytes, and format
`f ∈ {docx, pptx, xlsx, pdf}`:

```
pages_in_chunk = end - start + 1

# Pro-rated estimate: assume uniform per-page weight in input,
# amplified by Aspose's format-specific factor.
est_mb = (input_size_bytes / total_pages)
       × pages_in_chunk
       × amplification[f] / (1024 × 1024)
```

Constants in business-rules.md §1.

**Accepted limitation**: this formula assumes uniform per-page
weight. A 1-MB DOCX with a 50-MB embedded image on page 5 may
under-estimate that page's memory cost; the subdivision-on-OOM
retry path (§4) is the safety net.

### 2.2 Natural Seams Per Format

`ProbeResult.natural_seams: list[tuple[int, int]]`:

- **DOCX**: list of `(start_page, end_page)` ranges between section
  breaks. Single-section documents have an empty list.
- **PPTX**: empty list. The chunk planner treats every page (slide)
  boundary as a usable seam directly from `page_count`.
- **XLSX**: list of `(start_page, end_page)` ranges per sheet.
- **PDF**: empty list (no semantic seams).

### 2.3 Hybrid Split Strategy

```
function plan_chunks(probe, max_pages=10, max_mb=50):
  IF probe.natural_seams is empty:
    RETURN page_range_split(probe, max_pages, max_mb)

  candidate = group_seams_by_bounds(probe.natural_seams,
                                    max_pages, max_mb)
  largest_chunk_mb  = max(c.estimated_mb for c in candidate)
  largest_chunk_pgs = max(c.pages for c in candidate)

  IF largest_chunk_mb ≤ 1.5 × max_mb
     AND largest_chunk_pgs ≤ 1.5 × max_pages:
    RETURN candidate     # seam plan is balanced enough
  ELSE:
    RETURN page_range_split(probe, max_pages, max_mb)
```

### 2.4 Page-Range Split (Fallback)

```
function page_range_split(probe, max_pages, max_mb):
  chunks = []
  cursor = 1
  WHILE cursor ≤ probe.page_count:
    # Greedy: grow chunk until either bound is reached.
    chunk_end = cursor
    WHILE chunk_end < probe.page_count
          AND estimate_mb(cursor, chunk_end + 1, probe) ≤ max_mb
          AND (chunk_end + 1 - cursor + 1) ≤ max_pages:
      chunk_end += 1
    chunks.append(Chunk(index=len(chunks), page_range=(cursor, chunk_end),
                        natural_seam=False))
    cursor = chunk_end + 1
  RETURN ChunkPlan(chunks, probe.page_count, total_estimated_mb)
```

### 2.5 Seam-Grouped Split

```
function group_seams_by_bounds(seams, max_pages, max_mb):
  # Same greedy growth, but boundaries are pinned to seam edges.
  chunks = []
  remaining_seams = list(seams)
  WHILE remaining_seams:
    group = [remaining_seams.pop(0)]
    WHILE remaining_seams
          AND combined_pages(group + [remaining_seams[0]]) ≤ max_pages
          AND combined_mb(group + [remaining_seams[0]]) ≤ max_mb:
      group.append(remaining_seams.pop(0))
    chunks.append(merge_seams(group, natural_seam=True))
  RETURN chunks
```

### 2.6 Determinism

For a given `(probe, max_pages, max_mb)`, `plan_chunks` produces
identical output across runs. PBT (NFR-6) asserts:

- `sum(c.pages for c in chunks) == probe.page_count`
- chunks form a non-overlapping monotonic cover of `1..page_count`
- `all(c.estimated_mb ≤ max_mb × balance_factor for c in chunks)`
  where `balance_factor` is 1.0 for the fallback path and 1.5 for
  the seam path.

## 3. Render Pipeline (Per Chunk)

```
function render_chunk(chunk, input_path, format, settings):
  output_path = scratch/<request_id>/chunk-<chunk.index>.pdf

  argv = [
    "prlimit", "--as=2147483648", "--",
    "/usr/local/bin/office-convert-worker",      # native C++ binary
    "--mode", "render",
    "--input", input_path,
    "--page-range", f"{chunk.start}-{chunk.end}",
    "--output", output_path,
    "--format", format,
    "--license-path", settings.license_path,
  ]

  exit_code = await subprocess_exec(argv, timeout=300s)
  CASE exit_code OF:
    0   : RETURN output_path
    137 : RAISE OOMError(chunk)
    2   : RAISE LicenseExpiredError(read license expiry from worker stderr)
    3   : RAISE InputUnprocessableError(stderr_tail)
    1   : RAISE RenderError(chunk, stderr_tail)
    OTHER: RAISE RenderError(chunk, stderr_tail, exit_code)
  END

function render_with_retry(chunk, ...):
  TRY: RETURN render_chunk(chunk, ...)
  CATCH OOMError:
    sub_chunks = chunk_planner.subdivide(chunk)
    IF sub_chunks is empty:
      RAISE SubdivisionFloorError(chunk)
    pdfs = [render_with_retry(c, ...) for c in sub_chunks]
    # The orchestrator collects these and orders them; subdivision
    # produces sequential page ranges so order is preserved.
    RETURN merge_subchunk_pdfs(pdfs)    # in-memory only for ≤ chunk's pages
```

## 4. Subdivision Algorithm

```
function subdivide(chunk: Chunk) -> list[Chunk]:
  start, end = chunk.page_range
  span = end - start + 1
  IF span ≤ 1:
    RETURN []                          # floor reached
  half = ceil(span / 2)
  return [
    Chunk(index=chunk.index,    page_range=(start, start + half - 1),
          natural_seam=False),
    Chunk(index=chunk.index+0.5, page_range=(start + half, end),
          natural_seam=False),
  ]
```

Indices use fractional values to denote sub-chunks; the orchestrator
records subdivision retries but the final assembled chunks are
re-indexed in concat order before qpdf invocation.

**Termination proof:** Page-span strictly decreases by at least half
on each subdivide call. Floor is span = 1 (returns `[]`). Worst case:
log₂(10) ≈ 4 recursion levels for a 10-page initial chunk.

## 5. Merge Pipeline

```
function concat_streaming(chunk_paths: list[Path], cache_path: Path | None):
  ordered_paths = sort_by_chunk_index(chunk_paths)
  argv = ["qpdf", "--empty", "--pages"] + ordered_paths + ["--", "-"]
  process = await subprocess_exec(argv, stdout=PIPE, stderr=PIPE)

  IF cache_path is not None:
    cache_file = open(cache_path + ".tmp", "wb")

  WHILE chunk_bytes = await process.stdout.read(64 KB):
    IF cache_file: cache_file.write(chunk_bytes)
    YIELD chunk_bytes

  exit_code = await process.wait()
  IF cache_file:
    cache_file.close()
    IF exit_code == 0:
      atomic_rename(cache_path + ".tmp", cache_path)
    ELSE:
      delete(cache_path + ".tmp")

  IF exit_code != 0:
    RAISE MergeError(exit_code, read_tail(process.stderr, 1024))
```

The merge function is an async generator. The HTTP response handler
iterates over it, writing each yielded byte block directly into the
chunked-transfer response body.

## 6. License Expiry State Machine

```
state classify_license(days_remaining):
  IF days_remaining is None      : RETURN PERMANENT
  IF days_remaining > 7          : RETURN HEALTHY
  IF days_remaining ≥ 4          : RETURN WARN
  IF days_remaining ≥ 1          : RETURN CRITICAL
  IF days_remaining == 0         : RETURN EXPIRING_TODAY
  RETURN EXPIRED
```

Behavior per state (called at each `/convert` invocation and at
`/health`):

| State          | Log on /convert         | /health.ready | /convert outcome              |
| -------------- | ----------------------- | ------------- | ----------------------------- |
| PERMANENT      | none                    | true          | proceed normally              |
| HEALTHY        | DEBUG once              | true          | proceed normally              |
| WARN           | WARN once               | true          | proceed normally              |
| CRITICAL       | ERROR once              | true          | proceed normally              |
| EXPIRING_TODAY | ERROR once              | false         | proceed normally              |
| EXPIRED        | ERROR once              | false         | reject 503 license_expired    |

## 7. Cache Atomicity Protocol

Write protocol (used by `cache.put_chunk` and `cache.put_final`):

```
function atomic_write(target_path, source_path):
  tmp_path = f"{target_path}.tmp.{pid}.{uuid4()}"
  copy_file(source_path, tmp_path)
  fsync(tmp_path)
  os.rename(tmp_path, target_path)        # POSIX atomic within fs
```

Read protocol (used by `cache.get_chunk` and `cache.get_final`):

```
function read(target_path):
  IF target_path exists:
    RETURN target_path
  RETURN None
```

Atomic-rename guarantees readers see either the old file or the new
file, never a partial one. Orphaned `.tmp.*` files from crashed
writers are cleaned by an operator-side cron (not v1 service).

## 8. Hung-Render Handling

Per-chunk render is wrapped in a hard timeout (configurable via
`OFFICE_CONVERT_CHUNK_TIMEOUT_SECONDS`, default 300 s):

```
function render_chunk_with_timeout(chunk, ...):
  TRY (timeout=chunk_timeout_seconds):
    RETURN await render_chunk(chunk, ...)
  CATCH TimeoutError:
    kill subprocess (SIGTERM)
    AFTER 5 seconds:
      kill subprocess (SIGKILL)
    RAISE RenderError(chunk, "timeout exceeded", exit_code=-1)
```

Timeout-induced failures count as render failures (HTTP 500
`render_failed`), not as OOMs — they do NOT trigger subdivision
retry because the failure cause is not memory pressure.

## 9. Input Format Validation

```
function detect_format(magic_bytes: bytes) -> Literal["docx", "pptx", "xlsx", "pdf"]:
  IF magic_bytes[0:5] == b"%PDF-":
    RETURN "pdf"
  IF magic_bytes[0:4] == b"PK\x03\x04":
    # OOXML. Inspect content types to disambiguate.
    # The orchestrator reads ahead into the stream to find
    # [Content_Types].xml, parses it, and looks for:
    #   wordprocessingml → docx
    #   presentationml   → pptx
    #   spreadsheetml    → xlsx
    RETURN inspect_ooxml_content_types(magic_bytes_and_subsequent)
  RAISE UnsupportedFormatError(detected_magic=magic_bytes[0:8].hex())
```

Validation happens before the input is buffered to disk: server reads
the first 512 bytes from the multipart stream into memory, runs
detection, rejects if unsupported. On supported format, server then
buffers the full request body to disk and proceeds.

## 10. Failure Translation at Worker Boundary

The worker subprocess catches all Aspose exceptions and exits with
documented codes (see business-rules.md §4). The orchestrator's
`aspose_worker.render_chunk` reads the exit code and raises typed
Python exceptions:

```
function exit_code_to_exception(exit_code, chunk, stderr_tail):
  CASE exit_code:
    0   : (no exception — success path)
    1   : RAISE RenderError(chunk, stderr_tail, exit_code)
    2   : RAISE LicenseExpiredError(parse_expiry_from(stderr_tail))
    3   : RAISE InputUnprocessableError(stderr_tail)
    137 : RAISE OOMError(chunk)
    -SIGTERM, -SIGKILL : RAISE RenderError(chunk, "killed by orchestrator timeout")
    other: RAISE RenderError(chunk, f"unknown exit code {exit_code}", stderr_tail)
```

The translation is the single source of truth for failure
classification. Aspose's internal exception type names never appear
in orchestrator code; only exit codes cross the subprocess boundary.

## 11. Concurrency Coordination

### 11.1 Server-Level

```
server_semaphore = Semaphore(settings.max_jobs)

async function handle_request(request):
  IF NOT server_semaphore.acquire_nowait():
    RETURN 503 busy + Retry-After header
  TRY:
    RETURN await convert_job(request)
  FINALLY:
    server_semaphore.release()
```

### 11.2 Per-Job (Chunk Parallelism)

```
job_semaphore = Semaphore(settings.parallel)

async function dispatch_all_chunks(plan):
  tasks = [render_with_semaphore(c, job_semaphore) for c in plan.chunks]
  results = await gather(tasks)
  RETURN results
```

`gather` ordering preserves chunk-index order in `results` because
`gather` returns results in the same order as input tasks; the
qpdf concat then receives chunk PDFs in plan order.

### 11.3 Subdivision and Parallelism

When a chunk subdivides, its sub-chunks are scheduled through the
same per-job semaphore. They don't bypass the concurrency budget;
they queue up like any other chunk. This is intentional: it prevents
a single OOM cascade from spawning unbounded parallel sub-renders.
