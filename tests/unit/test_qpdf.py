"""Tests for office_convert.qpdf: streaming concat with the real qpdf binary.

Skipped on hosts without qpdf installed. ReportLab generates small PDFs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from office_convert.errors import MergeError
from office_convert.qpdf import concat_streaming

pytestmark = pytest.mark.skipif(
    shutil.which("qpdf") is None,
    reason="qpdf binary not installed; integration test requires it",
)


def _make_pdf(path: Path, pages: int) -> None:
    """Generate a simple multi-page PDF using ReportLab."""
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(100, 750, f"page {i + 1}")
        c.showPage()
    c.save()


async def test_concat_two_pdfs_yields_combined_bytes(tmp_path: Path) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    _make_pdf(a, 2)
    _make_pdf(b, 3)
    chunks: list[bytes] = []
    async for block in concat_streaming([a, b]):
        chunks.append(block)
    merged = b"".join(chunks)
    assert merged.startswith(b"%PDF-")
    # qpdf-merged output should be larger than either input
    assert len(merged) >= max(a.stat().st_size, b.stat().st_size)


async def test_concat_single_pdf_passes_through(tmp_path: Path) -> None:
    a = tmp_path / "a.pdf"
    _make_pdf(a, 5)
    chunks: list[bytes] = []
    async for block in concat_streaming([a]):
        chunks.append(block)
    assert b"".join(chunks).startswith(b"%PDF-")


async def test_concat_tee_writes_cache_file(tmp_path: Path) -> None:
    a = tmp_path / "a.pdf"
    _make_pdf(a, 2)
    cache_temp = tmp_path / "cache.tmp"
    chunks: list[bytes] = []
    async for block in concat_streaming([a], cache_temp_path=cache_temp):
        chunks.append(block)
    streamed = b"".join(chunks)
    assert cache_temp.exists()
    assert cache_temp.read_bytes() == streamed


async def test_concat_empty_list_raises() -> None:
    with pytest.raises(MergeError):
        async for _ in concat_streaming([]):
            pass


async def test_concat_missing_input_raises(tmp_path: Path) -> None:
    with pytest.raises(MergeError):
        async for _ in concat_streaming([tmp_path / "nonexistent.pdf"]):
            pass
