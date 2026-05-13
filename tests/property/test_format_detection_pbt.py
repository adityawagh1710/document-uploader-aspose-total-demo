"""PBT for format detection: random bytes are rejected, valid magic accepted."""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from office_convert.errors import UnsupportedFormatError
from office_convert.probe import OLE2_MAGIC, detect_format


@settings(max_examples=100, deadline=timedelta(seconds=2))
@given(prefix=st.binary(min_size=8, max_size=512))
def test_random_bytes_not_matching_magic_are_rejected(prefix: bytes) -> None:
    if prefix.startswith((b"%PDF-", b"PK\x03\x04", OLE2_MAGIC)):
        return  # would match a real magic; skip
    with pytest.raises(UnsupportedFormatError):
        detect_format(prefix)


@settings(max_examples=50, deadline=timedelta(seconds=2))
@given(suffix=st.binary(min_size=0, max_size=512))
def test_pdf_magic_is_accepted_regardless_of_suffix(suffix: bytes) -> None:
    assert detect_format(b"%PDF-" + suffix) == "pdf"
