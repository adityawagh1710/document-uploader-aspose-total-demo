"""Tests for office_convert.config: validation rules, defaults, env-var overrides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from office_convert.config import Settings


def test_defaults() -> None:
    s = Settings()
    assert s.max_jobs == 1
    assert s.parallel == 4
    assert s.cache_dir is None
    assert s.chunk_timeout_seconds == 300
    assert s.max_input_bytes == 1024 * 1024 * 1024
    assert s.log_format == "json"
    assert s.log_level == "info"


def test_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFFICE_CONVERT_MAX_JOBS", "5")
    monkeypatch.setenv("OFFICE_CONVERT_LOG_FORMAT", "human")
    s = Settings()
    assert s.max_jobs == 5
    assert s.log_format == "human"


def test_max_jobs_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFFICE_CONVERT_MAX_JOBS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_max_jobs_too_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFFICE_CONVERT_MAX_JOBS", "1000")
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_log_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFFICE_CONVERT_LOG_FORMAT", "xml")
    with pytest.raises(ValidationError):
        Settings()


def test_chunk_timeout_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFFICE_CONVERT_CHUNK_TIMEOUT_SECONDS", "10")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("OFFICE_CONVERT_CHUNK_TIMEOUT_SECONDS", "10000")
    with pytest.raises(ValidationError):
        Settings()
