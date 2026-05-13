"""Property-based tests for chunk_planner.plan_chunks.

500 examples per `@given` per NFR Q6. Invariants per nfr-requirements.md §9
and business-logic-model.md §2.6.
"""

from __future__ import annotations

from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from office_convert.chunk_planner import BALANCE_FACTOR, plan_chunks
from office_convert.types import FormatName, ProbeResult

FORMATS: tuple[FormatName, ...] = ("docx", "pptx", "xlsx", "pdf")

# Total page count: 1..200 covers single-page edge case through reasonable docs.
page_counts = st.integers(min_value=1, max_value=200)
# Input size: small (1 KB) to large (500 MB).
size_bytes = st.integers(min_value=1024, max_value=500 * 1024 * 1024)
formats = st.sampled_from(FORMATS)
max_pages = st.integers(min_value=1, max_value=50)
max_mb = st.integers(min_value=1, max_value=500)


@st.composite
def probe_strategy(draw: st.DrawFn) -> ProbeResult:
    pages = draw(page_counts)
    return ProbeResult(
        page_count=pages,
        format=draw(formats),
        natural_seams=(),  # seam variant tested separately
        size_bytes=draw(size_bytes),
    )


@st.composite
def probe_with_seams(draw: st.DrawFn) -> ProbeResult:
    """Generate a probe with non-overlapping monotonic seams covering all pages."""
    pages = draw(st.integers(min_value=1, max_value=100))
    # Pick 1-5 seam boundaries to split into 1-6 seams
    boundary_count = draw(st.integers(min_value=0, max_value=min(5, pages - 1)))
    if boundary_count == 0 or pages == 1:
        seams: tuple[tuple[int, int], ...] = ((1, pages),)
    else:
        boundaries = sorted(
            draw(
                st.sets(
                    st.integers(min_value=1, max_value=pages - 1),
                    min_size=boundary_count,
                    max_size=boundary_count,
                )
            )
        )
        seam_list: list[tuple[int, int]] = []
        prev = 1
        for b in boundaries:
            seam_list.append((prev, b))
            prev = b + 1
        seam_list.append((prev, pages))
        seams = tuple(seam_list)
    return ProbeResult(
        page_count=pages,
        format=draw(formats),
        natural_seams=seams,
        size_bytes=draw(size_bytes),
    )


@settings(max_examples=500, deadline=timedelta(seconds=5))
@given(probe=probe_strategy(), mp=max_pages, mm=max_mb)
def test_chunks_cover_input_exactly(probe: ProbeResult, mp: int, mm: int) -> None:
    plan = plan_chunks(probe, max_pages_per_chunk=mp, max_mb_per_chunk=mm)
    if probe.page_count == 0:
        assert plan.chunks == ()
        return
    assert sum(c.pages for c in plan.chunks) == probe.page_count


@settings(max_examples=500, deadline=timedelta(seconds=5))
@given(probe=probe_strategy(), mp=max_pages, mm=max_mb)
def test_chunks_are_monotonic_and_non_overlapping(probe: ProbeResult, mp: int, mm: int) -> None:
    plan = plan_chunks(probe, max_pages_per_chunk=mp, max_mb_per_chunk=mm)
    if not plan.chunks:
        return
    assert plan.chunks[0].page_range[0] == 1
    assert plan.chunks[-1].page_range[1] == probe.page_count
    for i in range(len(plan.chunks) - 1):
        assert plan.chunks[i].page_range[1] + 1 == plan.chunks[i + 1].page_range[0]


@settings(max_examples=500, deadline=timedelta(seconds=5))
@given(probe=probe_strategy(), mp=max_pages, mm=max_mb)
def test_page_range_chunks_respect_max_pages_with_balance_factor(
    probe: ProbeResult, mp: int, mm: int
) -> None:
    plan = plan_chunks(probe, max_pages_per_chunk=mp, max_mb_per_chunk=mm)
    if not plan.chunks:
        return
    # All formats now support page-range slicing, so all use the standard
    # page-range path. Seam path: balance factor applies.
    threshold = mp * (BALANCE_FACTOR if any(c.natural_seam for c in plan.chunks) else 1)
    for c in plan.chunks:
        assert c.pages <= threshold, f"chunk {c} exceeds {threshold} pages"


@settings(max_examples=500, deadline=timedelta(seconds=5))
@given(probe=probe_strategy(), mp=max_pages, mm=max_mb)
def test_planner_is_deterministic(probe: ProbeResult, mp: int, mm: int) -> None:
    a = plan_chunks(probe, max_pages_per_chunk=mp, max_mb_per_chunk=mm)
    b = plan_chunks(probe, max_pages_per_chunk=mp, max_mb_per_chunk=mm)
    assert a == b


@settings(max_examples=200, deadline=timedelta(seconds=5))
@given(probe=probe_with_seams(), mp=max_pages, mm=max_mb)
def test_seamed_probe_still_covers_input(probe: ProbeResult, mp: int, mm: int) -> None:
    """Seam-or-fallback both must produce a complete cover."""
    plan = plan_chunks(probe, max_pages_per_chunk=mp, max_mb_per_chunk=mm)
    assert sum(c.pages for c in plan.chunks) == probe.page_count
