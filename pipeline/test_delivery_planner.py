#!/usr/bin/env python3

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import delivery_planner


ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH = ROOT / "workspace/campaigns/examples/delivery-plan-input.json"


class DeliveryPlannerTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
        self.registry = delivery_planner.load_registry()
        self.segmentation = delivery_planner.load_segmentation()
        self.plan_at = datetime(2026, 6, 13, 13, tzinfo=timezone.utc)

    def plan(self, payload=None, registry=None):
        return delivery_planner.plan_batch(
            payload or self.payload,
            registry or self.registry,
            self.segmentation,
        )

    def candidate(self, decision_id):
        return next(
            item for item in self.payload["candidates"] if item["decision_id"] == decision_id
        )

    def test_recipient_mailbox_and_gateway_dimensions_remain_separate(self):
        google = delivery_planner.classify_recipient(
            self.candidate("decision-001"), self.segmentation, self.plan_at
        )
        microsoft = delivery_planner.classify_recipient(
            self.candidate("decision-003"), self.segmentation, self.plan_at
        )
        proofpoint = delivery_planner.classify_recipient(
            self.candidate("decision-004"), self.segmentation, self.plan_at
        )
        self.assertEqual(google["mailbox_provider"], "google_workspace")
        self.assertEqual(google["security_gateway"], "none")
        self.assertEqual(microsoft["routing_segment"], "microsoft_365")
        self.assertEqual(proofpoint["mailbox_provider"], "unknown")
        self.assertEqual(proofpoint["security_gateway"], "proofpoint")
        self.assertEqual(proofpoint["routing_segment"], "proofpoint_gateway")

    def test_batch_rotates_across_domains_then_mailboxes(self):
        report = self.plan()
        selected = [
            item["selected_sender"]["sender_identity_id"]
            for item in report["decisions"][:4]
        ]
        self.assertEqual(
            selected,
            ["sender-east-1", "sender-west-1", "sender-east-2", "sender-west-1"],
        )
        self.assertEqual(report["summary"]["planned_shadow"], 4)
        self.assertEqual(report["summary"]["held"], 3)
        self.assertEqual(
            report["policy"]["rotation"],
            "highest_segment_affinity_then_lowest_domain_and_mailbox_utilization",
        )

    def test_suppression_missing_approval_and_stale_mx_hold(self):
        report = self.plan()
        decisions = {item["decision_id"]: item for item in report["decisions"]}
        self.assertEqual(decisions["decision-005"]["holds"], ["recipient_suppressed"])
        self.assertEqual(decisions["decision-006"]["holds"], ["recipient_mx_stale"])
        self.assertEqual(decisions["decision-007"]["holds"], ["human_approval_missing"])
        for decision_id in ("decision-005", "decision-006", "decision-007"):
            self.assertEqual(decisions[decision_id]["status"], "held")
            self.assertIsNone(decisions[decision_id]["selected_sender"])

    def test_unhealthy_high_affinity_identity_is_excluded_with_evidence(self):
        report = self.plan()
        first = report["decisions"][0]
        held = next(
            item
            for item in first["eligibility"]
            if item["sender_identity_id"] == "sender-held-1"
        )
        self.assertFalse(held["eligible"])
        self.assertIn("dns_not_ready", held["reasons"])
        self.assertIn("domain_paused", held["reasons"])
        self.assertIn("provider_unhealthy", held["reasons"])

    def test_exhausted_quotas_fail_closed(self):
        registry = copy.deepcopy(self.registry)
        for domain in registry["domains"]:
            domain["sent_today"] = domain["daily_quota"]
        payload = copy.deepcopy(self.payload)
        payload["candidates"] = [payload["candidates"][0]]
        report = self.plan(payload, registry)
        decision = report["decisions"][0]
        self.assertEqual(decision["status"], "held")
        self.assertEqual(decision["holds"], ["no_eligible_sender_identity"])
        healthy_rows = [
            item
            for item in decision["eligibility"]
            if item["sender_identity_id"] != "sender-held-1"
        ]
        self.assertTrue(
            all("domain_quota_exhausted" in item["reasons"] for item in healthy_rows)
        )

    def test_unknown_mx_is_held(self):
        payload = copy.deepcopy(self.payload)
        payload["candidates"] = [payload["candidates"][0]]
        payload["candidates"][0]["recipient_mx_hosts"] = []
        report = self.plan(payload)
        decision = report["decisions"][0]
        self.assertEqual(decision["recipient_segment"]["status"], "unknown")
        self.assertEqual(decision["holds"], ["recipient_segment_unknown"])

    def test_unapproved_campaign_route_is_held(self):
        payload = copy.deepcopy(self.payload)
        payload["candidates"] = [payload["candidates"][0]]
        payload["candidates"][0]["campaign_route"] = "general_standard"
        report = self.plan(payload)
        self.assertEqual(report["decisions"][0]["holds"], ["route_unapproved"])

    def test_plan_is_deterministic_for_same_snapshot(self):
        first = self.plan()
        second = self.plan()
        self.assertEqual(first["decisions"], second["decisions"])
        self.assertEqual(first["simulated_usage"], second["simulated_usage"])

    def test_conflicting_duplicate_decision_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        duplicate = copy.deepcopy(payload["candidates"][0])
        duplicate["lead_id"] = "different-lead"
        payload["candidates"].append(duplicate)
        with self.assertRaisesRegex(
            delivery_planner.DeliveryPlannerError,
            "conflicting duplicate decision_id",
        ):
            self.plan(payload)

    def test_secret_bearing_registry_key_is_rejected(self):
        registry = copy.deepcopy(self.registry)
        registry["service_groups"][0]["api_key"] = "do-not-store-secrets-here"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "registry.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaisesRegex(
                delivery_planner.DeliveryPlannerError,
                "secret-bearing key is forbidden",
            ):
                delivery_planner.load_registry(path)

    def test_output_cannot_authorize_or_execute_send(self):
        report = self.plan()
        self.assertFalse(report["safety"]["external_send_allowed"])
        self.assertFalse(report["safety"]["provider_api_allowed"])
        self.assertFalse(report["safety"]["dns_lookup_allowed"])
        self.assertTrue(report["safety"]["usage_updates_are_simulated_only"])
        self.assertTrue(all(not item["send_authorized"] for item in report["decisions"]))
        self.assertTrue(
            all(not item["external_action_executed"] for item in report["decisions"])
        )


if __name__ == "__main__":
    unittest.main()
