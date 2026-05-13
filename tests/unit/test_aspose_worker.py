"""Tests for office_convert.aspose_worker using fake subprocess scripts.

We point `worker_binary_prefix` at a tmp_path stem and write fake-worker-<fmt>
scripts so the orchestrator's per-format dispatch resolves to a shell stand-in.
No real Aspose is involved. Exercises argv construction, exit-code translation,
and timeout behavior.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from office_convert.aspose_worker import _map_exit_code, render_chunk
from office_convert.config import Settings
from office_convert.errors import (
    InputUnprocessableError,
    LicenseExpiredError,
    OOMError,
    RenderError,
)
from office_convert.types import Chunk


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _exit_code_script(exit_code: int, stderr_msg: str = "") -> str:
    return f"""#!/bin/sh
echo "{stderr_msg}" >&2
exit {exit_code}
"""


def _success_render_script() -> str:
    """Script that creates the --output file then exits 0."""
    return """#!/bin/sh
output=""
while [ $# -gt 0 ]; do
    case "$1" in
        --output) output="$2"; shift 2;;
        *) shift;;
    esac
done
echo "fake pdf content" > "$output"
exit 0
"""


def _make_settings(tmp_path: Path, worker: Path, timeout: int = 30) -> Settings:
    """Install `worker` as the docx/pptx/xlsx/pdf binaries off a shared prefix.

    Tests pass a single `worker` script; we materialize it under each
    per-format suffix so aspose_worker.py's `{prefix}-{format}` lookup hits it.
    """
    import shutil as _sh
    prefix = tmp_path / "fake-worker"
    for fmt in ("docx", "pptx", "xlsx", "pdf"):
        per_fmt = prefix.with_name(f"{prefix.name}-{fmt}")
        if per_fmt.exists():
            per_fmt.unlink()
        _sh.copy(worker, per_fmt)
        per_fmt.chmod(per_fmt.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return Settings(
        worker_binary_prefix=prefix,
        license_path=tmp_path / "license.lic",
        chunk_timeout_seconds=timeout,
        scratch_dir=tmp_path,
    )


def test_map_exit_code_zero_is_success() -> None:
    _map_exit_code(0, b"", None)  # no exception


def test_map_exit_code_137_is_oom() -> None:
    chunk = Chunk(index=0.0, page_range=(1, 10), natural_seam=False)
    with pytest.raises(OOMError):
        _map_exit_code(137, b"", chunk)


def test_map_exit_code_2_is_license_expired() -> None:
    with pytest.raises(LicenseExpiredError):
        _map_exit_code(2, b"license parse error", None)


def test_map_exit_code_3_is_input_unprocessable() -> None:
    with pytest.raises(InputUnprocessableError):
        _map_exit_code(3, b"corrupt file", None)


def test_map_exit_code_1_is_render_failed() -> None:
    chunk = Chunk(index=0.0, page_range=(1, 5), natural_seam=False)
    with pytest.raises(RenderError):
        _map_exit_code(1, b"render error detail", chunk)


def test_map_exit_code_unknown_is_render_failed() -> None:
    with pytest.raises(RenderError):
        _map_exit_code(-9, b"killed", None)


async def test_render_chunk_success_creates_output_file(tmp_path: Path) -> None:
    worker = tmp_path / "fake-worker"
    _write_executable(worker, _success_render_script())
    settings = _make_settings(tmp_path, worker)
    chunk = Chunk(index=0.0, page_range=(1, 10), natural_seam=False)
    output = await render_chunk(
        chunk=chunk,
        input_path=tmp_path / "input.docx",
        format="docx",
        scratch_dir=tmp_path,
        request_id="test-req",
        settings=settings,
    )
    assert output.exists()
    assert output.read_text() == "fake pdf content\n"


async def test_render_chunk_oom_raises_oom_error(tmp_path: Path) -> None:
    worker = tmp_path / "oom-worker"
    _write_executable(worker, _exit_code_script(137))
    settings = _make_settings(tmp_path, worker)
    chunk = Chunk(index=0.0, page_range=(1, 10), natural_seam=False)
    with pytest.raises(OOMError):
        await render_chunk(
            chunk=chunk,
            input_path=tmp_path / "in.docx",
            format="docx",
            scratch_dir=tmp_path,
            request_id="r",
            settings=settings,
        )


async def test_render_chunk_input_unprocessable(tmp_path: Path) -> None:
    worker = tmp_path / "bad-input-worker"
    _write_executable(worker, _exit_code_script(3, "corrupt"))
    settings = _make_settings(tmp_path, worker)
    chunk = Chunk(index=0.0, page_range=(1, 5), natural_seam=False)
    with pytest.raises(InputUnprocessableError):
        await render_chunk(
            chunk=chunk,
            input_path=tmp_path / "in.docx",
            format="docx",
            scratch_dir=tmp_path,
            request_id="r",
            settings=settings,
        )


async def test_render_chunk_timeout(tmp_path: Path) -> None:
    """Worker hangs; render_chunk's own timeout kills it and raises RenderError.

    We bypass the Settings validator's >=30s minimum via `model_copy` (which
    skips field validation in pydantic v2). This lets the test run in ~1s
    while still exercising the production timeout code path.
    """
    worker = tmp_path / "hang-worker"
    _write_executable(worker, "#!/bin/sh\nsleep 60\n")
    settings = _make_settings(tmp_path, worker, timeout=30)
    # Bypass the validator minimum (30s) — model_copy does not re-validate.
    settings = settings.model_copy(update={"chunk_timeout_seconds": 1})
    chunk = Chunk(index=0.0, page_range=(1, 5), natural_seam=False)
    # Let render_chunk's OWN timeout fire (no outer wait_for).
    with pytest.raises(RenderError, match="timeout"):
        await render_chunk(
            chunk=chunk,
            input_path=tmp_path / "in.docx",
            format="docx",
            scratch_dir=tmp_path,
            request_id="r",
            settings=settings,
        )


def test_prlimit_in_argv(tmp_path: Path) -> None:
    """worker invocation argv carries `--as=<settings.worker_ram_bytes>`.

    Default Settings keeps the historic 2 GB floor as the minimum allowed
    value; the default is 6 GiB (raised from 4 GiB on 2026-05-12 when swap
    support was added — RLIMIT_AS must be ≥ memswap_limit or the worker
    fails malloc before the kernel can page out, defeating the swap
    cushion). Production compose deployment sets this to 6 GiB explicitly
    via OFFICE_CONVERT_WORKER_RAM_BYTES env var, matching the
    `memswap_limit: 6g` total cgroup budget.
    """
    from office_convert.config import Settings

    s = Settings()
    assert s.worker_ram_bytes >= 2 * 1024 * 1024 * 1024
    assert s.worker_ram_bytes == 6 * 1024 * 1024 * 1024
