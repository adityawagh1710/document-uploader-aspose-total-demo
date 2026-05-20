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
from typing import TYPE_CHECKING, Any

from office_convert.errors import (
    InputUnprocessableError,
    LicenseExpiredError,
    OOMError,
    RenderError,
)
from office_convert.heartbeats import heartbeat_store
from office_convert.job_progress import job_progress_store
from office_convert.logging import current_request_id, emit_event
from office_convert.timings import timing_store
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
        pool_index: int = 0,
    ) -> None:
        self._proc = proc
        self._format = format
        self._pid = pid
        self._pool_index = pool_index
        self._busy = False
        self._loaded = False
        self._stderr_task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def alive(self) -> bool:
        return self._proc.returncode is None

    def start_stderr_reader(self) -> None:
        """Begin draining stderr into the structured log.

        The C++ worker emits a per-second heartbeat JSON line to stderr while a
        load or render is in flight ({"type":"heartbeat","phase":"load",...}).
        Without this drain the buffer would fill and block the worker, and we
        would learn nothing during the 600s pool_load_timeout window. Non-JSON
        stderr (Aspose warnings) passes through as a warning log line.
        """
        if self._proc.stderr is None or self._stderr_task is not None:
            return
        self._stderr_task = asyncio.create_task(
            self._read_stderr(),
            name=f"pool-stderr-{self._format}-{self._pool_index}",
        )

    async def _read_stderr(self) -> None:
        assert self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                self._handle_stderr_line(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "pool stderr reader crashed (worker=%s pool_index=%s)",
                self._format,
                self._pool_index,
            )

    def _handle_stderr_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").rstrip()
        if not text:
            return
        msg = self._try_parse_typed_json(text)
        if msg is None:
            self._log_unparsed_stderr(text)
            return
        msg_type = msg.get("type")
        if msg_type == "load_progress":
            self._handle_load_progress(msg)
        elif msg_type == "heartbeat":
            self._handle_heartbeat(msg)
        elif msg_type == "timing":
            self._handle_timing(msg)
        else:
            self._log_unparsed_stderr(text)

    @staticmethod
    def _try_parse_typed_json(text: str) -> dict[str, Any] | None:
        if not text.startswith('{"type":"'):
            return None
        try:
            msg = json.loads(text)
        except ValueError:
            return None
        return msg if isinstance(msg, dict) else None

    def _log_unparsed_stderr(self, text: str) -> None:
        log.warning(
            "pool worker stderr (worker=%s pool_index=%s pid=%s): %s",
            self._format,
            self._pool_index,
            self._pid,
            text,
        )

    def _handle_load_progress(self, msg: dict[str, Any]) -> None:
        value = msg.get("value")
        if not isinstance(value, int | float):
            return
        rid = current_request_id.get()
        if rid and rid != "-":
            job_progress_store().update(rid, load_progress=float(value))

    def _handle_heartbeat(self, msg: dict[str, Any]) -> None:
        hb_record = {
            "worker": self._format,
            "pool_index": self._pool_index,
            "pid": self._pid,
            "phase": msg.get("phase"),
            "elapsed_s": msg.get("elapsed_s"),
            "rss_bytes": msg.get("rss_bytes"),
            "swap_bytes": msg.get("swap_bytes"),
            "cpu_jiffies": msg.get("cpu_jiffies"),
            "wall_ts": time.time(),
        }
        emit_event("pool_worker_heartbeat", level="debug", **hb_record)
        rid = current_request_id.get()
        if rid and rid != "-":
            heartbeat_store().record(rid, hb_record)

    def _handle_timing(self, msg: dict[str, Any]) -> None:
        # Forward every field except the dispatcher discriminator —
        # this way different stages can carry different fields
        # (e.g. `pool_render.summary` carries pages/per_page_ms in
        # addition to the base duration_ms) without changing this
        # parser.
        payload = {k: v for k, v in msg.items() if k != "type"}
        emit_event(
            "pool_worker_timing",
            level="info",
            worker=self._format,
            pool_index=self._pool_index,
            pid=self._pid,
            **payload,
        )
        rid = current_request_id.get()
        if rid and rid != "-":
            timing_store().record(
                rid,
                {
                    "worker": self._format,
                    "pool_index": self._pool_index,
                    "pid": self._pid,
                    "wall_ts": time.time(),
                    **payload,
                },
            )

    async def _cancel_stderr_task(self) -> None:
        if self._stderr_task is None:
            return
        if not self._stderr_task.done():
            self._stderr_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self._stderr_task
        self._stderr_task = None

    async def load_document(
        self,
        input_path: Path,
        license_path: Path,
    ) -> int:
        """Send load command. Returns page_count on success."""
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None

        cmd = (
            json.dumps(
                {
                    "cmd": "load",
                    "input": str(input_path),
                }
            )
            + "\n"
        )
        self._proc.stdin.write(cmd.encode())
        await self._proc.stdin.drain()

        response = await asyncio.wait_for(self._proc.stdout.readline(), timeout=600)
        result = json.loads(response)
        if result["status"] != "ok":
            self._raise_error(result, chunk=None)
        self._loaded = True
        return int(result["page_count"])

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
            cmd = (
                json.dumps(
                    {
                        "cmd": "render",
                        "page_start": chunk.page_range[0],
                        "page_end": chunk.page_range[1],
                        "output": str(output_path),
                    }
                )
                + "\n"
            )
            self._proc.stdin.write(cmd.encode())
            await self._proc.stdin.drain()

            response = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
            result = json.loads(response)
            if result["status"] != "ok":
                self._raise_error(result, chunk)
            return output_path
        finally:
            self._busy = False

    async def quit(self) -> None:
        """Gracefully shut down the worker."""
        if not self.alive:
            await self._cancel_stderr_task()
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
        await self._cancel_stderr_task()

    async def kill(self) -> None:
        """Force-kill the worker."""
        if self.alive:
            self._proc.kill()
            with suppress(Exception):
                await self._proc.wait()
        await self._cancel_stderr_task()

    def _raise_error(self, result: dict[str, Any], chunk: Chunk | None) -> None:
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
        self._available: asyncio.Queue[PooledWorker] | None = None
        self.actual_page_count: int | None = None  # Set after first worker loads

    async def __aenter__(self) -> WorkerPool:
        await self._spawn_workers()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._shutdown()

    async def _spawn_workers(self) -> None:
        """Spawn pool_size workers and load the document in each (in parallel).

        Each worker's subprocess spawn AND document load are launched concurrently
        via asyncio.gather. For a pool_size of 4 with a 5s per-worker load, total
        startup latency drops from ~20s (sequential) to ~5s (parallel).
        """
        worker_binary = f"{self._settings.worker_binary_prefix}-{self._format}"
        self._available = asyncio.Queue()
        argv_base = [
            "prlimit",
            f"--as={self._settings.worker_ram_bytes}",
            "--",
            worker_binary,
            "--mode",
            "pool",
            "--format",
            self._format,
            "--license-path",
            str(self._settings.license_path),
        ]

        async def _spawn_one(pool_index: int) -> tuple[PooledWorker, int]:
            proc = await asyncio.create_subprocess_exec(
                *argv_base,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            worker = PooledWorker(proc, self._format, proc.pid or 0, pool_index)
            worker.start_stderr_reader()
            emit_event(
                "pool_worker_spawn",
                level="info",
                worker=self._format,
                pool_index=pool_index,
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
                    pool_index=pool_index,
                    page_count=page_count,
                )
                return worker, page_count
            except Exception:
                await worker.kill()
                raise

        results = await asyncio.gather(
            *(_spawn_one(i) for i in range(self._pool_size)),
            return_exceptions=True,
        )

        # Collect failures vs successes; clean up live workers if anything failed.
        failures: list[BaseException] = []
        successes: list[tuple[PooledWorker, int]] = []
        for r in results:
            if isinstance(r, BaseException):
                failures.append(r)
            else:
                successes.append(r)

        if failures:
            for worker, _ in successes:
                with suppress(Exception):
                    await worker.kill()
            for exc in failures:
                log.warning("pool worker failed to load: %s", exc)
            raise failures[0]

        for worker, page_count in successes:
            if self.actual_page_count is None:
                self.actual_page_count = page_count
            self._workers.append(worker)
            self._available.put_nowait(worker)

    async def _shutdown(self) -> None:
        """Gracefully shut down all workers."""
        for w in self._workers:
            await w.quit()
        self._workers.clear()

    async def render_chunk(self, chunk: Chunk, scratch_dir: Path) -> Path:
        """Render a single chunk using the next available worker.

        Workers are checked out from an asyncio.Queue so that each worker
        handles at most one chunk at a time — required because each worker's
        stdin/stdout pair cannot be read concurrently by multiple coroutines
        (asyncio raises `RuntimeError: readuntil() called while another
        coroutine is already waiting for incoming data`).
        """
        assert self._available is not None, "pool not initialized; use as async context manager"
        worker = await self._available.get()
        try:
            output_path = scratch_dir / f"chunk-{chunk.index}.pdf"
            return await worker.render_chunk(
                chunk, output_path, timeout=self._settings.chunk_timeout_seconds
            )
        finally:
            self._available.put_nowait(worker)


class ForkedPoolLeader:
    """Single-process pool: one leader holds the loaded document and forks N
    children that render in parallel via socketpairs (see worker_cpp/pool.cpp
    pool_loop_forked). The orchestrator sees one stdin/stdout/stderr triple;
    each command carries a seq id so concurrent render responses can be
    demuxed back to the caller's future.

    Replaces the WorkerPool's N-independent-subprocesses model. Memory is
    ~1x the loaded document for all N renderers combined (COW), instead of
    Nx — which was the cause of the stress_test_100mb.docx 600s load timeout.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        format: FormatName,
        pid: int,
        pool_size: int,
    ) -> None:
        self._proc = proc
        self._format = format
        self._pid = pid
        self._pool_size = pool_size
        self._seq_counter = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._loaded = False

    @property
    def alive(self) -> bool:
        return self._proc.returncode is None

    def start_io_readers(self) -> None:
        if self._stdout_task is None and self._proc.stdout is not None:
            self._stdout_task = asyncio.create_task(
                self._read_stdout(),
                name=f"fork-leader-stdout-{self._format}",
            )
        if self._stderr_task is None and self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._read_stderr(),
                name=f"fork-leader-stderr-{self._format}",
            )

    async def _read_stdout(self) -> None:
        assert self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    # EOF — fail every pending future so callers don't hang.
                    self._fail_all_pending("worker stdout EOF")
                    return
                try:
                    data = json.loads(line)
                except ValueError:
                    log.warning(
                        "fork leader emitted non-JSON on stdout: %r",
                        line[:200],
                    )
                    continue
                seq = data.get("seq", 0) if isinstance(data, dict) else 0
                fut = self._pending.pop(seq, None)
                if fut and not fut.done():
                    fut.set_result(data)
                else:
                    log.warning("fork leader response with unknown seq=%s: %s", seq, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fork leader stdout reader crashed")
            self._fail_all_pending("stdout reader crashed")

    def _fail_all_pending(self, reason: str) -> None:
        for seq, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result(
                    {"seq": seq, "status": "error", "code": EXIT_RENDER_FAILURE, "detail": reason}
                )
        self._pending.clear()

    async def _read_stderr(self) -> None:
        assert self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                self._handle_stderr_line(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fork leader stderr reader crashed")

    def _handle_stderr_line(self, raw: bytes) -> None:  # noqa: PLR0912 — flat dispatch over C++ event types (load_progress / heartbeat / timing); branches reflect protocol, not complexity
        text = raw.decode("utf-8", errors="replace").rstrip()
        if not text:
            return
        if text.startswith('{"type":"load_progress"'):
            try:
                msg = json.loads(text)
            except ValueError:
                msg = None
            if isinstance(msg, dict):
                value = msg.get("value")
                if isinstance(value, int | float):
                    rid = current_request_id.get()
                    if rid and rid != "-":
                        job_progress_store().update(rid, load_progress=float(value))
                return
        if text.startswith('{"type":"heartbeat"'):
            try:
                hb = json.loads(text)
            except ValueError:
                hb = None
            if isinstance(hb, dict):
                # pool_index now comes from the C++ side — leader=0, children=1..N-1.
                # Each forked process tags its own heartbeat correctly even though
                # they all share the same stderr pipe.
                hb_record = {
                    "worker": self._format,
                    "pool_index": hb.get("pool_index", 0),
                    "pid": self._pid,
                    "phase": hb.get("phase"),
                    "elapsed_s": hb.get("elapsed_s"),
                    "rss_bytes": hb.get("rss_bytes"),
                    "swap_bytes": hb.get("swap_bytes"),
                    "cpu_jiffies": hb.get("cpu_jiffies"),
                    "wall_ts": time.time(),
                }
                emit_event("pool_worker_heartbeat", level="debug", **hb_record)
                rid = current_request_id.get()
                if rid and rid != "-":
                    heartbeat_store().record(rid, hb_record)
                return
        if text.startswith('{"type":"timing"'):
            try:
                msg = json.loads(text)
            except ValueError:
                msg = None
            if isinstance(msg, dict):
                # Mirror WorkerPool._read_stderr's timing path so the
                # Forked variant (docx/pptx/pdf) ends up at the same store
                # the legacy one (xlsx) writes to. Pre-fix, the line below
                # fell through to log.warning and the dashboard's Time /
                # Gantt charts stayed empty for non-XLSX conversions.
                payload = {k: v for k, v in msg.items() if k != "type"}
                emit_event(
                    "pool_worker_timing",
                    level="info",
                    worker=self._format,
                    pool_index=payload.get("pool_index", 0),
                    pid=self._pid,
                    **{k: v for k, v in payload.items() if k != "pool_index"},
                )
                rid = current_request_id.get()
                if rid and rid != "-":
                    timing_store().record(
                        rid,
                        {
                            "worker": self._format,
                            "pool_index": payload.get("pool_index", 0),
                            "pid": self._pid,
                            "wall_ts": time.time(),
                            **{k: v for k, v in payload.items() if k != "pool_index"},
                        },
                    )
                return
        log.warning(
            "fork leader stderr (worker=%s pid=%s): %s",
            self._format,
            self._pid,
            text,
        )

    async def load_document(self, input_path: Path, license_path: Path) -> int:
        assert self._proc.stdin is not None
        # seq=0 is reserved for load. Pre-register the future BEFORE writing
        # the command so the stdout reader can't race past the response.
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[0] = fut
        try:
            cmd = json.dumps({"cmd": "load", "seq": 0, "input": str(input_path)}) + "\n"
            self._proc.stdin.write(cmd.encode())
            await self._proc.stdin.drain()
            result = await asyncio.wait_for(fut, timeout=600)
        finally:
            self._pending.pop(0, None)
        if result.get("status") != "ok":
            self._raise_error(result, chunk=None)
        self._loaded = True
        return int(result["page_count"])

    async def render_chunk(self, chunk: Chunk, output_path: Path, timeout: int = 600) -> Path:
        assert self._proc.stdin is not None
        assert self._loaded, "render_chunk called before load_document"
        self._seq_counter += 1
        seq = self._seq_counter
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[seq] = fut
        cmd = (
            json.dumps(
                {
                    "cmd": "render",
                    "seq": seq,
                    "page_start": chunk.page_range[0],
                    "page_end": chunk.page_range[1],
                    "output": str(output_path),
                }
            )
            + "\n"
        )
        self._proc.stdin.write(cmd.encode())
        try:
            await self._proc.stdin.drain()
            result = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(seq, None)
        if result.get("status") != "ok":
            self._raise_error(result, chunk)
        return output_path

    def _raise_error(self, result: dict[str, Any], chunk: Chunk | None) -> None:
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

    async def quit(self) -> None:
        if not self.alive:
            await self._cancel_tasks()
            return
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(b'{"cmd":"quit"}\n')
            await self._proc.stdin.drain()
            await asyncio.wait_for(self._proc.wait(), timeout=10)
        except (TimeoutError, OSError, BrokenPipeError):
            self._proc.kill()
            await self._proc.wait()
        await self._cancel_tasks()

    async def kill(self) -> None:
        if self.alive:
            self._proc.kill()
            with suppress(Exception):
                await self._proc.wait()
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        for task in (self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        self._stdout_task = None
        self._stderr_task = None


class ForkedWorkerPool:
    """Same external interface as WorkerPool, backed by a single
    ForkedPoolLeader process. The leader forks pool_size-1 children that
    inherit the loaded Document via copy-on-write."""

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
        self._leader: ForkedPoolLeader | None = None
        self.actual_page_count: int | None = None

    async def __aenter__(self) -> ForkedWorkerPool:
        await self._spawn()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._leader is not None:
            await self._leader.quit()

    async def _spawn(self) -> None:
        worker_binary = f"{self._settings.worker_binary_prefix}-{self._format}"
        argv = [
            "prlimit",
            f"--as={self._settings.worker_ram_bytes}",
            "--",
            worker_binary,
            "--mode",
            "pool",
            "--format",
            self._format,
            "--license-path",
            str(self._settings.license_path),
            "--pool-size",
            str(self._pool_size),
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        leader = ForkedPoolLeader(proc, self._format, proc.pid or 0, self._pool_size)
        leader.start_io_readers()
        emit_event(
            "fork_pool_spawn",
            level="info",
            worker=self._format,
            pool_size=self._pool_size,
            pid=proc.pid,
        )
        try:
            page_count = await leader.load_document(
                self._input_path,
                self._settings.license_path,
            )
            emit_event(
                "fork_pool_loaded",
                level="info",
                worker=self._format,
                page_count=page_count,
            )
        except Exception:
            await leader.kill()
            raise
        self._leader = leader
        self.actual_page_count = page_count

    async def render_chunk(self, chunk: Chunk, scratch_dir: Path) -> Path:
        assert self._leader is not None
        output_path = scratch_dir / f"chunk-{chunk.index}.pdf"
        return await self._leader.render_chunk(
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


_FORK_UNSAFE_FORMATS: frozenset[FormatName] = frozenset({"xlsx"})


def fork_after_load_enabled(settings: Settings, format: FormatName) -> bool:
    """Whether to use the fork-after-load ForkedWorkerPool for this format.

    The global flag (`Settings.fork_after_load`, env `OFFICE_CONVERT_FORK_AFTER_LOAD`)
    is gated by a per-format allowlist: Aspose.Cells doesn't survive `fork()`
    (explicit `Startup()` lifecycle + OpenSSL state + internal worker threads
    that leave broken locks in children). Verified crash on a 98 MB XLSX on
    2026-05-15: load completed in the leader, first render attempt died with
    `worker stdout EOF` (leader SIGSEGV/SIGABRT without flushing stderr).

    DOCX (Aspose.Words) and PPTX (Aspose.Slides) survived fork on the files
    tested. PDF (Aspose.PDF) is currently allowed but unverified — if it
    crashes the same way, add "pdf" to _FORK_UNSAFE_FORMATS.
    """
    if format in _FORK_UNSAFE_FORMATS:
        return False
    return bool(getattr(settings, "fork_after_load", False))
