"""Probe via the C++ worker binary; format detection via magic bytes.

Implements:
  - Magic-byte format detection per business-rules.md §7.1 (FR-1 + NFR-3 fast-fail).
  - Probe step per business-logic-model.md §1 by invoking the worker in
    `--mode=probe`. The worker writes a JSON ProbeResult to stdout.

Both pure functions and async functions live here; the async ones depend on
aspose_worker for subprocess spawning.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from office_convert.errors import InputUnprocessableError, UnsupportedFormatError
from office_convert.types import DispatchFormat, FormatName, ProbeResult

if TYPE_CHECKING:
    from office_convert.config import Settings

log = logging.getLogger(__name__)

ACCEPTED_FORMATS: tuple[FormatName, ...] = ("docx", "pptx", "xlsx", "pdf")
# What the user is allowed to upload. The orchestrator maps legacy
# (.doc/.xls/.ppt) to the modern format names internally — Aspose's
# Document/Workbook/Presentation constructors detect OLE2 vs OOXML from
# content. CSV is normalized to XLSX in the server handler before this
# detection runs (see office_convert.csv_input). Exposed to the user only
# via the UnsupportedFormatError detail.
ACCEPTED_UPLOAD_FORMATS: tuple[str, ...] = (
    "docx",
    "pptx",
    "xlsx",
    "pdf",
    "doc",
    "xls",
    "ppt",
    "csv",
    "rtf",
    "odt",
    "ods",
    "odp",
    "odg",
    "png",
    "jpg",
    "jpeg",
    "tiff",
    "tif",
    "gif",
    "bmp",
    "webp",
    "svg",
    "eml",
)

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
# Microsoft Compound File Binary (CFB / OLE2). Used by every pre-2007
# Office binary format: .doc/.dot, .xls/.xlt/.xlm, .ppt/.pot/.pps.
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Rich Text Format — Aspose.Words ingests RTF natively, so we route it
# through the docx worker without any pre-conversion step.
RTF_MAGIC = b"{\\rtf"

# Raster + vector image magic bytes. All of these route to the LibreOffice
# fallback (`soffice --convert-to pdf` picks the right import filter by
# input extension — see DispatchFormat in types.py).
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
GIF87_MAGIC = b"GIF87a"
GIF89_MAGIC = b"GIF89a"
BMP_MAGIC = b"BM"
TIFF_LE_MAGIC = b"II*\x00"
TIFF_BE_MAGIC = b"MM\x00*"
# WEBP files start with RIFF<4 bytes>WEBP — the 4 bytes in between are the
# chunk size, so this is a two-segment match handled in detect_format().
RIFF_MAGIC = b"RIFF"
WEBP_TAG = b"WEBP"

# EML (RFC 5322) has no fixed magic. It's a text-format mail container that
# always begins with one of a small set of header field names followed by
# ": ". A real EML file may start with any of these; "From " (mbox separator)
# is excluded because that's the mbox archive format, not a standalone EML.
# Checking the first ~1 KB head sample after stripping leading whitespace +
# optional UTF-8 BOM is enough to disambiguate from random text without
# false-positives on plain text files (those lack the ": " continuation).
EML_HEADER_PREFIXES: tuple[bytes, ...] = (
    b"received:",
    b"return-path:",
    b"delivered-to:",
    b"message-id:",
    b"date:",
    b"from:",
    b"to:",
    b"subject:",
    b"mime-version:",
    b"x-",  # any X-* extension header
)

OOXML_CONTENT_TYPE_TO_FORMAT: dict[str, FormatName] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml": "xlsx",
}

# ODF files also start with ZIP magic. Per ODF spec, the first ZIP entry is
# an uncompressed "mimetype" file whose contents identify the document type.
# Aspose handles ODT/ODS/ODP natively via the docx/xlsx/pptx workers; ODG
# is routed to the LibreOffice fallback path (Aspose.Total C++ has no
# library that renders drawing pages).
ODF_MIMETYPE_TO_FORMAT: dict[str, DispatchFormat] = {
    "application/vnd.oasis.opendocument.text": "docx",
    "application/vnd.oasis.opendocument.spreadsheet": "xlsx",
    "application/vnd.oasis.opendocument.presentation": "pptx",
    "application/vnd.oasis.opendocument.graphics": "odg",
}

# ODF subtypes neither Aspose.Total C++ nor LibreOffice's --convert-to-pdf
# path produces meaningful PDFs for. Detected here so we can reject them
# at the gate with a precise message — without this dict the files
# default-route to the docx worker and surface as confusing
# `FileCorruptedException` errors. (.odf is a standalone MathML formula;
# .odb is a Base database container — neither has rendering semantics.)
UNRENDERABLE_ODF_SUBTYPES: dict[str, str] = {
    "application/vnd.oasis.opendocument.formula": "OpenDocument Formula (.odf)",
    "application/vnd.oasis.opendocument.base": "OpenDocument Base (.odb)",
}

# CFB directory entries store stream names as UTF-16LE. Search the first
# ~64 KB of an OLE2 file for these byte patterns to identify the app that
# wrote it. Order matters: longer/more-specific signatures first to avoid
# the "Workbook" prefix accidentally matching a "WorkbookView" stream in
# some hand-crafted CFB blob.
OLE2_STREAM_SIGNATURES: tuple[tuple[bytes, FormatName], ...] = (
    (
        b"P\x00o\x00w\x00e\x00r\x00P\x00o\x00i\x00n\x00t\x00 \x00D\x00o\x00c\x00u\x00m\x00e\x00n\x00t",  # noqa: E501
        "pptx",
    ),
    (b"W\x00o\x00r\x00d\x00D\x00o\x00c\x00u\x00m\x00e\x00n\x00t", "docx"),
    (b"W\x00o\x00r\x00k\x00b\x00o\x00o\x00k", "xlsx"),
    (b"B\x00o\x00o\x00k", "xlsx"),  # Excel 5.0 / Excel 95 wrote "Book" instead of "Workbook"
)

# Filename-extension fallback when OLE2 stream-name detection comes up empty
# (rare; usually means a malformed or non-Office CFB file).
OLE2_EXT_TO_FORMAT: dict[str, FormatName] = {
    "doc": "docx",
    "dot": "docx",
    "xls": "xlsx",
    "xlt": "xlsx",
    "xlm": "xlsx",
    "ppt": "pptx",
    "pot": "pptx",
    "pps": "pptx",
}


def detect_format(  # noqa: PLR0911
    magic_bytes: bytes,
    *,
    source_path: Path | None = None,
    filename: str | None = None,
) -> DispatchFormat:
    """Detect format by magic bytes, plus OOXML/OLE2 disambiguation.

    For OOXML formats (DOCX/PPTX/XLSX), the ZIP magic alone isn't enough.
    Python's zipfile needs the End-of-Central-Directory record (at the END
    of the archive) to enumerate members, so a byte prefix can't work for
    large files. When `source_path` is provided we open the full file;
    otherwise we fall back to the prefix path (kept for unit tests that
    pass in-memory zips small enough to contain their own EOCD).

    For OLE2 (pre-2007 binary Office: .doc/.xls/.ppt), the magic doesn't
    tell us which application wrote the file. We scan a head sample for
    distinctive stream names in the CFB directory (UTF-16LE), and fall
    back to the uploaded filename's extension when no signature matches.
    Legacy formats map to the modern format-name internally — Aspose
    handles both binary and zip-based variants through one constructor.
    """
    if not magic_bytes:
        raise UnsupportedFormatError(
            detected_magic="(empty)", accepted=list(ACCEPTED_UPLOAD_FORMATS)
        )

    if magic_bytes.startswith(PDF_MAGIC):
        return "pdf"

    if magic_bytes.startswith(ZIP_MAGIC):
        if source_path is not None:
            return _inspect_ooxml_path(source_path)
        return _inspect_ooxml_prefix(magic_bytes)

    if magic_bytes.startswith(OLE2_MAGIC):
        return _classify_ole2(source_path, filename)

    if magic_bytes.startswith(RTF_MAGIC):
        return "docx"

    image_fmt = _detect_image_format(magic_bytes)
    if image_fmt is not None:
        return image_fmt

    if _looks_like_eml(magic_bytes):
        return "eml"

    head_hex = magic_bytes[:8].hex()
    raise UnsupportedFormatError(detected_magic=head_hex, accepted=list(ACCEPTED_UPLOAD_FORMATS))


def _looks_like_eml(magic_bytes: bytes) -> bool:
    """Detect RFC 5322 email by header lines in the first ~1 KB.

    Tolerates a leading UTF-8 BOM and leading whitespace. The presence of
    a recognized header name followed by ': ' is the discriminator: plain
    text files happen to start with words but rarely with one of these
    specific names followed by a colon and a space.
    """
    head = magic_bytes[:1024].lstrip(b"\xef\xbb\xbf").lstrip()
    if b": " not in head[:200]:
        return False
    first_line = head.split(b"\n", 1)[0].lower()
    return any(first_line.startswith(prefix) for prefix in EML_HEADER_PREFIXES)


def _detect_image_format(magic_bytes: bytes) -> DispatchFormat | None:  # noqa: PLR0911
    """Magic-byte image detection.

    SVG is text-based with no fixed magic; we look for a `<svg` tag in the
    head sample (case-insensitive) after a tolerant XML/BOM prefix. The
    file is routed to LibreOffice regardless of the specific raster vs
    vector format because soffice's `--convert-to pdf` filter picks the
    correct importer from the input file's extension. PLR0911 ignored —
    one return per format reads more naturally than a dict lookup here.
    """
    if magic_bytes.startswith(PNG_MAGIC):
        return "png"
    if magic_bytes.startswith(JPEG_MAGIC):
        return "jpg"
    if magic_bytes.startswith((GIF87_MAGIC, GIF89_MAGIC)):
        return "gif"
    if magic_bytes.startswith(BMP_MAGIC):
        return "bmp"
    if magic_bytes.startswith((TIFF_LE_MAGIC, TIFF_BE_MAGIC)):
        return "tiff"
    if magic_bytes.startswith(RIFF_MAGIC) and WEBP_TAG in magic_bytes[:16]:
        return "webp"
    # SVG: tolerant text sniff in the first ~512 bytes
    head = magic_bytes[:512].lstrip(b"\xef\xbb\xbf").lstrip()
    if head.startswith((b"<?xml", b"<svg")) and b"<svg" in magic_bytes[:512].lower():
        return "svg"
    return None


def _classify_ole2(source_path: Path | None, filename: str | None) -> FormatName:
    """Identify a CFB/OLE2 file as docx/xlsx/pptx by scanning for stream-name
    signatures, with the uploaded filename's extension as a fallback hint.

    Scans up to 512KB of the file (OLE2 directory entries can be scattered
    throughout the file, especially in large documents). Collects ALL matching
    signatures and uses priority order: Word > PowerPoint > Excel, because
    Word documents often contain embedded Excel objects whose stream names
    would otherwise cause misrouting.
    """
    if source_path is not None:
        try:
            with open(source_path, "rb") as f:
                # Scan up to 512KB for stream signatures (65KB was too small
                # for large OLE2 files where directory entries are scattered)
                head = f.read(524288)

            # Collect all matching formats (a Word doc might also contain
            # Workbook streams from embedded Excel objects)
            found_formats: list[FormatName] = []
            for sig, fmt in OLE2_STREAM_SIGNATURES:
                if sig in head:
                    found_formats.append(fmt)

            if found_formats:
                # Priority: docx > pptx > xlsx
                # Word docs often embed Excel objects, so if WordDocument is
                # present alongside Workbook, it's a Word doc.
                if "docx" in found_formats:
                    return "docx"
                if "pptx" in found_formats:
                    return "pptx"
                if "xlsx" in found_formats:
                    return "xlsx"
                return found_formats[0]
        except OSError as e:
            log.debug("OLE2 head read failed for %s: %s", source_path, e)

    if filename:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix in OLE2_EXT_TO_FORMAT:
            return OLE2_EXT_TO_FORMAT[suffix]

    raise UnsupportedFormatError(
        detected_magic=OLE2_MAGIC.hex(), accepted=list(ACCEPTED_UPLOAD_FORMATS)
    )


def _inspect_ooxml_path(path: Path) -> DispatchFormat:
    """Classify a ZIP-magic document on disk as OOXML or ODF.

    ODF: read the `mimetype` entry (uncompressed first entry per ODF spec)
    and map text/spreadsheet/presentation → docx/xlsx/pptx workers.
    OOXML: fall back to `[Content_Types].xml` and map by content-type string.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "mimetype" in names:
                mimetype = zf.read("mimetype").decode("utf-8", errors="replace").strip()
                if mimetype in ODF_MIMETYPE_TO_FORMAT:
                    return ODF_MIMETYPE_TO_FORMAT[mimetype]
                if mimetype in UNRENDERABLE_ODF_SUBTYPES:
                    raise UnsupportedFormatError(
                        detected_magic=mimetype,
                        accepted=list(ACCEPTED_UPLOAD_FORMATS),
                        reason=(
                            f"{UNRENDERABLE_ODF_SUBTYPES[mimetype]} is not "
                            f"supported by Aspose.Total C++"
                        ),
                    )
            if "[Content_Types].xml" in names:
                content = zf.read("[Content_Types].xml").decode("utf-8", errors="replace")
                return _classify_by_content_types(content)
    except (zipfile.BadZipFile, OSError, EOFError, KeyError):
        log.debug("could not classify zip document %s; defaulting to docx", path)
    return "docx"


def _inspect_ooxml_prefix(prefix_bytes: bytes) -> FormatName:
    """Legacy prefix-only inspection — only viable when the prefix contains the zip EOCD."""
    try:
        with zipfile.ZipFile(io.BytesIO(prefix_bytes)) as zf:
            for name in zf.namelist():
                if name == "[Content_Types].xml":
                    content = zf.read(name).decode("utf-8", errors="replace")
                    return _classify_by_content_types(content)
    except (zipfile.BadZipFile, OSError, EOFError, KeyError):
        log.debug("could not inspect OOXML prefix; defaulting to docx")
    return "docx"


def _classify_by_content_types(content: str) -> FormatName:
    for ctype, fmt in OOXML_CONTENT_TYPE_TO_FORMAT.items():
        if ctype in content:
            return fmt
    return "docx"  # permissive default


def parse_probe_json(stdout_bytes: bytes) -> ProbeResult:
    """Parse the JSON ProbeResult the C++ worker writes to stdout."""
    try:
        data = json.loads(stdout_bytes)
    except json.JSONDecodeError as e:
        raise InputUnprocessableError(f"worker returned invalid JSON: {e}") from e

    try:
        return ProbeResult(
            page_count=int(data["page_count"]),
            format=data["format"],
            natural_seams=tuple(tuple(s) for s in data.get("natural_seams", [])),
            size_bytes=int(data["size_bytes"]),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise InputUnprocessableError(f"worker probe JSON missing field: {e}") from e


async def probe(
    input_path: Path,
    format: FormatName,
    settings: Settings,
    request_id: str,
) -> ProbeResult:
    """Two-tier probe: metadata-only first, full Aspose probe as fallback.

    Aspose's `Document(path).get_PageCount()` materializes the entire input
    in memory; for inputs over ~50 MB that pushes the worker past the 2 GB
    RLIMIT_AS ceiling and the kernel SIGKILLs it (exit 137). The lite tier
    reads page counts from OOXML `docProps/app.xml` / `xl/workbook.xml` or
    qpdf's PDF xref — memory cost bounded independent of input size. Only
    falls through to the Aspose worker for malformed/exotic inputs.

    Format mismatch retry: if the Aspose worker rejects the file with
    input_unprocessable and the error hints at a different format (e.g.,
    "This is a word doc file" from the XLSX worker), we retry with the
    hinted format. This handles mislabeled files gracefully.
    """
    from office_convert.aspose_worker import _run_worker
    from office_convert.probe_lite import probe_lite

    lite = await probe_lite(input_path, format)
    if lite is not None:
        return lite

    try:
        stdout, _stderr = await _run_worker(
            mode="probe",
            input_path=input_path,
            format=format,
            output_path=None,
            page_range=None,
            request_id=request_id,
            settings=settings,
            capture_stdout=True,
        )
        return parse_probe_json(stdout)
    except InputUnprocessableError as e:
        # Check if the error hints at a different format — retry once
        error_msg = str(e).lower()
        retry_format: FormatName | None = None
        if "word doc" in error_msg and format != "docx":
            retry_format = "docx"
        elif "excel" in error_msg or "workbook" in error_msg and format != "xlsx":
            retry_format = "xlsx"
        elif "powerpoint" in error_msg or "presentation" in error_msg and format != "pptx":
            retry_format = "pptx"

        if retry_format is not None:
            log.warning(
                "probe format mismatch: %s worker rejected file, retrying as %s",
                format,
                retry_format,
            )
            # Update the ProbeResult format to the correct one
            try:
                stdout, _stderr = await _run_worker(
                    mode="probe",
                    input_path=input_path,
                    format=retry_format,
                    output_path=None,
                    page_range=None,
                    request_id=request_id,
                    settings=settings,
                    capture_stdout=True,
                )
                result = parse_probe_json(stdout)
                # Return with corrected format
                return ProbeResult(
                    page_count=result.page_count,
                    format=retry_format,
                    natural_seams=result.natural_seams,
                    size_bytes=result.size_bytes,
                )
            except Exception:
                pass  # Retry failed too — raise original error
        raise
