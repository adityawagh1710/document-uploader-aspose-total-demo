"""Per-request orchestration: probe → plan → dispatch → merge → stream.

Implements FR-3 through FR-7, FR-9, FR-10. The end-to-end pipeline from
business-logic-model.md §1. Yields PDF bytes as an async generator.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from office_convert import aspose_worker, qpdf
from office_convert.cache import CacheManager
from office_convert.chunk_planner import adaptive_max_pages, chunk_sha256, plan_chunks, subdivide
from office_convert.errors import OOMError, SubdivisionFloorError
from office_convert.logging import emit_event
from office_convert.probe import probe as do_probe
from office_convert.worker_pool import WorkerPool, pool_mode_available
from office_convert.types import (
    Chunk,
    ConversionOptions,
    ConversionResult,
    FormatName,
    ProbeResult,
)

if TYPE_CHECKING:
    from office_convert.config import Settings

log = logging.getLogger(__name__)


async def convert_job(
    request_id: str,
    input_path: Path,
    format: FormatName,
    options: ConversionOptions,
    settings: Settings,
    cache: CacheManager,
    scratch_dir: Path,
) -> AsyncIterator[bytes]:
    """Run the full conversion pipeline for one request.

    Yields chunks of the merged PDF. Stores a `ConversionResult` on the
    generator's `result` attribute (set after iteration completes) for the
    caller to read into response headers.

    Raises typed ConversionError subclasses; FastAPI handler maps to HTTP.
    """
    started = time.monotonic()
    chunks_rendered = 0
    subdivision_retries = 0
    cache_hits = 0

    source_sha = _file_sha256(input_path)

    # Final-cache lookup
    if options.cache:
        final_cached = cache.get_final(source_sha)
        if final_cached is not None:
            emit_event(
                "cache_hit",
                level="info",
                layer="final",
                source_sha256=source_sha[:16],
            )
            cache_hits += 1
            async for block in _stream_file(final_cached):
                yield block
            _attach_result(
                convert_job,
                ConversionResult(
                    chunks_rendered=0,
                    subdivision_retries=0,
                    cache_hits=1,
                    duration_seconds=time.monotonic() - started,
                ),
            )
            return

    # Probe
    emit_event("probe_start", level="info", format=format)
    probe_started = time.monotonic()
    probe_result = await do_probe(input_path, format, settings, request_id)
    # The probe may correct the format (e.g., file detected as xlsx but is
    # actually a docx — the probe retries and returns the correct format).
    # Use the probe's format for all subsequent dispatch.
    format = probe_result.format
    emit_event(
        "probe_complete",
        level="info",
        page_count=probe_result.page_count,
        natural_seams=len(probe_result.natural_seams),
        size_bytes=probe_result.size_bytes,
        duration_s=round(time.monotonic() - probe_started, 3),
    )

    # Plan chunks. Use adaptive chunk sizing: compute the optimal pages-per-chunk
    # based on file size, page count, format, and RAM budget. The static config
    # value acts as a ceiling (operator override). XLSX gets a per-format floor
    # on max_pages_per_chunk: each Cells subprocess pays a fixed Workbook.Load +
    # pagination cost on the full workbook before rendering its slice, so the
    # chunk size has to be coarse enough to amortize that overhead.
    adaptive_pages = adaptive_max_pages(
        probe_result,
        worker_ram_bytes=settings.worker_ram_bytes,
        parallel=settings.parallel,
    )
    # The static config acts as a ceiling — operator can always cap it lower
    effective_max_pages = min(adaptive_pages, settings.max_pages_per_chunk)
    if probe_result.format == "xlsx":
        effective_max_pages = max(effective_max_pages, settings.xlsx_min_pages_per_chunk)
    elif probe_result.format == "pptx":
        effective_max_pages = max(effective_max_pages, settings.pptx_min_pages_per_chunk)
    emit_event(
        "adaptive_chunk_sizing",
        level="info",
        adaptive_pages=adaptive_pages,
        effective_max_pages=effective_max_pages,
        format=probe_result.format,
    )
    plan = plan_chunks(
        probe_result,
        max_pages_per_chunk=effective_max_pages,
        max_mb_per_chunk=settings.max_mb_per_chunk,
    )
    # Classify the strategy chosen by the planner so logs make it obvious
    # whether intra-request parallelism kicked in.
    if len(plan.chunks) == 1:
        strategy = "single_chunk"
    elif any(c.natural_seam for c in plan.chunks):
        strategy = "natural_seams"
    else:
        strategy = "page_range_split"
    emit_event(
        "plan_complete",
        level="info",
        chunks=len(plan.chunks),
        total_pages=plan.total_pages,
        strategy=strategy,
        parallel=settings.parallel,
    )

    # Dispatch chunk renders under per-job concurrency budget.
    # Two modes: pool (persistent workers, document loaded once) or one-shot
    # (subprocess per chunk). Pool mode eliminates per-chunk document-load
    # overhead but requires C++ worker support for --mode=pool.
    job_sem = asyncio.Semaphore(settings.parallel)
    counters = _Counters()
    use_pool = pool_mode_available(settings, format) and len(plan.chunks) > 1

    if use_pool:
        emit_event("dispatch_mode", level="info", mode="pool", workers=settings.parallel)
        pool_size = min(settings.parallel, len(plan.chunks))
        async with WorkerPool(format, input_path, settings, pool_size=pool_size) as pool:
            # Re-plan if the actual page count differs from the estimate
            # (pool workers report the real count after loading the document)
            if pool.actual_page_count is not None and pool.actual_page_count != plan.total_pages:
                emit_event(
                    "replan_from_pool",
                    level="info",
                    estimated_pages=plan.total_pages,
                    actual_pages=pool.actual_page_count,
                )
                actual_probe = ProbeResult(
                    page_count=pool.actual_page_count,
                    format=probe_result.format,
                    natural_seams=(),
                    size_bytes=probe_result.size_bytes,
                )
                plan = plan_chunks(
                    actual_probe,
                    max_pages_per_chunk=effective_max_pages,
                    max_mb_per_chunk=settings.max_mb_per_chunk,
                )

            async def _render_one_pooled(chunk: Chunk) -> tuple[Chunk, Path]:
                async with job_sem:
                    chunk_key = chunk_sha256(chunk, source_sha, format)
                    cached = cache.get_chunk(chunk_key) if options.cache else None
                    if cached is not None:
                        counters.cache_hits += 1
                        return chunk, cached
                    path = await pool.render_chunk(chunk, scratch_dir)
                    counters.rendered += 1
                    if options.cache:
                        cache.put_chunk(chunk_key, path)
                    return chunk, path

            rendered = await asyncio.gather(
                *(_render_one_pooled(c) for c in plan.chunks)
            )
    else:
        emit_event("dispatch_mode", level="info", mode="one_shot", workers=settings.parallel)

        async def _render_one(chunk: Chunk) -> tuple[Chunk, Path]:
            async with job_sem:
                return chunk, await _render_with_retry(
                    chunk=chunk,
                    input_path=input_path,
                    format=format,
                    scratch_dir=scratch_dir,
                    request_id=request_id,
                    settings=settings,
                    source_sha=source_sha,
                    cache=cache if options.cache else CacheManager(None, settings.aspose_version),
                    counters=counters,
                )

        rendered = await asyncio.gather(
            *(_render_one(c) for c in plan.chunks)
        )
    chunks_rendered = counters.rendered
    subdivision_retries = counters.subdivisions
    cache_hits += counters.cache_hits

    # Order chunk PDFs by chunk index (preserved by gather; here for safety)
    rendered.sort(key=lambda pair: pair[0].index)
    chunk_paths = [p for _, p in rendered]

    # Tee-to-cache: stream qpdf concat into the response AND optionally to cache.
    cache_temp = cache.final_temp_path(source_sha) if options.cache else None

    emit_event("merge_start", level="info", chunk_count=len(chunk_paths))
    merge_started = time.monotonic()
    output_bytes = 0
    try:
        async for block in qpdf.concat_streaming(
            chunk_paths,
            cache_temp_path=cache_temp,
        ):
            output_bytes += len(block)
            yield block
    except BaseException:
        if cache_temp is not None:
            with suppress(OSError):
                cache_temp.unlink(missing_ok=True)
        raise

    emit_event(
        "merge_complete",
        level="info",
        chunk_count=len(chunk_paths),
        output_bytes=output_bytes,
        duration_s=round(time.monotonic() - merge_started, 3),
    )

    if cache_temp is not None:
        cache.finalize_final(source_sha, cache_temp)

    duration = time.monotonic() - started
    emit_event(
        "request_complete",
        level="info",
        chunks_rendered=chunks_rendered,
        subdivision_retries=subdivision_retries,
        cache_hits=cache_hits,
        output_bytes=output_bytes,
        duration_seconds=round(duration, 3),
    )
    _attach_result(
        convert_job,
        ConversionResult(
            chunks_rendered=chunks_rendered,
            subdivision_retries=subdivision_retries,
            cache_hits=cache_hits,
            duration_seconds=duration,
        ),
    )


class _Counters:
    __slots__ = ("rendered", "subdivisions", "cache_hits")

    def __init__(self) -> None:
        self.rendered = 0
        self.subdivisions = 0
        self.cache_hits = 0


async def _render_with_retry(
    *,
    chunk: Chunk,
    input_path: Path,
    format: FormatName,
    scratch_dir: Path,
    request_id: str,
    settings: Settings,
    source_sha: str,
    cache: CacheManager,
    counters: _Counters,
    depth: int = 0,
) -> Path:
    """Render a chunk; on OOM, subdivide and recurse, then concat sub-PDFs.

    Returns a single PDF path for `chunk`. If subdivision was used, the
    returned path is itself a concatenation of sub-PDFs (so the caller
    sees one PDF per planned chunk).
    """
    chunk_key = chunk_sha256(chunk, source_sha, format)
    cached = cache.get_chunk(chunk_key)
    if cached is not None:
        counters.cache_hits += 1
        emit_event(
            "cache_hit",
            level="info",
            layer="chunk",
            chunk_index=chunk.index,
        )
        return cached

    try:
        emit_event(
            "chunk_render_start",
            level="info",
            chunk_index=chunk.index,
            page_range=list(chunk.page_range),
            page_count=chunk.pages,
            depth=depth,
            worker=format,
        )
        chunk_started = time.monotonic()
        path = await aspose_worker.render_chunk(
            chunk=chunk,
            input_path=input_path,
            format=format,
            scratch_dir=scratch_dir,
            request_id=request_id,
            settings=settings,
        )
        chunk_duration = round(time.monotonic() - chunk_started, 3)
        counters.rendered += 1
        try:
            output_bytes = path.stat().st_size
        except OSError:
            output_bytes = -1
        emit_event(
            "chunk_complete",
            level="info",
            chunk_index=chunk.index,
            page_range=list(chunk.page_range),
            depth=depth,
            duration_s=chunk_duration,
            output_bytes=output_bytes,
        )
        cache.put_chunk(chunk_key, path)
        return path
    except OOMError:
        counters.subdivisions += 1
        sub_chunks = subdivide(chunk)
        if not sub_chunks:
            raise SubdivisionFloorError(chunk=chunk, attempts=depth + 1) from None
        emit_event(
            "subdivision_retry",
            level="warn",
            chunk_index=chunk.index,
            page_range_before=list(chunk.page_range),
            sub_count=len(sub_chunks),
            depth=depth,
        )
        sub_paths = await asyncio.gather(
            *(
                _render_with_retry(
                    chunk=sc,
                    input_path=input_path,
                    format=format,
                    scratch_dir=scratch_dir,
                    request_id=request_id,
                    settings=settings,
                    source_sha=source_sha,
                    cache=cache,
                    counters=counters,
                    depth=depth + 1,
                )
                for sc in sub_chunks
            )
        )
        # Merge sub-chunks into one PDF for this chunk's slot.
        merged_path = scratch_dir / f"chunk-{chunk.index}-merged.pdf"
        await _merge_to_file(sub_paths, merged_path)
        return merged_path


async def _merge_to_file(chunk_paths: list[Path], output_path: Path) -> None:
    """Use qpdf to concat chunks into a single file (no streaming needed)."""
    from office_convert import qpdf as qpdf_mod

    with output_path.open("wb") as out:
        async for block in qpdf_mod.concat_streaming(chunk_paths):
            out.write(block)


async def _stream_file(path: Path) -> AsyncIterator[bytes]:
    """Stream a file in 64 KB chunks (for cache-hit responses)."""
    loop = asyncio.get_event_loop()
    with path.open("rb") as f:
        while True:
            block = await loop.run_in_executor(None, f.read, 65536)
            if not block:
                break
            yield block


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _attach_result(fn: object, result: ConversionResult) -> None:
    """Attach ConversionResult to the generator function for caller retrieval.

    We store it as an attribute on the orchestrator module (a module-level
    contextvar would also work). The server reads `convert_job.last_result`
    immediately after the generator exhausts.
    """
    # Simple module-level slot indexed by the running asyncio task.
    task = asyncio.current_task()
    if task is None:
        return
    _RESULTS[id(task)] = result


_RESULTS: dict[int, ConversionResult] = {}


def consume_result(task_id: int) -> ConversionResult | None:
    """Server reads (and clears) the ConversionResult for a completed task."""
    return _RESULTS.pop(task_id, None)
