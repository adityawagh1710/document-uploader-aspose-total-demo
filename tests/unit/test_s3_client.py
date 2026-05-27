"""Tests for office_convert.s3_client — moto-mocked S3, no Docker needed."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from office_convert import s3_client
from office_convert.config import Settings
from office_convert.errors import (
    S3InputForbiddenError,
    S3InputNotFoundError,
    S3InvalidUrlError,
    S3OutputForbiddenError,
)

REGION = "us-east-1"
IN_BUCKET = "in-bucket"
OUT_BUCKET = "out-bucket"


@pytest.fixture
def aws_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy creds so boto3 builds a client; clear any LocalStack endpoint."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@pytest.fixture
def s3(aws_creds: None) -> Iterator[Any]:
    """Active moto mock + a client with the two test buckets created."""
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=IN_BUCKET)
        client.create_bucket(Bucket=OUT_BUCKET)
        yield client


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "s3_enabled": True,
        "s3_region": REGION,
        "s3_input_buckets_allowlist": IN_BUCKET,
        "s3_output_buckets_allowlist": OUT_BUCKET,
    }
    base.update(overrides)
    return Settings(**base)


# ---- pure parsing (no network) ---------------------------------------------


def test_parse_s3_url_with_key() -> None:
    assert s3_client.parse_s3_url("s3://my-bucket/path/to/file.docx") == (
        "my-bucket",
        "path/to/file.docx",
    )


def test_parse_s3_url_bucket_only() -> None:
    assert s3_client.parse_s3_url("s3://my-bucket") == ("my-bucket", "")


@pytest.mark.parametrize("bad", ["", "http://x/y", "s3://", "my-bucket/key", "s3:///key"])
def test_parse_s3_url_invalid(bad: str) -> None:
    with pytest.raises(S3InvalidUrlError):
        s3_client.parse_s3_url(bad)


def test_parse_allowlist() -> None:
    assert s3_client.parse_allowlist("a, b ,,c ") == ["a", "b", "c"]
    assert s3_client.parse_allowlist(None) == []
    assert s3_client.parse_allowlist("") == []


def test_allowlist_helpers_fail_closed() -> None:
    s = _settings(s3_input_buckets_allowlist=None, s3_output_buckets_allowlist=None)
    assert s3_client.is_input_bucket_allowed("anything", s) is False
    assert s3_client.is_output_bucket_allowed("anything", s) is False


def test_output_default_bucket_implicitly_allowed() -> None:
    s = _settings(s3_output_buckets_allowlist=None, s3_default_output_bucket="default-out")
    assert s3_client.is_output_bucket_allowed("default-out", s) is True
    assert s3_client.is_output_bucket_allowed("other", s) is False


def test_resolve_output_target_uses_template() -> None:
    s = _settings(s3_output_key_template="pdf/{request_id}.pdf")
    assert s3_client.resolve_output_target("s3://out-bucket", "abc123", s) == (
        "out-bucket",
        "pdf/abc123.pdf",
    )
    # explicit key wins over the template
    assert s3_client.resolve_output_target("s3://out-bucket/custom.pdf", "abc123", s) == (
        "out-bucket",
        "custom.pdf",
    )


# ---- download (input) ------------------------------------------------------


async def test_download_to_path_returns_sha(s3: Any, tmp_path: Path) -> None:
    content = b"hello docx bytes" * 4096
    s3.put_object(Bucket=IN_BUCKET, Key="doc.docx", Body=content)
    dest = tmp_path / "downloaded.docx"

    sha = await s3_client.download_to_path(f"s3://{IN_BUCKET}/doc.docx", dest, _settings())

    assert dest.read_bytes() == content
    assert sha == hashlib.sha256(content).hexdigest()


async def test_download_missing_key_raises_not_found(s3: Any, tmp_path: Path) -> None:
    with pytest.raises(S3InputNotFoundError):
        await s3_client.download_to_path(f"s3://{IN_BUCKET}/nope.docx", tmp_path / "x", _settings())


async def test_download_bucket_not_allowlisted(s3: Any, tmp_path: Path) -> None:
    s3.put_object(Bucket=OUT_BUCKET, Key="doc.docx", Body=b"x")
    # OUT_BUCKET is not in the *input* allowlist → rejected before any S3 call.
    with pytest.raises(S3InputForbiddenError):
        await s3_client.download_to_path(f"s3://{OUT_BUCKET}/doc.docx", tmp_path / "x", _settings())


# ---- upload (output) -------------------------------------------------------


async def test_upload_file_lands_object(s3: Any, tmp_path: Path) -> None:
    local = tmp_path / "out.pdf"
    body = b"%PDF-1.7 fake pdf"
    local.write_bytes(body)

    await s3_client.upload_file(local, OUT_BUCKET, "pdf/result.pdf", _settings())

    got = s3.get_object(Bucket=OUT_BUCKET, Key="pdf/result.pdf")
    assert got["Body"].read() == body
    assert got["ContentType"] == "application/pdf"


async def test_upload_bucket_not_allowlisted(s3: Any, tmp_path: Path) -> None:
    local = tmp_path / "out.pdf"
    local.write_bytes(b"%PDF-1.7")
    with pytest.raises(S3OutputForbiddenError):
        await s3_client.upload_file(local, "rogue-bucket", "k.pdf", _settings())


# ---- presign ---------------------------------------------------------------


def test_generate_presigned_url(s3: Any) -> None:
    s3.put_object(Bucket=OUT_BUCKET, Key="pdf/result.pdf", Body=b"%PDF-1.7")
    url = s3_client.generate_presigned_get_url(OUT_BUCKET, "pdf/result.pdf", _settings())
    assert OUT_BUCKET in url
    assert "pdf/result.pdf" in url
    assert "X-Amz-Signature" in url


def test_presign_bucket_not_allowlisted(s3: Any) -> None:
    with pytest.raises(S3OutputForbiddenError):
        s3_client.generate_presigned_get_url("rogue-bucket", "k.pdf", _settings())


def test_presign_signs_against_public_endpoint(s3: Any) -> None:
    # When a public endpoint is configured (LocalStack host port), the URL must
    # carry that host so a browser on the host can follow it.
    s = _settings(s3_public_endpoint="http://localhost:4567")
    url = s3_client.generate_presigned_get_url(OUT_BUCKET, "pdf/x.pdf", s)
    assert url.startswith("http://localhost:4567/")
    assert "X-Amz-Signature" in url
