"""LibreOffice fallback path for formats Aspose.Total C++ cannot render.

Currently scoped to ODG (OpenDocument Graphics): Aspose.Words rejects ODG
as "Unknown" and Aspose.Slides rejects it as "Not a Open Office
presentation"; only LibreOffice handles drawing-page geometry correctly.

Shape: per-request invocation of `soffice --headless --convert-to pdf`.
LibreOffice is single-threaded per profile dir, so each call gets its own
`-env:UserInstallation` pointing at a fresh tmpfs subdirectory — that's the
only reliable way to run concurrent conversions without one instance
hijacking another's UNO socket.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from office_convert.errors import ConversionError, RenderError

log = logging.getLogger(__name__)

SOFFICE_BIN = "soffice"
DEFAULT_TIMEOUT_SECONDS = 120


class LibreOfficeNotInstalledError(ConversionError):
    """Raised when the soffice binary isn't on PATH at runtime."""

    failure_class = RenderError.failure_class
    http_status = 500

    def __init__(self) -> None:
        super().__init__("libreoffice (soffice) not installed")


async def convert_to_pdf(
    input_path: Path,
    output_dir: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    """Convert `input_path` to PDF via headless LibreOffice.

    Returns the path of the produced PDF (lives under `output_dir`). Raises
    RenderError on subprocess failure or timeout — caller's FastAPI handler
    surfaces it as a JSON Diagnostic.

    LibreOffice writes `<input-stem>.pdf` into `--outdir`. We can't use
    `--convert-to pdf:writer_pdf_Export -outdir ...` directly to ODG because
    the `writer_` filter rejects drawings; the bare `pdf` filter picks the
    correct one per input mimetype.
    """
    if shutil.which(SOFFICE_BIN) is None:
        raise LibreOfficeNotInstalledError

    output_dir.mkdir(parents=True, exist_ok=True)
    # Per-call profile dir: soffice writes ~250 KB of bootstrap state into
    # its UserInstallation on first run; sharing a profile across concurrent
    # invocations races on the lock file and one of them silently produces
    # no output. file:// URI is required by soffice for the -env arg.
    profile_dir = output_dir / "lo_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        SOFFICE_BIN,
        f"-env:UserInstallation=file://{profile_dir}",
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(input_path),
    ]
    log.debug("libreoffice convert: %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise LibreOfficeNotInstalledError from e

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RenderError(
            chunk=None,
            exit_code=-1,
            stderr_tail=f"libreoffice timed out after {timeout_seconds}s",
        ) from None

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        out = stdout.decode("utf-8", errors="replace")
        tail = (err or out)[-1024:]
        raise RenderError(
            chunk=None,
            exit_code=proc.returncode or 1,
            stderr_tail=f"libreoffice: {tail.strip()}",
        )

    # soffice writes `<stem>.pdf` into --outdir
    expected = output_dir / f"{input_path.stem}.pdf"
    if not expected.exists():
        # Some LibreOffice builds normalize the stem; fall back to scanning.
        candidates = sorted(output_dir.glob("*.pdf"))
        if not candidates:
            raise RenderError(
                chunk=None,
                exit_code=0,
                stderr_tail=(
                    "libreoffice exited 0 but no .pdf appeared in outdir "
                    f"(stderr: {stderr.decode('utf-8', errors='replace')[-512:].strip()})"
                ),
            )
        expected = candidates[0]
    return expected
