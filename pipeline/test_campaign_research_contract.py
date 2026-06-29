#!/usr/bin/env python3
"""Offline tests for the Campaign Genome source provenance bridge."""

from __future__ import annotations

import copy
import unittest

import campaign_research_contract
import campaign_studio


CONTENT_SHA = "1" * 64
ARTIFACT_SHA = "2" * 64


def evidence(url, locator):
    return {"url": url, "locator": locator, "content_sha256": CONTENT_SHA}


def valid_envelope():
    return {
        "schema_version": "outreach.research-envelope.v1",
        "source": {
            "adapter": "places_direct",
            "source_url": "https://maps.example/place/abc",
            "external_id": "places/abc",
            "captured_at": "2026-06-13T09:00:00Z",
            "source_artifact_sha256": ARTIFACT_SHA,
        },
        "entity": {
            "company": "Example Restaurant",
            "website": "https://example.com",
        },
        "observed": {
            "processor": "Toast",
            "tech_signals": ["restaurant_vertical_stack", "payment_link"],
        },
        "evidence": {
            "entity.company": [evidence("https://maps.example/place/abc", "displayName.text")],
            "entity.website": [evidence("https://maps.example/place/abc", "websiteUri")],
            "observed.processor": [evidence("https://example.com", "html:fingerprint:toast")],
            "observed.tech_signals.restaurant_vertical_stack": [
                evidence("https://example.com", "html:stack:restaurant")
            ],
            "observed.tech_signals.payment_link": [
                evidence("https://example.com", "html:link:pay-online")
            ],
        },
    }


class CampaignResearchContractTests(unittest.TestCase):
    def test_explicit_envelope_compiles_into_campaign_studio(self):
        record = campaign_research_contract.compile_envelope(valid_envelope())
        payload = {
            "campaign_id": "research-contract-001",
            "brand_id": "acme",
            "style_id": "operational-xray",
            "audience": "Independent restaurant operators",
            "objective": "Prepare a source-backed conversation",
            "offer": "Statement and workflow review",
            "research_records": [record],
        }

        brief = campaign_studio.compile_campaign(payload)
        claims = {item["text"]: item for item in brief["research"]["safe_claims"]}

        self.assertTrue(campaign_studio.verify_brief(brief)["passed"])
        self.assertEqual(claims["your current Toast setup"]["sources"], ["https://example.com"])
        self.assertNotIn("external_id", record["research_envelope"])
        self.assertEqual(len(record["research_envelope"]["external_id_sha256"]), 64)

    def test_legacy_places_row_is_audit_only(self):
        audit = campaign_research_contract.audit_legacy_row(
            {
                "company": "Example Restaurant",
                "website": "https://example.com",
                "source": "places_direct",
                "processor": "Toast",
                "tech_signals": ["payment_link"],
            }
        )

        self.assertFalse(audit["eligible_for_campaign_research"])
        self.assertEqual(audit["eligible_action"], "audit_only")
        self.assertIn("adapter_key_is_not_source_provenance", audit["blockers"])
        self.assertIn("stable_external_id_missing", audit["blockers"])
        self.assertIn("field_level_provenance_missing", audit["blockers"])
        self.assertFalse(audit["values_reproduced"])

    def test_nj_review_note_is_not_promoted_to_structured_provenance(self):
        audit = campaign_research_contract.audit_legacy_row(
            {
                "company": "Example Pharmacy",
                "website": "",
                "source": "nj_license_roster",
                "reviews": [
                    "source=https://state.example/roster | license=123 | source_row_hash=abc"
                ],
            }
        )

        self.assertIn("free_text_provenance_not_accepted", audit["blockers"])
        self.assertIn("campaign_website_missing", audit["blockers"])
        self.assertIn("field_level_provenance_missing", audit["blockers"])

    def test_inferred_or_private_fields_are_rejected(self):
        envelope = valid_envelope()
        envelope["observed"]["pain_theme"] = "manual_workflows"

        with self.assertRaisesRegex(
            campaign_research_contract.ResearchContractError,
            "prohibited inferred/private fields",
        ):
            campaign_research_contract.compile_envelope(envelope)

    def test_missing_or_unapproved_field_evidence_fails_closed(self):
        missing = valid_envelope()
        missing["evidence"].pop("observed.tech_signals.payment_link")
        with self.assertRaisesRegex(
            campaign_research_contract.ResearchContractError,
            "requires evidence for `observed.tech_signals.payment_link`",
        ):
            campaign_research_contract.compile_envelope(missing)

        unrelated = copy.deepcopy(valid_envelope())
        unrelated["evidence"]["observed.processor"][0]["url"] = "https://unrelated.example"
        with self.assertRaisesRegex(
            campaign_research_contract.ResearchContractError,
            "unapproved URL",
        ):
            campaign_research_contract.compile_envelope(unrelated)


if __name__ == "__main__":
    unittest.main()
