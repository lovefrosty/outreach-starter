#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE))

import control_plane as cp  # noqa: E402


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.config = PIPELINE.parent / "config" / "orchestration.json"
        self.control = cp.ControlPlane(self.root, self.config)
        self.run = self.control.start_run("acme", "builder_iteration", "Improve scraper reliability")
        self.artifact = Path(self.temp.name) / "candidate.json"
        self.artifact.write_text(json.dumps({"change": "skip malformed URLs"}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _checkpoint(self):
        return self.control.create_checkpoint(
            self.run["run_id"],
            "promote_builder_candidate",
            "Promote scraper candidate",
            "Candidate prevents malformed discovered URLs from reaching the fetcher.",
            "A bad parser change could suppress valid pages.",
            "Promote only after fixtures and a shadow run pass.",
            "Restore the previous scraper module.",
            [str(self.artifact)],
            ["unit_tests", "shadow_run"],
        )

    def test_approval_requires_all_verifier_checks(self):
        checkpoint = self._checkpoint()
        with self.assertRaises(cp.ControlPlaneError):
            self.control.decide(
                self.run["run_id"], checkpoint["checkpoint_id"],
                "approve", "Operator", "Looks correct.",
            )
        self.control.record_check(
            self.run["run_id"], checkpoint["checkpoint_id"],
            "unit_tests", True, "All fixture tests passed.",
        )
        still_pending = self.control.load_checkpoint(self.run["run_id"], checkpoint["checkpoint_id"])
        self.assertEqual(still_pending["status"], "pending_verification")
        ready = self.control.record_check(
            self.run["run_id"], checkpoint["checkpoint_id"],
            "shadow_run", True, "No regression across the held-out sample.",
        )
        self.assertEqual(ready["status"], "pending_human")

    def test_stale_artifact_cannot_be_approved(self):
        checkpoint = self._checkpoint()
        for name in checkpoint["required_checks"]:
            self.control.record_check(
                self.run["run_id"], checkpoint["checkpoint_id"],
                name, True, f"{name} passed.",
            )
        self.artifact.write_text(json.dumps({"change": "different"}), encoding="utf-8")
        with self.assertRaisesRegex(cp.ControlPlaneError, "changed after review"):
            self.control.decide(
                self.run["run_id"], checkpoint["checkpoint_id"],
                "approve", "Operator", "Approve tested version.",
            )

    def test_decision_is_auditable_and_does_not_execute(self):
        checkpoint = self._checkpoint()
        for name in checkpoint["required_checks"]:
            self.control.record_check(
                self.run["run_id"], checkpoint["checkpoint_id"],
                name, True, f"{name} passed.",
            )
        decided = self.control.decide(
            self.run["run_id"], checkpoint["checkpoint_id"],
            "approve", "Operator", "Approved for a separate promotion step.",
        )
        self.assertEqual(decided["status"], "approved")
        self.assertTrue(decided["decision"]["artifact_fingerprint"])
        decisions = (self.root / "runs" / self.run["run_id"] / "decisions.jsonl").read_text()
        self.assertIn("Approved for a separate promotion step", decisions)
        dashboard = self.control.dashboard(self.run["run_id"])
        self.assertIn("Pending decisions: 0", dashboard)

    def test_stage_results_advance_the_manifest(self):
        result = self.control.record_stage(
            self.run["run_id"], "value_benchmark", "completed", "Approved benchmark.",
            metrics={"valid_email_yield": 0.18}, artifacts=[str(self.artifact)],
        )
        self.assertEqual(result["metrics"]["valid_email_yield"], 0.18)
        manifest = self.control.load_run(self.run["run_id"])
        self.assertEqual(manifest["current_stage"], "baseline_repeated_runs")

    def test_value_promotion_checkpoint_requires_eligible_comparison(self):
        comparison = Path(self.temp.name) / "comparison.json"
        comparison.write_text(json.dumps({
            "schema_version": "outreach.value-comparison.v1",
            "promotion_eligible": True,
            "failed_checks": [],
            "checks": [{"name": "critical_non_regression", "passed": True}],
            "baseline": {"strategy_id": "scout-only"},
            "candidate": {"strategy_id": "layered"},
            "economics": {
                "critical_catch_rate_delta": 0.5,
                "weighted_value_capture_delta": 0.4,
                "net_expected_value_delta_per_run_usd": 100.0,
            },
            "recommendation": "Eligible for human promotion review.",
            "remaining_risks": ["Held-out data may differ from production."],
        }), encoding="utf-8")
        checkpoint = self.control.create_value_promotion_checkpoint(
            self.run["run_id"], comparison, "Promote layered strategy",
            "Restore scout-only routing.", [str(self.artifact)],
        )
        self.assertEqual(checkpoint["kind"], "value_based_promotion")
        self.assertEqual(checkpoint["status"], "pending_human")
        self.assertIn("net expected-value delta/run", checkpoint["summary"])
        self.assertEqual(len(checkpoint["value_guardrails"]), 1)
        self.assertIn("Value guardrails: 1/1 passed", self.control.dashboard(self.run["run_id"]))

        rejected = json.loads(comparison.read_text())
        rejected["promotion_eligible"] = False
        rejected["failed_checks"] = ["critical_floor"]
        comparison.write_text(json.dumps(rejected), encoding="utf-8")
        with self.assertRaisesRegex(cp.ControlPlaneError, "not eligible"):
            self.control.create_value_promotion_checkpoint(
                self.run["run_id"], comparison, "Reject weak strategy",
                "No promotion.", [],
            )


if __name__ == "__main__":
    unittest.main()
