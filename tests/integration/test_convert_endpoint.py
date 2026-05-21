"""Integration tests for POST /convert via FastAPI TestClient.

Use a fake worker (under tmp_path) to avoid needing the real Aspose SDK.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    shutil.which("qpdf") is None,
    reason="qpdf binary required for end-to-end /convert",
)


def test_convert_returns_pdf(client: TestClient, sample_pdf: Path) -> None:
    with sample_pdf.open("rb") as f:
        response = client.post(
            "/v1/convert",
            files={"file": ("sample.pdf", f, "application/pdf")},
            data={"options": "{}"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert "X-Request-ID" in response.headers


def test_convert_rejects_unsupported_format(client: TestClient) -> None:
    response = client.post(
        "/v1/convert",
        files={"file": ("bad.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["failure_class"] == "unsupported_format"
    assert "request_id" in body


def test_convert_rejects_oversized_input(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app with very low max_input_bytes rejects a small overflow."""
    from office_convert.config import Settings
    from office_convert.server import create_app

    # Build a fresh app with a tiny size limit. The prefix points at /bin/true
    # which is unused — the request is rejected on size before we'd resolve a
    # per-format binary.
    small_settings = Settings(
        worker_binary_prefix=Path("/bin/true"),
        license_path=tmp_path / "license.lic",
        scratch_dir=tmp_path / "scratch",
        cache_dir=None,
        chunk_timeout_seconds=30,
        max_input_bytes=1024 * 1024,  # 1 MB
        aspose_version="test",
    )
    # Provide a valid license to pass the pre-check
    from datetime import UTC, datetime, timedelta

    expiry = (datetime.now(UTC).date() + timedelta(days=30)).strftime("%Y%m%d")
    small_settings.license_path.write_text(
        f"<License><Data><SubscriptionExpiry>{expiry}</SubscriptionExpiry></Data></License>"
    )
    small_app = create_app(small_settings)
    with TestClient(small_app) as small_client:
        # PDF body of just over 1 MB
        body = b"%PDF-1.7\n" + b"x" * (1024 * 1024 + 10)
        response = small_client.post(
            "/v1/convert",
            files={"file": ("big.pdf", body, "application/pdf")},
        )
    assert response.status_code == 400
    assert response.json()["failure_class"] == "input_too_large"


def test_convert_failure_response_carries_request_id(client: TestClient) -> None:
    response = client.post(
        "/v1/convert",
        files={"file": ("bad.png", b"\x89PNG", "image/png")},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
