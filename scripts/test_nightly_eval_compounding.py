#!/usr/bin/env python3
"""Tests for the deterministic eval-compounding state machine."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("nightly_eval_compounding.py")
SPEC = importlib.util.spec_from_file_location("nightly_eval_compounding", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EvalCompoundingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_config(self, first_exit=0, second_exit=1):
        config = {
            "schema_version": 1,
            "history_limit": 3,
            "state_path": str(self.root / "state.json"),
            "digest_path": str(self.root / "digest.md"),
            "state_doc_path": str(self.root / "STATE.md"),
            "delivery": {
                "target": "telegram",
                "destination": "Outreach operator chat",
                "mode": "bot_send_message",
            },
            "tests": [
                {
                    "id": "first",
                    "command": [sys.executable, "-c", f"raise SystemExit({first_exit})"],
                    "owner_skill": "workspace/skills/first.md",
                },
                {
                    "id": "second",
                    "command": [sys.executable, "-c", f"raise SystemExit({second_exit})"],
                    "owner_skill": "workspace/skills/second.md",
                },
            ],
        }
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def test_baseline_does_not_claim_transitions(self):
        self.write_config()
        state = MODULE.execute_run(self.config_path, self.root)

        self.assertEqual(state["transitions"]["newly_passed"], [])
        self.assertEqual(state["transitions"]["newly_failed"], [])
        self.assertEqual(state["transitions"]["added"], ["first", "second"])
        self.assertTrue((self.root / "digest.md").exists())
        state_doc = (self.root / "STATE.md").read_text()
        self.assertIn("Digest target: telegram", state_doc)
        self.assertIn("Digest delivery: pending", state_doc)

    def test_second_run_records_new_pass_and_new_failure(self):
        self.write_config(first_exit=0, second_exit=1)
        MODULE.execute_run(self.config_path, self.root)
        self.write_config(first_exit=1, second_exit=0)

        state = MODULE.execute_run(self.config_path, self.root)
        state = MODULE.verify_run(self.config_path, self.root)

        self.assertEqual(state["transitions"]["newly_passed"], ["second"])
        self.assertEqual(state["transitions"]["newly_failed"], ["first"])
        self.assertEqual(state["verification_status"], "passed")
        self.assertIn("`second`", (self.root / "STATE.md").read_text())
        self.assertIn("Pending evidence-based investigation", (self.root / "digest.md").read_text())

    def test_delivery_and_distillation_are_persisted(self):
        self.write_config(first_exit=1, second_exit=0)
        MODULE.execute_run(self.config_path, self.root)
        self.write_config(first_exit=0, second_exit=0)
        MODULE.execute_run(self.config_path, self.root)

        def add_distillation(state):
            result = next(item for item in state["results"] if item["id"] == "first")
            state["distillations"].append(
                {
                    "test_id": "first",
                    "skill_path": result["owner_skill"],
                    "note": "Use a bounded retry.",
                    "recorded_at": MODULE.utc_now(),
                }
            )

        MODULE.update_state(self.config_path, self.root, add_distillation)

        def mark_posted(state):
            state["delivery"] = {
                "status": "posted",
                "target": "telegram",
                "destination": "Outreach operator chat",
                "receipt": "entry-20260613T120000Z",
            }

        state = MODULE.update_state(self.config_path, self.root, mark_posted)
        saved = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["delivery"]["status"], "posted")
        self.assertEqual(saved["distillations"][0]["test_id"], "first")
        self.assertIn("entry-20260613T120000Z", (self.root / "STATE.md").read_text())

    def test_delivery_gate_requires_verification_and_transition_notes(self):
        self.write_config(first_exit=1, second_exit=0)
        MODULE.execute_run(self.config_path, self.root)
        self.write_config(first_exit=0, second_exit=1)
        state = MODULE.execute_run(self.config_path, self.root)

        self.assertEqual(state["transitions"]["newly_passed"], ["first"])
        self.assertEqual(state["transitions"]["newly_failed"], ["second"])
        self.assertEqual(state["verification_status"], "pending")
        with self.assertRaisesRegex(ValueError, "independent verification"):
            MODULE.mark_delivery_posted(state, "entry-1")

        state = MODULE.verify_run(self.config_path, self.root)
        with self.assertRaisesRegex(ValueError, "Missing distillations"):
            MODULE.mark_delivery_posted(state, "entry-1")

        state["distillations"] = [
            {
                "test_id": "first",
                "skill_path": "workspace/skills/first.md",
                "note": "Verified test pattern.",
            }
        ]
        with self.assertRaisesRegex(ValueError, "Missing investigations"):
            MODULE.mark_delivery_posted(state, "entry-1")

        state["investigations"] = [
            {"test_id": "second", "note": "Failure reproduced and bounded."}
        ]
        MODULE.mark_delivery_posted(state, "entry-1")
        self.assertEqual(state["delivery"]["status"], "posted")
        self.assertEqual(state["delivery"]["target"], "telegram")
        self.assertEqual(state["delivery"]["receipt"], "entry-1")

        state["delivery"] = {
            "status": "blocked",
            "target": "telegram",
            "reason": "coordination file unavailable",
        }
        self.assertIn("Blocker: coordination file unavailable", MODULE.build_digest(state))

    def test_telegram_delivery_records_real_message_id(self):
        self.write_config(first_exit=0, second_exit=0)
        state = MODULE.execute_run(self.config_path, self.root)
        state = MODULE.verify_run(self.config_path, self.root)

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"ok": True, "result": {"message_id": 321}}
        ).encode("utf-8")
        receipt = MODULE.post_telegram_delivery(
            state, "test-token", "test-chat", urlopen=mock.Mock(return_value=response)
        )

        self.assertEqual(receipt, "telegram-message:321")
        self.assertEqual(state["delivery"]["status"], "posted")
        self.assertEqual(state["delivery"]["target"], "telegram")

    def test_telegram_delivery_rejects_missing_message_id(self):
        self.write_config(first_exit=0, second_exit=0)
        state = MODULE.execute_run(self.config_path, self.root)
        state = MODULE.verify_run(self.config_path, self.root)

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"ok": True, "result": {}}
        ).encode("utf-8")
        with self.assertRaisesRegex(RuntimeError, "lacked a message_id"):
            MODULE.post_telegram_delivery(
                state, "test-token", "test-chat", urlopen=mock.Mock(return_value=response)
            )
        self.assertEqual(state["delivery"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
