"""Aspose license XML parser and expiry helpers.

Implements FR-8 (Python side only — XML parsing for expiry checking). The C++
worker is the only place that actually calls Aspose's SetLicense().

Aspose temporary license files are XML envelopes signed by Aspose. The expiry
date appears as `<SubscriptionExpiry>YYYYMMDD</SubscriptionExpiry>` in the
License element. If the file is well-formed but lacks that field, we treat the
license as permanent (days_remaining is None).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from office_convert.types import LicenseState

# License-expiry day thresholds per business-rules.md §4.
LICENSE_HEALTHY_MIN_DAYS = 7  # strictly more than this → HEALTHY
LICENSE_WARN_MIN_DAYS = 4  # at or above this (but ≤ HEALTHY_MIN) → WARN
# Aspose `<SubscriptionExpiry>` numeric format is YYYYMMDD (8 digits).
ASPOSE_NUMERIC_DATE_LEN = 8


class LicenseManager:
    """Parse `.lic` XML for expiry. Stateless beyond holding the path."""

    def __init__(self, license_path: Path) -> None:
        self.license_path = license_path
        self._cached_expiry: date | None | _Sentinel = _UNREAD

    def refresh(self) -> None:
        """Re-read the license file from disk."""
        self._cached_expiry = _UNREAD

    def expiry_date(self) -> date | None:
        """Return the SubscriptionExpiry date, or None if not present."""
        if self._cached_expiry is _UNREAD:
            self._cached_expiry = _parse_expiry(self.license_path)
        return self._cached_expiry  # type: ignore[return-value]

    def days_remaining(self) -> int | None:
        expiry = self.expiry_date()
        if expiry is None:
            return None
        today = datetime.now(UTC).date()
        return (expiry - today).days

    def is_expired(self) -> bool:
        days = self.days_remaining()
        return days is not None and days < 0

    def state(self) -> LicenseState:
        days = self.days_remaining()
        return classify(days)


class _Sentinel:
    pass


_UNREAD = _Sentinel()


def classify(days_remaining: int | None) -> LicenseState:
    """Map days_remaining to a LicenseState per business-rules.md §4."""
    if days_remaining is None:
        return LicenseState.PERMANENT
    if days_remaining > LICENSE_HEALTHY_MIN_DAYS:
        return LicenseState.HEALTHY
    if days_remaining >= LICENSE_WARN_MIN_DAYS:
        return LicenseState.WARN
    if days_remaining >= 1:
        return LicenseState.CRITICAL
    if days_remaining == 0:
        return LicenseState.EXPIRING_TODAY
    return LicenseState.EXPIRED


def _parse_expiry(license_path: Path) -> date | None:
    """Parse the XML and return the SubscriptionExpiry date.

    Returns None if the license is well-formed but has no expiry field
    (a permanent license). Raises FileNotFoundError if missing,
    ET.ParseError if malformed.
    """
    text = license_path.read_text(encoding="utf-8")
    tree = ET.fromstring(text)

    # The Aspose XML license schema: <License><Data><SubscriptionExpiry>YYYYMMDD</...>
    for elem in tree.iter():
        local_name = elem.tag.rsplit("}", 1)[-1]
        if local_name == "SubscriptionExpiry" and elem.text:
            return _parse_aspose_date(elem.text.strip())
    return None


def _parse_aspose_date(raw: str) -> date:
    """Aspose dates are typically YYYYMMDD or YYYY-MM-DD."""
    raw = raw.strip()
    if len(raw) == ASPOSE_NUMERIC_DATE_LEN and raw.isdigit():
        return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
    return date.fromisoformat(raw)
