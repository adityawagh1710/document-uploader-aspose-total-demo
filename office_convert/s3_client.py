"""S3 source/sink helpers for the optional S3 integration.

See ``aidlc-docs/construction/plans/s3-source-integration-plan.md``.

Design: synchronous ``boto3`` wrapped in ``asyncio.to_thread`` rather than
``aioboto3`` — it sidesteps the moto/aiobotocore compatibility risk, and
``asyncio.to_thread`` for blocking I/O is already an established pattern in
this codebase (``aspose_worker``, the cache upload). ``boto3.upload_file``
still auto-multiparts internally for files ≥ 8 MB.

Environment routing: when ``AWS_ENDPOINT_URL_S3`` (or ``AWS_ENDPOINT_URL``) is
set, the client talks to that endpoint (LocalStack in compose) with path-style
addressing; otherwise it talks to real AWS. The application code is
endpoint-agnostic.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from office_convert.errors import (
    S3InputForbiddenError,
    S3InputNotFoundError,
    S3InvalidUrlError,
    S3OutputForbiddenError,
    S3OutputUploadFailedError,
)

if TYPE_CHECKING:
    from office_convert.config import Settings

_S3_SCHEME = "s3://"
_DOWNLOAD_CHUNK = 1024 * 1024
# botocore error codes that mean "the object/bucket isn't there".
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})
_FORBIDDEN_CODES = frozenset({"AccessDenied", "403", "AllAccessDisabled"})


# ---- URL + allowlist parsing (pure, no network) ----------------------------


def parse_s3_url(url: str) -> tuple[str, str]:
    """Split ``s3://bucket/key`` into ``(bucket, key)``.

    ``key`` may be empty (a bucket-only URL); callers decide whether that is
    acceptable. Raises :class:`S3InvalidUrlError` on a malformed scheme or
    missing bucket.
    """
    if not url or not url.startswith(_S3_SCHEME):
        raise S3InvalidUrlError(url)
    bucket, _, key = url[len(_S3_SCHEME) :].partition("/")
    if not bucket:
        raise S3InvalidUrlError(url)
    return bucket, key


def parse_allowlist(raw: str | None) -> list[str]:
    """Parse a comma-separated bucket allowlist. Empty/None → empty list."""
    if not raw:
        return []
    return [b.strip() for b in raw.split(",") if b.strip()]


def is_input_bucket_allowed(bucket: str, settings: Settings) -> bool:
    """Fail-closed: an empty input allowlist rejects every bucket."""
    return bucket in parse_allowlist(settings.s3_input_buckets_allowlist)


def is_output_bucket_allowed(bucket: str, settings: Settings) -> bool:
    """Fail-closed, but the configured default output bucket is implicitly OK."""
    allowed = set(parse_allowlist(settings.s3_output_buckets_allowlist))
    if settings.s3_default_output_bucket:
        allowed.add(settings.s3_default_output_bucket)
    return bucket in allowed


def resolve_output_target(url: str, request_id: str, settings: Settings) -> tuple[str, str]:
    """Resolve an ``s3_output`` URL to ``(bucket, key)``.

    A bucket-only URL (``s3://bucket``) falls back to the configured key
    template, substituting ``{request_id}``.
    """
    bucket, key = parse_s3_url(url)
    if not key:
        key = settings.s3_output_key_template.format(request_id=request_id)
    return bucket, key


# ---- boto3 client ----------------------------------------------------------


def _client(settings: Settings) -> Any:
    """Build a boto3 S3 client. Honors a LocalStack/custom endpoint via env."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    kwargs: dict[str, Any] = {}
    if settings.s3_region:
        kwargs["region_name"] = settings.s3_region
    if endpoint:
        kwargs["endpoint_url"] = endpoint
        # Path-style addressing avoids DNS games with `bucket.localstack`.
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        )
    else:
        config = Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        )
    return boto3.client("s3", config=config, **kwargs)


def _presign_client(settings: Settings) -> Any:
    """Client used to SIGN presigned URLs.

    When ``s3_public_endpoint`` is set, the URL is signed against that host so
    the party following it (a browser on the host, for LocalStack) can reach
    it — distinct from the in-network endpoint the server uses for get/put.
    Otherwise falls back to the normal client (real AWS public S3 endpoint).
    """
    if not settings.s3_public_endpoint:
        return _client(settings)
    kwargs: dict[str, Any] = {
        "endpoint_url": settings.s3_public_endpoint,
        "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    }
    if settings.s3_region:
        kwargs["region_name"] = settings.s3_region
    return boto3.client("s3", **kwargs)


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


# ---- download (input) ------------------------------------------------------


def _download_sync(bucket: str, key: str, dest: Path, settings: Settings) -> str:
    client = _client(settings)
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = _error_code(e)
        if code in _NOT_FOUND_CODES:
            raise S3InputNotFoundError(bucket, key) from e
        if code in _FORBIDDEN_CODES:
            raise S3InputForbiddenError(bucket) from e
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with dest.open("wb") as fh:
        for chunk in obj["Body"].iter_chunks(chunk_size=_DOWNLOAD_CHUNK):
            if chunk:
                fh.write(chunk)
                h.update(chunk)
    return h.hexdigest()


async def download_to_path(url: str, dest: Path, settings: Settings) -> str:
    """Stream an S3 object to ``dest``, returning its SHA-256 hex digest.

    The SHA is computed in the same pass as the download so it plugs into the
    existing cache key without a second read. Enforces the input allowlist
    *before* any S3 call.
    """
    bucket, key = parse_s3_url(url)
    if not key:
        raise S3InvalidUrlError(url)
    if not is_input_bucket_allowed(bucket, settings):
        raise S3InputForbiddenError(bucket)
    return await asyncio.to_thread(_download_sync, bucket, key, dest, settings)


# ---- upload (output) -------------------------------------------------------


def _upload_sync(local_path: Path, bucket: str, key: str, settings: Settings) -> None:
    client = _client(settings)
    try:
        client.upload_file(
            str(local_path),
            bucket,
            key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
    except (ClientError, BotoCoreError, OSError) as e:
        raise S3OutputUploadFailedError(bucket, key, str(e)) from e


async def upload_file(local_path: Path, bucket: str, key: str, settings: Settings) -> None:
    """Upload a local PDF to S3. Enforces the output allowlist before calling."""
    if not is_output_bucket_allowed(bucket, settings):
        raise S3OutputForbiddenError(bucket)
    await asyncio.to_thread(_upload_sync, local_path, bucket, key, settings)


# ---- presign (download link) -----------------------------------------------


def generate_presigned_get_url(bucket: str, key: str, settings: Settings) -> str:
    """Mint a short-TTL presigned GET URL for an output object.

    Enforces the output allowlist BEFORE signing — without this the endpoint
    is a presigning oracle for any object the pod's IAM role can read.
    Presigning is local crypto (no network), so it is not wrapped in a thread.
    """
    if not is_output_bucket_allowed(bucket, settings):
        raise S3OutputForbiddenError(bucket)
    client = _presign_client(settings)
    url: str = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=settings.s3_presign_ttl_seconds,
    )
    return url
