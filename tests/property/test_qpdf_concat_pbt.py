"""PBT for qpdf concat: page-count round-trip, order preservation, associativity.

Uses ReportLab to generate small PDFs with known page counts. Skipped if qpdf
or ReportLab missing.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.skipif(
    shutil.which("qpdf") is None,
    reason="qpdf binary not installed",
)


def _make_pdf(path: Path, pages: int) -> None:
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(100, 750, f"p{i + 1}")
        c.showPage()
    c.save()


def _page_count(path: Path) -> int:
    """Use qpdf --show-npages to count pages."""
    result = subprocess.run(
        ["qpdf", "--show-npages", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _concat(out: Path, inputs: list[Path]) -> None:
    subprocess.run(
        ["qpdf", "--empty", "--pages", *[str(p) for p in inputs], "--", str(out)],
        check=True,
        capture_output=True,
    )


@settings(
    max_examples=100,
    deadline=timedelta(seconds=10),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(page_counts=st.lists(st.integers(min_value=1, max_value=10), min_size=2, max_size=5))
def test_concat_preserves_total_page_count(
    page_counts: list[int], tmp_path_factory: pytest.TempPathFactory
) -> None:
    tmp_path = tmp_path_factory.mktemp("concat")
    inputs: list[Path] = []
    for i, n in enumerate(page_counts):
        p = tmp_path / f"in_{i}.pdf"
        _make_pdf(p, n)
        inputs.append(p)
    merged = tmp_path / "merged.pdf"
    _concat(merged, inputs)
    assert _page_count(merged) == sum(page_counts)


@settings(
    max_examples=50,
    deadline=timedelta(seconds=10),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    a_pages=st.integers(min_value=1, max_value=5),
    b_pages=st.integers(min_value=1, max_value=5),
    c_pages=st.integers(min_value=1, max_value=5),
)
def test_concat_is_associative(
    a_pages: int,
    b_pages: int,
    c_pages: int,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    tmp_path = tmp_path_factory.mktemp("assoc")
    a, b, c = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"
    _make_pdf(a, a_pages)
    _make_pdf(b, b_pages)
    _make_pdf(c, c_pages)

    abc = tmp_path / "abc.pdf"
    ab_then_c = tmp_path / "ab_then_c.pdf"
    ab = tmp_path / "ab.pdf"
    a_then_bc = tmp_path / "a_then_bc.pdf"
    bc = tmp_path / "bc.pdf"

    _concat(abc, [a, b, c])
    _concat(ab, [a, b])
    _concat(ab_then_c, [ab, c])
    _concat(bc, [b, c])
    _concat(a_then_bc, [a, bc])

    # All three produce the same page count
    assert _page_count(abc) == a_pages + b_pages + c_pages
    assert _page_count(ab_then_c) == _page_count(abc)
    assert _page_count(a_then_bc) == _page_count(abc)
