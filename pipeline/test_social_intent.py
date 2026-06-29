#!/usr/bin/env python3
"""Fixture tests for the research-only social intent lane."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import social_intent
import shared_context
from sources.social_intent_adapter import normalize_signal


class SocialIntentTests(unittest.TestCase):
    def test_normalizes_public_signal_into_research_evidence(self):
        signal = normalize_signal({
            "company": "Demo CPA",
            "source_url": "https://www.linkedin.com/company/demo-cpa/posts",
            "observed_text": "We are hiring and opening a new location this summer.",
            "locator": "post:demo",
            "captured_at": "2026-06-22T12:00:00Z",
        })
        self.assertEqual(signal["schema_version"], "outreach.social-intent-signal.v1")
        self.assertEqual(signal["source"]["kind"], "public_social")
        self.assertIn("hiring", signal["observed"]["detected_intents"])
        self.assertIn("new_location", signal["observed"]["detected_intents"])
        self.assertTrue(signal["evidence_refs"][0]["content_sha256"])
        self.assertTrue(any("Do not queue" in item for item in signal["do_not_do"]))

    def test_run_writes_research_only_sections(self):
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "signals.json"
            context_path = Path(td) / "context.json"
            input_path.write_text(json.dumps({
                "signals": [{
                    "company": "Demo Restaurant",
                    "source_url": "https://instagram.com/demo",
                    "observed_text": "Grand opening and pay online link now live.",
                    "locator": "profile bio",
                    "captured_at": "2026-06-22T12:00:00Z",
                }]
            }), encoding="utf-8")
            result = social_intent.run(input_path, context_path)
            doc = shared_context.load_context(context_path)
            self.assertEqual(result["signals_written"], 1)
            self.assertEqual(len(doc["research_signals"]), 1)
            self.assertIn("public social/web research signal", doc["handoff_summary"]["summary"])
            self.assertEqual(doc["operator_state"]["telegram_safe_status"], "")
            self.assertEqual(doc["audit"]["last_writer"], "social_intent")


if __name__ == "__main__":
    unittest.main()
