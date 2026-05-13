"""Structured logging with JSON-lines and human formats.

Implements FR-10. `current_request_id` ContextVar propagates through async tasks
spawned via asyncio.gather/create_task. RequestIdFilter injects the value into
every log record.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

current_request_id: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Inject the current_request_id ContextVar into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Schema per business-rules.md §8."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "request_id": getattr(record, "request_id", "-"),
            "event": getattr(record, "event", record.getMessage()),
        }
        # Pull extra fields from record.__dict__ if attached via logger.info(..., extra={...})
        for key, value in record.__dict__.items():
            if key in payload or key in _RESERVED_LOGRECORD_KEYS:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


class HumanFormatter(logging.Formatter):
    """Single-line human-readable. Useful for interactive dev runs and for
    tailing `make logs` while a conversion is in flight.

    Format:
        2026-05-13 06:00:00 INFO  [req_abc12345] event_name              k1=v1 k2=v2
    The event name is left-padded to 24 chars so columns line up across
    a request's sequence of events. Extra fields attached via
    `logger.log(..., extra={...})` are appended as space-separated k=v
    pairs sorted by key for determinism.
    """

    _EVENT_COLUMN_WIDTH = 24

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        rid = getattr(record, "request_id", "-")
        event = getattr(record, "event", record.getMessage())
        fields = []
        for key in sorted(record.__dict__):
            if key in _RESERVED_LOGRECORD_KEYS or key in {"request_id", "event"}:
                continue
            value = record.__dict__[key]
            fields.append(f"{key}={_render_value(value)}")
        suffix = (" " + " ".join(fields)) if fields else ""
        return (
            f"{timestamp} {record.levelname:5s} [req_{rid[:8]}] "
            f"{event:<{self._EVENT_COLUMN_WIDTH}s}{suffix}"
        )


def _render_value(value: Any) -> str:
    """Compact, log-friendly rendering of an extra field value.

    Lists/tuples become `[a,b,c]`; strings with whitespace get quoted;
    everything else gets `str()`. Numbers stay un-quoted so `duration_s=3.5`
    is grep-able. NOT round-trippable to JSON — that's the JsonFormatter's
    job.
    """
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_render_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if any(c.isspace() for c in s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


_RESERVED_LOGRECORD_KEYS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "getMessage",
    "taskName",
}

_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure(
    format: Literal["json", "human"] = "json",
    level: Literal["debug", "info", "warn", "error"] = "info",
) -> None:
    """Install root handler. Called once at server startup."""
    root = logging.getLogger()
    # Remove existing handlers to allow re-configuration in tests
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if format == "json" else HumanFormatter())
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(_LEVEL_MAP[level])


@contextmanager
def request_context(request_id: str) -> Iterator[None]:
    """Bind request_id to the ContextVar for the duration of the request."""
    token = current_request_id.set(request_id)
    try:
        yield
    finally:
        current_request_id.reset(token)


def emit_event(event: str, level: str = "info", **fields: Any) -> None:
    """Emit a structured log event using the canonical event vocabulary."""
    logger = logging.getLogger("office_convert")
    log_level = _LEVEL_MAP.get(level, logging.INFO)
    extra = {"event": event, **fields}
    logger.log(log_level, event, extra=extra)
