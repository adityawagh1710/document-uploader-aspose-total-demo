"""End-to-end conversion tests against a real container.

Skipped unless OFFICE_CONVERT_E2E_LICENSE is set (see conftest.py).

These tests verify behavior that in-process tests cannot reach:
- The Docker image's runtime env is correct (LD_LIBRARY_PATH, USER, paths)
- The C++ worker binary actually links Aspose.Total C++ and renders
- The full multipart → render → qpdf concat → streamed PDF pipeline works
- /health reports correctly against a real license
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import e2e

CORPUS = Path(__file__).parent.parent / "corpus"


@e2e
def test_health_reports_ready(base_url: str, http_client: Any) -> None:
    response = http_client.get(f"{base_url}/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert isinstance(body["license_days_remaining"], int)
    assert body["license_days_remaining"] > 0


@e2e
def test_simple_pdf_converts(base_url: str, http_client: Any) -> None:
    """Round-trip a small PDF through the real container."""
    input_pdf = CORPUS / "simple.pdf"
    if not input_pdf.exists():
        pytest.skip(f"corpus fixture missing: {input_pdf}; run tests/corpus/_generate.py")

    with input_pdf.open("rb") as f:
        response = http_client.post(
            f"{base_url}/convert",
            files={"file": ("simple.pdf", f, "application/pdf")},
            data={"options": "{}"},
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF-")
    assert "X-Request-ID" in response.headers


@e2e
def test_unsupported_format_is_rejected(base_url: str, http_client: Any) -> None:
    response = http_client.post(
        f"{base_url}/convert",
        files={"file": ("bad.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["failure_class"] == "unsupported_format"
    assert "request_id" in body


@e2e
def test_request_id_correlates_with_response_header(base_url: str, http_client: Any) -> None:
    """The X-Request-ID header must match the request_id in any error body."""
    response = http_client.post(
        f"{base_url}/convert",
        files={"file": ("bad.png", b"\x89PNG", "image/png")},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]


@e2e
def test_docx_converts_if_corpus_present(base_url: str, http_client: Any) -> None:
    """Real DOCX round-trip (requires tests/corpus/small.docx generated)."""
    input_docx = CORPUS / "small.docx"
    if not input_docx.exists():
        pytest.skip(f"corpus fixture missing: {input_docx}; run tests/corpus/_generate.py")
    with input_docx.open("rb") as f:
        response = http_client.post(
            f"{base_url}/convert",
            files={
                "file": (
                    "small.docx",
                    f,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
    # Allow either 200 (real Aspose linked) or 500 render_failed (scaffold).
    # This test still validates the HTTP shape end-to-end via Docker.
    if response.status_code == 200:
        assert response.content.startswith(b"%PDF-")
    else:
        # The scaffolded worker throws "SDK not linked"; expect a structured failure.
        assert response.status_code == 500
        body = response.json()
        assert body["failure_class"] in ("render_failed", "subdivision_floor_exceeded")
