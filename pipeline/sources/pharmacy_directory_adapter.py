#!/usr/bin/env python3
"""Pharmacy source-first adapter.

This adapter normalizes pharmacy board/license rows, NCPA-style directories,
Good Neighbor Pharmacy, Health Mart, CPESN, pharmacy associations, and local
directories into pre-C2 seeds. It never logs into protected systems and never
writes SQLite.
"""

from __future__ import annotations

from sources.base import SourceAdapter
from sources.source_first import clean, clean_list, evidence_note, seed_fields


SOURCE_PRIORITY = (
    "state_pharmacy_board",
    "nj_license_roster",
    "ncpa_directory",
    "good_neighbor_pharmacy",
    "health_mart",
    "cpesn",
    "pharmacy_association",
    "local_directory",
)


def _source_type(row):
    value = clean(row.get("source_type") or row.get("source_kind"))
    return value if value in SOURCE_PRIORITY else "pharmacy_directory"


def _external_id(row):
    for key in ("external_id", "license_number", "license_id", "npi", "profile_id", "listing_id"):
        value = clean(row.get(key))
        if value:
            return value
    return ""


def _is_outreach_status(row):
    status = clean(row.get("license_status") or row.get("status")).lower()
    if not status:
        return True
    return status.startswith(("active", "pending"))


def _to_raw(row):
    company = clean(row.get("pharmacy_name") or row.get("company") or row.get("name"))
    source_type = _source_type(row)
    source_url = clean(row.get("source_url"))
    external_id = _external_id(row)
    license_status = clean(row.get("license_status") or row.get("status"))
    pharmacy_type = clean(row.get("pharmacy_type") or row.get("type"))
    workflow_clues = clean_list(
        row.get("workflow_clues")
        or row.get("counter_workflow_clues")
        or row.get("inventory_payment_patient_data_signals")
        or [
            row.get("inventory_signal"),
            row.get("payment_signal"),
            row.get("signature_signal"),
            row.get("patient_data_signal"),
            row.get("compliance_signal"),
        ]
    )
    decision_signals = clean_list(
        row.get("decision_maker_signals")
        or row.get("owner_pharmacist_evidence")
        or row.get("pharmacist_evidence")
    )
    directory_clues = clean_list(
        row.get("directory_clues")
        or [row.get("profile_url"), row.get("association_url"), row.get("network_url")]
    )
    raw = {
        "company": company,
        "website": clean(row.get("website")),
        "phone": clean(row.get("phone")),
        "address": clean(row.get("address")),
        "city_state": clean(row.get("city_state") or row.get("location")),
        "vertical": "pharmacy",
        "owner_name": clean(row.get("owner_name") or row.get("pharmacist_name")),
        "directory_clues": directory_clues,
        "workflow_clues": workflow_clues,
        "decision_maker_signals": decision_signals,
    }
    raw.update(
        seed_fields(
            {**row, **raw},
            source_type,
            source_url,
            external_id,
            (
                "pharmacy",
                source_type,
                external_id,
                company,
                raw["phone"],
                raw["address"],
            ),
        )
    )
    raw["reviews"] = [
        evidence_note(
            source_url,
            source_type,
            external_id,
            row,
            {
                "license_status": license_status,
                "pharmacy_type": pharmacy_type,
                "workflow_clues": workflow_clues,
                "decision_signals": decision_signals,
            },
        )
    ]
    return raw


class PharmacyDirectoryAdapter(SourceAdapter):
    name = "pharmacy_directory"
    gets = (
        "pharmacy_seed_rows",
        "license_status",
        "pharmacy_type",
        "counter_workflow_clues",
        "inventory_payment_patient_data_signals",
        "owner_pharmacist_evidence",
    )

    def _fetch(self, params):
        rows = params.get("fixture_rows")
        if rows is None:
            rows = params.get("rows")
        if rows is None:
            raise RuntimeError(
                "PharmacyDirectoryAdapter is input-driven for now; pass fixture_rows "
                "from pharmacy boards, license rosters, NCPA, Good Neighbor, Health Mart, "
                "CPESN, associations, or local directories."
            )
        limit = int(params.get("max") or len(rows))
        output = []
        for row in rows:
            if len(output) >= limit:
                break
            if not _is_outreach_status(row):
                continue
            if clean(row.get("pharmacy_name") or row.get("company") or row.get("name")):
                output.append(_to_raw(row))
        return output
