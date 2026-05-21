"""Shared domain types: dataclasses and enums used across modules.

Frozen dataclasses for immutability — chunks and plans flow through async
pipelines and must not be mutated mid-flight.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

FormatName = Literal["docx", "pptx", "xlsx", "pdf"]
# Wider type returned by probe.detect_format: includes formats that don't go
# through an Aspose worker. Currently only ODG (handled by LibreOffice). The
# orchestrator + workers still operate on FormatName; the server routes
# `DispatchFormat \ FormatName` to the libreoffice path before the
# orchestrator is even constructed.
DispatchFormat = Literal["docx", "pptx", "xlsx", "pdf", "odg"]


class FailureClass(StrEnum):
    """Canonical failure classes returned to callers in the Diagnostic body."""

    UNSUPPORTED_FORMAT = "unsupported_format"
    MISSING_FILE = "missing_file"
    INPUT_TOO_LARGE = "input_too_large"
    INPUT_UNPROCESSABLE = "input_unprocessable"
    RENDER_FAILED = "render_failed"
    SUBDIVISION_FLOOR_EXCEEDED = "subdivision_floor_exceeded"
    MERGE_FAILED = "merge_failed"
    LICENSE_EXPIRED = "license_expired"
    BUSY = "busy"
    RATE_LIMITED = "rate_limited"


class LicenseState(StrEnum):
    """Computed from days_remaining per business-rules.md §4."""

    PERMANENT = "permanent"
    HEALTHY = "healthy"
    WARN = "warn"
    CRITICAL = "critical"
    EXPIRING_TODAY = "expiring_today"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Chunk:
    """A planning unit. One Chunk = one Aspose subprocess invocation."""

    index: float  # float allows fractional sub-chunk indices during subdivision
    page_range: tuple[int, int]  # inclusive, 1-based
    natural_seam: bool

    @property
    def pages(self) -> int:
        return self.page_range[1] - self.page_range[0] + 1


@dataclass(frozen=True)
class ChunkPlan:
    """Ordered, complete, non-overlapping cover of [1..total_pages]."""

    chunks: tuple[Chunk, ...]
    total_pages: int
    estimated_mb: float


@dataclass(frozen=True)
class ProbeResult:
    """Output of the probe step. Used by chunk_planner.plan_chunks."""

    page_count: int
    format: FormatName
    natural_seams: tuple[tuple[int, int], ...]
    size_bytes: int


@dataclass(frozen=True)
class ConversionOptions:
    """Per-request caller-supplied options from the multipart options JSON."""

    cache: bool = True
    log_level: str | None = None


@dataclass(frozen=True)
class ConversionResult:
    """Metadata about a successful conversion. Surfaced via X-* response headers."""

    chunks_rendered: int
    subdivision_retries: int
    cache_hits: int
    duration_seconds: float


@dataclass(frozen=True)
class Diagnostic:
    """Structured failure metadata. Returned as the HTTP error response body."""

    request_id: str
    failure_class: FailureClass
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "failure_class": str(self.failure_class),
            "detail": self.detail,
        }
