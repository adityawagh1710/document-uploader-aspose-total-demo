"""Unit tests for office_convert.libreoffice_convert.

The real soffice binary isn't in the test image (~500MB). We stub it with
a Python script on PATH that mimics the relevant behaviors: success
(write a tiny PDF to outdir), exit-nonzero (with stderr), and hang.
"""

from __future__ import annotations

import os
import stat
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from office_convert import libreoffice_convert
from office_convert.errors import RenderError
from office_convert.libreoffice_convert import (
    LibreOfficeNotInstalledError,
    convert_to_pdf,
)


def _write_fake_soffice(dir_: Path, body: str) -> None:
    target = dir_ / "soffice"
    target.write_text(f"#!/usr/bin/env python3\n{textwrap.dedent(body)}")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def empty_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """PATH containing only an empty dir — `soffice` resolves to nothing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    yield bin_dir


@pytest.fixture
def fake_soffice_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Keep system python on PATH (so the shebang works) but route soffice here.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    yield bin_dir


async def test_convert_raises_when_soffice_missing(empty_path: Path, tmp_path: Path) -> None:
    src = tmp_path / "x.odg"
    src.write_bytes(b"PK\x03\x04dummy")
    with pytest.raises(LibreOfficeNotInstalledError):
        await convert_to_pdf(src, tmp_path / "out")


async def test_convert_returns_path_on_success(fake_soffice_dir: Path, tmp_path: Path) -> None:
    # Fake soffice: read --outdir + last positional arg, write a tiny PDF
    _write_fake_soffice(
        fake_soffice_dir,
        """
        import sys, os
        argv = sys.argv[1:]
        outdir = argv[argv.index('--outdir') + 1]
        infile = argv[-1]
        stem = os.path.splitext(os.path.basename(infile))[0]
        with open(os.path.join(outdir, stem + '.pdf'), 'wb') as f:
            f.write(b'%PDF-1.4\\nfake\\n%%EOF\\n')
        """,
    )
    src = tmp_path / "drawing.odg"
    src.write_bytes(b"PK\x03\x04dummy")
    out = await convert_to_pdf(src, tmp_path / "out")
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-")
    assert out.name == "drawing.pdf"


async def test_convert_raises_render_error_on_nonzero_exit(
    fake_soffice_dir: Path, tmp_path: Path
) -> None:
    _write_fake_soffice(
        fake_soffice_dir,
        """
        import sys
        sys.stderr.write('Error: source file could not be loaded\\n')
        sys.exit(81)
        """,
    )
    src = tmp_path / "bad.odg"
    src.write_bytes(b"PK\x03\x04junk")
    with pytest.raises(RenderError) as exc:
        await convert_to_pdf(src, tmp_path / "out")
    assert exc.value.exit_code == 81
    assert "source file could not be loaded" in exc.value.stderr_tail


async def test_convert_raises_when_outdir_empty_despite_exit_zero(
    fake_soffice_dir: Path, tmp_path: Path
) -> None:
    # soffice sometimes exits 0 but produces no file (e.g., filter mismatch).
    _write_fake_soffice(fake_soffice_dir, "import sys\nsys.exit(0)\n")
    src = tmp_path / "broken.odg"
    src.write_bytes(b"PK\x03\x04junk")
    with pytest.raises(RenderError) as exc:
        await convert_to_pdf(src, tmp_path / "out")
    assert "no .pdf appeared" in exc.value.stderr_tail


async def test_convert_times_out(fake_soffice_dir: Path, tmp_path: Path) -> None:
    _write_fake_soffice(
        fake_soffice_dir,
        "import time\nwhile True: time.sleep(60)\n",
    )
    src = tmp_path / "slow.odg"
    src.write_bytes(b"PK\x03\x04dummy")
    with pytest.raises(RenderError) as exc:
        await convert_to_pdf(src, tmp_path / "out", timeout_seconds=1)
    assert "timed out" in exc.value.stderr_tail


def test_module_constant_exposes_binary_name() -> None:
    """Sanity-check the import surface; protects against accidental renames."""
    assert libreoffice_convert.SOFFICE_BIN == "soffice"
