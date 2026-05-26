"""Aspose.Email pipeline for EML → PDF.

Email rendering is a two-stage pipeline because Aspose.Email itself has no
PDF writer:

  1. office-convert-worker-email  loads EML, saves MHTML.
  2. office-convert-worker-docx   loads MHTML, saves PDF.

The two workers run in distinct processes so Aspose.Email's CodePorting
framework (25.12) never coexists with Aspose.Words's (26.3) in one address
space — the same isolation that the 4-binary split already provides for
Words/Cells/Slides/PDF.

Per-request shape mirrors `libreoffice_convert.py`: a single async function
that produces a PDF path. Emails are short and single-shot, so the chunk
planner is bypassed entirely; the orchestrator never sees an EML.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from office_convert.errors import (
    ConversionError,
    InputUnprocessableError,
    LicenseExpiredError,
    RenderError,
)
from office_convert.logging import emit_event

if TYPE_CHECKING:
    from office_convert.config import Settings

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RENDER_FAILURE = 1
EXIT_LICENSE_INVALID = 2
EXIT_INPUT_UNPROCESSABLE = 3
EXIT_OOM = 137

DEFAULT_EMAIL_TIMEOUT_SECONDS = 120


async def convert_to_pdf(
    input_path: Path,
    scratch_dir: Path,
    settings: Settings,
    *,
    request_id: str,
) -> Path:
    """Convert an EML at `input_path` to a PDF under `scratch_dir`.

    Returns the produced PDF path. Raises one of OOMError,
    LicenseExpiredError, InputUnprocessableError, RenderError on failure —
    same typed exceptions the orchestrator raises, so the FastAPI handler's
    diagnostic mapping works uniformly.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    mht_path = scratch_dir / f"{input_path.stem}.mht"
    pdf_path = scratch_dir / f"{input_path.stem}.pdf"

    # Stage 1: EML → MHT via worker-email.
    await _run_worker(
        worker="email",
        mode="render",
        input_path=input_path,
        output_path=mht_path,
        page_range=(1, 1),  # emails don't paginate; placeholder satisfies CLI.
        settings=settings,
        request_id=request_id,
    )
    if not mht_path.exists():
        raise RenderError(
            chunk=None,
            exit_code=0,
            stderr_tail="worker-email exited 0 but produced no MHT output",
        )

    # Stage 2: probe MHT via worker-docx to learn the rendered page count.
    probe_stdout, _ = await _run_worker(
        worker="docx",
        mode="probe",
        input_path=mht_path,
        output_path=None,
        page_range=None,
        settings=settings,
        request_id=request_id,
        capture_stdout=True,
    )
    try:
        probe = json.loads(probe_stdout.decode("utf-8", errors="replace"))
        page_count = int(probe.get("page_count", 0))
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        raise RenderError(
            chunk=None,
            exit_code=0,
            stderr_tail=f"worker-docx probe returned malformed JSON: {e}",
        ) from e
    if page_count < 1:
        raise RenderError(
            chunk=None,
            exit_code=0,
            stderr_tail=f"worker-docx probe reported page_count={page_count}",
        )

    # Stage 3: MHT → PDF via worker-docx render across the full page range.
    await _run_worker(
        worker="docx",
        mode="render",
        input_path=mht_path,
        output_path=pdf_path,
        page_range=(1, page_count),
        settings=settings,
        request_id=request_id,
    )
    if not pdf_path.exists():
        raise RenderError(
            chunk=None,
            exit_code=0,
            stderr_tail="worker-docx exited 0 but produced no PDF output",
        )
    return pdf_path


async def _run_worker(
    *,
    worker: Literal["email", "docx"],
    mode: Literal["render", "probe"],
    input_path: Path,
    output_path: Path | None,
    page_range: tuple[int, int] | None,
    settings: Settings,
    request_id: str,
    capture_stdout: bool = False,
) -> tuple[bytes, bytes]:
    """Spawn one worker binary under prlimit. Return (stdout, stderr) bytes.

    Format flag is keyed off `worker` directly — worker-email handles
    `--format email`, worker-docx handles `--format docx` (MHTML rides the
    same code path as DOCX inside Aspose.Words; `LoadFormat::Mhtml` is
    selected by the SDK based on file content, not extension).
    """
    binary = f"{settings.worker_binary_prefix}-{worker}"
    fmt = "email" if worker == "email" else "docx"
    argv: list[str] = [
        "prlimit",
        f"--as={settings.worker_ram_bytes}",
        "--",
        binary,
        "--mode",
        mode,
        "--input",
        str(input_path),
        "--format",
        fmt,
        "--license-path",
        str(settings.license_path),
    ]
    if mode == "render":
        assert output_path is not None
        assert page_range is not None
        argv.extend(
            [
                "--output",
                str(output_path),
                "--page-range",
                f"{page_range[0]}-{page_range[1]}",
            ]
        )
    log.debug("spawning email-pipeline worker [%s]: %s", request_id, shlex.join(argv))

    stdout_setting = asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=stdout_setting,
        stderr=asyncio.subprocess.PIPE,
    )
    emit_event(
        "worker_spawn",
        level="info",
        worker=fmt,
        mode=mode,
        pid=proc.pid,
        page_range=list(page_range) if page_range is not None else None,
        chunk_index=None,
    )
    spawned_at = time.monotonic()

    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=DEFAULT_EMAIL_TIMEOUT_SECONDS)
    except TimeoutError:
        log.warning("email-pipeline worker timeout [%s]; killing", request_id)
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        emit_event(
            "worker_exit",
            level="warn",
            worker=fmt,
            mode=mode,
            pid=proc.pid,
            exit_code=-1,
            outcome="timeout",
            duration_s=round(time.monotonic() - spawned_at, 3),
            chunk_index=None,
        )
        raise RenderError(
            chunk=None,
            exit_code=-1,
            stderr_tail=f"worker-{worker} timeout after {DEFAULT_EMAIL_TIMEOUT_SECONDS}s",
        ) from None

    assert proc.stderr is not None
    stderr_bytes = await proc.stderr.read()
    stdout_bytes = b""
    if capture_stdout:
        assert proc.stdout is not None
        stdout_bytes = await proc.stdout.read()

    emit_event(
        "worker_exit",
        level="info" if rc == EXIT_OK else "warn",
        worker=fmt,
        mode=mode,
        pid=proc.pid,
        exit_code=rc,
        outcome="ok" if rc == EXIT_OK else "error",
        duration_s=round(time.monotonic() - spawned_at, 3),
        stderr_bytes=len(stderr_bytes),
        chunk_index=None,
    )
    _map_exit_code(rc, stderr_bytes, worker)
    return stdout_bytes, stderr_bytes


def _map_exit_code(rc: int, stderr_bytes: bytes, worker: str) -> None:
    """Translate exit code into a typed exception. Chunk is always None here
    because emails don't go through the chunk planner."""
    if rc == EXIT_OK:
        return
    stderr_tail = stderr_bytes[-1024:].decode("utf-8", errors="replace")
    if rc == EXIT_OOM:
        raise RenderError(chunk=None, exit_code=rc, stderr_tail=f"{worker} OOM: {stderr_tail}")
    if rc == EXIT_LICENSE_INVALID:
        raise LicenseExpiredError(None)
    if rc == EXIT_INPUT_UNPROCESSABLE:
        raise InputUnprocessableError(stderr_tail or f"{worker} input unprocessable")
    raise RenderError(chunk=None, exit_code=rc, stderr_tail=f"{worker}: {stderr_tail}")


class AsposeEmailRenderError(ConversionError):
    """Catch-all marker (placeholder for any future email-specific failures
    that don't fit the existing typed errors)."""

    failure_class = RenderError.failure_class
    http_status = 500


__all__ = ["convert_to_pdf", "AsposeEmailRenderError"]
