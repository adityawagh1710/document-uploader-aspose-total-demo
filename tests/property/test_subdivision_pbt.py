"""PBT for chunk_planner.subdivide: termination + determinism."""

from __future__ import annotations

from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from office_convert.chunk_planner import subdivide
from office_convert.types import Chunk


@st.composite
def chunk_strategy(draw: st.DrawFn) -> Chunk:
    start = draw(st.integers(min_value=1, max_value=1000))
    span = draw(st.integers(min_value=1, max_value=100))
    return Chunk(
        index=float(draw(st.integers(min_value=0, max_value=100))),
        page_range=(start, start + span - 1),
        natural_seam=False,
    )


@settings(max_examples=100, deadline=timedelta(seconds=2))
@given(chunk=chunk_strategy())
def test_subdivide_at_floor_returns_empty(chunk: Chunk) -> None:
    if chunk.pages == 1:
        assert subdivide(chunk) == []


@settings(max_examples=100, deadline=timedelta(seconds=2))
@given(chunk=chunk_strategy())
def test_subdivide_above_floor_returns_two_chunks_covering_input(chunk: Chunk) -> None:
    if chunk.pages > 1:
        subs = subdivide(chunk)
        assert len(subs) == 2
        # Sub-chunks cover the same range, non-overlapping
        assert subs[0].page_range[0] == chunk.page_range[0]
        assert subs[1].page_range[1] == chunk.page_range[1]
        assert subs[0].page_range[1] + 1 == subs[1].page_range[0]
        # Total pages preserved
        assert sum(c.pages for c in subs) == chunk.pages


@settings(max_examples=100, deadline=timedelta(seconds=2))
@given(chunk=chunk_strategy())
def test_subdivide_is_deterministic(chunk: Chunk) -> None:
    a = subdivide(chunk)
    b = subdivide(chunk)
    assert a == b


@settings(max_examples=50, deadline=timedelta(seconds=2))
@given(chunk=chunk_strategy())
def test_subdivide_terminates_in_bounded_steps(chunk: Chunk) -> None:
    """Repeatedly subdivide; must reach floor in log2(pages) + ε steps."""
    current = [chunk]
    steps = 0
    max_steps = 10  # log2(100) ≈ 7; allow slack
    while any(c.pages > 1 for c in current) and steps < max_steps:
        next_round: list[Chunk] = []
        for c in current:
            subs = subdivide(c)
            if not subs:
                next_round.append(c)
            else:
                next_round.extend(subs)
        current = next_round
        steps += 1
    assert steps < max_steps
    assert all(c.pages == 1 for c in current)
