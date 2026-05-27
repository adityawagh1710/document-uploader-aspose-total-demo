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
    # max_pages_per_chunk is a HARD operator override on top of the adaptive
    # algorithm. The PRIMARY ceiling is per-format and lives in
    # chunk_planner.MAX_PAGES_CEILING (DOCX/PPTX=5000, XLSX/PDF=2000), tuned
    # to each format's cost model. This field exists only to force a lower
    # cap for testing (e.g., OOM-subdivision tests use OFFICE_CONVERT_MAX_PAGES_PER_CHUNK=2).
    # Default 10000 is intentionally well above every per-format ceiling so
    # it never engages in production. The historical default of 200 caused
    # ~38x over-chunking on thin-page files (e.g., 7,500-slide PPTX at ~1 KB/slide).
    max_pages_per_chunk: int = Field(default=10000, ge=1, le=50000)
    max_mb_per_chunk: int = Field(default=50, ge=1, le=1000)
    # XLSX rendered "pages" are much smaller than DOCX pages, and each chunk
    # subprocess pays a fixed Workbook.Load + full-workbook pagination cost
    # before it can render its slice. Coarse XLSX chunks amortize that cost;
    # without this floor a 30k-page workbook would explode into thousands of
    # subprocess spawns. The orchestrator applies this as a per-format floor
    # over max_pages_per_chunk; callers can still raise it above this.
    #
    # Lowered from 500 → 200 on 2026-05-15 alongside raising xlsx_max_pool_size
    # to 4: the original 500-page floor was tuned when xlsx_max_pool_size=2, so
    # 4 chunks meant 2 chunks per worker (= 2 loads/worker). With pool_size=4,
    # 4 chunks means 1 chunk per worker, so smaller chunks no longer multiply
    # setup overhead. Verified on sample_large.xlsx (800 pages): adaptive
    # planner picks 200 → 4 chunks → 1 chunk per worker → max parallelism.
    xlsx_min_pages_per_chunk: int = Field(default=200, ge=1, le=20000)
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
    # independently loads the document. Cap is per-file-size-class:
    #
    # - For workbooks ≲ 30 MB: 4 parallel Cells loads x ~150 MB amplified
    #   footprint = ~600 MB peak, comfortably inside the 4 GB cgroup cap.
    #   Default raised 2 → 4 on 2026-05-15: sample_large.xlsx (2.65 MB,
    #   800 pages) was render-bound (~2 s/page), so 2x more parallelism
    #   roughly halves wall-time of the render phase.
    # - For workbooks > 100 MB: the original 4-worker pattern OOM'd
    #   (req_e11ad522 2026-05-15 on a 98 MB / 23 k-page file). For that
    #   class, override OFFICE_CONVERT_XLSX_MAX_POOL_SIZE=2 via env, OR
    #   add a size-aware cap in the orchestrator (TODO).
    xlsx_max_pool_size: int = Field(default=4, ge=1, le=16)

    # Fork-after-load: one leader process loads the document and forks N-1
    # children that share the loaded Document via copy-on-write. Eliminates
    # the N-times duplicate parse cost that times out large-DOCX loads in the
    # legacy N-independent-workers pool model. Default ON since 2026-05-15
    # after the 100 MB DOCX stress file went from 600s timeout to 23-29s
    # success and Aspose's CodePorting framework threads survived fork()
    # without deadlock. Set OFFICE_CONVERT_FORK_AFTER_LOAD=0 to fall back
    # to the legacy pool if a future workload class misbehaves under fork.
    fork_after_load: bool = Field(default=True)

    # Per-IP rate limit on /convert (token bucket). Disabled by setting
    # OFFICE_CONVERT_RATE_LIMIT_ENABLED=0. Tokens refill at
    # rate_limit_per_ip_rpm / 60 per second; bucket capacity = rate_limit_burst.
    # In-memory only — multi-replica deployments get N x the effective rate.
    # max_keys bounds memory; LRU eviction after that.
    rate_limit_enabled: bool = Field(default=True)
    rate_limit_per_ip_rpm: int = Field(default=30, ge=1, le=10000)
    rate_limit_burst: int = Field(default=5, ge=1, le=1000)
    rate_limit_max_keys: int = Field(default=10000, ge=10, le=1000000)
    # Use the first IP in X-Forwarded-For as the client identifier (ALB
    # appends original client IP first). Set to 0 when the API is exposed
    # without a proxy — the header is spoofable.
    rate_limit_trust_xff: bool = Field(default=True)

    # ---- S3 source/sink integration (see plans/s3-source-integration-plan.md) ----
    # Master switch. When False, any request carrying s3_input/s3_output and
    # the /v1/downloads/presign endpoint are rejected with HTTP 400
    # (failure_class=s3_disabled). Off by default so existing deployments
    # without IAM/IRSA are unaffected.
    s3_enabled: bool = Field(default=False)
    # boto3 region. None → boto3 resolves from AWS_REGION / instance metadata.
    s3_region: str | None = Field(default=None)
    # Comma-separated bucket allowlists (NOT JSON — pydantic-settings would try
    # to JSON-decode a list-typed env value, and the compose/Helm config sets
    # plain "bucket-a,bucket-b"). Parsed via s3.parse_allowlist(). An empty
    # allowlist FAILS CLOSED: every bucket is rejected. The configured default
    # output bucket is implicitly allowed for output even if not listed.
    s3_input_buckets_allowlist: str | None = Field(default=None)
    s3_output_buckets_allowlist: str | None = Field(default=None)
    # Used when s3_output is requested without an explicit bucket (future:
    # s3_always_store_output). Currently only marks a bucket as implicitly
    # output-allowed.
    s3_default_output_bucket: str | None = Field(default=None)
    # Default key for s3_output when the caller passes a bucket-only URL.
    # `{request_id}` is substituted; the `pdf/` prefix keeps presign scoping
    # simple and avoids leaking input filenames in the key.
    s3_output_key_template: str = Field(default="pdf/{request_id}.pdf")
    # TTL for presigned GET URLs minted by /v1/downloads/presign. 15 min.
    s3_presign_ttl_seconds: int = Field(default=900, ge=1, le=7 * 24 * 3600)
    # Endpoint used ONLY for SIGNING presigned URLs, when it must differ from
    # the server-side endpoint. LocalStack case: the API reaches S3 in-network
    # at localstack:4566, but the browser following a presigned link is on the
    # host, where LocalStack is published at localhost:4567 — so the URL must
    # carry that host. Unset on real AWS (presigned URLs already point at the
    # public S3 endpoint). Mirrors classification-service's S3_PUBLIC_ENDPOINT.
    s3_public_endpoint: str | None = Field(default=None)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings accessor. Validated once on first call."""
    return Settings()
