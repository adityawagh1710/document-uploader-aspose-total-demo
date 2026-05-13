"""Orchestrator-side wrapper around the C++ worker subprocess.

Implements FR-6 (subprocess isolation) and FR-4 (OOM → subdivide signal).
Applies `prlimit RLIMIT_AS=2G` via external CLI per nfr-design-patterns.md §1.
Maps documented exit codes to typed exceptions.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from office_convert.errors import (
    InputUnprocessableError,
    LicenseExpiredError,
    OOMError,
    RenderError,
)
from office_convert.logging import emit_event
from office_convert.types import Chunk, FormatName

if TYPE_CHECKING:
    from office_convert.config import Settings

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RENDER_FAILURE = 1
EXIT_LICENSE_INVALID = 2
EXIT_INPUT_UNPROCESSABLE = 3
EXIT_OOM = 137


async def render_chunk(
    chunk: Chunk,
    input_path: Path,
    format: FormatName,
    scratch_dir: Path,
    request_id: str,
    settings: Settings,
) -> Path:
    """Spawn a worker subprocess to render one chunk.

    Returns the path of the produced chunk PDF on success.
    Raises one of: OOMError, LicenseExpiredError, InputUnprocessableError, RenderError.
    """
    output_path = scratch_dir / f"chunk-{chunk.index}.pdf"
    _, stderr = await _run_worker(
        mode="render",
        input_path=input_path,
        format=format,
        output_path=output_path,
        page_range=chunk.page_range,
        request_id=request_id,
        settings=settings,
        capture_stdout=False,
        chunk=chunk,
    )
    log.debug("render_chunk %s completed (stderr_len=%d)", chunk.index, len(stderr))
    return output_path


async def _run_worker(
    *,
    mode: Literal["render", "probe"],
    input_path: Path,
    format: FormatName,
    output_path: Path | None,
    page_range: tuple[int, int] | None,
    request_id: str,
    settings: Settings,
    capture_stdout: bool,
    chunk: Chunk | None = None,
) -> tuple[bytes, bytes]:
    """Run the worker binary under prlimit. Return (stdout_bytes, stderr_bytes).

    Raises typed exceptions on non-zero exit.
    """
    # Per-product binary: each Aspose library lives in its own worker so the
    # CodePorting framework versions never collide in one process. The Python
    # orchestrator picks the right one from the format.
    worker_binary = f"{settings.worker_binary_prefix}-{format}"
    argv = [
        "prlimit",
        f"--as={settings.worker_ram_bytes}",
        "--",
        worker_binary,
        "--mode",
        mode,
        "--input",
        str(input_path),
        "--format",
        format,
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
    log.debug("spawning worker [%s]: %s", request_id, shlex.join(argv))

    stdout_setting = asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=stdout_setting,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    emit_event(
        "worker_spawn",
        level="info",
        worker=format,
        mode=mode,
        pid=proc.pid,
        page_range=list(page_range) if page_range is not None else None,
        chunk_index=chunk.index if chunk is not None else None,
    )
    spawned_at = time.monotonic()
    stdout_task: asyncio.Task[bytes] | None = None
    if capture_stdout:
        assert proc.stdout is not None
        stdout_task = asyncio.create_task(proc.stdout.read())

    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=settings.chunk_timeout_seconds)
    except TimeoutError:
        log.warning("worker timeout [%s]; killing", request_id)
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        # Drain pipes so their transports close cleanly. Otherwise asyncio's
        # _UnixReadPipeTransport.__del__ raises ResourceWarning at GC time,
        # which pytest's filterwarnings=["error"] config promotes to a hard
        # error attached to whatever test runs next.
        await _drain_streams(proc, stdout_task)
        emit_event(
            "worker_exit",
            level="warn",
            worker=format,
            mode=mode,
            pid=proc.pid,
            exit_code=-1,
            outcome="timeout",
            duration_s=round(time.monotonic() - spawned_at, 3),
            chunk_index=chunk.index if chunk is not None else None,
        )
        raise RenderError(chunk, exit_code=-1, stderr_tail="timeout exceeded") from None

    stderr_bytes = await proc.stderr.read()
    stdout_bytes = b""
    if stdout_task is not None:
        try:
            stdout_bytes = await stdout_task
        except asyncio.CancelledError:
            stdout_bytes = b""

    emit_event(
        "worker_exit",
        level="info" if rc == EXIT_OK else "warn",
        worker=format,
        mode=mode,
        pid=proc.pid,
        exit_code=rc,
        outcome="ok" if rc == EXIT_OK else "error",
        duration_s=round(time.monotonic() - spawned_at, 3),
        stderr_bytes=len(stderr_bytes),
        chunk_index=chunk.index if chunk is not None else None,
    )
    _map_exit_code(rc, stderr_bytes, chunk)
    return stdout_bytes, stderr_bytes


async def _drain_streams(
    proc: asyncio.subprocess.Process,
    stdout_task: asyncio.Task[bytes] | None,
) -> None:
    """Drain stdout/stderr after a kill so their pipe transports close cleanly.

    Without this, asyncio's `_UnixReadPipeTransport.__del__` complains at GC
    time, leaking a `ResourceWarning` that pytest can promote to a hard error.
    """
    if stdout_task is not None and not stdout_task.done():
        stdout_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await stdout_task
    if proc.stderr is not None:
        with suppress(Exception):
            await proc.stderr.read()


def _map_exit_code(rc: int, stderr_bytes: bytes, chunk: Chunk | None) -> None:
    """Translate worker exit code to a typed exception (or return on rc=0)."""
    if rc == EXIT_OK:
        return
    stderr_tail = stderr_bytes[-1024:].decode("utf-8", errors="replace")
    if rc == EXIT_OOM:
        if chunk is not None:
            raise OOMError(chunk)
        raise RenderError(chunk, exit_code=rc, stderr_tail=stderr_tail)
    if rc == EXIT_LICENSE_INVALID:
        raise LicenseExpiredError(None)
    if rc == EXIT_INPUT_UNPROCESSABLE:
        raise InputUnprocessableError(stderr_tail or "input unprocessable")
    # Treat anything else as render failure (includes negative rc from signals).
    raise RenderError(chunk, exit_code=rc, stderr_tail=stderr_tail)
