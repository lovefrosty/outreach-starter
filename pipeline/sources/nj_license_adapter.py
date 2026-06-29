#!/usr/bin/env python3
"""
NJ license roster source adapter.

Purpose:
    Create seed rows from official NJ Division of Consumer Affairs business
    license roster data without mixing filing/license logic into C2.

Inputs:
    - roster_path=<csv|tsv>: parses a downloaded roster export.
    - source_url=<url>: records which public source produced the roster.
    - fixture_rows=[dict...]: test-only rows supplied by the test harness.
    - allowed_statuses=[...]: defaults to Active/Pending-style rows only.

This adapter is pure: it returns normalized rows through SourceAdapter and never
writes SQLite directly.

Important:
    This is a seed adapter, not a C2-ready website source. Most roster rows have
    no website/domain, so downstream code should put them into a seed/resolver
    lane before promotion to `pulled`.
"""

import csv
import hashlib
import json
from pathlib import Path

from sources.base import SourceAdapter


OFFICIAL_SOURCE_URL = "https://newjersey.mylicense.com/Verification_Bulk/Search.aspx?facility=Y"
DEFAULT_ALLOWED_STATUS_PREFIXES = ("active", "pending")


def _clean(value):
    return str(value or "").strip()


def _field(row, names):
    """Return the first matching column value across possible roster headers."""
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return _clean(value)
    return ""


def _is_pharmacy(row):
    """Identify pharmacy business rows from profession/license text."""
    text = " ".join(_clean(v).lower() for v in row.values())
    return "pharmacy" in text or "pharmac" in text


def _status_allowed(status, allowed_prefixes=DEFAULT_ALLOWED_STATUS_PREFIXES):
    """
    Keep active or pending-style rows by default.

    The official bulk page exposes many statuses. For lead sourcing, closed,
    retired, revoked, dissolved, and expired licenses should not become active
    outreach seeds.
    """
    value = _clean(status).lower()
    prefixes = tuple(_clean(v).lower() for v in allowed_prefixes if _clean(v))
    if not prefixes:
        return True
    return any(value.startswith(prefix) for prefix in prefixes)


def _city_state(row):
    """Normalize city/state columns into one operator-readable location."""
    city = _field(row, ("city", "business city", "licensee city"))
    state = _field(row, ("state", "business state", "licensee state")) or "NJ"
    combined = _field(row, ("city_state", "city state", "location"))
    if city:
        return f"{city}, {state}"
    return combined


def _source_note(row, source_url, license_number, license_type, status):
    """Create compact evidence text that survives normalization."""
    parts = [f"source={source_url}"]
    if license_number:
        parts.append(f"license={license_number}")
    if license_type:
        parts.append(f"type={license_type}")
    if status:
        parts.append(f"status={status}")
    email = _field(row, ("email", "business email", "licensee email"))
    if email:
        parts.append(f"email={email}")
    address = _field(row, (
        "address", "business address", "licensee address", "street address",
    ))
    if address:
        parts.append(f"address={address}")
    parts.append(f"source_row_hash={_source_row_hash(row)}")
    return " | ".join(parts)


def _source_row_hash(row):
    """Stable hash for dedupe/debugging across downloaded roster snapshots."""
    payload = json.dumps(
        {str(k).strip().lower(): _clean(v) for k, v in row.items()},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _to_raw(row, source_url=OFFICIAL_SOURCE_URL):
    """Convert one official roster row into the raw SourceAdapter schema."""
    company = _field(row, (
        "business name", "licensee name", "name", "company", "organization",
    ))
    license_type = _field(row, (
        "license type", "licensetype", "profession", "profession name",
    ))
    license_number = _field(row, (
        "license number", "licensenumber", "license no", "number",
    ))
    status = _field(row, ("status", "license status", "licensestatus"))

    return {
        "company": company,
        "website": "",
        "domain": "",
        "phone": _field(row, ("phone", "telephone", "business phone")),
        "city_state": _city_state(row),
        "vertical": "pharmacy",
        "rating": None,
        "review_count": None,
        "owner_name": "",
        "filing_date": "",
        "processor": "",
        "reviews": [_source_note(row, source_url, license_number, license_type, status)],
    }


def _read_roster(path, max_rows, source_url, allowed_statuses=DEFAULT_ALLOWED_STATUS_PREFIXES):
    """Parse a downloaded CSV/TSV/pipe roster and return pharmacy seed rows."""
    roster = Path(path).expanduser()
    text = roster.read_text(encoding="utf-8-sig", errors="replace")
    sample = text[:4096]
    dialect = csv.Sniffer().sniff(sample, delimiters=",\t|")
    rows = []
    for row in csv.DictReader(text.splitlines(), dialect=dialect):
        if not _is_pharmacy(row):
            continue
        status = _field(row, ("status", "license status", "licensestatus"))
        if not _status_allowed(status, allowed_statuses):
            continue
        raw = _to_raw(row, source_url)
        if raw["company"]:
            rows.append(raw)
        if len(rows) >= max_rows:
            break
    return rows


class NJLicenseRosterAdapter(SourceAdapter):
    name = "nj_license_roster"
    gets = ("license_roster", "seed_rows", "no_website")

    def _fetch(self, params):
        """Fetch fixture or downloaded roster rows and return raw seed rows."""
        max_rows = int(params.get("max") or 50)
        roster_path = params.get("roster_path") or self.config.get("roster_path")
        source_url = (
            params.get("source_url")
            or self.config.get("source_url")
            or OFFICIAL_SOURCE_URL
        )
        fixture_rows = params.get("fixture_rows")
        allowed_statuses = params.get("allowed_statuses") or self.config.get("allowed_statuses")
        if fixture_rows is not None:
            rows = []
            for row in fixture_rows:
                status = _field(row, ("status", "license status", "licensestatus"))
                if _status_allowed(status, allowed_statuses or DEFAULT_ALLOWED_STATUS_PREFIXES):
                    rows.append(_to_raw(row, source_url))
                if len(rows) >= max_rows:
                    break
            return rows
        if roster_path:
            return _read_roster(
                roster_path,
                max_rows,
                source_url,
                allowed_statuses or DEFAULT_ALLOWED_STATUS_PREFIXES,
            )
        raise RuntimeError(
            "NJLicenseRosterAdapter needs roster_path for live runs. "
            f"Download the official business roster from {source_url} "
            "or pass fixture_rows from a test harness."
        )
