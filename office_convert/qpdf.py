"""qpdf streaming concat wrapper.

Implements FR-3 (merge step) and NFR-1 (no full output buffering). Spawns the
`qpdf` binary with `--empty --pages <list> -- -`, yields stdout in 64 KB blocks
through an async generator. Optional tee-to-disk for cache write.

The merged PDF is NEVER materialized in memory or on disk on the orchestrator
side beyond the optional cache tee.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

from office_convert.errors import MergeError

log = logging.getLogger(__name__)

READ_BLOCK_SIZE = 65536  # 64 KB; matches typical Linux pipe-buffer behavior.


async def concat_streaming(
    chunk_paths: list[Path],
    cache_temp_path: Path | None = None,
    qpdf_binary: str = "qpdf",
) -> AsyncIterator[bytes]:
    """Stream the concatenation of chunk PDFs as bytes.

    Args:
        chunk_paths: ordered chunk PDFs to concatenate.
        cache_temp_path: optional path to tee streamed bytes into; the caller
            renames this into the cache atomically on success.
        qpdf_binary: name or path of the qpdf binary.

    Yields:
        Successive 64 KB byte blocks of the merged PDF.

    Raises:
        MergeError: if qpdf exits non-zero.
    """
    if not chunk_paths:
        raise MergeError(exit_code=-1, stderr_tail="no chunks to concatenate")

    argv = [qpdf_binary, "--empty", "--pages", *[str(p) for p in chunk_paths], "--", "-"]
    log.debug("invoking qpdf: %s", argv)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    tee_handle = cache_temp_path.open("wb") if cache_temp_path else None
    try:
        while True:
            block = await proc.stdout.read(READ_BLOCK_SIZE)
            if not block:
                break
            if tee_handle is not None:
                tee_handle.write(block)
            yield block
        if tee_handle is not None:
            tee_handle.flush()
            os.fsync(tee_handle.fileno())
    finally:
        if tee_handle is not None:
            tee_handle.close()

    stderr_bytes = await proc.stderr.read()
    rc = await proc.wait()
    if rc != 0:
        # Clean up partial tee file
        if cache_temp_path is not None:
            with suppress(OSError):
                cache_temp_path.unlink(missing_ok=True)
        tail = stderr_bytes[-1024:].decode("utf-8", errors="replace")
        raise MergeError(exit_code=rc, stderr_tail=tail)
