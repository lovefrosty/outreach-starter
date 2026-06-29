#!/usr/bin/env python3
"""Focused safety and reproducibility tests for campaign_studio.py."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import campaign_studio
import jsonschema


def sample_payload():
    return {
        "campaign_id": "acme-restaurant-ops-001",
        "brand_id": "acme",
        "style_id": "operational-xray",
        "audience": "Owner-operators of independent South Florida restaurants",
        "objective": "Earn a qualified conversation about payment operations",
        "offer": "A no-pressure statement and workflow review",
        "research_records": [
            {
                "company": "Example Restaurant",
                "website": "https://example.com",
                "source_urls": ["https://example.com/pay"],
                "processor": "Toast",
                "tech_signals": [
                    "Toast",
                    "restaurant_vertical_stack",
                    "third_party_ordering",
                    "payment_link"
                ],
                "field_sources": {
                    "processor": ["https://example.com"],
                    "tech_signals": {
                        "Toast": ["https://example.com"],
                        "restaurant_vertical_stack": ["https://example.com"],
                        "third_party_ordering": ["https://example.com/pay"],
                        "payment_link": ["https://example.com/pay"]
                    }
                }
            }
        ]
    }


class CampaignStudioTests(unittest.TestCase):
    def test_compiles_source_backed_draft_for_higgsfield(self):
        brief = campaign_studio.compile_campaign(sample_payload())

        self.assertEqual(brief["schema_version"], campaign_studio.SCHEMA_VERSION)
        self.assertEqual(brief["status"], "draft_pending_human")
        self.assertFalse(brief["render_manifest"]["ready_to_submit"])
        self.assertEqual(brief["render_manifest"]["provider"], "higgsfield")
        self.assertTrue(campaign_studio.verify_brief(brief)["passed"])
        self.assertEqual(len(brief["render_manifest"]["jobs"]), 3)

    def test_hypotheses_never_become_generation_claims(self):
        brief = campaign_studio.compile_campaign(sample_payload())
        hypotheses = [item["text"] for item in brief["research"]["hypotheses"]]
        prompts = "\n".join(item["prompt"] for item in brief["render_manifest"]["jobs"])

        self.assertTrue(hypotheses)
        self.assertFalse(any(item in prompts for item in hypotheses))
        self.assertNotIn("has manual internal workflows", prompts)
        self.assertNotIn("you will save", prompts.lower())
        self.assertIn("do not claim guaranteed savings", prompts.lower())

    def test_private_or_derived_fields_are_excluded_before_evidence_grading(self):
        payload = sample_payload()
        payload["research_records"][0].update(
            {
                "pain_theme": "The owner is overwhelmed by reconciliation",
                "internal_notes": "They are desperate to switch processors",
                "processing_volume": "$900,000 monthly",
            }
        )

        brief = campaign_studio.compile_campaign(payload)
        serialized = json.dumps(brief)
        exclusions = brief["research"]["input_exclusions"]

        self.assertEqual(
            exclusions,
            [
                {
                    "company": "Example Restaurant",
                    "fields": ["internal_notes", "pain_theme", "processing_volume"],
                    "values_retained": False,
                }
            ],
        )
        self.assertNotIn("overwhelmed", serialized)
        self.assertNotIn("desperate", serialized)
        self.assertNotIn("900,000", serialized)
        self.assertTrue(campaign_studio.verify_brief(brief)["passed"])

    def test_verifier_rejects_claim_lineage_or_fingerprint_tampering(self):
        brief = campaign_studio.compile_campaign(sample_payload())
        brief["research"]["safe_claims"][0]["sources"] = ["https://unrelated.example"]

        verification = campaign_studio.verify_brief(brief)

        self.assertFalse(verification["passed"])
        self.assertIn("campaign brief fingerprint is stale", verification["failures"])
        self.assertIn("research evidence fingerprint is stale", verification["failures"])
        self.assertIn("safe_claims item has invalid claim provenance", verification["failures"])

    def test_claims_use_only_the_field_sources_that_support_them(self):
        brief = campaign_studio.compile_campaign(sample_payload())
        claims = {item["text"]: item for item in brief["research"]["safe_claims"]}

        self.assertEqual(
            claims["your current Toast setup"]["sources"],
            ["https://example.com"],
        )
        self.assertEqual(
            claims["a public pay-online/payment-link path"]["sources"],
            ["https://example.com/pay"],
        )
        self.assertTrue(
            all(item["provenance_granularity"] == "field" for item in claims.values())
        )

    def test_compilation_is_content_reproducible_except_timestamps(self):
        first = campaign_studio.compile_campaign(sample_payload())
        second = campaign_studio.compile_campaign(sample_payload())

        for value in (first, second):
            value.pop("created_at")
            value.pop("brief_sha256")
        self.assertEqual(first, second)

    def test_requires_provenance_and_known_style(self):
        missing_source = sample_payload()
        missing_source["research_records"][0]["website"] = ""
        missing_source["research_records"][0]["source_urls"] = []
        with self.assertRaisesRegex(campaign_studio.CampaignStudioError, "source URLs"):
            campaign_studio.compile_campaign(missing_source)

        missing_field_source = sample_payload()
        missing_field_source["research_records"][0]["field_sources"]["tech_signals"].pop(
            "payment_link"
        )
        with self.assertRaisesRegex(campaign_studio.CampaignStudioError, "tech_signals.payment_link"):
            campaign_studio.compile_campaign(missing_field_source)

        unrelated_field_source = sample_payload()
        unrelated_field_source["research_records"][0]["field_sources"]["processor"] = [
            "https://unrelated.example"
        ]
        with self.assertRaisesRegex(campaign_studio.CampaignStudioError, "invalid field source"):
            campaign_studio.compile_campaign(unrelated_field_source)

        unknown_style = copy.deepcopy(sample_payload())
        unknown_style["style_id"] = "made-up-style"
        with self.assertRaisesRegex(campaign_studio.CampaignStudioError, "unknown campaign style"):
            campaign_studio.compile_campaign(unknown_style)

    def test_verifier_rejects_publish_or_spend_bypass(self):
        brief = campaign_studio.compile_campaign(sample_payload())
        brief["launch_policy"]["automatic_publish_allowed"] = True
        brief["launch_policy"]["automatic_spend_allowed"] = True

        verification = campaign_studio.verify_brief(brief)
        self.assertFalse(verification["passed"])
        self.assertIn("automatic publishing is enabled", verification["failures"])
        self.assertIn("automatic spend is enabled", verification["failures"])

    def test_email_is_one_part_of_a_shared_brand_and_reply_system(self):
        brief = campaign_studio.compile_campaign(sample_payload())
        channel_ids = {
            item["id"] for item in brief["brand_channel_system"]["channel_briefs"]
        }
        reply_system = brief["brand_channel_system"]["reply_system"]

        self.assertIn("email-entry-point", channel_ids)
        self.assertIn("landing-page-story", channel_ids)
        self.assertIn("founder-and-social-content", channel_ids)
        self.assertIn("objection", reply_system["intents"])
        self.assertIn("processor-comparison-questions", reply_system["resources"])
        self.assertFalse(reply_system["automatic_response_allowed"])
        self.assertGreaterEqual(len(brief["marketing_team"]["workstreams"]), 7)

    def test_sidecar_keeps_cross_channel_style_separate_from_email_template(self):
        brief = campaign_studio.compile_campaign(sample_payload())
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "brief.json"
            manifest_path.write_text(json.dumps(brief), encoding="utf-8")
            sidecar = campaign_studio.build_draft_sidecar(
                brief,
                business_id="lead-123",
                manifest_path=manifest_path,
                email_template_key="Restaurant2",
                email_sequence="restaurant_default",
            )

            self.assertEqual(sidecar["style"]["style_id"], "operational-xray")
            self.assertEqual(
                sidecar["email_mapping"]["email_template_key"],
                "Restaurant2",
            )
            self.assertEqual(
                sidecar["email_mapping"]["email_sequence"],
                "restaurant_default",
            )
            self.assertTrue(sidecar["email_mapping"]["promotable"])
            self.assertEqual(sidecar["email_mapping"]["status"], "proposed")
            self.assertFalse(sidecar["email_mapping"]["live_metadata_written"])
            self.assertTrue(campaign_studio.verify_sidecar(sidecar)["passed"])

            schema = json.loads(
                (campaign_studio.REPO_ROOT / "workspace/config/campaign_sidecar_schema.json")
                .read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(sidecar)

            sidecar["email_mapping"]["eligible_routes"] = [
                {"vertical": "pharmacy", "variant": "existing_default"}
            ]
            verification = campaign_studio.verify_sidecar(sidecar)
            self.assertFalse(verification["passed"])
            self.assertIn(
                "email eligible routes do not match captured router",
                verification["failures"],
            )

    def test_unmapped_sidecar_cannot_enable_live_actions(self):
        brief = campaign_studio.compile_campaign(sample_payload())
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "brief.json"
            manifest_path.write_text(json.dumps(brief), encoding="utf-8")
            sidecar = campaign_studio.build_draft_sidecar(
                brief,
                business_id="lead-456",
                manifest_path=manifest_path,
            )

            self.assertEqual(sidecar["email_mapping"]["status"], "unmapped")
            self.assertIsNone(sidecar["email_mapping"]["email_template_key"])
            self.assertIsNone(sidecar["email_mapping"]["email_sequence"])
            self.assertFalse(sidecar["email_mapping"]["promotable"])
            self.assertTrue(all(value is False for value in sidecar["safety"].values()))

            sidecar["safety"]["send_allowed"] = True
            verification = campaign_studio.verify_sidecar(sidecar)
            self.assertFalse(verification["passed"])
            self.assertIn(
                "sidecar enables prohibited action: send_allowed",
                verification["failures"],
            )

    def test_email_mapping_requires_a_router_emitted_approved_pair(self):
        brief = campaign_studio.compile_campaign(sample_payload())
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "brief.json"
            manifest_path.write_text(json.dumps(brief), encoding="utf-8")

            with self.assertRaisesRegex(campaign_studio.CampaignStudioError, "supplied together"):
                campaign_studio.build_draft_sidecar(
                    brief,
                    business_id="lead-789",
                    manifest_path=manifest_path,
                    email_template_key="Restaurant1",
                )

            with self.assertRaisesRegex(campaign_studio.CampaignStudioError, "not emitted"):
                campaign_studio.build_draft_sidecar(
                    brief,
                    business_id="lead-789",
                    manifest_path=manifest_path,
                    email_template_key="Restaurant1",
                    email_sequence="pharmacy_default",
                )

            with self.assertRaisesRegex(campaign_studio.CampaignStudioError, "unsupported_sequence"):
                campaign_studio.build_draft_sidecar(
                    brief,
                    business_id="lead-789",
                    manifest_path=manifest_path,
                    email_template_key="Standard1",
                    email_sequence="general_standard",
                )


if __name__ == "__main__":
    unittest.main()
