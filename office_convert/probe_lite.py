"""Metadata-only probe that avoids loading the full document into Aspose.

Aspose's per-product probe (`Document(path).get_PageCount()`) loads the
entire input to compute page count. For inputs over ~50 MB, that pushes
the worker past the 2 GB RLIMIT_AS ceiling and the kernel SIGKILLs the
probe (exit 137). The orchestrator never gets a page count, never plans
chunks, never gets a chance to subdivide — the request fails outright.

This module sidesteps Aspose for the probe step:

  - **OOXML (docx/pptx)**: open the .zip and read a few KB from
    `docProps/app.xml` (DOCX page count, PPTX slide count). Memory
    cost: tens of KB regardless of input size. XLSX is intentionally
    excluded: the C++ XLSX probe needs the rendered PDF page count
    (not the worksheet count) for chunk planning, so it always falls
    through to the Aspose worker.

  - **PDF**: shell out to qpdf `--show-npages` (qpdf is already in the
    runtime image for the streaming concat). Uses qpdf's own xref-table
    parser, which reads only the trailer + xref — memory cost bounded
    independent of PDF size.

The price: app.xml is written at *save time* by the authoring application.
If a document was edited externally between save and conversion (rare for
the usual upload flow), the count could drift. For chunk *planning* this
is acceptable — chunks are then merged via qpdf based on actual rendered
output, not a planning count. The render path validates against the live
document.

Returns `ProbeResult | None`. `None` means "no reliable metadata count
extractable; fall through to the C++ Aspose worker." Exceptions from
malformed inputs are swallowed (return None) — the caller's fallback path
is the canonical failure surface.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from office_convert.types import FormatName, ProbeResult

log = logging.getLogger(__name__)

_OOXML_APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"


async def probe_lite(input_path: Path, format: FormatName) -> ProbeResult | None:
    """Best-effort metadata-only probe. Returns None on any failure."""
    size = input_path.stat().st_size
    try:
        if format == "docx":
            n = await asyncio.to_thread(_ooxml_count, input_path, "Pages")
            # If app.xml doesn't have a page count, use a size-based estimate
            # rather than falling through to the expensive Aspose probe (which
            # loads the entire document for a full layout pass — 10+ minutes
            # for large files). The estimate is conservative (assumes ~20 KB
            # per page for DOCX) so the planner over-chunks slightly; the
            # OOM subdivision path handles any miscalculation.
            if n is None or n <= 0:
                n = _estimate_pages_from_size(size, format)
        elif format == "pptx":
            # Try app.xml first, then count actual slide XML files in the ZIP
            # (always accurate — each slide is a separate XML entry).
            n = await asyncio.to_thread(_ooxml_count, input_path, "Slides")
            if n is None or n <= 0:
                n = await asyncio.to_thread(_pptx_slide_count_from_zip, input_path)
            if n is None or n <= 0:
                n = _estimate_pages_from_size(size, format)
        elif format == "xlsx":
            # XLSX worksheet count was a valid over-estimate under the single-
            # chunk carve-out (one worksheet → at least one PDF page). Once
            # xlsx.cpp adopted PdfSaveOptions::SetPageIndex/SetPageCount, the
            # planner subdivides on *rendered* page count — a 1M-row sheet
            # paginates into thousands of PDF pages, so the worksheet count
            # would silently under-count and the slice would drop most rows.
            # Fall through to the C++ probe (WorkbookRender::GetPageCount).
            return None
        elif format == "pdf":
            n = await _qpdf_page_count(input_path)
        else:
            return None
    except Exception as e:
        log.debug("probe_lite(%s) failed, falling back: %s", format, e)
        return None

    if n is None or n <= 0:
        return None
    return ProbeResult(page_count=n, format=format, natural_seams=(), size_bytes=size)


def _ooxml_count(path: Path, element: str) -> int | None:
    """Read `<Pages>` or `<Slides>` from `docProps/app.xml` inside an OOXML zip.

    Returns None if the file isn't a valid OOXML zip, app.xml is missing,
    or the element isn't present / isn't an integer.
    """
    with zipfile.ZipFile(path) as z:
        try:
            with z.open("docProps/app.xml") as f:
                tree = ET.parse(f)
        except KeyError:
            return None
        node = tree.getroot().find(f"{{{_OOXML_APP_NS}}}{element}")
        if node is None or node.text is None:
            return None
        try:
            return int(node.text.strip())
        except ValueError:
            return None


def _pptx_slide_count_from_zip(path: Path) -> int | None:
    """Count actual slide XML files in the PPTX ZIP structure.

    Each slide in a PPTX is stored as `ppt/slides/slide<N>.xml`. This gives
    the exact slide count without loading the presentation into Aspose —
    just a ZIP directory listing (microseconds, zero memory).

    Returns None if the file isn't a valid ZIP or has no slide entries.
    """
    try:
        with zipfile.ZipFile(path) as z:
            count = sum(
                1 for name in z.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            return count if count > 0 else None
    except (zipfile.BadZipFile, OSError):
        return None


_QPDF_NPAGES_RE = re.compile(rb"^\s*(\d+)\s*$")


# Conservative bytes-per-page estimates for size-based fallback probing.
# These are intentionally LOW (meaning we estimate MORE pages than reality)
# so the chunk planner produces slightly more chunks than needed. Over-chunking
# is safe (just slightly more subprocess spawns); under-chunking risks OOM.
# Derived from empirical observation across typical Office documents.
_BYTES_PER_PAGE_ESTIMATE: dict[FormatName, int] = {
    "docx": 20_000,   # ~20 KB/page (text-heavy docs are ~5-10 KB, image-heavy ~50-100 KB)
    "pptx": 100_000,  # ~100 KB/slide (slides have images/shapes)
    "xlsx": 50_000,   # ~50 KB/page (not used — XLSX always goes to C++ probe)
    "pdf": 30_000,    # ~30 KB/page (not used — qpdf handles PDF)
}


def _estimate_pages_from_size(size_bytes: int, format: FormatName) -> int:
    """Estimate page count from file size when metadata is unavailable.

    Returns a conservative estimate (tends to over-count pages) so the
    chunk planner produces more, smaller chunks. This is safe because:
    - Over-chunking just means slightly more subprocess spawns
    - The adaptive chunk sizing will still produce reasonable chunk sizes
    - If a chunk covers pages that don't exist, the worker renders what's
      available (Aspose handles out-of-range page indices gracefully)

    Minimum return is 1 page.
    """
    bytes_per_page = _BYTES_PER_PAGE_ESTIMATE.get(format, 20_000)
    estimated = max(1, size_bytes // bytes_per_page)
    log.info(
        "probe_lite size-based estimate: format=%s size=%d bytes_per_page=%d estimated_pages=%d",
        format, size_bytes, bytes_per_page, estimated,
    )
    return estimated


async def _qpdf_page_count(path: Path) -> int | None:
    """qpdf --show-npages <file> → integer on stdout. Memory cost bounded.

    Returns None on non-zero exit or unparseable output (which then falls
    through to the C++ Aspose probe).
    """
    proc = await asyncio.create_subprocess_exec(
        "qpdf",
        "--show-npages",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    m = _QPDF_NPAGES_RE.match(stdout)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None
