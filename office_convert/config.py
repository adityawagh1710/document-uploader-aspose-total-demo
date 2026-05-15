"""Runtime configuration via OFFICE_CONVERT_* env vars.

Implements NFR-8 (single source of truth for config). Validation rules per
business-rules.md §12. Failures at server startup, before serving any request.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All OFFICE_CONVERT_* configuration. Loaded once at startup."""

    model_config = SettingsConfigDict(
        env_prefix="OFFICE_CONVERT_",
        env_file=None,
        case_sensitive=False,
    )

    max_jobs: int = Field(default=1, ge=1, le=10)
    parallel: int = Field(default=4, ge=1, le=16)

    cache_dir: Path | None = Field(default=None)
    license_path: Path = Field(default=Path("/aspose/license.lic"))
    scratch_dir: Path = Field(default=Path("/tmp/office-convert"))
    aspose_lib_dir: Path = Field(default=Path("/usr/local/lib/aspose"))
    # Prefix path; the concrete binary for a request is
    # `f"{worker_binary_prefix}-{format}"` (one of -docx/-pptx/-xlsx/-pdf).
    # Split into per-product binaries to keep each Aspose product's
    # CodePorting framework alone in its process address space.
    worker_binary_prefix: Path = Field(default=Path("/usr/local/bin/office-convert-worker"))

    log_format: Literal["json", "human"] = Field(default="json")
    log_level: Literal["debug", "info", "warn", "error"] = Field(default="info")

    chunk_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    max_input_bytes: int = Field(default=1024 * 1024 * 1024, ge=1024 * 1024, le=1024 * 1024 * 1024)

    # Per-chunk worker virtual-address-space ceiling enforced via prlimit
    # RLIMIT_AS. Default 6 GiB suits documents up to ~250 MB input size with
    # the swap-backed compose deployment; raise to 10/16 GiB for 500 MB-class
    # inputs (Aspose Words/Slides loads the *full* document for any render,
    # so RAM scales with total input size, not chunk size).
    #
    # IMPORTANT: when running under compose with `memswap_limit` set, this
    # value MUST be ≥ `memswap_limit` (= RAM + swap), otherwise the worker
    # hits RLIMIT_AS before the kernel ever pages out, defeating the swap
    # cushion. RLIMIT_AS counts swapped-out pages against the cap.
    # compose.yaml sets OFFICE_CONVERT_WORKER_RAM_BYTES to 6 GiB to match
    # `mem_limit: 4g` + `memswap_limit: 6g` (= 4 GiB RAM + 2 GiB swap).
    #
    # The 2 GiB historic floor is preserved as the lower bound for
    # nfr-design-patterns §1 (subprocess memory ceiling) so the
    # max_jobs * parallel * worker_ram_bytes peak-RAM budgeting still works.
    worker_ram_bytes: int = Field(
        default=6 * 1024 * 1024 * 1024,
        ge=2 * 1024 * 1024 * 1024,
        le=64 * 1024 * 1024 * 1024,
    )

    aspose_version: str = Field(default="unknown")

    # Chunk-planner constants (overridable for testing; adaptive sizing in production)
    # max_pages_per_chunk now acts as a CEILING for the adaptive algorithm.
    # The adaptive planner computes the optimal chunk size per-request based on
    # file size, page count, format, and RAM budget. Set this lower to force
    # smaller chunks (e.g., for testing OOM subdivision). The old default of 10
    # was overly conservative and caused excessive subprocess spawns.
    max_pages_per_chunk: int = Field(default=200, ge=1, le=1000)
    max_mb_per_chunk: int = Field(default=50, ge=1, le=1000)
    # XLSX rendered "pages" are much smaller than DOCX pages, and each chunk
    # subprocess pays a fixed Workbook.Load + full-workbook pagination cost
    # before it can render its slice. Coarse XLSX chunks amortize that cost;
    # without this floor a 30k-page workbook would explode into thousands of
    # subprocess spawns. The orchestrator applies this as a per-format floor
    # over max_pages_per_chunk; callers can still raise it above this.
    xlsx_min_pages_per_chunk: int = Field(default=500, ge=1, le=20000)
    # PPTX: Slides loads the full presentation for every chunk render, so
    # each subprocess pays a fixed Presentation.Load cost. Coarser chunks
    # amortize that overhead. Less extreme than XLSX because Slides' load
    # is faster, but still worth a floor above the default.
    pptx_min_pages_per_chunk: int = Field(default=25, ge=1, le=500)

    # Pool mode is enabled when chunk count meets this threshold. Default 2
    # mirrors the historical orchestrator hard-coded `> 1` gate (pool's
    # load-once amortization pays off across multiple chunks). Set to 1 to
    # force pool mode for every conversion — useful for exercising the
    # heartbeat dashboard on small single-chunk files.
    pool_min_chunks: int = Field(default=2, ge=1, le=64)

    # When fork-after-load is disabled (e.g., XLSX is in _FORK_UNSAFE_FORMATS),
    # the legacy N-independent-workers pool is used and each worker
    # independently loads the document. For XLSX on a 98 MB / 23k-page
    # workbook, 4 parallel Cells loads * ~1 GB amplification = ~4 GB peak,
    # right at mem_limit. req_e11ad522 (2026-05-15) hit OOM on one worker
    # during the parallel load phase. Cap at the historical sweet spot of 2
    # — matches the "~36 min at parallel=2" data point in the state file.
    # Set higher only if you've also raised mem_limit to match.
    xlsx_max_pool_size: int = Field(default=2, ge=1, le=16)

    # Fork-after-load: one leader process loads the document and forks N-1
    # children that share the loaded Document via copy-on-write. Eliminates
    # the N-times duplicate parse cost that times out large-DOCX loads in the
    # legacy N-independent-workers pool model. Default ON since 2026-05-15
    # after the 100 MB DOCX stress file went from 600s timeout to 23-29s
    # success and Aspose's CodePorting framework threads survived fork()
    # without deadlock. Set OFFICE_CONVERT_FORK_AFTER_LOAD=0 to fall back
    # to the legacy pool if a future workload class misbehaves under fork.
    fork_after_load: bool = Field(default=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings accessor. Validated once on first call."""
    return Settings()
