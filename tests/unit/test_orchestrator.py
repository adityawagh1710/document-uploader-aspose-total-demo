"""Unit tests for the orchestrator using mocked aspose_worker + real qpdf.

End-to-end tests of the request pipeline. Aspose calls are replaced by a fake
that writes a small valid PDF (via ReportLab) to the chunk output path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from office_convert import aspose_worker, orchestrator
from office_convert.cache import CacheManager
from office_convert.config import Settings
from office_convert.errors import OOMError, SubdivisionFloorError
from office_convert.types import Chunk, ConversionOptions, FormatName, ProbeResult

pytestmark = pytest.mark.skipif(
    shutil.which("qpdf") is None,
    reason="qpdf binary required for orchestrator integration",
)


@pytest.fixture(autouse=True)
def _force_one_shot_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests mock `aspose_worker.render_chunk` to inject fake PDFs.
    The orchestrator's default pool-mode dispatch bypasses that mock by
    spawning real worker subprocesses, so disable pool mode for the
    duration of this module to keep the mock on the hot path."""
    monkeypatch.setenv("OFFICE_CONVERT_POOL_MODE", "0")


def _make_pdf(path: Path, pages: int = 2) -> None:
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(100, 750, f"p{i + 1}")
        c.showPage()
    c.save()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # render_chunk is mocked in this test, so the worker binaries don't need
    # to exist on disk — only the prefix is referenced.
    return Settings(
        worker_binary_prefix=tmp_path / "fake-worker",
        license_path=tmp_path / "license.lic",
        scratch_dir=tmp_path / "scratch",
        chunk_timeout_seconds=60,
        max_pages_per_chunk=2,
        max_mb_per_chunk=50,
    )


async def test_orchestrator_happy_path_yields_pdf(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock probe + aspose_worker; orchestrator should produce a merged PDF."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    input_path = tmp_path / "input.docx"
    input_path.write_bytes(b"fake docx content" * 100)

    async def fake_probe(path: Path, fmt: FormatName, _s: object, _rid: str) -> ProbeResult:
        return ProbeResult(page_count=4, format=fmt, natural_seams=(), size_bytes=100)

    rendered_chunks: list[Chunk] = []

    async def fake_render(
        chunk: Chunk,
        input_path: Path,
        format: FormatName,
        scratch_dir: Path,
        request_id: str,
        settings: Settings,
    ) -> Path:
        rendered_chunks.append(chunk)
        out = scratch_dir / f"chunk-{chunk.index}.pdf"
        _make_pdf(out, pages=chunk.pages)
        return out

    monkeypatch.setattr("office_convert.orchestrator.do_probe", fake_probe)
    monkeypatch.setattr(aspose_worker, "render_chunk", fake_render)

    cache = CacheManager(None, settings.aspose_version)
    blocks: list[bytes] = []
    async for block in orchestrator.convert_job(
        request_id="req-1",
        input_path=input_path,
        format="docx",
        options=ConversionOptions(cache=False),
        settings=settings,
        cache=cache,
        scratch_dir=scratch,
    ):
        blocks.append(block)

    merged = b"".join(blocks)
    assert merged.startswith(b"%PDF-")
    # 4 pages / max_pages=2 → 2 chunks
    assert len(rendered_chunks) == 2


async def test_orchestrator_subdivision_floor_failure(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every render OOMs even at floor=1 page, raise SubdivisionFloorError."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    input_path = tmp_path / "input.docx"
    input_path.write_bytes(b"x" * 100)

    async def fake_probe(path: Path, fmt: FormatName, _s: object, _rid: str) -> ProbeResult:
        return ProbeResult(page_count=1, format=fmt, natural_seams=(), size_bytes=100)

    async def always_oom(chunk: Chunk, *args: object, **kwargs: object) -> Path:
        raise OOMError(chunk)

    monkeypatch.setattr("office_convert.orchestrator.do_probe", fake_probe)
    monkeypatch.setattr(aspose_worker, "render_chunk", always_oom)

    cache = CacheManager(None, settings.aspose_version)
    with pytest.raises(SubdivisionFloorError):
        async for _ in orchestrator.convert_job(
            request_id="req-floor",
            input_path=input_path,
            format="docx",
            options=ConversionOptions(cache=False),
            settings=settings,
            cache=cache,
            scratch_dir=scratch,
        ):
            pass


async def test_orchestrator_subdivision_recovers(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First attempt OOMs on a 2-page chunk; sub-chunks succeed."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    input_path = tmp_path / "input.docx"
    input_path.write_bytes(b"x" * 100)

    async def fake_probe(path: Path, fmt: FormatName, _s: object, _rid: str) -> ProbeResult:
        return ProbeResult(page_count=2, format=fmt, natural_seams=(), size_bytes=100)

    call_count = {"n": 0}

    async def oom_first_then_succeed(chunk: Chunk, *args: object, **kwargs: object) -> Path:
        call_count["n"] += 1
        # First call (the 2-page chunk) OOMs; subsequent (single-page) succeed.
        if chunk.pages == 2:
            raise OOMError(chunk)
        out = scratch / f"chunk-{chunk.index}.pdf"
        _make_pdf(out, pages=chunk.pages)
        return out

    monkeypatch.setattr("office_convert.orchestrator.do_probe", fake_probe)
    monkeypatch.setattr(aspose_worker, "render_chunk", oom_first_then_succeed)

    cache = CacheManager(None, settings.aspose_version)
    blocks: list[bytes] = []
    async for block in orchestrator.convert_job(
        request_id="req-sub",
        input_path=input_path,
        format="docx",
        options=ConversionOptions(cache=False),
        settings=settings,
        cache=cache,
        scratch_dir=scratch,
    ):
        blocks.append(block)
    assert b"".join(blocks).startswith(b"%PDF-")
    assert call_count["n"] >= 2  # parent OOM + at least 1 sub-render
