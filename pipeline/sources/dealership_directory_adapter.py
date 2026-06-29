#!/usr/bin/env python3
"""Dealership source-first adapter.

This adapter turns public dealer locators, dealer group pages, marketplace
listings, and state dealer-license rows into pre-C2 lead seeds. It never
scrapes dealership websites and never writes SQLite.
"""

from __future__ import annotations

from sources.base import SourceAdapter
from sources.source_first import clean, clean_list, evidence_note, seed_fields


SOURCE_PRIORITY = (
    "oem_dealer_locator",
    "dealer_group_page",
    "state_dealer_license",
    "dealer_association",
    "dealer_com_footprint",
    "dealeron_footprint",
    "sincro_footprint",
    "dealer_inspire_footprint",
    "cars_com_listing",
    "autotrader_listing",
    "cargurus_listing",
    "edmunds_listing",
)


def _source_type(row):
    value = clean(row.get("source_type") or row.get("source_kind"))
    return value if value in SOURCE_PRIORITY else "dealership_directory"


def _external_id(row):
    for key in ("external_id", "dealer_id", "license_number", "listing_id", "locator_id"):
        value = clean(row.get(key))
        if value:
            return value
    return ""


def _to_raw(row):
    company = clean(row.get("rooftop_name") or row.get("company") or row.get("name"))
    source_type = _source_type(row)
    source_url = clean(row.get("source_url"))
    external_id = _external_id(row)
    brand = clean(row.get("brand") or row.get("oem_brand"))
    group_owner = clean(row.get("group_owner") or row.get("dealer_group"))
    workflow_clues = clean_list(
        row.get("workflow_clues")
        or row.get("service_payment_clues")
        or row.get("payment_workflow_clues")
    )
    decision_signals = clean_list(
        row.get("decision_maker_signals")
        or row.get("staff_contact_pages")
        or row.get("staff_pages")
    )
    directory_clues = clean_list(
        row.get("directory_clues")
        or [
            row.get("platform"),
            row.get("staff_url"),
            row.get("contact_url"),
            row.get("service_url"),
        ]
    )
    raw = {
        "company": company,
        "website": clean(row.get("website")),
        "phone": clean(row.get("phone")),
        "address": clean(row.get("address")),
        "city_state": clean(row.get("city_state") or row.get("location")),
        "vertical": "dealership",
        "brand": brand,
        "group_owner": group_owner,
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
                "dealership",
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
                "brand": brand,
                "group": group_owner,
                "workflow_clues": workflow_clues,
                "decision_signals": decision_signals,
            },
        )
    ]
    return raw


class DealershipDirectoryAdapter(SourceAdapter):
    name = "dealership_directory"
    gets = (
        "dealer_seed_rows",
        "brand",
        "group_ownership",
        "dealer_platform_clues",
        "service_payment_workflow_clues",
        "decision_maker_signals",
    )

    def _fetch(self, params):
        rows = params.get("fixture_rows")
        if rows is None:
            rows = params.get("rows")
        if rows is None:
            raise RuntimeError(
                "DealershipDirectoryAdapter is input-driven for now; pass fixture_rows "
                "from OEM locators, dealer groups, marketplaces, license rosters, or associations."
            )
        limit = int(params.get("max") or len(rows))
        return [_to_raw(row) for row in rows[:limit] if clean(row.get("rooftop_name") or row.get("company") or row.get("name"))]
