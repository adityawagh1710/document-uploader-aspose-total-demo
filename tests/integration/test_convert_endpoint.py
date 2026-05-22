"""Integration tests for POST /convert via FastAPI TestClient.

Use a fake worker (under tmp_path) to avoid needing the real Aspose SDK.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    shutil.which("qpdf") is None,
    reason="qpdf binary required for end-to-end /convert",
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_valid_license(path: Path, days: int = 30) -> None:
    expiry = (datetime.now(UTC).date() + timedelta(days=days)).strftime("%Y%m%d")
    path.write_text(
        f"<License><Data><SubscriptionExpiry>{expiry}</SubscriptionExpiry></Data></License>",
        encoding="utf-8",
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


def test_convert_routes_odg_through_libreoffice(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ODG bypasses the Aspose orchestrator and reaches LibreOffice.

    Verifies the server's ODG branch + libreoffice_convert wrapper end-to-end
    by stubbing `soffice` on PATH with a Python script that emits a tiny
    valid PDF into --outdir. Real soffice isn't in the test image.
    """
    import zipfile

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    soffice = fake_bin / "soffice"
    soffice.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os\n"
        "argv = sys.argv[1:]\n"
        "outdir = argv[argv.index('--outdir') + 1]\n"
        "infile = argv[-1]\n"
        "stem = os.path.splitext(os.path.basename(infile))[0]\n"
        "with open(os.path.join(outdir, stem + '.pdf'), 'wb') as f:\n"
        "    f.write(b'%PDF-1.4\\nfake odg pdf\\n%%EOF\\n')\n"
    )
    soffice.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    # Build a minimal-but-spec-correct ODG so detect_format → "odg".
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        mi = zipfile.ZipInfo("mimetype")
        mi.compress_type = zipfile.ZIP_STORED
        z.writestr(mi, b"application/vnd.oasis.opendocument.graphics")
        z.writestr("META-INF/manifest.xml", "<manifest/>")
        z.writestr("content.xml", "<office:document-content/>")
    odg_body = buf.getvalue()

    response = client.post(
        "/v1/convert",
        files={"file": ("drawing.odg", odg_body, "application/octet-stream")},
    )
    assert response.status_code == 200, response.content
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert b"fake odg pdf" in response.content


def test_convert_render_failure_returns_json_diagnostic(tmp_path: Path) -> None:
    """Regression: a worker that fails on --mode=render must surface as a
    JSON Diagnostic with a 4xx/5xx status, not HTTP 200 with an empty body.

    Previously the orchestrator generator was passed to StreamingResponse
    directly. Starlette flushed `200 OK Content-Type: application/pdf` to
    the wire before pulling the first chunk; the render error then raised
    mid-iteration and the ConversionError handler couldn't substitute a
    JSON error response — Starlette logged "Caught handled exception, but
    response already started" and the client saw a successful empty PDF.
    """
    from office_convert.config import Settings
    from office_convert.server import create_app

    failing_worker = """#!/usr/bin/env python3
import argparse, json, sys
p = argparse.ArgumentParser()
p.add_argument("--mode"); p.add_argument("--input"); p.add_argument("--output")
p.add_argument("--format"); p.add_argument("--license-path"); p.add_argument("--page-range")
args, _ = p.parse_known_args()
if args.mode == "probe":
    sys.stdout.write(json.dumps({
        "page_count": 1, "format": args.format, "natural_seams": [], "size_bytes": 64,
    }))
    sys.exit(0)
sys.stderr.write("load: Aspose::Words::FileCorruptedException: simulated")
sys.exit(1)
"""
    prefix = tmp_path / "fake-worker"
    for fmt in ("docx", "pptx", "xlsx", "pdf"):
        _write_executable(prefix.with_name(f"{prefix.name}-{fmt}"), failing_worker)
    license_path = tmp_path / "license.lic"
    _make_valid_license(license_path, days=30)
    settings = Settings(
        worker_binary_prefix=prefix,
        license_path=license_path,
        scratch_dir=tmp_path / "scratch",
        cache_dir=None,
        chunk_timeout_seconds=30,
        max_pages_per_chunk=10,
        max_mb_per_chunk=50,
        aspose_version="test",
    )
    app = create_app(settings)
    # Smallest plausible PDF — passes magic-byte detection; qpdf probe_lite
    # returns a page count without invoking the worker, so the failing path
    # is the render step (matching the production ODT->docx render failure).
    pdf_body = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\n%%EOF\n"
    with TestClient(app) as client:
        response = client.post(
            "/v1/convert",
            files={"file": ("bad.pdf", pdf_body, "application/pdf")},
        )
    assert response.status_code >= 400, (
        f"render failure must produce an error response, got "
        f"{response.status_code} with body starting "
        f"{response.content[:16]!r}"
    )
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert "failure_class" in body
    assert body["request_id"] == response.headers["X-Request-ID"]
