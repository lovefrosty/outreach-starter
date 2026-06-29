#!/usr/bin/env python3

import copy
import json
import tempfile
import unittest
from pathlib import Path

import replacement_readiness


ROOT = Path(__file__).resolve().parents[2]


class ReplacementReadinessTests(unittest.TestCase):
    def setUp(self):
        self.requirements = replacement_readiness._read_json(replacement_readiness.DEFAULT_REQUIREMENTS)
        self.control = replacement_readiness._read_json(replacement_readiness.DEFAULT_CONTROL)
        self.eval_config = replacement_readiness._read_json(replacement_readiness.DEFAULT_EVAL_CONFIG)
        results = [
            {"id": item["id"], "status": "pass"}
            for item in self.eval_config["tests"]
        ]
        self.eval_state = {
            "run_id": "synthetic-verified-state",
            "verification_status": "passed",
            "results": copy.deepcopy(results),
            "verification_results": copy.deepcopy(results),
        }

    def evaluate(self, requirements=None, control=None, eval_config=None, eval_state=None):
        return replacement_readiness.evaluate(
            requirements or self.requirements,
            control or self.control,
            eval_config or self.eval_config,
            eval_state or self.eval_state,
            ROOT,
        )

    def test_all_requested_layers_are_offline_ready_but_production_is_blocked(self):
        report = self.evaluate()
        self.assertEqual(len(report["layers"]), 12)
        self.assertTrue(report["offline_shadow_ready"])
        self.assertFalse(report["production_replacement_ready"])
        self.assertEqual(report["decision"], "offline_shadow_ready_live_gates_blocked")
        self.assertTrue(all(layer["offline_shadow_ready"] for layer in report["layers"]))

    def test_missing_evidence_file_makes_offline_shadow_incomplete(self):
        requirements = copy.deepcopy(self.requirements)
        requirements["layers"][0]["evidence_files"].append("workspace/missing-proof.file")
        report = self.evaluate(requirements=requirements)
        self.assertFalse(report["offline_shadow_ready"])
        self.assertEqual(report["decision"], "offline_shadow_incomplete")
        self.assertEqual(report["layers"][0]["missing_evidence_files"], ["workspace/missing-proof.file"])

    def test_unregistered_eval_makes_layer_incomplete(self):
        requirements = copy.deepcopy(self.requirements)
        requirements["layers"][0]["eval_ids"].append("not-registered")
        report = self.evaluate(requirements=requirements)
        self.assertEqual(report["layers"][0]["missing_eval_registrations"], ["not-registered"])
        self.assertFalse(report["offline_shadow_ready"])

    def test_failed_or_unverified_eval_makes_layer_incomplete(self):
        state = copy.deepcopy(self.eval_state)
        target = self.requirements["layers"][0]["eval_ids"][0]
        next(item for item in state["verification_results"] if item["id"] == target)["status"] = "fail"
        report = self.evaluate(eval_state=state)
        self.assertIn(target, report["layers"][0]["nonpassing_evals"])
        self.assertFalse(report["offline_shadow_ready"])

    def test_shadow_execution_flag_is_rejected(self):
        control = copy.deepcopy(self.control)
        control["execution"]["email_send_allowed"] = True
        with self.assertRaisesRegex(replacement_readiness.ReplacementReadinessError, "enables execution"):
            self.evaluate(control=control)

    def test_satisfied_production_gate_without_artifact_is_rejected(self):
        requirements = copy.deepcopy(self.requirements)
        requirements["production_gates"][0]["satisfied"] = True
        with self.assertRaisesRegex(replacement_readiness.ReplacementReadinessError, "without evidence"):
            self.evaluate(requirements=requirements)

    def test_one_evidenced_gate_cannot_make_production_ready(self):
        requirements = copy.deepcopy(self.requirements)
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / "approved.json"
            evidence.write_text("{}\n", encoding="utf-8")
            requirements["production_gates"][0]["satisfied"] = True
            requirements["production_gates"][0]["evidence_path"] = str(evidence)
            report = self.evaluate(requirements=requirements)
        self.assertTrue(report["production_gates"][0]["satisfied"])
        self.assertFalse(report["production_replacement_ready"])

    def test_all_actions_remain_disabled_in_readiness_output(self):
        report = self.evaluate()
        self.assertEqual(report["safety"], {
            "email_send_allowed": False,
            "provider_api_calls_allowed": False,
            "crm_mutation_allowed": False,
            "production_routing_change_allowed": False,
        })

    def test_pending_live_state_fails_closed(self):
        state = copy.deepcopy(self.eval_state)
        state["verification_status"] = "pending"
        state.pop("verification_results")
        report = self.evaluate(eval_state=state)
        self.assertFalse(report["offline_shadow_ready"])
        self.assertFalse(report["production_replacement_ready"])


if __name__ == "__main__":
    unittest.main()
