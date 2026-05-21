"""Tests for office_convert.probe: format detection and probe JSON parsing."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from office_convert.errors import InputUnprocessableError, UnsupportedFormatError
from office_convert.probe import (
    ACCEPTED_UPLOAD_FORMATS,
    OLE2_MAGIC,
    detect_format,
    parse_probe_json,
)
from office_convert.types import ProbeResult


def test_detect_pdf_by_magic() -> None:
    assert detect_format(b"%PDF-1.7\n...") == "pdf"


def test_detect_rejects_random_bytes() -> None:
    with pytest.raises(UnsupportedFormatError) as exc:
        detect_format(b"\x00\x00\x00\x00not a real file")
    assert "00000000" in exc.value.detected_magic
    assert set(exc.value.accepted) == set(ACCEPTED_UPLOAD_FORMATS)


def test_detect_empty_input_raises() -> None:
    with pytest.raises(UnsupportedFormatError):
        detect_format(b"")


def _build_ooxml(content_type_xml: str) -> bytes:
    """Build an in-memory zip with a [Content_Types].xml entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_type_xml)
    return buf.getvalue()


def test_detect_docx_content_types() -> None:
    xml = (
        '<?xml version="1.0"?><Types>'
        '<Override ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    blob = _build_ooxml(xml)
    assert detect_format(blob) == "docx"


def test_detect_pptx_content_types() -> None:
    xml = (
        '<?xml version="1.0"?><Types>'
        '<Override ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        "</Types>"
    )
    assert detect_format(_build_ooxml(xml)) == "pptx"


def test_detect_xlsx_content_types() -> None:
    xml = (
        '<?xml version="1.0"?><Types>'
        '<Override ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        "</Types>"
    )
    assert detect_format(_build_ooxml(xml)) == "xlsx"


def test_detect_large_pptx_requires_source_path(tmp_path) -> None:
    """OOXML files larger than the byte prefix can only be classified via source_path.

    Python's zipfile needs the End-of-Central-Directory record (at the END
    of the archive) to enumerate members. Without source_path, a large pptx
    falls back to the permissive 'docx' default.
    """
    ct_xml = (
        '<?xml version="1.0"?><Types>'
        '<Override ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        "</Types>"
    )
    big = tmp_path / "big.pptx"
    with zipfile.ZipFile(big, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", ct_xml)
        zf.writestr("ppt/media/blob.bin", b"\x00" * (200 * 1024))

    head = big.read_bytes()[:512]

    assert detect_format(head, source_path=big) == "pptx"
    assert detect_format(head) == "docx"


def test_parse_probe_json_valid() -> None:
    data = {
        "page_count": 42,
        "format": "docx",
        "natural_seams": [[1, 10], [11, 42]],
        "size_bytes": 12345,
    }
    result = parse_probe_json(json.dumps(data).encode("utf-8"))
    assert isinstance(result, ProbeResult)
    assert result.page_count == 42
    assert result.format == "docx"
    assert result.natural_seams == ((1, 10), (11, 42))
    assert result.size_bytes == 12345


def test_parse_probe_json_invalid_json_raises() -> None:
    with pytest.raises(InputUnprocessableError):
        parse_probe_json(b"not json at all")


def test_parse_probe_json_missing_field_raises() -> None:
    with pytest.raises(InputUnprocessableError):
        parse_probe_json(b'{"page_count": 1}')


def test_parse_probe_json_empty_seams() -> None:
    data = {"page_count": 5, "format": "pdf", "natural_seams": [], "size_bytes": 100}
    result = parse_probe_json(json.dumps(data).encode("utf-8"))
    assert result.natural_seams == ()


def _utf16le(s: str) -> bytes:
    """ASCII string → UTF-16LE bytes (the encoding CFB uses for stream names)."""
    return s.encode("utf-16-le")


def _build_ole2(stream_name: str | None, tmp_path: Path, name: str = "input.bin") -> Path:
    """Build a minimal OLE2-looking blob: magic header + pad + optional
    UTF-16LE stream name embedded in the first 64 KB. The real CFB layout
    is irrelevant — detect_format only scans for the signature substring."""
    body = bytearray(OLE2_MAGIC)
    body.extend(b"\x00" * 64)  # placeholder for CFB header fields
    if stream_name is not None:
        body.extend(_utf16le(stream_name))
    body.extend(b"\x00" * 1024)
    path = tmp_path / name
    path.write_bytes(bytes(body))
    return path


def test_detect_ole2_doc_by_stream(tmp_path) -> None:
    """OLE2 file containing the "WordDocument" stream → docx worker."""
    p = _build_ole2("WordDocument", tmp_path, name="legacy.doc")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p) == "docx"


def test_detect_ole2_xls_by_stream(tmp_path) -> None:
    """OLE2 with the modern "Workbook" stream → xlsx worker."""
    p = _build_ole2("Workbook", tmp_path, name="legacy.xls")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p) == "xlsx"


def test_detect_ole2_xls_excel95_book_stream(tmp_path) -> None:
    """OLE2 with the legacy Excel 5.0/95 "Book" stream → xlsx worker."""
    p = _build_ole2("Book", tmp_path, name="ancient.xls")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p) == "xlsx"


def test_detect_ole2_ppt_by_stream(tmp_path) -> None:
    """OLE2 containing "PowerPoint Document" → pptx worker."""
    p = _build_ole2("PowerPoint Document", tmp_path, name="deck.ppt")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p) == "pptx"


def test_detect_ole2_no_signature_uses_filename(tmp_path) -> None:
    """No stream signature in head → fall back to uploaded filename extension."""
    p = _build_ole2(stream_name=None, tmp_path=tmp_path, name="anonymous")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p, filename="MyReport.xls") == "xlsx"
    assert detect_format(head, source_path=p, filename="MyReport.DOC") == "docx"
    assert detect_format(head, source_path=p, filename="deck.PPS") == "pptx"


def test_detect_ole2_no_signature_no_filename_rejects(tmp_path) -> None:
    """OLE2 magic with no signature and no filename → unsupported."""
    p = _build_ole2(stream_name=None, tmp_path=tmp_path, name="anonymous")
    head = p.read_bytes()[:512]
    with pytest.raises(UnsupportedFormatError) as exc:
        detect_format(head, source_path=p, filename=None)
    assert exc.value.detected_magic == OLE2_MAGIC.hex()


def test_detect_ole2_stream_takes_precedence_over_misleading_filename(tmp_path) -> None:
    """A correctly-stamped Word doc renamed to .xls is still routed to docx."""
    p = _build_ole2("WordDocument", tmp_path, name="suspicious.xls")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p, filename="suspicious.xls") == "docx"


def _build_odf(mimetype: str, tmp_path: Path, name: str) -> Path:
    """Build a minimal ODF zip with the given mimetype as the first
    uncompressed entry (per ODF 1.2 §3.3)."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        mi = zipfile.ZipInfo("mimetype")
        mi.compress_type = zipfile.ZIP_STORED
        zf.writestr(mi, mimetype.encode("utf-8"))
        zf.writestr("META-INF/manifest.xml", "<manifest/>")
    return path


def test_detect_odt_routes_to_docx(tmp_path: Path) -> None:
    p = _build_odf("application/vnd.oasis.opendocument.text", tmp_path, "sample.odt")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p, filename="sample.odt") == "docx"


def test_detect_ods_routes_to_xlsx(tmp_path: Path) -> None:
    p = _build_odf("application/vnd.oasis.opendocument.spreadsheet", tmp_path, "sample.ods")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p, filename="sample.ods") == "xlsx"


def test_detect_odp_routes_to_pptx(tmp_path: Path) -> None:
    p = _build_odf("application/vnd.oasis.opendocument.presentation", tmp_path, "sample.odp")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p, filename="sample.odp") == "pptx"


def test_detect_odg_routes_to_libreoffice_fallback(tmp_path: Path) -> None:
    """ODG is no longer rejected at the gate — it routes to a 'odg'
    dispatch format that the server hands to LibreOffice (Aspose.Total
    C++ has no library that renders drawing pages, but soffice does)."""
    p = _build_odf("application/vnd.oasis.opendocument.graphics", tmp_path, "sample.odg")
    head = p.read_bytes()[:512]
    assert detect_format(head, source_path=p, filename="sample.odg") == "odg"


@pytest.mark.parametrize(
    ("mimetype", "ext", "expected_subtype"),
    [
        ("application/vnd.oasis.opendocument.formula", "odf", "OpenDocument Formula"),
        ("application/vnd.oasis.opendocument.base", "odb", "OpenDocument Base"),
    ],
)
def test_detect_rejects_unrenderable_odf_subtype(
    tmp_path: Path, mimetype: str, ext: str, expected_subtype: str
) -> None:
    """ODF (formula)/ODB get rejected with a precise message at the gate.

    Neither has a rendering semantic — .odf is a standalone MathML
    formula and .odb is a Base database container. Letting them
    default-route to the docx worker surfaces as a confusing
    `FileCorruptedException` at render time.
    """
    p = _build_odf(mimetype, tmp_path, f"sample.{ext}")
    head = p.read_bytes()[:512]
    with pytest.raises(UnsupportedFormatError) as exc:
        detect_format(head, source_path=p, filename=f"sample.{ext}")
    assert exc.value.detected_magic == mimetype
    assert exc.value.reason is not None
    assert expected_subtype in exc.value.reason
    # And the error's serialized detail dict carries the reason for the client.
    assert exc.value.as_detail_dict()["reason"] == exc.value.reason
