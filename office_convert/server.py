"""FastAPI server: POST /convert + GET /health.

Implements FR-1, FR-2, FR-5, FR-9, NFR-3, NFR-6, NFR-8.

Request lifecycle per services.md: receive multipart → format-detect on first
512 bytes → buffer to scratch → license check → cache lookup → orchestrator
async generator → StreamingResponse with chunked transfer encoding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, cast

import aiofiles
from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from office_convert import aspose_email_convert, libreoffice_convert, orchestrator, s3_client
from office_convert import logging as oc_logging
from office_convert.cache import CacheManager
from office_convert.config import Settings, get_settings
from office_convert.csv_input import csv_bytes_to_xlsx_bytes, is_csv_filename
from office_convert.errors import (
    BusyError,
    ConversionError,
    InputSourceConflictError,
    InputTooLargeError,
    LicenseExpiredError,
    MissingFileError,
    RateLimitedError,
    S3DisabledError,
    S3OutputForbiddenError,
)
from office_convert.license import LicenseManager
from office_convert.probe import ACCEPTED_FORMATS, detect_format
from office_convert.rate_limit import RateLimiter, client_id_for
from office_convert.recent import ConversionRecord, default_store
from office_convert.types import (
    ConversionOptions,
    Diagnostic,
    FormatName,
)

log = logging.getLogger(__name__)


_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>office-convert</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {
    --bg: #0f1419;
    --panel: #1a1f29;
    --panel-2: #232936;
    --border: #2d3441;
    --text: #d7dde6;
    --muted: #7c8694;
    --accent: #4ea1ff;
    --ok: #4ade80;
    --ok-bg: rgba(74,222,128,.10);
    --warn: #fbbf24;
    --err: #f87171;
    --err-bg: rgba(248,113,113,.12);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 14px; line-height: 1.5; }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 32px 24px 80px; }
  header { display: flex; align-items: center; gap: 16px; padding-bottom: 20px; border-bottom: 1px solid var(--border); margin-bottom: 28px; }
  header h1 { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.01em; }
  header .ver { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  header .sub { color: var(--muted); margin-left: auto; font-size: 13px; }
  .pill { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; }
  .pill.ok { background: var(--ok-bg); color: var(--ok); border: 1px solid rgba(74,222,128,.3); }
  .pill.err { background: var(--err-bg); color: var(--err); border: 1px solid rgba(248,113,113,.3); }
  .section-title { font-size: 11px; font-weight: 700; letter-spacing: 0.12em; color: var(--muted); text-transform: uppercase; margin: 28px 0 12px; }
  .kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .kpi { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 16px 18px; }
  .kpi .label { color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; }
  .kpi .value { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 28px; font-weight: 500; margin-top: 6px; color: var(--text); }
  .kpi .value.accent { color: var(--accent); }
  .badges { display: flex; flex-wrap: wrap; gap: 6px; }
  .badge { background: var(--panel-2); border: 1px solid var(--border); border-radius: 4px; padding: 4px 10px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--text); }
  /* Two-tone shields.io-style stack badges */
  .stack { display: flex; flex-wrap: wrap; gap: 8px; }
  .sb { display: inline-flex; border-radius: 4px; overflow: hidden; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; line-height: 1; box-shadow: 0 1px 0 rgba(0,0,0,.35); }
  .sb-l { background: #2d3441; color: #d7dde6; padding: 6px 9px; font-weight: 500; }
  .sb-r { padding: 6px 9px; color: #fff; font-weight: 600; }
  .v-blue   { background: #3b82f6; }
  .v-green  { background: #10b981; }
  .v-pink   { background: #ec4899; }
  .v-orange { background: #f97316; }
  .v-sky    { background: #38bdf8; color: #0b2030; }
  .v-violet { background: #8b5cf6; }
  .v-amber  { background: #f59e0b; color: #2a1a00; }
  .row-title { font-size: 10px; font-weight: 700; letter-spacing: 0.14em; color: var(--muted); text-transform: uppercase; margin: 14px 0 8px; }
  .row-title:first-of-type { margin-top: 0; }
  table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); }
  th { background: var(--panel-2); color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; }
  tr:last-child td { border-bottom: 0; }
  td.method { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; width: 70px; }
  td.method .m { display: inline-block; padding: 2px 7px; border-radius: 3px; font-size: 11px; font-weight: 600; }
  td.method .m.GET { background: rgba(74,222,128,.12); color: var(--ok); }
  td.method .m.POST { background: rgba(78,161,255,.14); color: var(--accent); }
  td.method .m.DELETE { background: rgba(248,113,113,.12); color: var(--err); }
  td.path { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; color: var(--text); }
  td.desc { color: var(--muted); }
  .docs-links { display: flex; flex-wrap: wrap; gap: 10px; }
  .docs-links a { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; text-decoration: none; color: var(--text); font-size: 13px; display: flex; align-items: center; gap: 8px; transition: border-color 0.15s; }
  .docs-links a:hover { border-color: var(--accent); }
  .docs-links a code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--accent); font-size: 12px; }
  pre { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 14px 16px; overflow-x: auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; color: var(--text); margin: 0; }
  pre .arg { color: var(--accent); }
  footer { color: var(--muted); font-size: 12px; margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>office-convert</h1>
    <span class="ver">v{{VERSION}}</span>
    <span class="pill {{STATUS_CLASS}}">{{STATUS_LABEL}}</span>
    <span class="sub">Office documents → PDF</span>
  </header>

  <div class="row-title">Runtime</div>
  <div class="stack">
    <span class="sb"><span class="sb-l">python</span><span class="sb-r v-blue">3.12</span></span>
    <span class="sb"><span class="sb-l">fastapi</span><span class="sb-r v-green">0.115</span></span>
    <span class="sb"><span class="sb-l">aspose.total</span><span class="sb-r v-pink">c++ 26.4</span></span>
    <span class="sb"><span class="sb-l">qpdf</span><span class="sb-r v-orange">streaming merge</span></span>
  </div>

  <div class="row-title">Quality</div>
  <div class="stack">
    <span class="sb"><span class="sb-l">🐳 docker</span><span class="sb-r v-blue">required</span></span>
    <span class="sb"><span class="sb-l">mypy</span><span class="sb-r v-sky">strict</span></span>
    <span class="sb"><span class="sb-l">ruff</span><span class="sb-r v-violet">linted</span></span>
    <span class="sb"><span class="sb-l">tests</span><span class="sb-r v-green">passing</span></span>
  </div>

  <div class="section-title">Supported input formats</div>
  <div class="row-title" style="margin-top:0">Office &amp; documents</div>
  <div class="badges">
    <span class="badge">.docx</span>
    <span class="badge">.doc</span>
    <span class="badge">.xlsx</span>
    <span class="badge">.xls</span>
    <span class="badge">.pptx</span>
    <span class="badge">.ppt</span>
    <span class="badge">.pdf</span>
    <span class="badge">.odt</span>
    <span class="badge">.ods</span>
    <span class="badge">.odp</span>
    <span class="badge">.odg</span>
    <span class="badge">.rtf</span>
    <span class="badge">.csv</span>
  </div>

  <div class="row-title">Images</div>
  <div class="badges">
    <span class="badge">.png</span>
    <span class="badge">.jpg</span>
    <span class="badge">.jpeg</span>
    <span class="badge">.tiff</span>
    <span class="badge">.gif</span>
    <span class="badge">.bmp</span>
    <span class="badge">.webp</span>
    <span class="badge">.svg</span>
  </div>

  <div class="row-title">Email</div>
  <div class="badges">
    <span class="badge">.eml</span>
  </div>

  <div class="section-title">Endpoints</div>
  <table>
    <thead><tr><th>Method</th><th>Path</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td class="method"><span class="m POST">POST</span></td><td class="path">/v1/convert</td><td class="desc">Upload an office document, returns PDF</td></tr>
      <tr><td class="method"><span class="m GET">GET</span></td><td class="path">/v1/stats</td><td class="desc">Container CPU/memory snapshot</td></tr>
      <tr><td class="method"><span class="m GET">GET</span></td><td class="path">/v1/workers</td><td class="desc">Worker process inventory</td></tr>
      <tr><td class="method"><span class="m GET">GET</span></td><td class="path">/v1/jobs/{id}/heartbeats</td><td class="desc">Per-job liveness events</td></tr>
      <tr><td class="method"><span class="m GET">GET</span></td><td class="path">/v1/jobs/{id}/timings</td><td class="desc">Per-job stage timings</td></tr>
      <tr><td class="method"><span class="m GET">GET</span></td><td class="path">/v1/jobs/{id}/progress</td><td class="desc">Per-job progress snapshot</td></tr>
      <tr><td class="method"><span class="m DELETE">DEL</span></td><td class="path">/v1/cache</td><td class="desc">Wipe on-disk conversion cache</td></tr>
      <tr><td class="method"><span class="m GET">GET</span></td><td class="path">/health</td><td class="desc">Service readiness (unversioned, used by probes)</td></tr>
    </tbody>
  </table>

  <div class="section-title">Documentation</div>
  <div class="docs-links">
    <a href="/docs">Swagger UI <code>/docs</code></a>
    <a href="/redoc">ReDoc <code>/redoc</code></a>
    <a href="/openapi.json">OpenAPI spec <code>/openapi.json</code></a>
    <a href="/health">Health JSON <code>/health</code></a>
  </div>

  <footer>office-convert · backed by Aspose.Total for C++ · all routes versioned under <code>/v1/</code> except <code>/health</code></footer>
</div>
</body>
</html>
"""


class HealthChecker:
    """Hybrid health check: static at startup, live for license."""

    def __init__(self, settings: Settings, license_mgr: LicenseManager) -> None:
        self.settings = settings
        self.license_mgr = license_mgr
        self.static_problems: list[str] = []

        # Verify all five per-product worker binaries are present. Each
        # product gets its own binary to keep Aspose's CodePorting framework
        # versions from colliding in one process. Email is checked alongside
        # the four FormatName workers even though it routes outside the
        # orchestrator — startup readiness should still fail if it's absent.
        prefix = settings.worker_binary_prefix
        required_workers: tuple[str, ...] = (*ACCEPTED_FORMATS, "email")
        missing = [
            w for w in required_workers if not prefix.with_name(f"{prefix.name}-{w}").exists()
        ]
        if missing:
            self.static_problems.append("worker_binary_missing")
        if not shutil.which("qpdf"):
            self.static_problems.append("qpdf_missing")
        # Scratch dir may not exist yet at construction time; create if missing.
        try:
            settings.scratch_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.static_problems.append("scratch_dir_unwritable")

    def snapshot(self, active_jobs: int) -> dict[str, Any]:
        problems = list(self.static_problems)
        days_remaining: int | None = None
        license_expired = False
        try:
            days_remaining = self.license_mgr.days_remaining()
            license_expired = self.license_mgr.is_expired()
        except (FileNotFoundError, OSError):
            problems.append("license_path_missing")
        except Exception:  # malformed XML, etc.
            problems.append("license_invalid")
        if license_expired:
            problems.append("license_expired")

        ready = not problems
        return {
            "ready": ready,
            "license_days_remaining": days_remaining,
            "active_jobs": active_jobs,
            "max_jobs": self.settings.max_jobs,
            "problems": problems,
        }


def _s3_output_headers(target: tuple[str, str] | None) -> dict[str, str]:
    """X-S3-Output-* headers for a resolved (bucket, key), or empty."""
    if target is None:
        return {}
    return {"X-S3-Output-Bucket": target[0], "X-S3-Output-Key": target[1]}


def _build_record(
    *,
    request_id: str,
    t0: float,
    source: str,
    input_filename: str | None,
    fmt: str,
    s3_out_target: tuple[str, str] | None,
    status: str,
    error_code: str | None,
    output_size_bytes: int | None,
) -> ConversionRecord:
    """Construct a ConversionRecord for the recent-conversions ring buffer.

    Called from each /v1/convert streaming generator's finally: block (the
    semaphore-release site, where success vs failure is observable) and from
    the first_chunk pre-stream failure branch in the main Aspose path.

    `duration_ms` is the monotonic delta from route entry (t0 captured before
    rate-limit + semaphore acquire) — full server-observed wall time.
    """
    return ConversionRecord(
        request_id=request_id,
        completion_ts=time.time(),
        source=source,  # type: ignore[arg-type]
        input_filename=input_filename,
        format=fmt,
        page_count=None,
        duration_ms=int((time.monotonic() - t0) * 1000),
        status=status,  # type: ignore[arg-type]
        error_code=error_code,
        output_s3_uri=f"s3://{s3_out_target[0]}/{s3_out_target[1]}" if s3_out_target else None,
        output_size_bytes=output_size_bytes,
    )


async def _tee_to_s3(
    inner: AsyncIterator[bytes],
    target: tuple[str, str] | None,
    settings: Settings,
) -> AsyncIterator[bytes]:
    """Stream `inner` to the client AND (if `target` set) to S3 after the
    last byte. Generic wrapper used by all three streaming paths.

    The PDF is teed to a private temp file — NOT the cache temp (only exists
    when caching is active) and NOT `scratch_dir` (the inner generators delete
    it in their `finally`). Upload happens after the inner stream completes so
    `boto3.upload_file` reads a complete file. Passthrough with zero overhead
    when `target is None`.
    """
    if target is None:
        async for block in inner:
            yield block
        return

    bucket, key = target
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="s3out-")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        async with aiofiles.open(tmp, "wb") as fh:
            async for block in inner:
                await fh.write(block)
                yield block
        await s3_client.upload_file(tmp, bucket, key, settings)
        oc_logging.emit_event(
            "s3_output_uploaded",
            level="info",
            bucket=bucket,
            key=key,
        )
    finally:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory. Tests build a custom-settings app via this."""
    s = settings if settings is not None else get_settings()
    oc_logging.configure(format=s.log_format, level=s.log_level)

    license_mgr = LicenseManager(s.license_path)
    health_checker = HealthChecker(s, license_mgr)
    cache = CacheManager(s.cache_dir, s.aspose_version)
    server_sem = asyncio.Semaphore(s.max_jobs)
    rate_limiter: RateLimiter | None = (
        RateLimiter(
            per_minute=s.rate_limit_per_ip_rpm,
            burst=s.rate_limit_burst,
            max_keys=s.rate_limit_max_keys,
        )
        if s.rate_limit_enabled
        else None
    )
    state: dict[str, Any] = {
        "active_jobs": 0,
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        oc_logging.emit_event(
            "server_start",
            level="info",
            license_days_remaining=health_checker.snapshot(0)["license_days_remaining"],
            max_jobs=s.max_jobs,
            parallel=s.parallel,
        )
        yield
        oc_logging.emit_event("server_shutdown", level="info")

    app = FastAPI(lifespan=lifespan, title="office-convert", version="0.1.0")

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        with oc_logging.request_context(rid):
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response

    @app.exception_handler(ConversionError)
    async def conversion_error_handler(_request: Request, exc: ConversionError) -> JSONResponse:
        diagnostic = Diagnostic(
            request_id=oc_logging.current_request_id.get(),
            failure_class=exc.failure_class,
            detail=exc.as_detail_dict(),
        )
        headers: dict[str, str] = {}
        if isinstance(exc, BusyError):
            headers["Retry-After"] = str(exc.retry_after_seconds)
        elif isinstance(exc, RateLimitedError):
            headers["Retry-After"] = str(exc.retry_after_seconds)
            headers["X-RateLimit-Limit"] = str(exc.limit)
            headers["X-RateLimit-Remaining"] = "0"
            headers["X-RateLimit-Reset"] = str(int(time.time() + exc.retry_after_seconds))
        oc_logging.emit_event(
            "request_failed",
            level="error",
            failure_class=exc.failure_class.value,
            **exc.as_detail_dict(),
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=diagnostic.to_dict(),
            headers=headers,
        )

    # All routes except /health live under /v1 to keep the API contract
    # versionable. /health stays at root by convention so K8s probes and
    # the AWS ALB target group health check don't break on a version bump.
    v1 = APIRouter(prefix="/v1")

    @v1.post(
        "/convert",
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
                "description": "Converted PDF file (binary stream).",
            }
        },
    )
    async def convert(
        request: Request,
        file: Annotated[UploadFile | None, File()] = None,
        s3_input: Annotated[str | None, Form()] = None,
        s3_output: Annotated[str | None, Form()] = None,
        options: Annotated[str, Form()] = "{}",
    ) -> StreamingResponse:
        rid = oc_logging.current_request_id.get()
        oc_logging.emit_event("request_received", level="info")

        # Recent-conversions ring buffer hook — captured at route entry so
        # `duration_ms` reflects full server-observed wall time (including
        # rate-limit queue + semaphore wait + body read + conversion). Both
        # the success path (each stream*() generator's finally:) and the
        # main-Aspose pre-stream-failure path (first_chunk except) record
        # via _build_record(). Pre-validation failures (rate limit, busy,
        # license expired, S3 disabled, missing/conflicting input) do NOT
        # record — they fail before format is known.
        t0 = time.monotonic()
        recent_source: str = "cross" if s3_input else "ui"
        recent_store = default_store()

        # Per-IP rate limit (token bucket). Runs BEFORE semaphore acquire so
        # 429s don't briefly hold a job slot.
        rate_headers: dict[str, str] = {}
        if rate_limiter is not None:
            client_id = client_id_for(request, trust_xff=s.rate_limit_trust_xff)
            decision = await rate_limiter.check(client_id)
            rate_headers = {
                "X-RateLimit-Limit": str(decision.limit),
                "X-RateLimit-Remaining": str(decision.remaining),
                "X-RateLimit-Reset": str(decision.reset_epoch_seconds),
            }
            if not decision.allowed:
                oc_logging.emit_event(
                    "rate_limited",
                    level="warn",
                    client_id_hash=hash(client_id) & 0xFFFF,
                    limit=decision.limit,
                    retry_after_seconds=decision.retry_after_seconds,
                )
                raise RateLimitedError(
                    retry_after_seconds=decision.retry_after_seconds,
                    limit=decision.limit,
                )

        # Non-blocking acquire — 503 on contention
        try:
            await asyncio.wait_for(server_sem.acquire(), timeout=0.001)
        except TimeoutError as e:
            raise BusyError(retry_after_seconds=60) from e
        state["active_jobs"] += 1

        try:
            # Parse options JSON (tolerant of missing/malformed)
            try:
                opt_data = json.loads(options) if options else {}
            except json.JSONDecodeError:
                opt_data = {}
            opts = ConversionOptions(
                cache=bool(opt_data.get("cache", True)),
                log_level=opt_data.get("log_level"),
            )

            # License pre-check (fast fail before disk I/O)
            try:
                if license_mgr.is_expired():
                    raise LicenseExpiredError(license_mgr.expiry_date())
            except FileNotFoundError as e:
                raise LicenseExpiredError(None) from e

            # --- Input source selection (S3 integration) ---
            # Exactly one of `file` / `s3_input` is required; `s3_output` is
            # independent. Any S3 field requires the feature flag to be on.
            has_file = file is not None and (file.filename is not None or (file.size or 0) > 0)
            if (s3_input or s3_output) and not s.s3_enabled:
                raise S3DisabledError()
            if has_file and s3_input:
                raise InputSourceConflictError()
            if not has_file and not s3_input:
                raise MissingFileError("provide exactly one input source: 'file' or 's3_input'")

            # Resolve + allowlist-check the output target BEFORE rendering, so a
            # forbidden bucket fails fast with 400 rather than after a full
            # conversion. Streaming + upload happen later via _tee_to_s3.
            s3_out_target: tuple[str, str] | None = None
            if s3_output:
                out_bucket, out_key = s3_client.resolve_output_target(s3_output, rid, s)
                if not s3_client.is_output_bucket_allowed(out_bucket, s):
                    raise S3OutputForbiddenError(out_bucket)
                s3_out_target = (out_bucket, out_key)

            scratch_dir = s.scratch_dir / rid
            scratch_dir.mkdir(parents=True, exist_ok=True)
            input_path_tmp = scratch_dir / "input.tmp"

            # Acquire the input into input_path_tmp (size-bounded), then detect
            # format from the COMPLETE file. OOXML files (DOCX/PPTX/XLSX) place
            # `[Content_Types].xml` in the ZIP central directory near the END
            # of the archive — head-only detection misclassifies them.
            if s3_input:
                # Allowlist enforced inside download_to_path before any S3 call.
                await s3_client.download_to_path(s3_input, input_path_tmp, s)
                size = input_path_tmp.stat().st_size
                if size > s.max_input_bytes:
                    raise InputTooLargeError(size, s.max_input_bytes)
                source_filename: str | None = s3_client.parse_s3_url(s3_input)[1].rsplit("/", 1)[-1]
            else:
                assert file is not None
                source_filename = file.filename
                size = 0
                async with aiofiles.open(input_path_tmp, "wb") as dest:
                    while True:
                        block = await file.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > s.max_input_bytes:
                            raise InputTooLargeError(size, s.max_input_bytes)
                        await dest.write(block)
            if size == 0:
                raise MissingFileError("file is empty")

            # CSV has no magic bytes; the workers only speak XLSX/DOCX/PPTX/PDF.
            # Rewrite the buffered body in-place as XLSX so the rest of the
            # pipeline (detect → probe → workers) sees a normal spreadsheet.
            # Done in a thread to keep the event loop responsive on big CSVs.
            if is_csv_filename(source_filename):
                csv_bytes = await asyncio.to_thread(input_path_tmp.read_bytes)
                xlsx_bytes = await asyncio.to_thread(csv_bytes_to_xlsx_bytes, csv_bytes)
                if len(xlsx_bytes) > s.max_input_bytes:
                    raise InputTooLargeError(len(xlsx_bytes), s.max_input_bytes)
                await asyncio.to_thread(input_path_tmp.write_bytes, xlsx_bytes)
                size = len(xlsx_bytes)

            # Detect format from the buffered file: read a small head for the
            # magic-byte check, and pass the path so OOXML disambiguation can
            # use the zip central directory (which lives at the end of the file).
            # The uploaded filename is passed as a fallback hint for OLE2
            # legacy formats (.doc/.xls/.ppt) where the magic is ambiguous.
            async with aiofiles.open(input_path_tmp, "rb") as src:
                head = await src.read(min(size, 512))
            fmt = detect_format(head, source_path=input_path_tmp, filename=source_filename)

            oc_logging.emit_event(
                "format_detected",
                level="info",
                source_filename=source_filename or "",
                size_bytes=size,
                format=fmt,
            )

            # ODF/RTF files are routed to docx/xlsx/pptx workers, but Aspose's
            # loaders use the file extension as a format hint — renaming an ODT
            # to .docx makes Aspose.Words try to parse it as OOXML and fail with
            # FileCorruptedException. Preserve the original extension for these.
            # Image formats (png/jpg/etc.) also need the original extension
            # preserved so soffice's --convert-to pdf picks the right importer.
            image_ext_hints = {"jpg", "jpeg", "tif", "tiff"}
            ext_hint_formats = {"odt", "ods", "odp", "odg", "rtf"} | image_ext_hints
            # Suffix is a free-form filename hint, not a DispatchFormat — it
            # carries non-Aspose-product extensions like .odt back through to
            # Aspose's loaders so they pick the right LoadFormat.
            suffix: str = fmt
            if source_filename and "." in source_filename:
                orig_ext = source_filename.rsplit(".", 1)[-1].lower()
                if orig_ext in ext_hint_formats:
                    suffix = orig_ext
            input_path = scratch_dir / f"input.{suffix}"
            input_path_tmp.rename(input_path)

            # Formats that bypass the Aspose orchestrator entirely and go
            # through `soffice --convert-to pdf`. ODG was the original consumer
            # (Aspose.Total C++ has no drawing-page renderer); raster + vector
            # images followed because LibreOffice Draw is already installed,
            # imports the lot, and dropping Pillow/ImageMagick into the image
            # would just duplicate that capability.
            libreoffice_formats = {"odg", "png", "jpg", "tiff", "gif", "bmp", "webp", "svg"}
            if fmt in libreoffice_formats:
                # LibreOffice fallback. Aspose.Total C++ has no library that
                # renders these formats — drawing pages, raster images, or
                # vector SVG. We shell out to `soffice --headless
                # --convert-to pdf` (the bare `pdf` filter picks the right
                # importer per input extension) and stream the produced PDF
                # back. No chunking, no caching — one subprocess per request.
                lo_output_dir = scratch_dir / "lo_out"
                try:
                    pdf_path = await libreoffice_convert.convert_to_pdf(
                        input_path,
                        output_dir=lo_output_dir,
                        timeout_seconds=s.chunk_timeout_seconds,
                    )
                except BaseException:
                    shutil.rmtree(scratch_dir, ignore_errors=True)
                    raise

                async def stream_libreoffice() -> AsyncIterator[bytes]:
                    status = "failed"
                    error_code: str | None = "stream_aborted"
                    out_size = 0
                    try:
                        async with aiofiles.open(pdf_path, "rb") as fh:
                            while True:
                                block = await fh.read(64 * 1024)
                                if not block:
                                    break
                                out_size += len(block)
                                yield block
                        status = "success"
                        error_code = None
                    except ConversionError as e:
                        error_code = e.failure_class.value
                        raise
                    finally:
                        recent_store.record(
                            _build_record(
                                request_id=rid,
                                t0=t0,
                                source=recent_source,
                                input_filename=source_filename,
                                fmt=fmt,
                                s3_out_target=s3_out_target,
                                status=status,
                                error_code=error_code,
                                output_size_bytes=out_size if status == "success" else None,
                            )
                        )
                        shutil.rmtree(scratch_dir, ignore_errors=True)
                        state["active_jobs"] -= 1
                        server_sem.release()

                return StreamingResponse(
                    _tee_to_s3(stream_libreoffice(), s3_out_target, s),
                    media_type="application/pdf",
                    headers={
                        "X-Request-ID": rid,
                        "Content-Type": "application/pdf",
                        **_s3_output_headers(s3_out_target),
                    },
                )

            # EML goes through the Aspose.Email → MHTML → worker-docx PDF
            # pipeline (see office_convert.aspose_email_convert). Like the
            # libreoffice path it bypasses the chunk planner entirely: emails
            # are short, single-shot, and the two Aspose products' cs2cpp
            # frameworks must not coexist in one process.
            if fmt == "eml":
                email_scratch = scratch_dir / "email_out"
                try:
                    pdf_path = await aspose_email_convert.convert_to_pdf(
                        input_path,
                        scratch_dir=email_scratch,
                        settings=s,
                        request_id=rid,
                    )
                except BaseException:
                    shutil.rmtree(scratch_dir, ignore_errors=True)
                    raise

                async def stream_email_pdf() -> AsyncIterator[bytes]:
                    status = "failed"
                    error_code: str | None = "stream_aborted"
                    out_size = 0
                    try:
                        async with aiofiles.open(pdf_path, "rb") as fh:
                            while True:
                                block = await fh.read(64 * 1024)
                                if not block:
                                    break
                                out_size += len(block)
                                yield block
                        status = "success"
                        error_code = None
                    except ConversionError as e:
                        error_code = e.failure_class.value
                        raise
                    finally:
                        recent_store.record(
                            _build_record(
                                request_id=rid,
                                t0=t0,
                                source=recent_source,
                                input_filename=source_filename,
                                fmt=fmt,
                                s3_out_target=s3_out_target,
                                status=status,
                                error_code=error_code,
                                output_size_bytes=out_size if status == "success" else None,
                            )
                        )
                        shutil.rmtree(scratch_dir, ignore_errors=True)
                        state["active_jobs"] -= 1
                        server_sem.release()

                return StreamingResponse(
                    _tee_to_s3(stream_email_pdf(), s3_out_target, s),
                    media_type="application/pdf",
                    headers={
                        "X-Request-ID": rid,
                        "Content-Type": "application/pdf",
                        **_s3_output_headers(s3_out_target),
                    },
                )

            # All other formats go through the Aspose orchestrator. The
            # libreoffice_formats `if` above returned for everything outside
            # FormatName, but mypy can't narrow off a runtime-set membership
            # check. `cast` is safe by construction — the only DispatchFormat
            # values that reach here are exactly FormatName.
            assert fmt in ("docx", "pptx", "xlsx", "pdf"), f"unexpected dispatch format {fmt}"
            aspose_fmt = cast(FormatName, fmt)
            # Hand off to orchestrator; build the streaming response
            cache_local = cache if opts.cache else CacheManager(None, s.aspose_version)
            gen = orchestrator.convert_job(
                request_id=rid,
                input_path=input_path,
                format=aspose_fmt,
                options=opts,
                settings=s,
                cache=cache_local,
                scratch_dir=scratch_dir,
            )

            # Drive the generator far enough to materialize the first body
            # chunk (or exhaust it for empty results) BEFORE constructing the
            # StreamingResponse. Aspose load errors surface during the first
            # render — `FileCorruptedException` on a malformed ODT, RLIMIT_AS
            # kills on huge inputs, etc. If we let Starlette pull the first
            # chunk it will already have flushed `200 OK Content-Type:
            # application/pdf` headers; the ConversionError handler then
            # can't substitute a JSONResponse and Starlette logs
            # "Caught handled exception, but response already started" while
            # the client sees a successful response with an empty body.
            try:
                first_chunk = await gen.__anext__()
            except StopAsyncIteration:
                first_chunk = b""
            except BaseException as exc:
                # Generator failed before yielding — scratch needs cleanup
                # here because stream() never gets a chance to run. The outer
                # except handles semaphore release. Record a failed entry
                # for the dashboard since format detection succeeded.
                err_code = (
                    exc.failure_class.value if isinstance(exc, ConversionError) else "render_failed"
                )
                recent_store.record(
                    _build_record(
                        request_id=rid,
                        t0=t0,
                        source=recent_source,
                        input_filename=source_filename,
                        fmt=fmt,
                        s3_out_target=s3_out_target,
                        status="failed",
                        error_code=err_code,
                        output_size_bytes=None,
                    )
                )
                shutil.rmtree(scratch_dir, ignore_errors=True)
                raise

            async def stream() -> AsyncIterator[bytes]:
                status = "failed"
                error_code: str | None = "stream_aborted"
                out_size = 0
                try:
                    if first_chunk:
                        out_size += len(first_chunk)
                        yield first_chunk
                    async for block in gen:
                        out_size += len(block)
                        yield block
                    status = "success"
                    error_code = None
                except ConversionError as e:
                    error_code = e.failure_class.value
                    raise
                finally:
                    # Recent-conversions ring buffer
                    recent_store.record(
                        _build_record(
                            request_id=rid,
                            t0=t0,
                            source=recent_source,
                            input_filename=source_filename,
                            fmt=fmt,
                            s3_out_target=s3_out_target,
                            status=status,
                            error_code=error_code,
                            output_size_bytes=out_size if status == "success" else None,
                        )
                    )
                    # Cleanup scratch + release semaphore
                    shutil.rmtree(scratch_dir, ignore_errors=True)
                    state["active_jobs"] -= 1
                    server_sem.release()

            # Pre-compute headers; ConversionResult fields fill in after the
            # generator runs, so we update them post-hoc (clients see only
            # headers that exist at start; the metadata headers are sent via
            # trailers... but FastAPI doesn't support HTTP trailers cleanly).
            # Compromise: emit a best-effort fixed set; precise values are in logs.
            headers = {
                "X-Request-ID": rid,
                "Content-Type": "application/pdf",
                **rate_headers,
                **_s3_output_headers(s3_out_target),
            }
            return StreamingResponse(
                _tee_to_s3(stream(), s3_out_target, s),
                media_type="application/pdf",
                headers=headers,
            )
        except BaseException:
            state["active_jobs"] -= 1
            server_sem.release()
            raise

    @app.get("/health")
    async def health() -> JSONResponse:
        snap = health_checker.snapshot(state["active_jobs"])
        status_code = 200 if snap["ready"] else 503
        return JSONResponse(status_code=status_code, content=snap)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def landing() -> HTMLResponse:
        ready = bool(health_checker.snapshot(state["active_jobs"]).get("ready"))
        return HTMLResponse(
            _LANDING_HTML.replace("{{STATUS_LABEL}}", "READY" if ready else "NOT READY")
            .replace("{{STATUS_CLASS}}", "ok" if ready else "err")
            .replace("{{VERSION}}", app.version)
        )

    @v1.get("/jobs/{request_id}/heartbeats")
    async def get_heartbeats(request_id: str) -> JSONResponse:
        from office_convert.heartbeats import heartbeat_store

        beats = heartbeat_store().get(request_id)
        return JSONResponse(content={"request_id": request_id, "heartbeats": beats})

    @v1.get("/jobs/{request_id}/timings")
    async def get_timings(request_id: str) -> JSONResponse:
        from office_convert.timings import timing_store

        events = timing_store().get(request_id)
        return JSONResponse(content={"request_id": request_id, "timings": events})

    @v1.get("/stats")
    async def container_stats() -> JSONResponse:
        from office_convert.container_stats import read_container_stats

        return JSONResponse(content=read_container_stats())

    @v1.delete("/cache")
    async def clear_cache() -> JSONResponse:
        """Wipe the on-disk conversion cache. No-op success (200) when the
        cache is disabled (no OFFICE_CONVERT_CACHE_DIR set) — the caller
        can branch on `enabled` in the response. No auth gate: v1 has no
        app-layer auth (per requirements Q6=X), so this is consistent
        with the rest of the API surface."""
        return JSONResponse(content=cache.clear())

    @v1.get("/workers")
    async def container_workers() -> JSONResponse:
        from office_convert.container_stats import list_workers

        return JSONResponse(content={"workers": list_workers()})

    @v1.get("/jobs/{request_id}/progress")
    async def get_progress(request_id: str) -> JSONResponse:
        from office_convert.job_progress import job_progress_store

        jp = job_progress_store().get(request_id)
        if jp is None:
            return JSONResponse(
                content={
                    "request_id": request_id,
                    "phase": "unknown",
                    "total_chunks": 0,
                    "chunks_rendered": 0,
                    "load_progress": 0.0,
                    "merge_done": 0.0,
                    "weighted_percent": 0.0,
                    "elapsed_s": 0.0,
                }
            )
        return JSONResponse(content={"request_id": request_id, **jp.to_dict()})

    @v1.get("/downloads/presign")
    async def presign_download(bucket: str, key: str) -> JSONResponse:
        """Mint a short-TTL presigned GET URL for an S3 output object.

        The service owns the S3 credentials (IRSA); clients never see them,
        only a time-boxed URL. The output-bucket allowlist is enforced inside
        s3_client BEFORE signing. A fresh URL is minted per call because
        presigned URLs expire — that is why this is a callable endpoint rather
        than a one-shot header on /convert.
        """
        if not s.s3_enabled:
            raise S3DisabledError()
        url = s3_client.generate_presigned_get_url(bucket, key, s)
        expires_at = datetime.now(UTC) + timedelta(seconds=s.s3_presign_ttl_seconds)
        oc_logging.emit_event("s3_presign", level="info", bucket=bucket, key=key)
        return JSONResponse(
            content={
                "download_url": url,
                "bucket": bucket,
                "key": key,
                "expires_in_seconds": s.s3_presign_ttl_seconds,
                "expires_at": expires_at.isoformat(),
            }
        )

    app.include_router(v1)
    return app


# Module-level `app` for `uvicorn office_convert.server:app`
app = create_app()
