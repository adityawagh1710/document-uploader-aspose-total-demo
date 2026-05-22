"""PBT for format detection: random bytes are rejected, valid magic accepted."""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from office_convert.errors import UnsupportedFormatError
from office_convert.probe import OLE2_MAGIC, detect_format

# All real magic-byte prefixes that detect_format recognizes. Random bytes
# starting with any of these would (correctly) NOT raise UnsupportedFormatError;
# the test skips them so we only assert rejection on bytes that genuinely
# look like nothing.
_RECOGNIZED_MAGIC_PREFIXES = (
    b"%PDF-",  # PDF
    b"PK\x03\x04",  # ZIP (OOXML, ODF)
    OLE2_MAGIC,  # OLE2/CFB (legacy DOC/XLS/PPT)
    b"{\\rtf",  # RTF
    b"\x89PNG\r\n\x1a\n",  # PNG (8 bytes — unlikely to collide randomly)
    b"\xff\xd8\xff",  # JPEG SOI + first marker (only 3 bytes — ~1 in 16M random hits)
    b"GIF87a",
    b"GIF89a",
    b"BM",  # BMP (2 bytes — ~1 in 65K random hits)
    b"II*\x00",  # TIFF little-endian
    b"MM\x00*",  # TIFF big-endian
    b"RIFF",  # WEBP needs RIFF + WEBP tag; covered by the RIFF check below
    b"<?xml",  # SVG with XML prolog
    b"<svg",  # SVG without prolog
)


def _starts_with_any_magic(prefix: bytes) -> bool:
    if prefix.startswith(_RECOGNIZED_MAGIC_PREFIXES):
        return True
    # SVG with a UTF-8 BOM is also recognized after lstrip
    stripped = prefix.lstrip(b"\xef\xbb\xbf").lstrip()
    return stripped.startswith((b"<?xml", b"<svg"))


@settings(max_examples=100, deadline=timedelta(seconds=2))
@given(prefix=st.binary(min_size=8, max_size=512))
def test_random_bytes_not_matching_magic_are_rejected(prefix: bytes) -> None:
    if _starts_with_any_magic(prefix):
        return  # would match a real magic; skip
    with pytest.raises(UnsupportedFormatError):
        detect_format(prefix)


@settings(max_examples=50, deadline=timedelta(seconds=2))
@given(suffix=st.binary(min_size=0, max_size=512))
def test_pdf_magic_is_accepted_regardless_of_suffix(suffix: bytes) -> None:
    assert detect_format(b"%PDF-" + suffix) == "pdf"
