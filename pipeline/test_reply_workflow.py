#!/usr/bin/env python3

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import reply_workflow


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "workspace/campaigns/examples/reply-intelligence-report.json"


class ReplyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        self.playbooks = reply_workflow.load_playbooks()
        self.resources = reply_workflow.load_resources()
        self.generated_at = datetime(2026, 6, 13, 13, tzinfo=timezone.utc)

    def compile(self, report=None, playbooks=None, resources=None, generated_at=None):
        return reply_workflow.compile_workflows(
            report or self.report,
            playbooks or self.playbooks,
            resources or self.resources,
            generated_at=generated_at or self.generated_at,
        )

    def test_compiles_intent_specific_human_review_states(self):
        output = self.compile()
        states = {item["intent"]: item["state"] for item in output["workflows"]}
        self.assertEqual(states["out_of_office"], "defer_review_pending")
        self.assertEqual(states["positive_interest"], "draft_pending_human")
        self.assertEqual(states["booking"], "draft_pending_human")
        self.assertEqual(states["question"], "draft_pending_human")
        self.assertEqual(states["unsubscribe"], "suppression_review_pending")

    def test_assignment_is_stable_by_lead_and_experiment(self):
        experiment = next(
            item
            for item in self.playbooks["experiments"]
            if item["experiment_id"] == "positive-interest-response-v1"
        )
        first = reply_workflow.assign_variant(experiment, "lead-stable")
        second = reply_workflow.assign_variant(experiment, "lead-stable")
        self.assertEqual(first, second)
        self.assertEqual(first["assignment_unit"], "lead_id")
        self.assertNotEqual(first["assignment_key_sha256"], "lead-stable")

    def test_assignment_is_not_counted_as_exposure(self):
        output = self.compile()
        assigned = [
            item for item in output["workflows"] if item["experiment_assignment"]
        ]
        self.assertEqual(len(assigned), 3)
        self.assertTrue(all(item["learning"]["assignment_recordable"] for item in assigned))
        self.assertTrue(all(not item["learning"]["exposure_recordable"] for item in assigned))

    def test_ooo_step_preserves_return_date_without_scheduling(self):
        output = self.compile()
        workflow = next(item for item in output["workflows"] if item["intent"] == "out_of_office")
        self.assertTrue(
            all(step["not_before_date"] == "2026-06-17" for step in workflow["subsequence_steps"])
        )
        self.assertTrue(
            all(not step["automatic_execution_allowed"] for step in workflow["subsequence_steps"])
        )

    def test_resources_are_fingerprinted_preview_only_and_not_attachable(self):
        output = self.compile()
        workflow = next(item for item in output["workflows"] if item["intent"] == "question")
        resource = workflow["resource_preview"]
        self.assertEqual(resource["resource_id"], "payment-operations-faq")
        self.assertEqual(resource["availability"], "preview_only")
        self.assertEqual(len(resource["sha256"]), 64)
        self.assertFalse(resource["external_use_allowed"])
        self.assertFalse(resource["automatic_attachment_allowed"])

    def test_expired_resource_blocks_draft_review(self):
        resources = copy.deepcopy(self.resources)
        resources["statement-review-prep"]["expires_at"] = "2026-06-12T00:00:00Z"
        output = self.compile(resources=resources)
        states = [
            item["state"]
            for item in output["workflows"]
            if item["intent"] in {"booking", "positive_interest"}
        ]
        self.assertEqual(states, ["resource_review_blocked", "resource_review_blocked"])

    def test_resource_fingerprint_drift_is_rejected(self):
        registry = json.loads(
            (ROOT / "workspace/config/custom_resource_registry.json").read_text(encoding="utf-8")
        )
        registry["resources"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "resources.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaisesRegex(
                reply_workflow.ReplyWorkflowError,
                "fingerprint drift",
            ):
                reply_workflow.load_resources(path)

    def test_unsupported_resource_intent_mapping_is_rejected(self):
        playbooks = copy.deepcopy(self.playbooks)
        playbooks["playbooks"]["question"]["resource_id"] = "statement-review-prep"
        with self.assertRaisesRegex(
            reply_workflow.ReplyWorkflowError,
            "resource does not support intent",
        ):
            self.compile(playbooks=playbooks)

    def test_unsafe_playbook_policy_is_rejected(self):
        config = copy.deepcopy(self.playbooks)
        config["safety"]["external_send_allowed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "playbooks.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                reply_workflow.ReplyWorkflowError,
                "enable an external action",
            ):
                reply_workflow.load_playbooks(path)

    def test_output_cannot_send_schedule_attach_or_mutate(self):
        output = self.compile()
        self.assertFalse(output["safety"]["external_send_allowed"])
        self.assertFalse(output["safety"]["automatic_subsequence_allowed"])
        self.assertFalse(output["safety"]["automatic_scheduling_allowed"])
        self.assertFalse(output["safety"]["crm_mutation_allowed"])
        self.assertFalse(output["safety"]["resource_attachment_allowed"])
        self.assertTrue(all(not item["send_authorized"] for item in output["workflows"]))
        self.assertTrue(
            all(not item["external_action_executed"] for item in output["workflows"])
        )


if __name__ == "__main__":
    unittest.main()
