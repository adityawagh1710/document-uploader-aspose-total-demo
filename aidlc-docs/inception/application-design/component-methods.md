# Component Methods — Office Converter (Local v1)

Method signatures and high-level purpose per component. Detailed
business rules (chunk-size math, subdivision policy, license-expiry
state machine) are deferred to Functional Design.

Type names without import paths refer to `types.py` dataclasses
(see `components.md` §12).

---

## server

```python
app: FastAPI

@app.post("/convert")
async def convert(
    request: Request,
    file: UploadFile = File(...),
    options: str = Form("{}"),
) -> StreamingResponse:
    """Buffer input to scratch, hand off to orchestrator, stream PDF
    response. Maps orchestrator exceptions to 4xx/5xx per FR-5."""

@app.get("/health")
async def health() -> HealthResponse:
    """Return readiness + license_days_remaining + active_jobs + max_jobs."""

@app.on_event("startup")
async def startup() -> None:
    """Validate license file exists + parses, configure logging,
    record server_start event."""
```

`HealthResponse`:

```python
@dataclass
class HealthResponse:
    ready: bool
    license_days_remaining: int | None
    active_jobs: int
    max_jobs: int
```

---

## config

```python
class Settings(BaseSettings):
    max_jobs: int = 1
    parallel: int = 2
    cache_dir: Path | None = None
    license_path: Path = Path("/aspose/license.lic")
    scratch_dir: Path = Path("/tmp/office-convert")
    log_format: Literal["json", "human"] = "json"
    log_level: Literal["debug", "info", "warn", "error"] = "info"
    aspose_version: str = "unknown"     # resolved at startup from aspose-python package metadata

    model_config = SettingsConfigDict(env_prefix="OFFICE_CONVERT_")

@lru_cache(maxsize=1)
def get_settings() -> Settings: ...
```

---

## orchestrator

```python
async def convert_job(
    request_id: str,
    input_path: Path,
    options: ConversionOptions,
    settings: Settings,
) -> AsyncIterator[tuple[bytes, ConversionResult | None]]:
    """Run the full conversion pipeline for one request.

    Yields chunks of the merged PDF (bytes) followed by a final tuple
    where ConversionResult is non-None and bytes is empty — signalling
    completion and carrying metadata for response headers.

    Raises:
        UnsupportedFormatError    → mapped to HTTP 400
        InputUnprocessableError   → mapped to HTTP 422
        SubdivisionFloorError     → mapped to HTTP 500
        RenderError               → mapped to HTTP 500
        MergeError                → mapped to HTTP 500
        LicenseExpiredError       → mapped to HTTP 503
        BusyError                 → mapped to HTTP 503
    """
```

---

## chunk_planner

```python
def plan_chunks(
    probe: ProbeResult,
    max_pages_per_chunk: int = 10,
    max_mb_per_chunk: int = 50,
) -> ChunkPlan:
    """Pure function. Deterministic for a given (probe, max_pages,
    max_mb). Uses hybrid natural-seam-or-page-range strategy:
    prefer natural seams when they produce balanced chunks within
    the bound, otherwise fall back to page-range splitting."""

def subdivide(chunk: Chunk) -> list[Chunk]:
    """Pure function. Return 2 sub-chunks halving the page range,
    or empty list if chunk is already a single page (subdivision
    floor). Deterministic for a given chunk."""

def chunk_sha256(chunk: Chunk, source_sha256: str) -> str:
    """Pure function. Stable hash combining source content hash and
    chunk page range; used as the cache key."""
```

---

## aspose_worker

```python
async def render_chunk(
    chunk: Chunk,
    input_path: Path,
    format: str,
    scratch_dir: Path,
    request_id: str,
    settings: Settings,
) -> Path:
    """Spawn `prlimit --as=2147483648 -- office-convert-worker
    --input ... --page-range ... --output ... --format ...
    --license-path ...` via asyncio.create_subprocess_exec.

    Awaits subprocess exit. Maps exit codes:
        0       → success, return chunk PDF Path
        137     → OOMError (raise; orchestrator retries via subdivide)
        2       → LicenseError (re-raise as LicenseExpiredError)
        1, etc. → RenderError carrying stderr diagnostic
    """
```

---

## worker (C++ binary `office-convert-worker`)

This is a **native C++ binary**, not Python. Its CLI is the
contract:

```
office-convert-worker
    --input <path>
    --page-range <start>-<end>
    --output <path>             # required in --mode=render only
    --format docx|pptx|xlsx|pdf
    --license-path <path>
    --mode render|probe
```

**Semantics:**

- `--mode render`: open the input via the format-appropriate
  Aspose C++ namespace, apply license, render the requested page
  range to a chunk PDF written to `--output`. Use
  `LoadOptions::TempFolder` + memory-optimization knobs where
  the C++ API exposes them. Exit 0 on success.
- `--mode probe`: open the input via Aspose, read page count and
  natural seams metadata, write JSON `ProbeResult` to stdout.
  Exit 0 on success.

**Exit code contract (identical to the Python-worker contract):**

| Code | Meaning                                                          |
| ---- | ---------------------------------------------------------------- |
| 0    | Success                                                          |
| 1    | Generic render failure (diagnostic JSON on stderr)               |
| 2    | License invalid or expired                                       |
| 3    | Input unprocessable (corrupt, encrypted, unsupported)            |
| 137  | OOM — `Aspose::OutOfMemoryException` caught OR kernel SIGKILL on RLIMIT_AS overflow |

**Internal structure (informational; not part of the orchestrator
contract):**

```
worker_cpp/
├── CMakeLists.txt
├── main.cpp                 # argv parsing, mode dispatch, exit code mapping
├── license.cpp / license.h  # SetLicense() wrapper
├── render.cpp / render.h    # format dispatch table
├── probe.cpp / probe.h      # metadata extraction + JSON serialization
├── formats/
│   ├── docx.cpp             # Aspose::Words
│   ├── pptx.cpp             # Aspose::Slides
│   ├── xlsx.cpp             # Aspose::Cells
│   └── pdf.cpp              # Aspose::Pdf
└── error.cpp / error.h      # exception → exit code translation
```

---

## qpdf

```python
async def concat_streaming(
    chunk_paths: list[Path],
) -> AsyncIterator[bytes]:
    """Spawn `qpdf --empty --pages <chunk_paths> -- -` via
    asyncio.create_subprocess_exec with stdout=PIPE. Yield chunks
    of stdout as they arrive. Await process exit at end; raise
    MergeError on non-zero return. Never buffer the full PDF."""

def chunk_paths_to_qpdf_args(chunk_paths: list[Path]) -> list[str]:
    """Pure function. Convert list of paths to the qpdf --pages
    argument list. Each path appears with its page selector."""
```

---

## cache

```python
class CacheManager:
    def __init__(
        self,
        cache_dir: Path | None,
        aspose_version: str,
    ) -> None: ...

    def enabled(self) -> bool: ...    # True iff cache_dir is set

    def get_final(self, source_sha256: str) -> Path | None: ...
    def put_final(self, source_sha256: str, pdf_path: Path) -> None: ...

    def get_chunk(self, chunk_sha256: str) -> Path | None: ...
    def put_chunk(self, chunk_sha256: str, pdf_path: Path) -> None: ...
```

Key layout (documented invariant for backward compat):

```
<cache_dir>/<aspose_version>/final/<source_sha256>.pdf
<cache_dir>/<aspose_version>/chunks/<chunk_sha256>.pdf
```

---

## license

```python
class LicenseManager:
    def __init__(self, license_path: Path) -> None: ...

    def days_remaining(self) -> int | None:
        """Parse the .lic file and return days until expiry, or None
        if the license format does not expose an expiry date."""

    def is_expired(self) -> bool: ...

    def refresh(self) -> None:
        """Re-read the license file from disk. Used if the operator
        replaces the .lic file in place; the orchestrator may call
        this on a schedule or on /health invocations."""
```

---

## probe

```python
async def probe(
    input_path: Path,
    format: str,
    scratch_dir: Path,
    request_id: str,
    settings: Settings,
) -> ProbeResult:
    """Invoke the worker binary in --probe mode under prlimit.
    Parse the JSON metadata written to worker stdout. Maps worker
    exit codes the same way as aspose_worker.render_chunk."""

def detect_format(input_path: Path) -> str:
    """Pure function. Detect file format by magic bytes (not by
    extension). Returns 'docx' | 'pptx' | 'xlsx' | 'pdf' or raises
    UnsupportedFormatError."""
```

---

## logging

```python
def configure(
    format: Literal["json", "human"],
    level: Literal["debug", "info", "warn", "error"],
) -> None:
    """Install root handler emitting the requested format."""

@contextmanager
def request_context(request_id: str) -> Iterator[None]:
    """ContextVar binding so all log records emitted within the
    context include request_id."""

def emit_event(event: str, **fields: Any) -> None:
    """Emit a structured log event with the standard vocabulary.
    Automatically picks up request_id from contextvars."""
```

Standard event names (canonical vocabulary):

`server_start`, `server_shutdown`, `request_received`, `cache_hit`,
`cache_miss`, `chunk_render_start`, `chunk_complete`,
`subdivision_retry`, `merge_start`, `merge_complete`,
`request_complete`, `request_failed`, `license_warn`,
`license_error`.

---

## types (dataclasses, no methods)

```python
@dataclass(frozen=True)
class Chunk:
    index: int
    page_range: tuple[int, int]    # inclusive, 1-based
    natural_seam: bool             # True if boundary is a natural seam

@dataclass(frozen=True)
class ChunkPlan:
    chunks: tuple[Chunk, ...]
    total_pages: int
    estimated_mb: int

@dataclass(frozen=True)
class ProbeResult:
    page_count: int
    format: Literal["docx", "pptx", "xlsx", "pdf"]
    natural_seams: tuple[tuple[int, int], ...]
    size_bytes: int

@dataclass(frozen=True)
class ConversionOptions:
    cache: bool = True
    log_level: str | None = None

@dataclass(frozen=True)
class ConversionResult:
    chunks_rendered: int
    subdivision_retries: int
    cache_hits: int
    duration_seconds: float

@dataclass(frozen=True)
class Diagnostic:
    request_id: str
    failure_class: str
    detail: dict[str, Any]
```
