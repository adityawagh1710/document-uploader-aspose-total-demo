"""Internal exception hierarchy.

Implements FR-5. Each subclass carries a `failure_class` and `http_status`
attribute so the FastAPI exception handler can map to the canonical response.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from office_convert.types import Chunk, FailureClass


class ConversionError(Exception):
    """Base class for all caller-facing conversion failures."""

    failure_class: FailureClass = FailureClass.RENDER_FAILED
    http_status: int = 500

    def as_detail_dict(self) -> dict[str, Any]:
        return {"message": str(self) or self.failure_class.value}


class UnsupportedFormatError(ConversionError):
    failure_class = FailureClass.UNSUPPORTED_FORMAT
    http_status = 400

    def __init__(
        self,
        detected_magic: str,
        accepted: list[str],
        reason: str | None = None,
    ) -> None:
        msg = f"unsupported format (magic={detected_magic})"
        if reason:
            msg = f"{msg}: {reason}"
        super().__init__(msg)
        self.detected_magic = detected_magic
        self.accepted = accepted
        self.reason = reason

    def as_detail_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "detected_magic": self.detected_magic,
            "accepted": self.accepted,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


class MissingFileError(ConversionError):
    failure_class = FailureClass.MISSING_FILE
    http_status = 400


class InputTooLargeError(ConversionError):
    failure_class = FailureClass.INPUT_TOO_LARGE
    http_status = 400

    def __init__(self, size_bytes: int, ceiling_bytes: int) -> None:
        super().__init__(f"input too large: {size_bytes} > {ceiling_bytes}")
        self.size_bytes = size_bytes
        self.ceiling_bytes = ceiling_bytes

    def as_detail_dict(self) -> dict[str, Any]:
        return {"size_bytes": self.size_bytes, "ceiling_bytes": self.ceiling_bytes}


class InputUnprocessableError(ConversionError):
    failure_class = FailureClass.INPUT_UNPROCESSABLE
    http_status = 422

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def as_detail_dict(self) -> dict[str, Any]:
        return {"reason": self.reason}


class RenderError(ConversionError):
    failure_class = FailureClass.RENDER_FAILED
    http_status = 500

    def __init__(self, chunk: Chunk | None, exit_code: int, stderr_tail: str) -> None:
        super().__init__(f"render failed (exit={exit_code}): {stderr_tail[:200]}")
        self.chunk = chunk
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail

    def as_detail_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stderr_tail": self.stderr_tail[-1024:],
            "chunk_index": self.chunk.index if self.chunk else None,
            "page_range": list(self.chunk.page_range) if self.chunk else None,
        }


class OOMError(RenderError):
    """Internal — caught by orchestrator and translated to subdivision.

    Never surfaces as an HTTP response.
    """

    def __init__(self, chunk: Chunk) -> None:
        super().__init__(chunk, exit_code=137, stderr_tail="OOM (exit 137)")


class SubdivisionFloorError(ConversionError):
    failure_class = FailureClass.SUBDIVISION_FLOOR_EXCEEDED
    http_status = 500

    def __init__(self, chunk: Chunk, attempts: int) -> None:
        super().__init__(f"subdivision floor reached on chunk {chunk.index}")
        self.chunk = chunk
        self.attempts = attempts

    def as_detail_dict(self) -> dict[str, Any]:
        return {
            "failing_page_range": list(self.chunk.page_range),
            "attempts": self.attempts,
        }


class MergeError(ConversionError):
    failure_class = FailureClass.MERGE_FAILED
    http_status = 500

    def __init__(self, exit_code: int, stderr_tail: str) -> None:
        super().__init__(f"qpdf merge failed (exit={exit_code})")
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail

    def as_detail_dict(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "stderr_tail": self.stderr_tail[-1024:]}


class LicenseExpiredError(ConversionError):
    failure_class = FailureClass.LICENSE_EXPIRED
    http_status = 503

    def __init__(self, expired_on: date | None = None) -> None:
        super().__init__(f"license expired on {expired_on}" if expired_on else "license expired")
        self.expired_on = expired_on

    def as_detail_dict(self) -> dict[str, Any]:
        return {"expired_on": self.expired_on.isoformat() if self.expired_on else None}


class BusyError(ConversionError):
    failure_class = FailureClass.BUSY
    http_status = 503

    def __init__(self, retry_after_seconds: int = 60) -> None:
        super().__init__("server at max_jobs capacity")
        self.retry_after_seconds = retry_after_seconds

    def as_detail_dict(self) -> dict[str, Any]:
        return {"retry_after_seconds": self.retry_after_seconds}


class RateLimitedError(ConversionError):
    failure_class = FailureClass.RATE_LIMITED
    http_status = 429

    def __init__(self, *, retry_after_seconds: int, limit: int) -> None:
        super().__init__(f"rate limit exceeded ({limit} req/min/IP)")
        self.retry_after_seconds = retry_after_seconds
        self.limit = limit

    def as_detail_dict(self) -> dict[str, Any]:
        return {"retry_after_seconds": self.retry_after_seconds, "limit": self.limit}
