# Business Rules — office-converter (Local v1)

Concrete decision rules, validation logic, thresholds, and constants.
Implementation-agnostic — these rules must hold regardless of
language or framework.

## 1. Chunk-Planning Constants

| Rule                                           | Value                                       |
| ---------------------------------------------- | ------------------------------------------- |
| `MAX_PAGES_PER_CHUNK`                          | 10                                          |
| `MAX_MB_PER_CHUNK`                             | 50                                          |
| `BALANCE_FACTOR_FOR_SEAM_PATH`                 | 1.5                                         |
| Subdivision floor                              | 1 page                                      |
| Maximum recursion depth (safety check)         | 6                                           |

### 1.1 Per-Format Amplification

Used in `est_mb = (size_bytes / total_pages) × pages × amplification[f]`:

| Format | Amplification |
| ------ | :-----------: |
| DOCX   | 5             |
| PPTX   | 8             |
| XLSX   | 4             |
| PDF    | 2             |

The chunk planner uses this pro-rated estimate as the authoritative
memory cost. Per-document outliers (e.g. a small DOCX with a huge
embedded image on one page) are caught by the subdivision-on-OOM
retry path rather than at plan time.

### 1.1.1 Worker Page-Range Single-Chunk Carve-Out (v1, 2026-05-12)

`plan_chunks` short-circuits for any format whose C++ worker
discards `--page-range`. Currently that set is `{pptx, pdf}` —
for those formats the planner returns exactly one chunk spanning
`(1, page_count)`. The amplification factor in §1.1 is still used
to compute `estimated_mb`, but pagination and balance tests are
skipped.

**Why**: `worker_cpp/formats/pptx.cpp` and `worker_cpp/formats/pdf.cpp`
discard `--page-range` in v1 (every render saves the full
document). Any multi-chunk plan would emit N copies of the full
document and qpdf would concatenate them into incorrect output.
Single-chunk preserves correctness at the cost of zero
intra-request parallelism for those formats.

**NOT carved out**:
- **DOCX** honors `--page-range` via `Aspose::Words::Saving::PageSet`
  in `docx.cpp`.
- **XLSX** honors `--page-range` via
  `Aspose::Cells::PdfSaveOptions::SetPageIndex/SetPageCount` in
  `xlsx.cpp` (lifted 2026-05-13). The XLSX probe uses
  `Aspose::Cells::Rendering::WorkbookRender::GetPageCount()` to
  emit a real page count; pagination is PageSetup-driven and so
  matches the indices used by render-side `PdfSaveOptions`.

Both DOCX and XLSX stay on the normal chunked path and benefit
from worker parallelism. XLSX additionally has a per-format
`max_pages_per_chunk` floor applied at the orchestrator call
site (`config.xlsx_min_pages_per_chunk`, default 1500) — each
Cells subprocess pays a fixed `Workbook.Load` + full-workbook
pagination cost on a 30k-page file before rendering its slice,
so chunks must be coarse enough to amortize that overhead.

**Lift condition (remaining formats)**: once a format's worker
implements page-range slicing (PPTX via
`Aspose::Slides::Presentation::get_Slides()->RemoveAt`; PDF via
`Aspose::Pdf::Document::DeletePage` or `PdfFileEditor::Extract`),
remove that format from the carve-out tuple in
`office_convert/chunk_planner.py::plan_chunks` AND from the matching
exemption in `tests/property/test_chunk_planner_pbt.py::
test_page_range_chunks_respect_max_pages_with_balance_factor`.

### 1.2 Balance Test (Q2 seam policy)

Seam-grouped plan is accepted iff:

```
max(c.estimated_mb     for c in plan) ≤ 1.5 × MAX_MB_PER_CHUNK
AND
max(c.pages            for c in plan) ≤ 1.5 × MAX_PAGES_PER_CHUNK
```

Both conditions must hold. If either fails, fall back to
page-range split.

## 2. Worker Subprocess Exit-Code Contract

Authoritative table. Any code not listed is treated as `1` by the
orchestrator translation.

| Code | Meaning                  | Orchestrator action            |
| :--: | ------------------------ | ------------------------------ |
| 0    | success                  | use output path                |
| 1    | generic render failure   | raise `RenderError`            |
| 2    | license invalid/expired  | raise `LicenseExpiredError`    |
| 3    | input unprocessable      | raise `InputUnprocessableError`|
| 137  | OOM (Aspose OOM caught + clean exit, OR SIGKILL by kernel after RLIMIT_AS hit) | raise `OOMError` → subdivide   |

**Code 137** is intentionally reused for both:

- Worker caught `OutOfMemoryException`, logged, exited with 137
- Kernel killed the process when it exceeded `RLIMIT_AS=2 GB` (SIGKILL, exit status 128+9=137)

Both cases mean "this chunk doesn't fit in 2 GB"; the orchestrator's
response (subdivide) is identical.

## 3. HTTP Status Mapping

Canonical failure-class → HTTP status. Every error response body
includes `{request_id, failure_class, detail}`.

| failure_class                | HTTP | Origin                                        |
| ---------------------------- | :--: | --------------------------------------------- |
| `unsupported_format`         | 400  | magic-byte detection at receive               |
| `missing_file`               | 400  | multipart missing `file` field                |
| `input_too_large`            | 400  | buffered size > 1 GB (NFR-3)                  |
| `input_unprocessable`        | 422  | worker exit 3                                 |
| `render_failed`              | 500  | worker exit 1 OR timeout OR unknown code      |
| `subdivision_floor_exceeded` | 500  | subdivide returned empty list                 |
| `merge_failed`               | 500  | qpdf exit ≠ 0                                 |
| `license_expired`            | 503  | license_manager.is_expired() OR worker exit 2 |
| `busy`                       | 503  | server_semaphore acquire_nowait failed        |

`busy` includes `Retry-After: 60` header.

## 4. License Expiry State Classification

| `days_remaining`         | State           | Log level on /convert | /health.ready |
| ------------------------ | --------------- | --------------------- | :-----------: |
| `None` (no expiry field) | PERMANENT       | (none)                | true          |
| > 7                      | HEALTHY         | DEBUG once            | true          |
| 4–7                      | WARN            | WARN once             | true          |
| 1–3                      | CRITICAL        | ERROR once            | true          |
| 0                        | EXPIRING_TODAY  | ERROR once            | false         |
| past expiry              | EXPIRED         | ERROR + reject 503    | false         |

"Once per request" means at most one log line per request for that
threshold, even if multiple workers within the request also log.

## 5. Cache Key Structure

```
<cache_dir>/<aspose_version>/final/<source_sha256>.pdf
<cache_dir>/<aspose_version>/chunks/<chunk_sha256>.pdf
```

`source_sha256` = SHA-256 hex digest of the entire input file
contents.

`chunk_sha256` = SHA-256 hex digest of the concatenation:

```
source_sha256
|| ":"
|| str(chunk.page_range[0])
|| "-"
|| str(chunk.page_range[1])
|| ":"
|| format_name
```

Stable across runs for a given `(source, page_range, format)`.

`aspose_version` = the major.minor version reported by the installed
Aspose Python package at server startup (e.g. `"24.6"`). Changing
Aspose version naturally invalidates the cache because keys land in
a new directory.

## 6. Concurrency and Timeout Constants

| Setting                             | Env var                                  | Default | Range          |
| ----------------------------------- | ---------------------------------------- | :-----: | -------------- |
| Concurrent jobs (HTTP requests)     | `OFFICE_CONVERT_MAX_JOBS`                | 1       | 1–10           |
| Concurrent chunks within one job    | `OFFICE_CONVERT_PARALLEL`                | 2       | 1–10           |
| Chunk render timeout                | `OFFICE_CONVERT_CHUNK_TIMEOUT_SECONDS`   | 300     | 30–3600        |
| Worker SIGKILL grace period         | (constant)                               | 5 s     | (not tunable)  |

Peak Aspose worker subprocesses = `max_jobs × parallel`. Peak Aspose
RAM = that × 2 GB. Operators raising either value must ensure host
has the RAM headroom.

## 7. Input Validation Rules

### 7.1 Format Detection

Performed on the buffered file (a 512-byte head for the magic check; the
full file for OOXML central-directory inspection):

| Detected magic                                                                         | Routed worker |
| -------------------------------------------------------------------------------------- | ------------- |
| `%PDF-` (5 bytes)                                                                      | PDF           |
| `PK\x03\x04` + OOXML Content-Types → wordprocessingml.document.main+xml                | DOCX          |
| `PK\x03\x04` + OOXML Content-Types → presentationml.presentation.main+xml              | PPTX          |
| `PK\x03\x04` + OOXML Content-Types → spreadsheetml.sheet.main+xml                      | XLSX          |
| `PK\x03\x04` + ODF mimetype entry `application/vnd.oasis.opendocument.text`            | DOCX (.odt)   |
| `PK\x03\x04` + ODF mimetype entry `application/vnd.oasis.opendocument.spreadsheet`     | XLSX (.ods)   |
| `PK\x03\x04` + ODF mimetype entry `application/vnd.oasis.opendocument.presentation`    | PPTX (.odp)   |
| `PK\x03\x04` + ODF mimetype entry `application/vnd.oasis.opendocument.graphics`        | **LibreOffice** (.odg) |
| `PK\x03\x04` + ODF mimetype `application/vnd.oasis.opendocument.formula` or `.base`    | reject 400 `unsupported_format` (precise reason in `detail`) |
| `\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1` (OLE2/CFB) + UTF-16LE stream `WordDocument`         | DOCX (.doc)   |
| `\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1` (OLE2/CFB) + UTF-16LE stream `Workbook` or `Book`   | XLSX (.xls)   |
| `\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1` (OLE2/CFB) + UTF-16LE stream `PowerPoint Document`  | PPTX (.ppt)   |
| `\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1` + no stream signature, filename ext `.doc/.dot`     | DOCX (fallback) |
| `\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1` + no stream signature, filename ext `.xls/.xlt/.xlm` | XLSX (fallback) |
| `\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1` + no stream signature, filename ext `.ppt/.pot/.pps` | PPTX (fallback) |
| `{\\rtf` (RTF magic)                                                                   | DOCX (RTF native to Aspose.Words) |
| CSV — detected by `is_csv_filename` upstream of `detect_format`; normalized to XLSX bytes before magic-byte detection runs | XLSX (via `csv_input.csv_bytes_to_xlsx_bytes`) |
| `\x89PNG\r\n\x1a\n`                                                                    | **LibreOffice** (.png) |
| `\xFF\xD8\xFF` (JPEG SOI + first marker)                                              | **LibreOffice** (.jpg) |
| `GIF87a` or `GIF89a`                                                                  | **LibreOffice** (.gif) |
| `BM` (BMP)                                                                            | **LibreOffice** (.bmp) |
| `II*\x00` (TIFF little-endian) or `MM\x00*` (TIFF big-endian)                         | **LibreOffice** (.tiff) |
| `RIFF` + `WEBP` tag within first 16 bytes                                             | **LibreOffice** (.webp) |
| `<?xml…<svg…` or `<svg…` (tolerant text sniff in first 512 bytes, BOM-tolerant)       | **LibreOffice** (.svg) |
| RFC 5322 header sniff (first 1 KB, lowercased, starts with one of `received:`/`return-path:`/`delivered-to:`/`message-id:`/`date:`/`from:`/`to:`/`subject:`/`mime-version:`/`x-…` AND contains `": "` in the first 200 bytes) | **Aspose.Email pipeline** (.eml) |
| anything else                                                                          | reject 400 `unsupported_format` |

**LibreOffice fallback path (ODG + images, added 2026-05-22)**: rows marked
**LibreOffice** in the table above bypass the Aspose orchestrator entirely.
The server hands the buffered input to `office_convert.libreoffice_convert.
convert_to_pdf()`, which shells out to `soffice --headless --convert-to pdf`.
The bare `pdf` filter (not `pdf:writer_pdf_Export`) lets soffice pick the
right importer from the input file's extension — that's why the dispatch
loop preserves the original extension as `input.<ext>` rather than the
generic `input.<format>`. One subprocess per request, no chunking, no
caching. Rationale: Aspose.Total for C++ has no library for drawing pages,
raster images, or SVG; Aspose.Imaging is .NET/Java only; LibreOffice
Draw is already installed via `libreoffice-draw-nogui` for the original
ODG use case (commit `2dd40cf`), so reusing it costs zero new image weight.

**Aspose.Email pipeline (EML, added 2026-05-26)**: EML inputs also bypass
the orchestrator. The server hands the buffered input to
`office_convert.aspose_email_convert.convert_to_pdf()`, which runs a
three-subprocess pipeline:

1. `office-convert-worker-email` loads the EML via `Aspose::Email::
   MailMessage::Load`, saves MHTML via `SaveOptions::get_DefaultMhtml()`.
2. `office-convert-worker-docx --mode probe` on the MHTML returns the
   rendered page count.
3. `office-convert-worker-docx --mode render --page-range 1-N` on the
   MHTML produces the final PDF. Aspose.Words selects `LoadFormat::Mhtml`
   (51) automatically from the file content.

The stages run in distinct processes so Aspose.Email's CodePorting
framework 25.12 and Aspose.Words's CodePorting framework 26.3 never
coexist in one address space — the same isolation that the 4-binary
split already provides for Words/Cells/Slides/PDF. No chunking, no
caching; emails are short, single-shot, and the orchestrator's chunk
planner adds no value here.

**Legacy binary Office formats (added 2026-05-13)**: OLE2 / Compound File
Binary inputs (pre-2007 `.doc`/`.xls`/`.ppt` and their template/template-show
variants) are accepted. The magic alone doesn't identify which Office
application wrote the file, so the orchestrator scans the first ~64 KB for
UTF-16LE stream-name signatures in the CFB directory. If no signature
matches (rare), the uploaded multipart filename's extension is used as a
fallback hint. Legacy formats map to the corresponding modern worker
internally — Aspose.Words / Aspose.Cells / Aspose.Slides load both binary
and OOXML through one constructor.

**Stream-signature-vs-filename precedence**: stream signature always wins
over filename extension. A `.doc`-stamped Word file renamed to `.xls`
still routes to the DOCX worker.

### 7.2 Size Limit

Reject with 400 `input_too_large` when buffered size exceeds:

| Setting       | Env var                            | Default |
| ------------- | ---------------------------------- | :-----: |
| `MAX_INPUT_BYTES` | `OFFICE_CONVERT_MAX_INPUT_BYTES` | 1 073 741 824 (1 GB) |

### 7.3 Options Parsing

Multipart form field `options` is a JSON object:

```
{
  "cache": <bool, default true>,
  "log_level": <"debug" | "info" | "warn" | "error", default settings.log_level>
}
```

Unknown fields are ignored (forward compatibility). Malformed JSON
is rejected with 400 `missing_file` (degenerate; we treat it as
"options missing" since `options` defaults to `"{}"`).

## 8. Logging Event Schema

Every event includes these standard fields:

| Field        | Type             | Always present?              |
| ------------ | ---------------- | ---------------------------- |
| `timestamp`  | ISO-8601 string  | yes                          |
| `level`      | string           | yes                          |
| `event`      | string           | yes (vocabulary below)       |
| `request_id` | UUID string      | yes (server events have no request, use literal `"server"`) |

Event vocabulary (closed set):

`server_start`, `server_shutdown`, `request_received`,
`cache_hit`, `cache_miss`, `probe_complete`,
`chunk_render_start`, `chunk_complete`, `subdivision_retry`,
`merge_start`, `merge_complete`, `request_complete`,
`request_failed`, `license_warn`, `license_error`,
`worker_timeout`.

Event-specific fields documented in business-logic-model.md
sections.

## 9. Resource Lifecycle Rules

### 9.1 Scratch Directory

Created on request arrival at `<scratch_dir>/<request_id>/`.
Cleaned up:

- On request success: after the last byte is written to the
  response.
- On request failure: in the error-handler before the HTTP
  response is sent.
- On server crash: leftover; documented operator cleanup
  (cron `find /tmp/office-convert -mtime +1 -delete` or similar).

### 9.2 Worker Subprocess

Spawned per chunk render or per probe. Always exits before its
parent (the orchestrator's coroutine for that chunk) awaits its
completion. Never reused across chunks.

### 9.3 qpdf Subprocess

Spawned per merge. Reads chunk PDFs from disk, writes merged PDF
to stdout. Exits after writing the entire merged PDF or on error.
Never reused.

## 10. Determinism Invariants

The following must hold across runs with identical inputs:

| Function                  | Deterministic?                              |
| ------------------------- | ------------------------------------------- |
| `chunk_planner.plan_chunks` | yes — same input → same plan, byte-for-byte |
| `chunk_planner.subdivide` | yes                                         |
| `chunk_planner.chunk_sha256` | yes                                      |
| `cache.get_*`, `cache.put_*` | yes (filesystem operations)              |
| `qpdf` concat             | yes (qpdf is deterministic for given inputs)|
| Aspose render             | **NOT deterministic** at byte level (NFR-5) |

PBT (NFR-6) verifies determinism of the deterministic functions
across random inputs.

## 11. Security and Trust Rules

| Rule                                                    | Enforcement                                |
| ------------------------------------------------------- | ------------------------------------------ |
| Aspose `.lic` file is never logged or echoed            | License manager redacts on any error path  |
| Document content is never logged                        | Log events carry metadata only (page count, format, sizes) |
| HTTP server binds to 0.0.0.0:8080 inside container      | Operator chooses host port mapping         |
| No application-layer auth in v1                         | Documented in NFR-8                        |
| Input format detected by magic bytes, not file extension| Detection happens before any disk write    |
| Subprocess workers cannot write outside scratch dir     | Worker argv only includes paths under scratch |
| qpdf binary path is fixed at build time                 | Image installs qpdf to a known location    |

## 12. Configuration Validation Rules

Loaded once at server startup via `pydantic-settings`. Fail-fast on
invalid configuration before any HTTP request is served.

| Setting                | Constraint                                                          |
| ---------------------- | ------------------------------------------------------------------- |
| `max_jobs`             | Integer, 1 ≤ x ≤ 10                                                 |
| `parallel`             | Integer, 1 ≤ x ≤ 10                                                 |
| `cache_dir`            | Either unset OR path exists and is a writable directory             |
| `license_path`         | Path exists and is a readable file                                  |
| `scratch_dir`          | Path exists OR can be created; is writable                          |
| `log_format`           | `"json"` or `"human"`                                               |
| `log_level`            | `"debug"`, `"info"`, `"warn"`, or `"error"`                         |
| `chunk_timeout_seconds`| Integer, 30 ≤ x ≤ 3600                                              |
| `max_input_bytes`      | Integer, 1 048 576 ≤ x ≤ 1 073 741 824 (1 MB to 1 GB)               |

On any validation failure, the server logs a `server_start` event
with `level: error`, prints the failure to stderr, and exits with
a non-zero status code.

## 13. Versioning Rules (v1)

| Surface                  | v1 contract                                              |
| ------------------------ | -------------------------------------------------------- |
| HTTP API path            | `/convert`, `/health` (no `/v1/` prefix in v1)           |
| Cache key layout         | Documented (§5); changes require cache directory cleanup |
| Worker exit codes        | Documented (§2); stable contract                         |
| Log event vocabulary     | Closed set (§8); additions are minor versions; removals require major |
| Failure-class names      | Closed set (§3); same rules as event vocabulary          |
