"""Persistent worker process pool for amortizing document-load cost.

Strategy #4 from the performance improvement plan: instead of spawning a
new subprocess per chunk (each paying process-start + full-document-load),
maintain a pool of long-lived worker processes that load the document once
and render multiple page ranges on demand.

Protocol (line-delimited JSON over stdin/stdout):
  → {"cmd": "load", "input": "/path/to/file", "license_path": "/path/to/lic"}
  ← {"status": "ok", "page_count": N}

  → {"cmd": "render", "page_start": 1, "page_end": 10, "output": "/tmp/chunk-0.pdf"}
  ← {"status": "ok", "output": "/tmp/chunk-0.pdf"}

  → {"cmd": "quit"}
  ← (process exits cleanly)

Error response:
  ← {"status": "error", "code": 1|2|3|137, "detail": "..."}

This module provides the Python-side pool manager. The C++ worker needs a
corresponding `--mode=pool` that enters a read-eval-render loop instead of
the current one-shot mode. Until the C++ side is updated, this module falls
back to the existing one-shot subprocess model transparently.

The pool is per-request (one document loaded per pool), not global, because
each worker holds the full document in memory and we don't want stale docs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

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

# Exit codes matching the one-shot worker contract
EXIT_OK = 0
EXIT_RENDER_FAILURE = 1
EXIT_LICENSE_INVALID = 2
EXIT_INPUT_UNPROCESSABLE = 3
EXIT_OOM = 137


class PooledWorker:
    """A single persistent worker process that holds a loaded document.

    Lifecycle: spawn → load document → render N chunks → quit.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        format: FormatName,
        pid: int,
    ) -> None:
        self._proc = proc
        self._format = format
        self._pid = pid
        self._busy = False
        self._loaded = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def alive(self) -> bool:
        return self._proc.returncode is None

    async def load_document(
        self,
        input_path: Path,
        license_path: Path,
    ) -> int:
        """Send load command. Returns page_count on success."""
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None

        cmd = json.dumps({
            "cmd": "load",
            "input": str(input_path),
        }) + "\n"
        self._proc.stdin.write(cmd.encode())
        await self._proc.stdin.drain()

        response = await asyncio.wait_for(
            self._proc.stdout.readline(), timeout=120
        )
        result = json.loads(response)
        if result["status"] != "ok":
            self._raise_error(result, chunk=None)
        self._loaded = True
        return result["page_count"]

    async def render_chunk(
        self,
        chunk: Chunk,
        output_path: Path,
        timeout: int = 600,
    ) -> Path:
        """Send render command for a page range. Returns output path."""
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        assert self._loaded

        self._busy = True
        try:
            cmd = json.dumps({
                "cmd": "render",
                "page_start": chunk.page_range[0],
                "page_end": chunk.page_range[1],
                "output": str(output_path),
            }) + "\n"
            self._proc.stdin.write(cmd.encode())
            await self._proc.stdin.drain()

            response = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=timeout
            )
            result = json.loads(response)
            if result["status"] != "ok":
                self._raise_error(result, chunk)
            return output_path
        finally:
            self._busy = False

    async def quit(self) -> None:
        """Gracefully shut down the worker."""
        if not self.alive:
            return
        assert self._proc.stdin is not None
        try:
            cmd = json.dumps({"cmd": "quit"}) + "\n"
            self._proc.stdin.write(cmd.encode())
            await self._proc.stdin.drain()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (TimeoutError, OSError, BrokenPipeError):
            self._proc.kill()
            await self._proc.wait()

    async def kill(self) -> None:
        """Force-kill the worker."""
        if self.alive:
            self._proc.kill()
            with suppress(Exception):
                await self._proc.wait()

    def _raise_error(self, result: dict, chunk: Chunk | None) -> None:
        code = result.get("code", EXIT_RENDER_FAILURE)
        detail = result.get("detail", "unknown error")
        if code == EXIT_OOM:
            if chunk:
                raise OOMError(chunk)
            raise RenderError(chunk, exit_code=code, stderr_tail=detail)
        if code == EXIT_LICENSE_INVALID:
            raise LicenseExpiredError(None)
        if code == EXIT_INPUT_UNPROCESSABLE:
            raise InputUnprocessableError(detail)
        raise RenderError(chunk, exit_code=code, stderr_tail=detail)


class WorkerPool:
    """Per-request pool of persistent workers sharing the same loaded document.

    Usage:
        async with WorkerPool(format, input_path, settings, pool_size=4) as pool:
            results = await pool.render_chunks(chunks, scratch_dir)
    """

    def __init__(
        self,
        format: FormatName,
        input_path: Path,
        settings: Settings,
        pool_size: int = 4,
    ) -> None:
        self._format = format
        self._input_path = input_path
        self._settings = settings
        self._pool_size = pool_size
        self._workers: list[PooledWorker] = []

    async def __aenter__(self) -> "WorkerPool":
        await self._spawn_workers()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._shutdown()

    async def _spawn_workers(self) -> None:
        """Spawn pool_size workers and load the document in each."""
        worker_binary = f"{self._settings.worker_binary_prefix}-{self._format}"

        for i in range(self._pool_size):
            argv = [
                "prlimit",
                f"--as={self._settings.worker_ram_bytes}",
                "--",
                worker_binary,
                "--mode", "pool",
                "--format", self._format,
                "--license-path", str(self._settings.license_path),
            ]

            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            worker = PooledWorker(proc, self._format, proc.pid or 0)
            emit_event(
                "pool_worker_spawn",
                level="info",
                worker=self._format,
                pool_index=i,
                pid=proc.pid,
            )

            try:
                page_count = await worker.load_document(
                    self._input_path,
                    self._settings.license_path,
                )
                emit_event(
                    "pool_worker_loaded",
                    level="info",
                    worker=self._format,
                    pool_index=i,
                    page_count=page_count,
                )
                self._workers.append(worker)
            except Exception as e:
                log.warning("pool worker %d failed to load: %s", i, e)
                await worker.kill()
                raise

    async def _shutdown(self) -> None:
        """Gracefully shut down all workers."""
        for w in self._workers:
            await w.quit()
        self._workers.clear()

    async def render_chunk(self, chunk: Chunk, scratch_dir: Path) -> Path:
        """Render a single chunk using the next available worker."""
        # Simple round-robin; the semaphore in the orchestrator already
        # limits concurrency to pool_size.
        worker = self._workers[int(chunk.index) % len(self._workers)]
        output_path = scratch_dir / f"chunk-{chunk.index}.pdf"
        return await worker.render_chunk(
            chunk, output_path, timeout=self._settings.chunk_timeout_seconds
        )


def pool_mode_available(settings: Settings, format: FormatName) -> bool:
    """Check if the worker binary supports --mode=pool.

    Pool mode is now implemented in the C++ worker (main.cpp dispatches to
    pool_loop which reads JSON commands from stdin). Enabled by default for
    all formats. Set OFFICE_CONVERT_POOL_MODE=0 to force one-shot fallback.
    """
    import os
    return os.environ.get("OFFICE_CONVERT_POOL_MODE", "1") != "0"
