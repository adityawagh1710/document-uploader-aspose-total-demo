"""Pure chunk-planning algorithm.

Implements FR-3 (chunk planning) and FR-4 (subdivision retry helper). Pure
functions: no I/O, no subprocess, no Aspose. Property-based tests verify
invariants per nfr-requirements.md §9.

Memory cost formula (Functional Design Q1 = B): pro-rated by input size and
amplified by a per-format factor. Per-page floor was rejected; outlier pages
are caught by the subdivision-on-OOM retry path instead.
"""

from __future__ import annotations

import hashlib
from typing import Final

from office_convert.types import Chunk, ChunkPlan, FormatName, ProbeResult

AMPLIFICATION: Final[dict[FormatName, int]] = {
    "docx": 5,
    "pptx": 8,
    "xlsx": 4,
    "pdf": 2,
}

# Per-format fixed overhead in MB that each subprocess consumes regardless of
# chunk size (Aspose product init + full document load + license validation).
# Used by adaptive_max_pages to compute how many pages justify one spawn.
SUBPROCESS_OVERHEAD_MB: Final[dict[FormatName, int]] = {
    "docx": 200,
    "pptx": 300,
    "xlsx": 250,
    "pdf": 100,
}

# Minimum number of chunks to produce (ensures some parallelism even for
# files that fit entirely in RAM).
MIN_CHUNKS: Final[int] = 2

BALANCE_FACTOR: Final[float] = 1.5
SUBDIVISION_FLOOR_PAGES: Final[int] = 1


def estimate_chunk_mb(
    pages_in_chunk: int,
    total_pages: int,
    input_size_bytes: int,
    format: FormatName,
) -> float:
    """Pro-rated rendered-MB estimate (functional-design Q1 = B)."""
    if total_pages <= 0 or pages_in_chunk <= 0:
        return 0.0
    per_page_bytes = input_size_bytes / total_pages
    return (per_page_bytes * pages_in_chunk * AMPLIFICATION[format]) / (1024 * 1024)


def adaptive_max_pages(
    probe: ProbeResult,
    worker_ram_bytes: int,
    parallel: int,
    max_pages_ceiling: int = 200,
    min_pages_floor: int = 10,
) -> int:
    """Compute the largest chunk size (in pages) that fits in the RAM budget.

    Strategy:
      1. Estimate per-page rendered RAM cost from the probe data.
      2. Compute how many pages fit in the per-worker RAM budget (with a
         safety margin for Aspose's full-document load overhead).
      3. Clamp between min_pages_floor and max_pages_ceiling.
      4. Ensure we produce at least MIN_CHUNKS chunks (so parallelism is
         utilized), unless the file is tiny.

    This replaces the static `max_pages_per_chunk=10` default with a value
    that adapts to the actual file: small files get one or two big chunks
    (fewer spawns), large files get chunks sized to the RAM budget.
    """
    if probe.page_count <= 0 or probe.size_bytes <= 0:
        return min_pages_floor

    format = probe.format
    amp = AMPLIFICATION[format]
    overhead_mb = SUBPROCESS_OVERHEAD_MB[format]

    # Available MB for actual page rendering per worker (subtract overhead)
    worker_budget_mb = (worker_ram_bytes / (1024 * 1024)) * 0.75  # 75% safety margin
    render_budget_mb = max(worker_budget_mb - overhead_mb, 50.0)

    # Per-page cost estimate
    per_page_bytes = probe.size_bytes / probe.page_count
    per_page_rendered_mb = (per_page_bytes * amp) / (1024 * 1024)

    if per_page_rendered_mb <= 0:
        return max_pages_ceiling

    # How many pages fit in the render budget
    pages_by_ram = int(render_budget_mb / per_page_rendered_mb)

    # Ensure we produce enough chunks to fill the parallelism slots
    # (no point having 1 chunk when parallel=4)
    if probe.page_count > min_pages_floor:
        desired_chunks = max(MIN_CHUNKS, parallel)
        pages_by_parallelism = probe.page_count // desired_chunks
        # Take the smaller of RAM-limited and parallelism-limited
        optimal = min(pages_by_ram, pages_by_parallelism)
    else:
        optimal = pages_by_ram

    # Clamp
    return max(min_pages_floor, min(optimal, max_pages_ceiling))


def plan_chunks(
    probe: ProbeResult,
    max_pages_per_chunk: int = 10,
    max_mb_per_chunk: int = 50,
) -> ChunkPlan:
    """Produce a deterministic chunk plan.

    Hybrid strategy:
      1. If natural seams are present, group seams under the size
         bounds. If the largest grouped chunk stays within
         balance_factor of the targets, return that plan.
      2. Otherwise fall back to greedy page-range splitting.

    All four formats (DOCX, PPTX, XLSX, PDF) now support page-range
    slicing in their respective C++ workers, so chunking parallelizes
    correctly for all formats.
    """
    if probe.page_count <= 0:
        return ChunkPlan(chunks=(), total_pages=0, estimated_mb=0.0)

    if probe.natural_seams:
        seam_plan = _group_seams(
            probe,
            max_pages_per_chunk,
            max_mb_per_chunk,
        )
        if _is_balanced(seam_plan, max_pages_per_chunk, max_mb_per_chunk):
            return seam_plan

    return _page_range_split(probe, max_pages_per_chunk, max_mb_per_chunk)


def subdivide(chunk: Chunk) -> list[Chunk]:
    """Binary halving subdivision. Returns [] at the single-page floor."""
    start, end = chunk.page_range
    span = end - start + 1
    if span <= SUBDIVISION_FLOOR_PAGES:
        return []
    half = (span + 1) // 2  # ceiling
    mid = start + half - 1
    return [
        Chunk(index=chunk.index, page_range=(start, mid), natural_seam=False),
        Chunk(index=chunk.index + 0.5, page_range=(mid + 1, end), natural_seam=False),
    ]


def chunk_sha256(chunk: Chunk, source_sha256: str, format: FormatName) -> str:
    """Stable hash for the per-chunk cache key. Deterministic by construction."""
    h = hashlib.sha256()
    h.update(source_sha256.encode("ascii"))
    h.update(b":")
    h.update(f"{chunk.page_range[0]}-{chunk.page_range[1]}".encode("ascii"))
    h.update(b":")
    h.update(format.encode("ascii"))
    return h.hexdigest()


def _is_balanced(plan: ChunkPlan, max_pages: int, max_mb: int) -> bool:
    """Seam plan is balanced iff every chunk stays within balance_factor."""
    if not plan.chunks:
        return True
    pages_threshold = max_pages * BALANCE_FACTOR
    mb_threshold = max_mb * BALANCE_FACTOR
    for c in plan.chunks:
        if c.pages > pages_threshold:
            return False
    # plan.estimated_mb is the sum; we want per-chunk check, recompute briefly
    return not any(_chunk_mb(c, plan) > mb_threshold for c in plan.chunks)


def _chunk_mb(chunk: Chunk, plan: ChunkPlan) -> float:
    """Approximation: estimated_mb is summed; we can't recover per-chunk without probe."""
    if plan.total_pages == 0:
        return 0.0
    return plan.estimated_mb * (chunk.pages / plan.total_pages)


def _group_seams(
    probe: ProbeResult,
    max_pages: int,
    max_mb: int,
) -> ChunkPlan:
    """Group consecutive natural seams greedily under the bounds."""
    chunks: list[Chunk] = []
    pending_start: int | None = None
    pending_end: int = 0
    pending_pages: int = 0

    for seam_start, seam_end in probe.natural_seams:
        seam_pages = seam_end - seam_start + 1
        seam_mb = estimate_chunk_mb(seam_pages, probe.page_count, probe.size_bytes, probe.format)

        if pending_start is None:
            pending_start = seam_start
            pending_end = seam_end
            pending_pages = seam_pages
            continue

        combined_pages = pending_pages + seam_pages
        combined_mb = estimate_chunk_mb(
            combined_pages, probe.page_count, probe.size_bytes, probe.format
        )
        if combined_pages <= max_pages and combined_mb <= max_mb:
            pending_end = seam_end
            pending_pages = combined_pages
        else:
            chunks.append(
                Chunk(
                    index=float(len(chunks)),
                    page_range=(pending_start, pending_end),
                    natural_seam=True,
                )
            )
            pending_start = seam_start
            pending_end = seam_end
            pending_pages = seam_pages
        _ = seam_mb  # used for symmetry; kept for readability

    if pending_start is not None:
        chunks.append(
            Chunk(
                index=float(len(chunks)),
                page_range=(pending_start, pending_end),
                natural_seam=True,
            )
        )

    total_mb = sum(
        estimate_chunk_mb(c.pages, probe.page_count, probe.size_bytes, probe.format) for c in chunks
    )
    return ChunkPlan(chunks=tuple(chunks), total_pages=probe.page_count, estimated_mb=total_mb)


def _page_range_split(
    probe: ProbeResult,
    max_pages: int,
    max_mb: int,
) -> ChunkPlan:
    """Greedy page-range split bounded by max_pages and max_mb."""
    chunks: list[Chunk] = []
    cursor = 1
    while cursor <= probe.page_count:
        # Grow the chunk from `cursor` forward.
        end = cursor
        while end + 1 <= probe.page_count:
            candidate_pages = end + 1 - cursor + 1
            if candidate_pages > max_pages:
                break
            candidate_mb = estimate_chunk_mb(
                candidate_pages, probe.page_count, probe.size_bytes, probe.format
            )
            if candidate_mb > max_mb:
                break
            end += 1
        chunks.append(
            Chunk(
                index=float(len(chunks)),
                page_range=(cursor, end),
                natural_seam=False,
            )
        )
        cursor = end + 1

    total_mb = sum(
        estimate_chunk_mb(c.pages, probe.page_count, probe.size_bytes, probe.format) for c in chunks
    )
    return ChunkPlan(chunks=tuple(chunks), total_pages=probe.page_count, estimated_mb=total_mb)
