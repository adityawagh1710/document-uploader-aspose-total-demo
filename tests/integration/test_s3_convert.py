"""Integration tests for S3 source/sink + presign on POST /v1/convert.

moto mocks S3 in-process (no Docker/LocalStack needed). A fake worker stands
in for Aspose. Covers the 8 must-pass dispatch scenarios from the plan plus
the presign endpoint and the feature-flag gate.
"""

from __future__ import annotations

import shutil
import stat
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from office_convert.config import Settings
from office_convert.server import create_app

pytestmark = pytest.mark.skipif(
    shutil.which("qpdf") is None,
    reason="qpdf binary required for end-to-end /convert",
)

REGION = "us-east-1"
IN_BUCKET = "in-bucket"
OUT_BUCKET = "out-bucket"

# Tiny valid PDF: passes magic detection; probe_lite returns a page count
# without invoking the worker, and the fake worker renders a reportlab PDF.
PDF_BODY = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF\n"

_FAKE_WORKER = """#!/usr/bin/env python3
import argparse, json, sys
p = argparse.ArgumentParser()
for a in ("--mode", "--input", "--output", "--format", "--license-path", "--page-range"):
    p.add_argument(a)
args, _ = p.parse_known_args()
if args.mode == "probe":
    sys.stdout.write(json.dumps(
        {"page_count": 1, "format": args.format, "natural_seams": [], "size_bytes": 64}
    ))
    sys.exit(0)
try:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(args.output)
    c.drawString(100, 750, "page")
    c.showPage()
    c.save()
except Exception as e:
    sys.stderr.write(str(e))
    sys.exit(1)
sys.exit(0)
"""


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    prefix = tmp_path / "fake-worker"
    for fmt in ("docx", "pptx", "xlsx", "pdf", "email"):
        _exe(prefix.with_name(f"{prefix.name}-{fmt}"), _FAKE_WORKER)
    lic = tmp_path / "license.lic"
    expiry = (datetime.now(UTC).date() + timedelta(days=30)).strftime("%Y%m%d")
    lic.write_text(
        f"<License><Data><SubscriptionExpiry>{expiry}</SubscriptionExpiry></Data></License>"
    )
    base: dict[str, Any] = {
        "worker_binary_prefix": prefix,
        "license_path": lic,
        "scratch_dir": tmp_path / "scratch",
        "cache_dir": None,
        "chunk_timeout_seconds": 60,
        "max_pages_per_chunk": 10,
        "max_mb_per_chunk": 50,
        "aspose_version": "test",
        "rate_limit_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@pytest.fixture
def env(tmp_path: Path, _aws_env: None) -> Iterator[tuple[TestClient, Any]]:
    """S3-enabled app + moto-backed S3 with input/output buckets created."""
    settings = _build_settings(
        tmp_path,
        s3_enabled=True,
        s3_region=REGION,
        s3_input_buckets_allowlist=IN_BUCKET,
        s3_output_buckets_allowlist=OUT_BUCKET,
    )
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=IN_BUCKET)
        s3.create_bucket(Bucket=OUT_BUCKET)
        with TestClient(create_app(settings)) as client:
            yield client, s3


# ---- the 8 must-pass dispatch scenarios ------------------------------------


def test_1_file_only(env: tuple[TestClient, Any]) -> None:
    client, _ = env
    r = client.post("/v1/convert", files={"file": ("doc.pdf", PDF_BODY, "application/pdf")})
    assert r.status_code == 200, r.content
    assert r.content.startswith(b"%PDF-")
    assert "X-S3-Output-Bucket" not in r.headers


def test_2_s3_input_only(env: tuple[TestClient, Any]) -> None:
    client, s3 = env
    s3.put_object(Bucket=IN_BUCKET, Key="doc.pdf", Body=PDF_BODY)
    r = client.post("/v1/convert", data={"s3_input": f"s3://{IN_BUCKET}/doc.pdf"})
    assert r.status_code == 200, r.content
    assert r.content.startswith(b"%PDF-")
    assert "X-S3-Output-Bucket" not in r.headers


def test_3_s3_input_and_output(env: tuple[TestClient, Any]) -> None:
    client, s3 = env
    s3.put_object(Bucket=IN_BUCKET, Key="doc.pdf", Body=PDF_BODY)
    r = client.post(
        "/v1/convert",
        data={
            "s3_input": f"s3://{IN_BUCKET}/doc.pdf",
            "s3_output": f"s3://{OUT_BUCKET}/result.pdf",
        },
    )
    assert r.status_code == 200, r.content
    assert r.headers["X-S3-Output-Bucket"] == OUT_BUCKET
    assert r.headers["X-S3-Output-Key"] == "result.pdf"
    assert s3.get_object(Bucket=OUT_BUCKET, Key="result.pdf")["Body"].read().startswith(b"%PDF-")


def test_4_file_and_s3_output(env: tuple[TestClient, Any]) -> None:
    client, s3 = env
    r = client.post(
        "/v1/convert",
        files={"file": ("doc.pdf", PDF_BODY, "application/pdf")},
        data={"s3_output": f"s3://{OUT_BUCKET}/from-upload.pdf"},
    )
    assert r.status_code == 200, r.content
    assert r.headers["X-S3-Output-Key"] == "from-upload.pdf"
    body = s3.get_object(Bucket=OUT_BUCKET, Key="from-upload.pdf")["Body"].read()
    assert body.startswith(b"%PDF-")
    # tee correctness: streamed bytes == stored bytes
    assert body == r.content


def test_5_both_inputs_conflict(env: tuple[TestClient, Any]) -> None:
    client, s3 = env
    s3.put_object(Bucket=IN_BUCKET, Key="doc.pdf", Body=PDF_BODY)
    r = client.post(
        "/v1/convert",
        files={"file": ("doc.pdf", PDF_BODY, "application/pdf")},
        data={"s3_input": f"s3://{IN_BUCKET}/doc.pdf"},
    )
    assert r.status_code == 400
    assert r.json()["failure_class"] == "input_source_conflict"


def test_6_neither_input(env: tuple[TestClient, Any]) -> None:
    client, _ = env
    r = client.post("/v1/convert", data={"options": "{}"})
    assert r.status_code == 400
    assert r.json()["failure_class"] == "missing_file"


def test_7_s3_input_not_found(env: tuple[TestClient, Any]) -> None:
    client, _ = env
    r = client.post("/v1/convert", data={"s3_input": f"s3://{IN_BUCKET}/nope.pdf"})
    assert r.status_code == 404
    assert r.json()["failure_class"] == "s3_input_not_found"


def test_8_s3_input_bucket_not_allowlisted(env: tuple[TestClient, Any]) -> None:
    client, s3 = env
    s3.put_object(Bucket=OUT_BUCKET, Key="doc.pdf", Body=PDF_BODY)
    # OUT_BUCKET is not in the *input* allowlist → rejected before any S3 GET.
    r = client.post("/v1/convert", data={"s3_input": f"s3://{OUT_BUCKET}/doc.pdf"})
    assert r.status_code == 400
    assert r.json()["failure_class"] == "s3_input_forbidden"


def test_s3_output_bucket_not_allowlisted_fails_fast(env: tuple[TestClient, Any]) -> None:
    client, _ = env
    r = client.post(
        "/v1/convert",
        files={"file": ("doc.pdf", PDF_BODY, "application/pdf")},
        data={"s3_output": "s3://rogue-bucket/out.pdf"},
    )
    assert r.status_code == 400
    assert r.json()["failure_class"] == "s3_output_forbidden"


# ---- presign endpoint (Phase 7) --------------------------------------------


def test_presign_ok(env: tuple[TestClient, Any]) -> None:
    client, s3 = env
    s3.put_object(Bucket=OUT_BUCKET, Key="pdf/x.pdf", Body=PDF_BODY)
    r = client.get("/v1/downloads/presign", params={"bucket": OUT_BUCKET, "key": "pdf/x.pdf"})
    assert r.status_code == 200
    body = r.json()
    assert OUT_BUCKET in body["download_url"]
    assert "pdf/x.pdf" in body["download_url"]
    assert body["expires_in_seconds"] == 900
    assert body["bucket"] == OUT_BUCKET


def test_presign_bucket_not_allowlisted(env: tuple[TestClient, Any]) -> None:
    client, _ = env
    r = client.get("/v1/downloads/presign", params={"bucket": "rogue", "key": "k.pdf"})
    assert r.status_code == 400
    assert r.json()["failure_class"] == "s3_output_forbidden"


# ---- feature flag gate (no moto needed — rejected before any S3 call) ------


def test_s3_field_rejected_when_disabled(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path, s3_enabled=False)
    with TestClient(create_app(settings)) as client:
        r = client.post(
            "/v1/convert",
            files={"file": ("doc.pdf", PDF_BODY, "application/pdf")},
            data={"s3_output": "s3://out/x.pdf"},
        )
    assert r.status_code == 400
    assert r.json()["failure_class"] == "s3_disabled"


def test_presign_rejected_when_disabled(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path, s3_enabled=False)
    with TestClient(create_app(settings)) as client:
        r = client.get("/v1/downloads/presign", params={"bucket": "out", "key": "x.pdf"})
    assert r.status_code == 400
    assert r.json()["failure_class"] == "s3_disabled"
