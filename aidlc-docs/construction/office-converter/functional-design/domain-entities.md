# Domain Entities — office-converter (Local v1)

Frozen dataclasses (and a few enums) that constitute the domain
model. These cross module boundaries; their invariants are
load-bearing. Implementation lives in `office_convert/types.py`
per Application Design.

## Entity Diagram

```
ProbeResult ──┐
              ├─► ChunkPlan ─► [Chunk, Chunk, …]
              │
              │   ConversionOptions (caller input)
              │
              └─► ConversionResult (output metadata)
                  Diagnostic        (failure metadata)

LicenseManager (object) ─► LicenseState (enum, computed)
```

## ProbeResult

Output of the probe step. Used by `chunk_planner.plan_chunks`.

```python
@dataclass(frozen=True)
class ProbeResult:
    page_count: int
    format: Literal["docx", "pptx", "xlsx", "pdf"]
    natural_seams: tuple[tuple[int, int], ...]
    size_bytes: int
```

**Field semantics:**

| Field            | Description                                                                  |
| ---------------- | ---------------------------------------------------------------------------- |
| `page_count`     | Total number of pages (slides for PPTX; rendered pages for XLSX)             |
| `format`         | Detected format (from magic-byte detection at server, confirmed by Aspose)   |
| `natural_seams`  | List of `(start_page, end_page)` per natural seam (§Q8 in plan). Empty tuple if format has no useful seams (PDF, single-section DOCX, all PPTX, single-sheet XLSX). |
| `size_bytes`     | Size of the buffered input file in bytes                                     |

**Invariants:**

- `page_count ≥ 1`
- `size_bytes ≥ 1`
- Every `(start, end)` in `natural_seams`: `1 ≤ start ≤ end ≤ page_count`
- Seams are non-overlapping and monotonic: for adjacent seams `s_i, s_{i+1}`: `s_i.end < s_{i+1}.start`
- Seams cover the document: if non-empty, `seams[0].start = 1` and `seams[-1].end = page_count`

**Construction sources:**

- Built by `office_convert.probe.probe()` from worker `--probe` mode
  JSON output.

## Chunk

A planning unit. One Chunk = one Aspose subprocess invocation.

```python
@dataclass(frozen=True)
class Chunk:
    index: int
    page_range: tuple[int, int]
    natural_seam: bool
```

**Field semantics:**

| Field          | Description                                                          |
| -------------- | -------------------------------------------------------------------- |
| `index`        | Monotonic index within the plan, used for ordering in concat. Subdivided chunks may use non-integer indices internally; orchestrator re-indexes to monotonic integers before concat. |
| `page_range`   | `(start, end)` inclusive, 1-based page numbers                       |
| `natural_seam` | True iff this chunk's boundary aligns with a natural seam (from `ProbeResult.natural_seams`); False if the chunk came from page-range fallback or subdivision |

**Invariants:**

- `page_range[0] ≥ 1`
- `page_range[0] ≤ page_range[1]`
- `pages_in_chunk = page_range[1] - page_range[0] + 1` is well-defined
  and ≥ 1

**Derived properties (computed, not stored):**

- `pages`: `page_range[1] - page_range[0] + 1`
- `estimated_mb`: computed via `business_rules §1.1/§1.2` formula
  given the source `ProbeResult`

## ChunkPlan

The output of `chunk_planner.plan_chunks`. Ordered, complete, non-
overlapping cover of `1..page_count`.

```python
@dataclass(frozen=True)
class ChunkPlan:
    chunks: tuple[Chunk, ...]
    total_pages: int
    estimated_mb: int
```

**Invariants** (verified by PBT):

- `sum(c.pages for c in chunks) == total_pages`
- Chunks are sequential: `chunks[i].page_range[1] + 1 == chunks[i+1].page_range[0]`
  for all valid `i`
- `chunks[0].page_range[0] == 1`
- `chunks[-1].page_range[1] == total_pages`
- `all(c.estimated_mb ≤ max_mb_per_chunk × balance_factor for c in chunks)`
  where `balance_factor` is 1.0 for the page-range path and 1.5 for
  the seam path (§1.3)
- `all(c.pages ≤ max_pages_per_chunk × balance_factor for c in chunks)`
- `chunks` is non-empty when `total_pages ≥ 1`

## ConversionOptions

Per-request caller-supplied options. Comes from the multipart
`options` JSON field.

```python
@dataclass(frozen=True)
class ConversionOptions:
    cache: bool = True
    log_level: Optional[str] = None
```

**Field semantics:**

| Field       | Description                                                            |
| ----------- | ---------------------------------------------------------------------- |
| `cache`     | Whether to use the filesystem cache (both read and write) for this request. Defaults to True. Per-request bypass when False (FR-7). |
| `log_level` | Per-request log level override, or None to use server default. Useful for debug-mode runs without restarting the server. |

**Invariants:**

- If `log_level` is set, it must be one of `"debug"`, `"info"`,
  `"warn"`, `"error"`.

**Forward compatibility:**

Unknown JSON fields in the request body are ignored. New optional
fields can be added in minor versions without breaking existing
clients.

## ConversionResult

Metadata about a successful conversion. Returned to the orchestrator
as the final yield in the async generator; values populated as
response headers (`X-*`).

```python
@dataclass(frozen=True)
class ConversionResult:
    chunks_rendered: int
    subdivision_retries: int
    cache_hits: int
    duration_seconds: float
```

**Field semantics:**

| Field                 | Description                                                                            |
| --------------------- | -------------------------------------------------------------------------------------- |
| `chunks_rendered`     | Total Aspose render invocations performed for this request, including subdivision retries. Excludes cache hits. |
| `subdivision_retries` | Number of OOM-triggered subdivisions. Each subdivide call counts once regardless of how many sub-chunks it produced. |
| `cache_hits`          | Sum of chunk-cache hits + (final-cache hit ? 1 : 0). A final-cache hit short-circuits the whole job, so most requests have either 0 (full miss), some chunk hits (partial), or 1 (full final hit, chunks_rendered = 0). |
| `duration_seconds`    | Wall time from request_received to last yielded byte                                   |

**Invariants:**

- `chunks_rendered ≥ 0`
- `subdivision_retries ≥ 0`
- `cache_hits ≥ 0`
- `duration_seconds ≥ 0.0`
- If `cache_hits ≥ 1` due to final-cache hit, `chunks_rendered == 0`
  and `subdivision_retries == 0`.

## Diagnostic

Structured failure metadata. Populated on the error path; returned
as the HTTP error response body.

```python
@dataclass(frozen=True)
class Diagnostic:
    request_id: str
    failure_class: FailureClass
    detail: Mapping[str, Any]
```

**Field semantics:**

| Field           | Description                                                          |
| --------------- | -------------------------------------------------------------------- |
| `request_id`    | UUID assigned by the server on request arrival                       |
| `failure_class` | One of the canonical failure classes (see §3 of business-rules)      |
| `detail`        | Failure-class-specific structured fields (e.g. `chunk_index`, `page_range`, `aspose_exit_code`, `stderr_tail`). Kept under 4 KB to fit in a single response. |

**Invariants:**

- `request_id` is a valid UUID
- `failure_class` is in the canonical set (`FailureClass` enum)
- `detail` is JSON-serializable

**Wire format:**

JSON body of the HTTP error response is exactly the JSON
serialization of `Diagnostic` with `failure_class` rendered as
its string value.

## FailureClass (enum)

```python
class FailureClass(StrEnum):
    UNSUPPORTED_FORMAT          = "unsupported_format"
    MISSING_FILE                = "missing_file"
    INPUT_TOO_LARGE             = "input_too_large"
    INPUT_UNPROCESSABLE         = "input_unprocessable"
    RENDER_FAILED               = "render_failed"
    SUBDIVISION_FLOOR_EXCEEDED  = "subdivision_floor_exceeded"
    MERGE_FAILED                = "merge_failed"
    LICENSE_EXPIRED             = "license_expired"
    BUSY                        = "busy"
```

Closed set in v1. Additions are minor versions; removals are major.

## LicenseState (enum, computed)

```python
class LicenseState(StrEnum):
    PERMANENT       = "permanent"
    HEALTHY         = "healthy"
    WARN            = "warn"
    CRITICAL        = "critical"
    EXPIRING_TODAY  = "expiring_today"
    EXPIRED         = "expired"
```

Computed from `LicenseManager.days_remaining()` via the table in
`business-rules.md §4`.

## Internal Exception Hierarchy

Defined alongside the entities; raised by `aspose_worker` and
propagated through the orchestrator.

```python
class ConversionError(Exception):
    """Base class for all conversion failures."""
    failure_class: FailureClass
    http_status: int

class UnsupportedFormatError(ConversionError):    # 400
    ...

class MissingFileError(ConversionError):          # 400
    ...

class InputTooLargeError(ConversionError):        # 400
    ...

class InputUnprocessableError(ConversionError):   # 422
    ...

class RenderError(ConversionError):               # 500
    chunk: Chunk
    exit_code: int
    stderr_tail: str

class OOMError(RenderError):                      # internal — not directly surfaced as HTTP
    """Raised on worker exit 137. Orchestrator catches and subdivides."""

class SubdivisionFloorError(ConversionError):     # 500
    chunk: Chunk

class MergeError(ConversionError):                # 500
    exit_code: int
    stderr_tail: str

class LicenseExpiredError(ConversionError):       # 503
    expired_on: date | None

class BusyError(ConversionError):                 # 503
    retry_after_seconds: int
```

The relationship to `Diagnostic`:

```python
def diagnostic_from_exception(exc: ConversionError, request_id: str) -> Diagnostic:
    return Diagnostic(
        request_id=request_id,
        failure_class=exc.failure_class,
        detail=exc.as_detail_dict(),     # each subclass implements
    )
```

## Entity Lifecycle (Per Request)

```
HTTP arrives
  → server generates request_id (UUID)
  → server reads first 512 bytes, calls detect_format()
       → may raise UnsupportedFormatError
  → server buffers body, raises InputTooLargeError on overrun
  → server checks LicenseManager.is_expired()
       → may raise LicenseExpiredError
  → orchestrator computes source_sha256
  → orchestrator invokes probe() → ProbeResult
       → may raise InputUnprocessableError
  → orchestrator invokes chunk_planner.plan_chunks(ProbeResult)
       → produces ChunkPlan
  → orchestrator iterates plan.chunks:
       per chunk:
         → cache.get_chunk(chunk_sha) ?
             hit: skip to next chunk; cache_hits += 1
             miss: aspose_worker.render_chunk(chunk)
                     may raise OOMError → subdivide → recurse
                                              if floor: raise SubdivisionFloorError
                     may raise RenderError, LicenseExpiredError, InputUnprocessableError
                     on success: cache.put_chunk(chunk_sha, path)
                                  chunks_rendered += 1
  → orchestrator invokes qpdf.concat_streaming(paths)
       may raise MergeError
       yields bytes upward
  → server writes ConversionResult metadata to response headers
  → server cleans up scratch dir
  → server releases server_semaphore
  → server emits request_complete event
```
