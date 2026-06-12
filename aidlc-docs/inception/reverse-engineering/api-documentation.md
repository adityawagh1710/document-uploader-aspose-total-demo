# API Documentation

> Reverse-engineered 2026-06-12. Both orchestrators (Python FastAPI, Go chi) expose the **same
> 14-endpoint contract**; the Python↔Go golden-parity gate (14/14) enforces wire equivalence.

## REST APIs

Base: the service listens on `:8080`. All conversion/observability routes are under `/v1`;
`/health` and `/` are unversioned.

### Convert
- **Method**: POST
- **Path**: `/v1/convert`
- **Purpose**: Convert an Office/PDF/email/image/CSV input to PDF.
- **Request**: `multipart/form-data` — `file` (upload, optional), `s3_input` (form string,
  optional), `s3_output` (form string, optional), `options` (form JSON string, default `{}`;
  fields `cache: bool`, `log_level: str?`). Exactly one of `file` / `s3_input` must be present.
- **Response**: `200` `application/pdf` (streamed). Headers: `X-Request-ID`, `X-RateLimit-*`,
  and on S3 output `X-S3-Output-Bucket` / `X-S3-Output-Key`.
- **Status codes**: `200`; `400` (missing/unsupported/too-large/source-conflict/S3 errors);
  `404` (S3 input not found); `422` (input unprocessable); `429` (rate limited, `Retry-After`);
  `503` (busy / license expired, `Retry-After` on busy). Error bodies are a JSON `Diagnostic`
  (see Data Models).

### Health
- **Method**: GET · **Path**: `/health`
- **Purpose**: readiness probe (K8s/ALB).
- **Response**: JSON `{ready, license_days_remaining, active_jobs, max_jobs, problems[]}`.
- **Status**: `200` ready / `503` not ready.

### Landing page
- **Method**: GET · **Path**: `/` · **Response**: `text/html` (status badge + endpoint table). `200`.

### Container stats
- **Method**: GET · **Path**: `/v1/stats`
- **Response**: JSON `{cpu_usage_usec, mem_bytes, mem_max_bytes, pids_current, sampled_at, cgroup_version}`. `200`.

### Worker inventory
- **Method**: GET · **Path**: `/v1/workers`
- **Response**: JSON `{workers: [{pid, cmdline, cpu_usage_usec, rss_bytes, etime_sec, sampled_at}]}`. `200`.

### Job heartbeats
- **Method**: GET · **Path**: `/v1/jobs/{request_id}/heartbeats`
- **Response**: JSON `{request_id, heartbeats: [{worker, pool_index, pid, phase, elapsed_s, rss_bytes, swap_bytes, cpu_jiffies, wall_ts}]}`. `200`.

### Job timings
- **Method**: GET · **Path**: `/v1/jobs/{request_id}/timings`
- **Response**: JSON `{request_id, timings: [{worker, pool_index, pid, wall_ts, stage, ...}]}`. `200`.

### Job progress
- **Method**: GET · **Path**: `/v1/jobs/{request_id}/progress`
- **Response**: JSON `{request_id, phase, total_chunks, chunks_rendered, load_progress, merge_done, weighted_percent, elapsed_s}`. `200`.

### Active jobs
- **Method**: GET · **Path**: `/v1/jobs/active`
- **Response**: JSON `{jobs: [ {request_id, phase, ...JobProgress} ]}` (non-complete jobs). `200`.

### Cache clear
- **Method**: DELETE · **Path**: `/v1/cache`
- **Response**: JSON `{enabled, files_deleted, bytes_freed, errors}`. `200`.

### Recent conversions
- **Method**: GET · **Path**: `/v1/conversions`
- **Query**: `cursor` (opaque base64), `limit` (1–100, default 20), `filter` (`all`|`ui`|`cross`|`failed`).
- **Response**: JSON `{entries: [ConversionRecord...], next_cursor, has_more, stale_cursor, buffer_size}`. `200`.

### Conversion stats
- **Method**: GET · **Path**: `/v1/conversions/stats`
- **Response**: JSON `{per_format: {fmt: {count, avg_ms, p95_ms}}, totals: {count, successes, failures}}`. `200`.

### Dashboard
- **Method**: GET · **Path**: `/v1/dashboard` · **Response**: `text/html` (self-contained live dashboard; embedded by the UI iframe). `200`.

### Presign download
- **Method**: GET · **Path**: `/v1/downloads/presign`
- **Query**: `bucket`, `key`.
- **Response**: JSON `{download_url, bucket, key, expires_in_seconds, expires_at}`. `200`; `400` if S3 disabled or bucket not allowlisted.

## Internal APIs

### Worker subprocess contract (orchestrator ↔ C++ binary)
- **Binary**: `office-convert-worker-{docx|pptx|xlsx|pdf|email}`, invoked under
  `prlimit --as=<WORKER_RAM_BYTES> -- <binary> ...`.
- **One-shot argv**: `--mode {render|probe} --input <path> --format <fmt> --license-path <path>
  [--output <path>] [--page-range START-END]` (page range 1-based inclusive).
- **probe stdout (JSON)**: `{"page_count": N, "format": "...", "natural_seams": [[s,e],...], "size_bytes": N}`.
- **Exit codes**: `0` ok · `1` render failure / bad args · `2` license invalid · `3` input
  unprocessable / format mismatch · `137` OOM (in-process `bad_alloc` or kernel SIGKILL on
  RLIMIT_AS).

### Pool protocol (JSON-stdio, newline-delimited)
- **Legacy pool** (`--mode pool`): requests `{"cmd":"load","input":...}` / `{"cmd":"render","page_start":S,"page_end":E,"output":...}` / `{"cmd":"quit"}`; responses `{"status":"ok","page_count":N}` / `{"status":"ok","output":...}` / `{"status":"error","code":N,"detail":...}`.
- **Forked pool** (`--mode pool --pool-size N`): same shape but every render carries a `"seq"`
  integer so the leader demuxes concurrent renders across the N forked children;
  `{"cmd":"render","seq":42,...}` → `{"seq":42,"status":"ok","output":...}`.
- **stderr side-channel** (both modes): `{"type":"heartbeat","pool_index":I,"phase":"load|render","elapsed_s":N,"rss_bytes":N,"swap_bytes":N,"cpu_jiffies":N}`, `{"type":"timing","stage":"...","duration_ms":N,...}`, and (DOCX) `{"type":"load_progress","pool_index":I,"value":0.NN}`.

## Data Models

### Chunk
- **Fields**: `index` (float — allows fractional sub-chunk indices during subdivision),
  `page_range` `(start, end)` (1-based inclusive), `natural_seam` (bool).
- **Relationships**: members of a `ChunkPlan`.
- **Validation**: ranges are contiguous, non-overlapping, and cover `[1..total_pages]`.

### ChunkPlan
- **Fields**: `chunks` (ordered tuple), `total_pages`, `estimated_mb`.
- **Validation**: complete non-overlapping cover (asserted by property-based tests in both langs).

### ProbeResult
- **Fields**: `page_count`, `format` (one of docx/pptx/xlsx/pdf), `natural_seams` (tuple of ranges), `size_bytes`.
- **Relationships**: input to the planner.

### ConversionOptions
- **Fields**: `cache` (bool, default true), `log_level` (str?).
- **Validation**: parsed from the multipart `options` JSON; unknown keys rejected.

### ConversionResult
- **Fields**: `chunks_rendered`, `subdivision_retries`, `cache_hits`, `duration_seconds`.
- **Usage**: feeds `X-*` response headers and the recent-conversion record.

### Diagnostic (error body)
- **Fields**: `request_id`, `failure_class` (one of the 17–18 canonical `FailureClass` values),
  `detail` (object — class-specific, e.g. `size_bytes`/`ceiling_bytes`, `exit_code`/`stderr_tail`).
- **Validation**: every `ConversionError` subclass declares its `failure_class` and `http_status`.

### ConversionRecord (recent feed)
- **Fields**: `request_id`, `completion_ts`, `source` (`ui`|`cross`), `input_filename`, `format`,
  `page_count`, `duration_ms`, `status`, `error_code`, `output_s3_uri`, `output_size_bytes`.

### FailureClass (enum, wire-stable)
`unsupported_format`, `missing_file`, `input_too_large`, `input_unprocessable`, `render_failed`,
`subdivision_floor_exceeded`, `merge_failed`, `license_expired`, `busy`, `rate_limited`,
`input_source_conflict`, `s3_disabled`, `s3_invalid_url`, `s3_input_not_found`,
`s3_input_forbidden`, `s3_output_forbidden`, `s3_output_upload_failed`.

### LicenseState (enum)
`permanent`, `healthy`, `warn`, `critical`, `expiring_today`, `expired`.
