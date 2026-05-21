"""Integration tests for per-IP rate limiting on /convert.

Uses the fake worker so we don't depend on the real Aspose SDK; behavior under
test is purely the rate-limit middleware-equivalent at the top of /convert.
"""

from __future__ import annotations

import shutil
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from office_convert.config import Settings
from office_convert.server import create_app

pytestmark = pytest.mark.skipif(
    shutil.which("qpdf") is None,
    reason="qpdf binary required for end-to-end /convert",
)


@pytest.fixture
def tight_rate_limit_client(test_settings: Settings) -> Generator[TestClient, None, None]:
    """Tight per-IP limit: burst=2, 6 rpm. 6/60 = 0.1 tokens/sec — slow enough
    that the bucket cannot refill across the few seconds these tests run.
    """
    tight = test_settings.model_copy(
        update={
            "rate_limit_enabled": True,
            "rate_limit_per_ip_rpm": 6,
            "rate_limit_burst": 2,
            "rate_limit_trust_xff": True,
        }
    )
    app = create_app(tight)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def disabled_rate_limit_client(test_settings: Settings) -> Generator[TestClient, None, None]:
    disabled = test_settings.model_copy(update={"rate_limit_enabled": False})
    app = create_app(disabled)
    with TestClient(app) as c:
        yield c


def _post_pdf(client: TestClient, sample_pdf: Path, *, xff: str) -> httpx.Response:
    """Convenience: POST sample_pdf with a synthetic X-Forwarded-For."""
    with sample_pdf.open("rb") as f:
        return client.post(
            "/v1/convert",
            files={"file": ("sample.pdf", f, "application/pdf")},
            data={"options": "{}"},
            headers={"X-Forwarded-For": xff},
        )


def test_burst_then_429(tight_rate_limit_client: TestClient, sample_pdf: Path) -> None:
    """burst=2 → first two requests pass; third gets 429 with rate-limit headers."""
    r1 = _post_pdf(tight_rate_limit_client, sample_pdf, xff="9.9.9.9")
    r2 = _post_pdf(tight_rate_limit_client, sample_pdf, xff="9.9.9.9")
    r3 = _post_pdf(tight_rate_limit_client, sample_pdf, xff="9.9.9.9")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429

    # Standard rate-limit response headers
    assert r3.headers.get("Retry-After") is not None
    assert int(r3.headers["Retry-After"]) >= 1
    assert r3.headers.get("X-RateLimit-Limit") == "6"
    assert r3.headers.get("X-RateLimit-Remaining") == "0"
    assert int(r3.headers.get("X-RateLimit-Reset", "0")) > 0

    # Diagnostic body shape
    body = r3.json()
    assert body["failure_class"] == "rate_limited"
    assert body["detail"]["limit"] == 6
    assert body["detail"]["retry_after_seconds"] >= 1


def test_separate_ips_independent(tight_rate_limit_client: TestClient, sample_pdf: Path) -> None:
    """Different X-Forwarded-For values have independent buckets."""
    a1 = _post_pdf(tight_rate_limit_client, sample_pdf, xff="1.1.1.1")
    a2 = _post_pdf(tight_rate_limit_client, sample_pdf, xff="1.1.1.1")
    a3 = _post_pdf(tight_rate_limit_client, sample_pdf, xff="1.1.1.1")
    assert (a1.status_code, a2.status_code, a3.status_code) == (200, 200, 429)
    # A different client IP is unaffected.
    b1 = _post_pdf(tight_rate_limit_client, sample_pdf, xff="2.2.2.2")
    assert b1.status_code == 200


def test_disabled_passthrough(disabled_rate_limit_client: TestClient, sample_pdf: Path) -> None:
    """With rate_limit_enabled=False, repeated requests all succeed and no rate-limit headers."""
    for _ in range(3):
        r = _post_pdf(disabled_rate_limit_client, sample_pdf, xff="3.3.3.3")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" not in r.headers


def test_success_response_includes_rate_limit_headers(
    tight_rate_limit_client: TestClient, sample_pdf: Path
) -> None:
    r = _post_pdf(tight_rate_limit_client, sample_pdf, xff="4.4.4.4")
    assert r.status_code == 200
    assert r.headers.get("X-RateLimit-Limit") == "6"
    # Burst was 2, consumed 1 → remaining 1.
    assert r.headers.get("X-RateLimit-Remaining") == "1"
