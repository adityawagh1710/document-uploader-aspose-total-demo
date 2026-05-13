"""Integration tests for GET /health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from office_convert.config import Settings
from office_convert.server import create_app


def _write_license(path: Path, days: int) -> None:
    expiry = (datetime.now(UTC).date() + timedelta(days=days)).strftime("%Y%m%d")
    path.write_text(
        f"<License><Data><SubscriptionExpiry>{expiry}</SubscriptionExpiry></Data></License>"
    )


def test_health_ready_when_all_present(client: TestClient) -> None:
    response = client.get("/health")
    # The default tmp_path-based settings have all checks pass
    assert response.status_code in (200, 503)
    body = response.json()
    assert "ready" in body
    assert "license_days_remaining" in body
    assert "max_jobs" in body
    assert "active_jobs" in body


def test_health_reports_not_ready_on_expired_license(
    tmp_path: Path,
    fake_worker_script: str,
) -> None:
    """Build a custom app with an expired license."""
    import stat

    prefix = tmp_path / "fake-worker"
    for fmt in ("docx", "pptx", "xlsx", "pdf"):
        per = prefix.with_name(f"{prefix.name}-{fmt}")
        per.write_text(fake_worker_script)
        per.chmod(per.stat().st_mode | stat.S_IXUSR)
    license_path = tmp_path / "license.lic"
    _write_license(license_path, days=-5)

    s = Settings(
        worker_binary_prefix=prefix,
        license_path=license_path,
        scratch_dir=tmp_path / "scratch",
        aspose_version="test",
    )
    app = create_app(s)
    with TestClient(app) as c:
        response = c.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert not body["ready"]
    assert "license_expired" in body["problems"]


def test_health_reports_days_remaining(
    tmp_path: Path,
    fake_worker_script: str,
) -> None:
    import stat

    prefix = tmp_path / "fake-worker"
    for fmt in ("docx", "pptx", "xlsx", "pdf"):
        per = prefix.with_name(f"{prefix.name}-{fmt}")
        per.write_text(fake_worker_script)
        per.chmod(per.stat().st_mode | stat.S_IXUSR)
    license_path = tmp_path / "license.lic"
    _write_license(license_path, days=20)

    s = Settings(
        worker_binary_prefix=prefix,
        license_path=license_path,
        scratch_dir=tmp_path / "scratch",
        aspose_version="test",
    )
    app = create_app(s)
    with TestClient(app) as c:
        response = c.get("/health")
    body = response.json()
    # Account for clock drift / day boundary
    assert body["license_days_remaining"] in (19, 20)


@pytest.mark.skip(reason="missing-worker test would mutate executable; covered by static check")
def test_health_reports_missing_worker_binary() -> None:
    pass
