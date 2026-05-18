"""Tests for office_convert.logging: JSON output, context propagation, format switching."""

from __future__ import annotations

import asyncio
import json
import logging
from io import StringIO

import pytest

from office_convert.logging import (
    HumanFormatter,
    JsonFormatter,
    RequestIdFilter,
    configure,
    current_request_id,
    emit_event,
    request_context,
)


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Ensure tests don't leak handler state."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_json_formatter_emits_required_fields() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.event = "test_event"
    record.request_id = "abc123"
    out = formatter.format(record)
    payload = json.loads(out)
    assert payload["event"] == "test_event"
    assert payload["request_id"] == "abc123"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_human_formatter_emits_single_line() -> None:
    formatter = HumanFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="warning text",
        args=(),
        exc_info=None,
    )
    record.event = "license_warn"
    record.request_id = "abc123def"
    out = formatter.format(record)
    assert "\n" not in out
    assert "WARNI" in out  # level rendered
    assert "abc123de" in out  # truncated request id
    assert "license_warn" in out


def test_human_formatter_renders_extra_fields_sorted_kv() -> None:
    """Step-by-step logging puts extras after the event name as `k=v`,
    sorted by key for deterministic columns."""
    formatter = HumanFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="ignored",
        args=(),
        exc_info=None,
    )
    record.event = "chunk_complete"
    record.request_id = "abc123def"
    record.chunk_index = 0.0
    record.duration_s = 5.43
    record.page_range = [1, 1500]
    record.output_bytes = 12345
    out = formatter.format(record)
    assert "chunk_complete" in out
    # Sorted-by-key order: chunk_index, duration_s, output_bytes, page_range
    expected = "chunk_index=0.0 duration_s=5.43 output_bytes=12345 page_range=[1,1500]"
    assert expected in out


def test_human_formatter_quotes_whitespace_values() -> None:
    formatter = HumanFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="ignored",
        args=(),
        exc_info=None,
    )
    record.event = "format_detected"
    record.request_id = "xyz"
    record.source_filename = "Two Words.xls"
    out = formatter.format(record)
    assert 'source_filename="Two Words.xls"' in out


def test_request_context_propagates_to_log_record() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    with request_context("req-xyz-789"):
        emit_event("test_event", foo="bar")

    lines = stream.getvalue().strip().split("\n")
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["request_id"] == "req-xyz-789"
    assert payload["event"] == "test_event"
    assert payload["foo"] == "bar"


async def _spawn_chunk_render() -> str:
    """Simulated chunk-render coroutine that emits one log line."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    emit_event("chunk_complete", chunk_index=0)
    return stream.getvalue()


async def test_contextvar_propagates_through_gather() -> None:
    """asyncio.gather must auto-copy ContextVar state into spawned tasks."""
    configure(format="json", level="info")
    with request_context("parent-req-id"):
        # Spawn 5 fake chunk renders concurrently
        results = await asyncio.gather(
            *(asyncio.create_task(_spawn_chunk_render()) for _ in range(5))
        )
    for output in results:
        payload = json.loads(output.strip().split("\n")[-1])
        assert payload["request_id"] == "parent-req-id"


def test_configure_format_switch() -> None:
    configure(format="json", level="debug")
    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    configure(format="human", level="info")
    assert isinstance(root.handlers[0].formatter, HumanFormatter)


def test_context_reset_after_request() -> None:
    with request_context("inner"):
        assert current_request_id.get() == "inner"
    assert current_request_id.get() == "-"
