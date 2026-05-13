"""Tests for office_convert.license: XML parsing, state classification, refresh."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from office_convert.license import LicenseManager, classify
from office_convert.types import LicenseState


def _write_license(path: Path, expiry: date | None) -> None:
    if expiry is None:
        body = "<License><Data><LicensedTo>test</LicensedTo></Data></License>"
    else:
        ymd = expiry.strftime("%Y%m%d")
        body = (
            "<License><Data>" f"<SubscriptionExpiry>{ymd}</SubscriptionExpiry>" "</Data></License>"
        )
    path.write_text(body, encoding="utf-8")


def test_classify_no_expiry_is_permanent() -> None:
    assert classify(None) == LicenseState.PERMANENT


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (30, LicenseState.HEALTHY),
        (8, LicenseState.HEALTHY),
        (7, LicenseState.WARN),
        (4, LicenseState.WARN),
        (3, LicenseState.CRITICAL),
        (1, LicenseState.CRITICAL),
        (0, LicenseState.EXPIRING_TODAY),
        (-1, LicenseState.EXPIRED),
        (-30, LicenseState.EXPIRED),
    ],
)
def test_classify_state_thresholds(days: int, expected: LicenseState) -> None:
    assert classify(days) == expected


def test_license_manager_reads_expiry(tmp_path: Path) -> None:
    today = datetime.now(UTC).date()
    expiry = today + timedelta(days=15)
    lic = tmp_path / "license.lic"
    _write_license(lic, expiry)

    mgr = LicenseManager(lic)
    assert mgr.expiry_date() == expiry
    assert mgr.days_remaining() == 15
    assert not mgr.is_expired()
    assert mgr.state() == LicenseState.HEALTHY


def test_license_manager_expired(tmp_path: Path) -> None:
    today = datetime.now(UTC).date()
    expiry = today - timedelta(days=1)
    lic = tmp_path / "license.lic"
    _write_license(lic, expiry)
    mgr = LicenseManager(lic)
    assert mgr.is_expired()
    assert mgr.state() == LicenseState.EXPIRED


def test_license_manager_no_expiry_field_means_permanent(tmp_path: Path) -> None:
    lic = tmp_path / "license.lic"
    _write_license(lic, None)
    mgr = LicenseManager(lic)
    assert mgr.expiry_date() is None
    assert mgr.days_remaining() is None
    assert not mgr.is_expired()
    assert mgr.state() == LicenseState.PERMANENT


def test_license_manager_refresh_picks_up_new_expiry(tmp_path: Path) -> None:
    today = datetime.now(UTC).date()
    lic = tmp_path / "license.lic"
    _write_license(lic, today + timedelta(days=5))

    mgr = LicenseManager(lic)
    assert mgr.days_remaining() == 5

    # Operator rotates license to a new 30-day window
    _write_license(lic, today + timedelta(days=30))
    # Without refresh, cached value persists
    assert mgr.days_remaining() == 5
    mgr.refresh()
    assert mgr.days_remaining() == 30


def test_license_manager_missing_file_raises(tmp_path: Path) -> None:
    mgr = LicenseManager(tmp_path / "no-such-license.lic")
    with pytest.raises(FileNotFoundError):
        mgr.expiry_date()


def test_license_manager_iso_date_format(tmp_path: Path) -> None:
    """Aspose dates may also appear as YYYY-MM-DD per the parser."""
    today = datetime.now(UTC).date()
    expiry = today + timedelta(days=10)
    lic = tmp_path / "license.lic"
    lic.write_text(
        f"<License><Data><SubscriptionExpiry>{expiry.isoformat()}</SubscriptionExpiry></Data></License>",
        encoding="utf-8",
    )
    mgr = LicenseManager(lic)
    assert mgr.expiry_date() == expiry
