"""Shared test fixtures: TestClient, fake worker, sample paths."""

from __future__ import annotations

import stat
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from office_convert.config import Settings
from office_convert.server import create_app


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_valid_license(path: Path, days: int = 30) -> None:
    expiry = (datetime.now(UTC).date() + timedelta(days=days)).strftime("%Y%m%d")
    path.write_text(
        f"<License><Data><SubscriptionExpiry>{expiry}</SubscriptionExpiry></Data></License>",
        encoding="utf-8",
    )


@pytest.fixture
def fake_worker_script() -> str:
    """Worker that always succeeds with a fake PDF written via reportlab.

    Uses `--mode` to dispatch: render writes a PDF; probe writes JSON.
    """
    return """#!/usr/bin/env python3
import argparse
import json
import sys

p = argparse.ArgumentParser()
p.add_argument("--mode")
p.add_argument("--input")
p.add_argument("--output")
p.add_argument("--format")
p.add_argument("--license-path")
p.add_argument("--page-range")
args = p.parse_args()

if args.mode == "probe":
    out = {"page_count": 4, "format": args.format, "natural_seams": [], "size_bytes": 1000}
    sys.stdout.write(json.dumps(out))
    sys.exit(0)

# render mode: produce a tiny valid PDF
try:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(args.output)
    pr = args.page_range.split("-")
    pages = int(pr[1]) - int(pr[0]) + 1
    for i in range(pages):
        c.drawString(100, 750, f"page {i+1}")
        c.showPage()
    c.save()
except Exception as e:
    sys.stderr.write(f"fake worker error: {e}")
    sys.exit(1)
sys.exit(0)
"""


@pytest.fixture
def test_settings(
    tmp_path: Path,
    fake_worker_script: str,
) -> Settings:
    """Settings pointing at a fake per-format worker set + valid license."""
    # Production has one worker binary per Aspose product (post-2026-05-12
    # 4-binary split; Email worker added 2026-05-26). Tests mirror that
    # layout: write the same fake script to <prefix>-<fmt> for each.
    prefix = tmp_path / "fake-worker"
    for fmt in ("docx", "pptx", "xlsx", "pdf", "email"):
        _write_executable(prefix.with_name(f"{prefix.name}-{fmt}"), fake_worker_script)
    license_path = tmp_path / "license.lic"
    _make_valid_license(license_path, days=30)
    return Settings(
        worker_binary_prefix=prefix,
        license_path=license_path,
        scratch_dir=tmp_path / "scratch",
        cache_dir=None,
        chunk_timeout_seconds=60,
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
        aspose_version="test",
    )


@pytest.fixture
def client(test_settings: Settings) -> Generator[TestClient, None, None]:
    app = create_app(test_settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """Generate a small valid PDF for upload tests."""
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")
    path = tmp_path / "sample.pdf"
    c = canvas.Canvas(str(path))
    for i in range(4):
        c.drawString(100, 750, f"page {i + 1}")
        c.showPage()
    c.save()
    return path


@pytest.fixture
def sample_eml() -> Path:
    """Pre-checked-in RFC 5322 email fixture (multipart/alternative)."""
    return Path(__file__).parent / "corpus" / "sample.eml"
