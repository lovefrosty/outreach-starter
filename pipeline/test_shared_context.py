#!/usr/bin/env python3
"""Focused tests for the Outreach shared context store."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shared_context


class SharedContextTests(unittest.TestCase):
    def test_missing_file_bootstraps_for_writers(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "context.json"
            doc = shared_context.load_context(path, bootstrap_missing=True)
            self.assertEqual(doc["schema_version"], shared_context.SCHEMA_VERSION)
            self.assertEqual(doc["research_signals"], [])

    def test_missing_file_fails_closed_for_readers(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(shared_context.SharedContextError):
                shared_context.operator_safe_snapshot(Path(td) / "missing.json")

    def test_malformed_and_unknown_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "context.json"
            path.write_text("{bad json", encoding="utf-8")
            with self.assertRaises(shared_context.SharedContextError):
                shared_context.load_context(path)
            path.write_text(json.dumps({"schema_version": "old"}), encoding="utf-8")
            with self.assertRaises(shared_context.SharedContextError):
                shared_context.load_context(path)

    def test_atomic_write_and_audit_append(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "context.json"
            shared_context.write_section(
                path,
                "operator_state",
                {
                    "telegram_safe_status": "review queue healthy",
                    "pending_approvals": ["2 leads"],
                    "delivery_summaries": [],
                    "live_action_blockers": [],
                    "updated_at": "",
                },
                writer="outreach",
                source_path="test",
                captured_at="2026-06-22T12:00:00Z",
            )
            doc = shared_context.load_context(path)
            self.assertEqual(doc["operator_state"]["telegram_safe_status"], "review queue healthy")
            self.assertEqual(doc["audit"]["last_writer"], "outreach")
            self.assertEqual(len(doc["audit"]["updates"]), 1)
            self.assertFalse(list(Path(td).glob("*.tmp")))

    def test_writer_section_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "context.json"
            with self.assertRaises(shared_context.SharedContextPermissionError):
                shared_context.write_section(path, "research_signals", [], writer="outreach")
            with self.assertRaises(shared_context.SharedContextPermissionError):
                shared_context.write_section(path, "operator_state", {}, writer="social_intent")


if __name__ == "__main__":
    unittest.main()
