#!/usr/bin/env python3
"""Tests for the Campaign Genome human promotion-review boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import campaign_promotion_review
import campaign_studio
import control_plane


def sample_payload():
    return {
        "campaign_id": "acme-review-001",
        "brand_id": "acme",
        "style_id": "operational-xray",
        "audience": "Independent restaurant operators",
        "objective": "Prepare a source-backed conversation",
        "offer": "Statement and workflow review",
        "research_records": [
            {
                "company": "Example Restaurant",
                "website": "https://example.com",
                "source_urls": ["https://example.com/pay"],
                "processor": "Toast",
                "tech_signals": ["Toast"],
                "field_sources": {
                    "processor": ["https://example.com"],
                    "tech_signals": {"Toast": ["https://example.com"]},
                },
            }
        ],
    }


class CampaignPromotionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.brief = campaign_studio.compile_campaign(sample_payload())
        self.manifest = self.root / "brief.json"
        self.manifest.write_text(json.dumps(self.brief), encoding="utf-8")
        self.sidecar_path = self.root / "sidecar.json"

    def tearDown(self):
        self.temp.cleanup()

    def _write_sidecar(self, mapped=True):
        kwargs = {}
        if mapped:
            kwargs = {
                "email_template_key": "Restaurant2",
                "email_sequence": "restaurant_default",
            }
        sidecar = campaign_studio.build_draft_sidecar(
            self.brief,
            "example-restaurant-001",
            self.manifest,
            **kwargs,
        )
        self.sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        return sidecar

    def test_packet_is_scoped_and_cannot_authorize_external_actions(self):
        self._write_sidecar()
        packet = campaign_promotion_review.build_review_packet(self.sidecar_path)

        self.assertEqual(packet["status"], "pending_human")
        self.assertEqual(packet["requested_change"]["fields"]["template_key"], "Restaurant2")
        self.assertFalse(packet["requested_change"]["executor_implemented"])
        self.assertIn("email_send_or_queue", packet["does_not_authorize"])
        self.assertIn("live_metadata_write", packet["does_not_authorize"])
        self.assertEqual(
            packet["live_send_gate_compatibility"]["status"],
            "not_evaluated_review_only",
        )
        compatibility = packet["live_send_gate_compatibility"]
        self.assertFalse(compatibility["send_ready"])
        self.assertEqual(compatibility["approval_signal"]["approved_value"], "queued")
        self.assertTrue(compatibility["approval_signal"]["does_not_send"])
        self.assertEqual(
            compatibility["required_at_execution"]["non_dry_run_enabler"],
            {
                "environment_variable": "PIPELINE_SENDING_ENABLED",
                "operator": "exact_string_equal",
                "value": "1",
            },
        )
        self.assertFalse(compatibility["legacy_gates_policy"]["runtime_enforced"])
        self.assertNotIn("qa_passed", compatibility["required_at_execution"])
        self.assertNotIn("human_approval_logged", compatibility["required_at_execution"])
        decision = packet["human_decision_interface"]
        self.assertEqual(decision["scope"], "one_business_one_mapping")
        self.assertEqual(decision["target_business_id"], "example-restaurant-001")
        self.assertEqual(decision["delivery_surface"], "local_file_checkpoint")
        self.assertFalse(decision["batch_approval_allowed"])
        self.assertFalse(decision["telegram_mutation_allowed"])
        self.assertTrue(decision["approval_and_send_are_separate"])
        self.assertTrue(decision["fresh_state_recheck_required_before_execution"])
        self.assertEqual(
            decision["allowed_decisions"],
            ["approve_metadata_only", "request_changes", "reject"],
        )
        live_surface = packet["live_human_approval_surface"]
        self.assertEqual(live_surface["status"], "blocked_security_review")
        self.assertFalse(live_surface["campaign_genome_integration_allowed"])
        blocker_ids = {
            blocker["id"] for blocker in live_surface["observed_security_blockers"]
        }
        self.assertIn("telegram_operator_allowlist_missing", blocker_ids)
        self.assertIn("telegram_callback_origin_unverified", blocker_ids)
        self.assertIn("telegram_allsend_unbounded", blocker_ids)
        self.assertIn("telegram_state_mutation", packet["does_not_authorize"])
        automation = packet["live_automation_boundary"]
        self.assertEqual(automation["status"], "verified_no_automatic_queue_or_send")
        self.assertEqual(automation["highest_automated_output_stage"], "personalized")
        self.assertFalse(automation["orchestrator_can_target_queued"])
        self.assertFalse(automation["orchestrator_imports_c7"])
        self.assertFalse(automation["scheduled_pipeline_invokes_c7"])
        self.assertFalse(automation["cron_invokes_c7"])
        self.assertEqual(
            set(automation["queued_writers"]),
            {"outreach_bot.appr_send", "outreach_bot.appr_allsend"},
        )
        self.assertTrue(automation["known_redeployment_risks"])

    def test_unmapped_or_tampered_sidecar_fails_closed(self):
        self._write_sidecar(mapped=False)
        with self.assertRaisesRegex(
            campaign_promotion_review.CampaignPromotionReviewError,
            "must be proposed",
        ):
            campaign_promotion_review.build_review_packet(self.sidecar_path)

        sidecar = self._write_sidecar()
        sidecar["email_mapping"]["eligible_routes"] = []
        self.sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        with self.assertRaisesRegex(
            campaign_promotion_review.CampaignPromotionReviewError,
            "eligible routes",
        ):
            campaign_promotion_review.build_review_packet(self.sidecar_path)

    def test_checkpoint_is_pending_human_and_does_not_decide(self):
        self._write_sidecar()
        control_root = self.root / "control"
        config = campaign_studio.REPO_ROOT / "workspace/config/orchestration.json"
        control = control_plane.ControlPlane(control_root, config)
        run = control.start_run("acme", "creative_campaign", "Review a draft mapping")
        packet_path = self.root / "review-packet.json"

        checkpoint = campaign_promotion_review.create_checkpoint(
            packet_path,
            self.sidecar_path,
            control_root,
            config,
            run["run_id"],
        )

        self.assertEqual(checkpoint["kind"], "campaign_email_metadata_promotion")
        self.assertEqual(checkpoint["status"], "pending_human")
        self.assertIsNone(checkpoint["decision"])
        self.assertIn("single-business metadata review", checkpoint["risk"])
        self.assertEqual(len(checkpoint["artifacts"]), 5)
        decisions = control_root / "runs" / run["run_id"] / "decisions.jsonl"
        self.assertFalse(decisions.exists())

    def test_malformed_or_legacy_live_contract_fails_closed(self):
        self._write_sidecar()
        contract_path = self.root / "live-send-contract.json"
        contract = campaign_promotion_review.load_live_send_contract()
        contract["legacy_gates_policy"]["runtime_enforced"] = True
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        with self.assertRaisesRegex(
            campaign_promotion_review.CampaignPromotionReviewError,
            "incorrectly marked as live",
        ):
            campaign_promotion_review.build_review_packet(
                self.sidecar_path,
                send_contract_path=contract_path,
            )

    def test_live_approval_contract_cannot_hide_security_blockers(self):
        self._write_sidecar()
        contract_path = self.root / "live-send-contract.json"
        contract = campaign_promotion_review.load_live_send_contract()
        contract["human_approval"]["campaign_genome_integration_allowed"] = True
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        with self.assertRaisesRegex(
            campaign_promotion_review.CampaignPromotionReviewError,
            "unsafe live approval integration",
        ):
            campaign_promotion_review.build_review_packet(
                self.sidecar_path,
                send_contract_path=contract_path,
            )

    def test_contract_cannot_reintroduce_automated_queue_or_send(self):
        self._write_sidecar()
        contract_path = self.root / "live-send-contract.json"
        contract = campaign_promotion_review.load_live_send_contract()
        contract["automation_boundary"]["orchestrator_can_target_queued"] = True
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

        with self.assertRaisesRegex(
            campaign_promotion_review.CampaignPromotionReviewError,
            "automated queue/send boundary",
        ):
            campaign_promotion_review.build_review_packet(
                self.sidecar_path,
                send_contract_path=contract_path,
            )


if __name__ == "__main__":
    unittest.main()
