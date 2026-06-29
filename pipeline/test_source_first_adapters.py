#!/usr/bin/env python3
"""Offline tests for source-first lead acquisition adapters."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = (
    PIPELINE_DIR.parent
    if (PIPELINE_DIR.parent / "config/source_first_source_map.json").exists()
    else PIPELINE_DIR.parent / "workspace"
)
sys.path.insert(0, str(PIPELINE_DIR))

from sources.dealership_directory_adapter import DealershipDirectoryAdapter  # noqa: E402
from sources.pharmacy_directory_adapter import PharmacyDirectoryAdapter  # noqa: E402
from sources.restaurant_directory_adapter import RestaurantDirectoryAdapter  # noqa: E402
import source_first_runner  # noqa: E402


class SourceFirstAdapterTests(unittest.TestCase):
    def test_dealership_adapter_preserves_source_first_seed_fields(self):
        rows = DealershipDirectoryAdapter().fetch(
            {
                "fixture_rows": [
                    {
                        "source_type": "oem_dealer_locator",
                        "source_url": "https://locator.example/dealers/123",
                        "dealer_id": "oem-123",
                        "rooftop_name": "Example Ford",
                        "brand": "Ford",
                        "dealer_group": "Example Auto Group",
                        "website": "https://exampleford.test",
                        "phone": "+1 201 555 0101",
                        "address": "100 Route 1",
                        "city_state": "Edison, NJ",
                        "staff_url": "https://exampleford.test/staff",
                        "service_payment_clues": ["online service scheduler"],
                        "decision_maker_signals": ["staff page lists general manager"],
                    }
                ]
            }
        )

        self.assertEqual(len(rows), 1)
        seed = rows[0]
        self.assertEqual(seed["vertical"], "dealership")
        self.assertEqual(seed["source"], "dealership_directory")
        self.assertEqual(seed["seed_stage"], "pulled_candidate")
        self.assertEqual(seed["seed_confidence"], "high")
        self.assertFalse(seed["automatic_lead_promotion_allowed"])
        self.assertIn("website", seed["promotion_evidence"])
        self.assertIn("phone", seed["promotion_evidence"])
        self.assertEqual(seed["brand"], "Ford")
        self.assertEqual(seed["group_owner"], "Example Auto Group")
        self.assertTrue(seed["dedupe_key"])
        self.assertIn("source_row_hash=", seed["reviews"][0])

    def test_no_website_dealership_seed_is_not_discarded_or_promoted(self):
        rows = DealershipDirectoryAdapter().fetch(
            {
                "fixture_rows": [
                    {
                        "source_type": "state_dealer_license",
                        "source_url": "https://state.example/dealers",
                        "license_number": "DL-999",
                        "company": "No Website Motors",
                        "phone": "+1 973 555 0199",
                        "address": "200 Market Street",
                        "city_state": "Newark, NJ",
                    }
                ]
            }
        )

        self.assertEqual(len(rows), 1)
        seed = rows[0]
        self.assertEqual(seed["website"], "")
        self.assertEqual(seed["seed_stage"], "seeded_research_needed")
        self.assertEqual(seed["seed_confidence"], "medium")
        self.assertFalse(seed["automatic_lead_promotion_allowed"])
        self.assertIn("phone", seed["promotion_evidence"])
        self.assertIn("address", seed["promotion_evidence"])

    def test_restaurant_adapter_keeps_ordering_and_operator_context_as_evidence(self):
        rows = RestaurantDirectoryAdapter().fetch(
            {
                "fixture_rows": [
                    {
                        "source_type": "toast_profile",
                        "source_url": "https://toast.example/restaurants/abc",
                        "profile_id": "toast-abc",
                        "restaurant_name": "Example Bistro",
                        "website": "https://examplebistro.test",
                        "phone": "+1 856 555 0133",
                        "address": "10 Main Street",
                        "city_state": "Collingswood, NJ",
                        "toast_url": "https://toast.example/example-bistro",
                        "pos_order_payment_clues": ["online ordering profile"],
                        "owner_operator_evidence": ["about page names operator"],
                        "social_links": ["https://instagram.com/examplebistro"],
                    }
                ]
            }
        )

        seed = rows[0]
        self.assertEqual(seed["vertical"], "restaurant")
        self.assertEqual(seed["seed_confidence"], "high")
        self.assertIn("online ordering profile", seed["workflow_clues"])
        self.assertIn("about page names operator", seed["decision_maker_signals"])
        self.assertEqual(seed["social_links"], ["https://instagram.com/examplebistro"])
        self.assertFalse(seed["automatic_lead_promotion_allowed"])

    def test_pharmacy_adapter_filters_inactive_and_keeps_license_seeds(self):
        rows = PharmacyDirectoryAdapter().fetch(
            {
                "fixture_rows": [
                    {
                        "source_type": "state_pharmacy_board",
                        "source_url": "https://state.example/pharmacies",
                        "license_number": "PH-123",
                        "pharmacy_name": "Example Family Pharmacy",
                        "license_status": "Active",
                        "pharmacy_type": "Community pharmacy",
                        "phone": "+1 609 555 0177",
                        "address": "50 Broad Street",
                        "city_state": "Trenton, NJ",
                        "counter_workflow_clues": ["immunization profile"],
                        "owner_pharmacist_evidence": ["license record names pharmacist in charge"],
                    },
                    {
                        "source_type": "state_pharmacy_board",
                        "source_url": "https://state.example/pharmacies",
                        "license_number": "PH-OLD",
                        "pharmacy_name": "Closed Pharmacy",
                        "license_status": "Expired",
                        "phone": "+1 609 555 0000",
                        "address": "1 Closed Street",
                    },
                ]
            }
        )

        self.assertEqual(len(rows), 1)
        seed = rows[0]
        self.assertEqual(seed["company"], "Example Family Pharmacy")
        self.assertEqual(seed["seed_stage"], "seeded_research_needed")
        self.assertEqual(seed["seed_confidence"], "medium")
        self.assertIn("official_license_id", seed["promotion_evidence"])
        self.assertIn("immunization profile", seed["workflow_clues"])
        self.assertFalse(seed["automatic_lead_promotion_allowed"])

    def test_source_map_names_first_three_adapters_and_keeps_safety_off(self):
        source_map = json.loads(
            (WORKSPACE_DIR / "config/source_first_source_map.json").read_text(
                encoding="utf-8"
            )
        )
        safety = source_map["safety"]
        self.assertFalse(safety["external_fetch_allowed"])
        self.assertFalse(safety["database_writes_allowed"])
        self.assertFalse(safety["automatic_lead_promotion_allowed"])
        self.assertFalse(safety["paid_enrichment_allowed"])
        self.assertEqual(
            [item["adapter"] for item in source_map["recommended_first_adapters"]],
            ["dealership_directory", "restaurant_directory", "pharmacy_directory"],
        )
        self.assertEqual(
            source_map["verticals"]["dealership"]["ranked_sources"][0]["source_type"],
            "oem_dealer_locator",
        )

    def test_no_send_runner_compiles_seed_batch_without_external_actions(self):
        payload = {
            "schema_version": "outreach.source-first-input.v1",
            "rows": [
                {
                    "source_type": "google_places",
                    "source_url": "https://maps.example/place/abc",
                    "place_id": "place-abc",
                    "restaurant_name": "Runner Cafe",
                    "phone": "+1 201 555 0188",
                    "address": "1 Runner Ave",
                    "city_state": "Hoboken, NJ",
                }
            ],
        }

        report = source_first_runner.compile_seed_batch(payload, "restaurant_directory")

        self.assertEqual(report["schema_version"], "outreach.source-first-seed-batch.v1")
        self.assertEqual(report["mode"], "no_send_source_first")
        self.assertFalse(report["safety"]["external_fetch_allowed"])
        self.assertFalse(report["safety"]["database_writes_allowed"])
        self.assertFalse(report["safety"]["automatic_lead_promotion_allowed"])
        self.assertEqual(report["summary"]["seed_rows"], 1)
        self.assertEqual(
            report["summary"]["seed_stages"],
            {"seeded_research_needed": 1},
        )
        self.assertEqual(report["records"][0]["seed_confidence"], "high")

    def test_no_send_runner_rejects_unsafe_source_map(self):
        payload = {"schema_version": "outreach.source-first-input.v1", "rows": []}
        unsafe = json.loads(
            (WORKSPACE_DIR / "config/source_first_source_map.json").read_text(
                encoding="utf-8"
            )
        )
        unsafe["safety"]["external_fetch_allowed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "unsafe.json"
            path.write_text(json.dumps(unsafe), encoding="utf-8")
            with self.assertRaisesRegex(
                source_first_runner.SourceFirstRunnerError,
                "enables an external or unsafe action",
            ):
                source_first_runner.compile_seed_batch(
                    payload,
                    "restaurant_directory",
                    source_map=path,
                )


if __name__ == "__main__":
    unittest.main()
