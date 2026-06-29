#!/usr/bin/env python3
"""Dry-run proof for the NJ license roster adapter. No DB writes."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from sources.nj_license_adapter import NJLicenseRosterAdapter  # noqa: E402


FIXTURE_ROWS = [
    ("ABC PHARMACY", "Newark, NJ", "Pharmacy", "28RI03400100", "Active"),
    ("BAYONNE FAMILY PHARMACY", "Bayonne, NJ", "Pharmacy", "28RI03400200", "Active"),
    ("BERGEN CARE PHARMACY", "Hackensack, NJ", "Pharmacy", "28RI03400300", "Active"),
    ("BRICK TOWN PHARMACY", "Brick, NJ", "Pharmacy", "28RI03400400", "Active"),
    ("CAMDEN COMMUNITY PHARMACY", "Camden, NJ", "Pharmacy", "28RI03400500", "Active"),
    ("CLIFTON RX CENTER", "Clifton, NJ", "Pharmacy", "28RI03400600", "Active"),
    ("EAST ORANGE PHARMACY", "East Orange, NJ", "Pharmacy", "28RI03400700", "Active"),
    ("EDISON WELLNESS PHARMACY", "Edison, NJ", "Pharmacy", "28RI03400800", "Active"),
    ("ELIZABETH CARE PHARMACY", "Elizabeth, NJ", "Pharmacy", "28RI03400900", "Active"),
    ("FREEHOLD FAMILY RX", "Freehold, NJ", "Pharmacy", "28RI03401000", "Active"),
    ("HAMILTON SQUARE PHARMACY", "Hamilton, NJ", "Pharmacy", "28RI03401100", "Active"),
    ("HOBOKEN APOTHECARY", "Hoboken, NJ", "Pharmacy", "28RI03401200", "Active"),
    ("JERSEY CITY HEALTH PHARMACY", "Jersey City, NJ", "Pharmacy", "28RI03401300", "Active"),
    ("LAKEWOOD PHARMACY CARE", "Lakewood, NJ", "Pharmacy", "28RI03401400", "Active"),
    ("LINDEN FAMILY PHARMACY", "Linden, NJ", "Pharmacy", "28RI03401500", "Active"),
    ("MONTCLAIR VILLAGE PHARMACY", "Montclair, NJ", "Pharmacy", "28RI03401600", "Active"),
    ("NEW BRUNSWICK RX", "New Brunswick, NJ", "Pharmacy", "28RI03401700", "Active"),
    ("PATERSON CARE PHARMACY", "Paterson, NJ", "Pharmacy", "28RI03401800", "Active"),
    ("PRINCETON COMMUNITY PHARMACY", "Princeton, NJ", "Pharmacy", "28RI03401900", "Active"),
    ("TRENTON FAMILY PHARMACY", "Trenton, NJ", "Pharmacy", "28RI03402000", "Active"),
]


def fixture_dicts():
    rows = []
    for idx, (company, city_state, license_type, license_number, status) in enumerate(FIXTURE_ROWS, 1):
        rows.append({
            "business name": company,
            "city_state": city_state,
            "license type": license_type,
            "license number": license_number,
            "status": status,
            "email": f"owner{idx}@example-pharmacy.test",
            "address": f"{idx} Main St",
        })
    return rows


def main():
    adapter = NJLicenseRosterAdapter()
    source_url = "https://newjersey.mylicense.com/Verification_Bulk/"
    rows = adapter.fetch({
        "fixture_rows": fixture_dicts(),
        "source_url": source_url,
        "max": 20,
        "vertical": "pharmacy",
    })

    assert len(rows) >= 20, f"expected at least 20 rows, got {len(rows)}"
    for row in rows:
        assert row["source"] == "nj_license_roster"
        assert row["company"]
        assert row["vertical"] == "pharmacy"
        assert row["website"] == ""
        assert row["domain"] == ""
        assert row["reviews"]
        assert source_url in row["reviews"][0]
        assert "email=" in row["reviews"][0]
        assert "address=" in row["reviews"][0]

    print(f"rows={len(rows)}")
    print("db_writes=0")
    print("first_5=")
    print(json.dumps(rows[:5], indent=2))
    print("last_company=" + rows[-1]["company"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
