#!/usr/bin/env python3
"""Restaurant source-first adapter.

This adapter normalizes public restaurant directories and profile pages into
pre-C2 seeds. It keeps Google/Places-style, reservation, delivery, association,
tourism, chamber, and local-list data out of C2.
"""

from __future__ import annotations

from sources.base import SourceAdapter
from sources.source_first import clean, clean_list, evidence_note, seed_fields


SOURCE_PRIORITY = (
    "google_places",
    "opentable",
    "resy",
    "toast_profile",
    "chownow",
    "slice",
    "doordash",
    "ubereats",
    "restaurant_association",
    "tourism_directory",
    "chamber_directory",
    "best_of_local_list",
)


def _source_type(row):
    value = clean(row.get("source_type") or row.get("source_kind"))
    return value if value in SOURCE_PRIORITY else "restaurant_directory"


def _external_id(row):
    for key in ("external_id", "place_id", "profile_id", "listing_id", "membership_id"):
        value = clean(row.get(key))
        if value:
            return value
    return ""


def _to_raw(row):
    company = clean(row.get("restaurant_name") or row.get("company") or row.get("name"))
    source_type = _source_type(row)
    source_url = clean(row.get("source_url"))
    external_id = _external_id(row)
    ordering_links = clean_list(
        row.get("ordering_links")
        or row.get("delivery_links")
        or [row.get("toast_url"), row.get("chownow_url"), row.get("slice_url")]
    )
    reservation_links = clean_list(
        row.get("reservation_links") or [row.get("opentable_url"), row.get("resy_url")]
    )
    social_links = clean_list(row.get("social_links"))
    workflow_clues = clean_list(
        row.get("workflow_clues")
        or row.get("pos_order_payment_clues")
        or [
            row.get("pos_clue"),
            row.get("delivery_signal"),
            row.get("reservation_signal"),
            row.get("catering_signal"),
        ]
    )
    directory_clues = clean_list(
        row.get("directory_clues")
        or ordering_links
        + reservation_links
        + [row.get("menu_url"), row.get("catering_url"), row.get("profile_url")]
    )
    decision_signals = clean_list(
        row.get("decision_maker_signals")
        or row.get("owner_operator_evidence")
        or row.get("operator_evidence")
    )
    raw = {
        "company": company,
        "website": clean(row.get("website")),
        "phone": clean(row.get("phone")),
        "address": clean(row.get("address")),
        "city_state": clean(row.get("city_state") or row.get("location")),
        "vertical": "restaurant",
        "rating": row.get("rating"),
        "review_count": row.get("review_count"),
        "owner_name": clean(row.get("owner_name") or row.get("operator_name")),
        "directory_clues": directory_clues,
        "workflow_clues": workflow_clues,
        "decision_maker_signals": decision_signals,
        "social_links": social_links,
    }
    raw.update(
        seed_fields(
            {**row, **raw},
            source_type,
            source_url,
            external_id,
            (
                "restaurant",
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
                "ordering": ordering_links,
                "reservations": reservation_links,
                "workflow_clues": workflow_clues,
                "decision_signals": decision_signals,
            },
        )
    ]
    return raw


class RestaurantDirectoryAdapter(SourceAdapter):
    name = "restaurant_directory"
    gets = (
        "restaurant_seed_rows",
        "places_profile",
        "reservation_delivery_ordering_clues",
        "pos_payment_clues",
        "multi_location_signals",
        "owner_operator_evidence",
        "social_links",
    )

    def _fetch(self, params):
        rows = params.get("fixture_rows")
        if rows is None:
            rows = params.get("rows")
        if rows is None:
            raise RuntimeError(
                "RestaurantDirectoryAdapter is input-driven for now; pass fixture_rows "
                "from Places-style exports, OpenTable/Resy profiles, delivery/order profiles, "
                "associations, tourism pages, chambers, or local lists."
            )
        limit = int(params.get("max") or len(rows))
        return [_to_raw(row) for row in rows[:limit] if clean(row.get("restaurant_name") or row.get("company") or row.get("name"))]
