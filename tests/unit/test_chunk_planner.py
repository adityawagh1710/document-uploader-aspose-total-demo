"""Example-based tests for office_convert.chunk_planner."""

from __future__ import annotations

import pytest

from office_convert.chunk_planner import (
    SUBDIVISION_FLOOR_PAGES,
    chunk_sha256,
    estimate_chunk_mb,
    plan_chunks,
    subdivide,
)
from office_convert.types import Chunk, ProbeResult


def _probe(
    page_count: int,
    *,
    format: str = "docx",
    size_bytes: int = 1_000_000,
    natural_seams: tuple[tuple[int, int], ...] = (),
) -> ProbeResult:
    return ProbeResult(
        page_count=page_count,
        format=format,  # type: ignore[arg-type]
        natural_seams=natural_seams,
        size_bytes=size_bytes,
    )


def test_estimate_chunk_mb_zero_pages_is_zero() -> None:
    assert estimate_chunk_mb(0, 100, 1_000_000, "docx") == 0.0


def test_estimate_chunk_mb_scales_with_pages() -> None:
    one_page = estimate_chunk_mb(1, 100, 10_000_000, "docx")
    ten_pages = estimate_chunk_mb(10, 100, 10_000_000, "docx")
    assert ten_pages == pytest.approx(one_page * 10)


def test_estimate_chunk_mb_pptx_higher_than_docx() -> None:
    docx = estimate_chunk_mb(10, 100, 10_000_000, "docx")
    pptx = estimate_chunk_mb(10, 100, 10_000_000, "pptx")
    assert pptx > docx


def test_plan_chunks_empty_document() -> None:
    plan = plan_chunks(_probe(0))
    assert plan.chunks == ()
    assert plan.total_pages == 0


def test_plan_chunks_single_page() -> None:
    plan = plan_chunks(_probe(1, size_bytes=1000))
    assert len(plan.chunks) == 1
    assert plan.chunks[0].page_range == (1, 1)
    assert plan.total_pages == 1


def test_plan_chunks_page_range_split_no_seams() -> None:
    plan = plan_chunks(_probe(25, size_bytes=1000), max_pages_per_chunk=10, max_mb_per_chunk=50)
    assert plan.total_pages == 25
    # 10, 10, 5 — three chunks
    assert len(plan.chunks) == 3
    assert plan.chunks[0].page_range == (1, 10)
    assert plan.chunks[1].page_range == (11, 20)
    assert plan.chunks[2].page_range == (21, 25)
    assert all(not c.natural_seam for c in plan.chunks)


def test_plan_chunks_pptx_splits_like_docx() -> None:
    """PPTX is no longer carved out: pptx.cpp honors --page-range via
    slide-index array export, so the planner subdivides the same way it
    does for DOCX. The per-format chunk-size floor is applied at the
    orchestrator call site, not here."""
    plan = plan_chunks(
        _probe(40, format="pptx", size_bytes=10_000_000),
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
    )
    assert len(plan.chunks) == 4
    assert plan.chunks[0].page_range == (1, 10)
    assert plan.chunks[-1].page_range == (31, 40)
    assert all(not c.natural_seam for c in plan.chunks)


def test_plan_chunks_pptx_single_page() -> None:
    """Trivial: single-slide PPTX yields one chunk (same as the general case)."""
    plan = plan_chunks(_probe(1, format="pptx", size_bytes=500_000))
    assert len(plan.chunks) == 1
    assert plan.chunks[0].page_range == (1, 1)


def test_plan_chunks_docx_still_splits() -> None:
    """Regression guard: DOCX must NOT take the single-chunk fast-path
    (PageSet is honored in docx.cpp, so chunking parallelizes correctly)."""
    plan = plan_chunks(
        _probe(30, format="docx", size_bytes=1000),
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
    )
    assert len(plan.chunks) == 3


def test_plan_chunks_pdf_splits_like_docx() -> None:
    """PDF is no longer carved out: pdf.cpp honors --page-range via page
    extraction into a new Document, so the planner subdivides normally.
    Full-document ranges still take the fast-path inside the worker."""
    plan = plan_chunks(
        _probe(50, format="pdf", size_bytes=5_000_000),
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
    )
    assert len(plan.chunks) == 5
    assert plan.chunks[0].page_range == (1, 10)
    assert plan.chunks[-1].page_range == (41, 50)
    assert all(not c.natural_seam for c in plan.chunks)


def test_plan_chunks_xlsx_splits_like_docx() -> None:
    """XLSX is no longer carved out: xlsx.cpp honors --page-range via
    PdfSaveOptions::PageIndex/PageCount, so the planner subdivides the
    same way it does for DOCX. The per-format chunk-size floor is
    applied at the orchestrator call site, not here."""
    plan = plan_chunks(
        _probe(40, format="xlsx", size_bytes=1_000_000),
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
    )
    assert len(plan.chunks) == 4
    assert plan.chunks[0].page_range == (1, 10)
    assert plan.chunks[-1].page_range == (31, 40)
    assert all(not c.natural_seam for c in plan.chunks)


def test_plan_chunks_xlsx_seams_grouped_when_balanced() -> None:
    """XLSX with per-sheet seams now flows through the seam path (formerly
    forced single-chunk by the carve-out). The planner greedily merges
    adjacent seams under max_pages_per_chunk: (1,5)+(6,10) packs into a
    single 10-page chunk, then (11,15) lands alone."""
    seams: tuple[tuple[int, int], ...] = ((1, 5), (6, 10), (11, 15))
    plan = plan_chunks(
        _probe(15, format="xlsx", size_bytes=1_000_000, natural_seams=seams),
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
    )
    assert len(plan.chunks) == 2
    assert all(c.natural_seam for c in plan.chunks)
    assert plan.chunks[0].page_range == (1, 10)
    assert plan.chunks[1].page_range == (11, 15)


def test_plan_chunks_seams_used_when_balanced() -> None:
    """Balanced seams → seam-based plan. Uses DOCX as the canonical
    non-carved-out format; the seam logic is format-agnostic, so DOCX with
    synthetic seams exercises the same code path PPTX and PDF will use once
    their workers honor --page-range. XLSX exercises the seam path
    separately in test_plan_chunks_xlsx_seams_grouped_when_balanced."""
    seams: tuple[tuple[int, int], ...] = ((1, 5), (6, 10), (11, 15))
    plan = plan_chunks(
        _probe(15, format="docx", size_bytes=1000, natural_seams=seams),
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
    )
    assert all(c.natural_seam for c in plan.chunks)


def test_plan_chunks_seams_rejected_when_unbalanced() -> None:
    """One huge seam → fallback to page-range. See note on
    test_plan_chunks_seams_used_when_balanced for the DOCX choice."""
    seams: tuple[tuple[int, int], ...] = ((1, 50),)  # 50-page seam vs max 10
    plan = plan_chunks(
        _probe(50, format="docx", size_bytes=1000, natural_seams=seams),
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
    )
    assert all(not c.natural_seam for c in plan.chunks)
    assert len(plan.chunks) == 5  # 10, 10, 10, 10, 10


def test_plan_chunks_full_page_coverage() -> None:
    plan = plan_chunks(_probe(100, size_bytes=10_000), max_pages_per_chunk=10, max_mb_per_chunk=50)
    total = sum(c.pages for c in plan.chunks)
    assert total == 100
    # No overlaps, monotonic
    for i, c in enumerate(plan.chunks):
        if i + 1 < len(plan.chunks):
            assert c.page_range[1] + 1 == plan.chunks[i + 1].page_range[0]


def test_subdivide_floor_returns_empty() -> None:
    chunk = Chunk(index=0.0, page_range=(7, 7), natural_seam=False)
    assert subdivide(chunk) == []


def test_subdivide_two_pages_to_two_singles() -> None:
    chunk = Chunk(index=0.0, page_range=(5, 6), natural_seam=False)
    subs = subdivide(chunk)
    assert len(subs) == 2
    assert subs[0].page_range == (5, 5)
    assert subs[1].page_range == (6, 6)


def test_subdivide_ten_pages_halving() -> None:
    chunk = Chunk(index=2.0, page_range=(11, 20), natural_seam=False)
    subs = subdivide(chunk)
    assert len(subs) == 2
    assert subs[0].page_range == (11, 15)
    assert subs[1].page_range == (16, 20)


def test_subdivide_preserves_coverage() -> None:
    chunk = Chunk(index=0.0, page_range=(1, 7), natural_seam=False)
    subs = subdivide(chunk)
    assert subs[0].page_range[0] == 1
    assert subs[-1].page_range[1] == 7


def test_chunk_sha256_deterministic() -> None:
    c = Chunk(index=0.0, page_range=(1, 10), natural_seam=False)
    a = chunk_sha256(c, "sha-x", "docx")
    b = chunk_sha256(c, "sha-x", "docx")
    assert a == b


def test_chunk_sha256_changes_on_format_change() -> None:
    c = Chunk(index=0.0, page_range=(1, 10), natural_seam=False)
    assert chunk_sha256(c, "sha-x", "docx") != chunk_sha256(c, "sha-x", "pptx")


def test_chunk_sha256_changes_on_range_change() -> None:
    a = Chunk(index=0.0, page_range=(1, 10), natural_seam=False)
    b = Chunk(index=0.0, page_range=(2, 11), natural_seam=False)
    assert chunk_sha256(a, "sha-x", "docx") != chunk_sha256(b, "sha-x", "docx")


def test_floor_constant() -> None:
    assert SUBDIVISION_FLOOR_PAGES == 1
