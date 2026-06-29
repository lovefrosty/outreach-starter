#!/usr/bin/env python3
"""Focused tests for Telegram operator nudges."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import operator_nudge


class OperatorNudgeTests(unittest.TestCase):
    def test_remembered_operator_chat_id_falls_back_to_newest_operator(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operator_chats.json"
            path.write_text(json.dumps({
                "operators": {
                    "a": {"chat_id": "111", "updated_at": "2026-06-27T01:00:00+00:00"},
                    "b": {"chat_id": "222", "updated_at": "2026-06-27T02:00:00+00:00"},
                }
            }), encoding="utf-8")
            self.assertEqual(operator_nudge.remembered_operator_chat_id(path), "222")

    def test_active_update_message_uses_health_and_brief(self):
        with mock.patch.object(operator_nudge, "heartbeat_health") as health, mock.patch.object(
            operator_nudge, "session_brief"
        ) as brief:
            health.snapshot.return_value = {"stage_counts": {}, "due_calls": 0, "upcoming_calls": 0, "sending_enabled": False, "issues": [], "next_best_action": "Review."}
            health.format_text.return_value = "Health block"
            brief.build_brief.return_value = "Brief block"
            text = operator_nudge.active_update_message("review")
            self.assertIn("Outreach proactive update", text)
            self.assertIn("Health block", text)
            self.assertIn("Brief block", text)


if __name__ == "__main__":
    unittest.main()
