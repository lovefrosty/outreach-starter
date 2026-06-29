#!/usr/bin/env python3

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import reply_intelligence


ROOT = Path(__file__).resolve().parents[2]
EVENTS_PATH = ROOT / "workspace/campaigns/examples/reply-events.json"
CONTROL_PLANE_PATH = ROOT / "workspace/config/outbound_control_plane.json"


class ReplyIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        self.policy = reply_intelligence.load_policy()
        self.marketing = reply_intelligence.load_marketing_team()
        self.generated_at = datetime(2026, 6, 13, 12, 5, tzinfo=timezone.utc)

    def evaluate(self, payload=None):
        return reply_intelligence.evaluate(
            payload or self.payload,
            self.policy,
            self.marketing,
            generated_at=self.generated_at,
        )

    def test_metrics_distinguish_email_lead_and_ooo_adjusted_rates(self):
        report = self.evaluate()
        metrics = report["funnel_metrics"]
        self.assertEqual(report["input"]["delivered_outbound_messages"], 5)
        self.assertEqual(report["input"]["contacted_leads"], 4)
        self.assertEqual(metrics["emails_with_reply"], 4)
        self.assertEqual(metrics["leads_with_reply"], 3)
        self.assertEqual(metrics["per_email_reply_rate"], 0.8)
        self.assertEqual(metrics["per_lead_reply_rate"], 0.75)
        self.assertEqual(metrics["per_email_ooo_adjusted_reply_rate"], 0.6)
        self.assertEqual(metrics["per_lead_ooo_adjusted_reply_rate"], 0.75)
        self.assertEqual(metrics["per_lead_actionable_conversion_rate"], 0.5)
        self.assertEqual(metrics["per_lead_booking_conversion_rate"], 0.25)

    def test_ooo_extracts_return_date_and_never_authorizes_execution(self):
        report = self.evaluate()
        result = next(item for item in report["reply_results"] if item["event_id"] == "reply-001")
        self.assertEqual(result["classification"]["intent"], "out_of_office")
        self.assertEqual(result["proposal"]["subsequence"]["not_before_date"], "2026-06-17")
        self.assertFalse(result["proposal"]["automatic_send_allowed"])
        self.assertFalse(result["proposal"]["crm_mutation_allowed"])
        self.assertFalse(result["proposal"]["external_action_executed"])

    def test_positive_reply_routes_resource_to_human_review(self):
        report = self.evaluate()
        result = next(item for item in report["reply_results"] if item["event_id"] == "reply-002")
        self.assertEqual(result["classification"]["intent"], "positive_interest")
        self.assertEqual(result["proposal"]["status"], "pending_human")
        self.assertEqual(result["proposal"]["resource_id"], "statement-review-prep")
        self.assertTrue(result["proposal"]["human_review_required"])
        self.assertEqual(result["proposal"]["subsequence"]["status"], "proposed_only")

    def test_report_minimizes_raw_reply_content(self):
        report = self.evaluate()
        serialized = json.dumps(report)
        for event in self.payload["inbound_events"]:
            self.assertNotIn(event["body"], serialized)

    def test_unsubscribe_precedes_other_language_and_has_no_draft(self):
        report = self.evaluate()
        result = next(item for item in report["reply_results"] if item["event_id"] == "reply-004")
        self.assertEqual(result["classification"]["intent"], "unsubscribe")
        self.assertEqual(result["proposal"]["status"], "review_only")
        self.assertEqual(result["proposal"]["resource_id"], "none")
        self.assertEqual(result["proposal"]["proposed_action"], "suppress")

    def test_multiple_replies_do_not_inflate_email_or_lead_conversion(self):
        report = self.evaluate()
        variant_b = next(
            item for item in report["template_variants"]
            if item["template_variant_id"] == "restaurant-ops-b"
        )
        self.assertEqual(variant_b["delivered_emails"], 3)
        self.assertEqual(variant_b["contacted_leads"], 3)
        self.assertEqual(variant_b["emails_with_reply"], 2)
        self.assertEqual(variant_b["leads_with_reply"], 2)
        self.assertEqual(variant_b["per_email_reply_rate"], 0.666667)

    def test_exact_duplicate_event_is_idempotent(self):
        payload = copy.deepcopy(self.payload)
        payload["inbound_events"].append(copy.deepcopy(payload["inbound_events"][0]))
        report = self.evaluate(payload)
        self.assertEqual(report["input"]["unique_inbound_events"], 5)

    def test_conflicting_provider_message_id_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        duplicate = copy.deepcopy(payload["inbound_events"][0])
        duplicate["event_id"] = "reply-conflict"
        duplicate["body"] = "Different body"
        payload["inbound_events"].append(duplicate)
        with self.assertRaisesRegex(
            reply_intelligence.ReplyIntelligenceError,
            "conflicting duplicate provider_message_id",
        ):
            self.evaluate(payload)

    def test_unknown_outbound_attribution_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["inbound_events"][0]["in_reply_to_message_id"] = "unknown-message"
        with self.assertRaisesRegex(
            reply_intelligence.ReplyIntelligenceError,
            "unknown outbound message",
        ):
            self.evaluate(payload)

    def test_policy_cannot_enable_external_actions(self):
        policy = copy.deepcopy(self.policy)
        policy["safety"]["external_send_allowed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "unsafe-policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(
                reply_intelligence.ReplyIntelligenceError,
                "enables an external action",
            ):
                reply_intelligence.load_policy(path)

    def test_architecture_control_plane_is_non_executing(self):
        control = json.loads(CONTROL_PLANE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(control["status"], "architecture_only")
        self.assertFalse(control["execution"]["enabled"])
        self.assertFalse(control["execution"]["email_send_allowed"])
        self.assertFalse(control["replacement_gate"]["instantly_can_be_retired"])


if __name__ == "__main__":
    unittest.main()
