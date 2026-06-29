#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
WORKSPACE = PIPELINE.parent
sys.path.insert(0, str(PIPELINE))

import value_evaluation as ve  # noqa: E402


class ValueEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = ve.load_json(WORKSPACE / "config" / "value_evaluation.json")
        cls.benchmark = ve.load_json(WORKSPACE / "evals" / "layered-agent-benchmark.json")
        cls.scout = ve.evaluate(
            cls.benchmark,
            ve.load_json(WORKSPACE / "evals" / "scout-only-example.json"),
            cls.policy,
        )
        cls.layered = ve.evaluate(
            cls.benchmark,
            ve.load_json(WORKSPACE / "evals" / "layered-example.json"),
            cls.policy,
        )

    def test_expensive_strategy_can_be_cheaper_for_priority_finding(self):
        scout_priority = self.scout["finding_metrics"][0]
        layered_priority = self.layered["finding_metrics"][0]
        self.assertGreater(
            self.layered["summary"]["median_model_cost_per_run_usd"],
            self.scout["summary"]["median_model_cost_per_run_usd"],
        )
        self.assertLess(
            layered_priority["expected_spend_to_surface_once_usd"],
            scout_priority["expected_spend_to_surface_once_usd"],
        )

    def test_layering_improves_weighted_value_and_critical_recall(self):
        self.assertGreater(
            self.layered["summary"]["weighted_value_capture_rate"],
            self.scout["summary"]["weighted_value_capture_rate"],
        )
        self.assertGreaterEqual(self.layered["summary"]["critical_catch_rate"], 0.8)

    def test_promotion_policy_uses_value_and_quality_guardrails(self):
        comparison = ve.compare(self.scout, self.layered, self.policy)
        self.assertTrue(comparison["promotion_eligible"])
        self.assertGreater(comparison["economics"]["model_cost_ratio"], 1)
        self.assertGreater(comparison["economics"]["net_expected_value_delta_per_run_usd"], 0)

    def test_cheaper_strategy_cannot_pass_with_critical_regression(self):
        comparison = ve.compare(self.layered, self.scout, self.policy)
        self.assertFalse(comparison["promotion_eligible"])
        self.assertIn("critical_floor", comparison["failed_checks"])
        self.assertIn("critical_non_regression", comparison["failed_checks"])

    def test_economic_router_escalates_high_value_contradictions(self):
        decision = ve.escalation_decision(
            self.policy,
            "cross_source_contradiction",
            ["conflicting_limits"],
        )
        self.assertEqual(decision["decision"], "escalate")
        self.assertGreater(decision["expected_value_to_cost_ratio"], 1)

    def test_false_finding_is_counted_without_trusting_model_status(self):
        self.assertGreater(self.scout["summary"]["false_positive_rate"], 0)


if __name__ == "__main__":
    unittest.main()
