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
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import aiofiles
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from office_convert import logging as oc_logging
from office_convert import orchestrator
from office_convert.cache import CacheManager
from office_convert.config import Settings, get_settings
from office_convert.errors import (
    BusyError,
    ConversionError,
    InputTooLargeError,
    LicenseExpiredError,
    MissingFileError,
)
from office_convert.license import LicenseManager
from office_convert.probe import ACCEPTED_FORMATS, detect_format
from office_convert.types import (
    ConversionOptions,
    Diagnostic,
)

log = logging.getLogger(__name__)


class HealthChecker:
    """Hybrid health check: static at startup, live for license."""

    def __init__(self, settings: Settings, license_mgr: LicenseManager) -> None:
        self.settings = settings
        self.license_mgr = license_mgr
        self.static_problems: list[str] = []

        # Verify all four per-format worker binaries are present. Each format
        # is served by its own binary to keep Aspose's CodePorting framework
        # versions from colliding in one process.
        prefix = settings.worker_binary_prefix
        missing = [fmt for fmt in ACCEPTED_FORMATS
                   if not prefix.with_name(f"{prefix.name}-{fmt}").exists()]
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


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory. Tests build a custom-settings app via this."""
    s = settings if settings is not None else get_settings()
    oc_logging.configure(format=s.log_format, level=s.log_level)

    license_mgr = LicenseManager(s.license_path)
    health_checker = HealthChecker(s, license_mgr)
    cache = CacheManager(s.cache_dir, s.aspose_version)
    server_sem = asyncio.Semaphore(s.max_jobs)
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

    @app.post("/convert")
    async def convert(
        file: Annotated[UploadFile, File(...)],
        options: Annotated[str, Form()] = "{}",
    ) -> StreamingResponse:
        rid = oc_logging.current_request_id.get()
        oc_logging.emit_event("request_received", level="info")

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

            # Validate file field is present before buffering.
            if file.filename is None and file.size == 0:
                raise MissingFileError("file field is required")

            scratch_dir = s.scratch_dir / rid
            scratch_dir.mkdir(parents=True, exist_ok=True)

            # Buffer body to scratch FIRST (size-bounded), then detect format
            # from the complete file. OOXML files (DOCX/PPTX/XLSX) place
            # `[Content_Types].xml` in the ZIP central directory near the END
            # of the archive — the previous 512-byte head-only detection
            # misclassified anything but tiny DOCX as DOCX by default.
            input_path_tmp = scratch_dir / "input.tmp"
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

            # Detect format from the buffered file: read a small head for the
            # magic-byte check, and pass the path so OOXML disambiguation can
            # use the zip central directory (which lives at the end of the file).
            # The uploaded filename is passed as a fallback hint for OLE2
            # legacy formats (.doc/.xls/.ppt) where the magic is ambiguous.
            async with aiofiles.open(input_path_tmp, "rb") as src:
                head = await src.read(min(size, 512))
            fmt = detect_format(head, source_path=input_path_tmp, filename=file.filename)

            oc_logging.emit_event(
                "format_detected",
                level="info",
                source_filename=file.filename or "",
                size_bytes=size,
                format=fmt,
            )

            # Rename to the format-specific extension.
            input_path = scratch_dir / f"input.{fmt}"
            input_path_tmp.rename(input_path)

            # Hand off to orchestrator; build the streaming response
            cache_local = cache if opts.cache else CacheManager(None, s.aspose_version)
            gen = orchestrator.convert_job(
                request_id=rid,
                input_path=input_path,
                format=fmt,
                options=opts,
                settings=s,
                cache=cache_local,
                scratch_dir=scratch_dir,
            )

            async def stream() -> AsyncIterator[bytes]:
                try:
                    async for block in gen:
                        yield block
                finally:
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
            }
            return StreamingResponse(
                stream(),
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

    return app


# Module-level `app` for `uvicorn office_convert.server:app`
app = create_app()
