#!/usr/bin/env python3
"""Tests for offline live-contract source drift verification."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import live_contract_provenance


class LiveContractProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshots = {}
        sources = {}
        for key, content in {
            "sender": "sender source\n",
            "human_approval": "approval source\n",
            "legacy_policy": "legacy policy\n",
        }.items():
            path = self.root / f"{key}.txt"
            path.write_text(content, encoding="utf-8")
            self.snapshots[key] = str(path)
            sources[key] = {
                "path": f"/live/{key}.txt",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        self.contract = self.root / "contract.json"
        self.contract.write_text(
            json.dumps(
                {
                    "schema_version": "outreach.live-send-contract.v1",
                    "captured_at": "2026-06-13T00:00:00Z",
                    "sources": sources,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_matching_snapshot_passes_without_changing_action_eligibility(self):
        report = live_contract_provenance.verify(self.contract, self.snapshots)
        self.assertTrue(report["passed"])
        self.assertEqual(report["action_eligibility"], "unchanged_review_only")
        self.assertTrue(all(item["passed"] for item in report["results"]))

    def test_source_drift_blocks(self):
        Path(self.snapshots["sender"]).write_text("changed\n", encoding="utf-8")
        report = live_contract_provenance.verify(self.contract, self.snapshots)
        self.assertFalse(report["passed"])
        self.assertEqual(report["action_eligibility"], "blocked_source_drift")
        sender = next(item for item in report["results"] if item["source_key"] == "sender")
        self.assertFalse(sender["passed"])

    def test_missing_or_unknown_snapshot_fails_closed(self):
        incomplete = dict(self.snapshots)
        incomplete.pop("legacy_policy")
        with self.assertRaisesRegex(
            live_contract_provenance.LiveContractProvenanceError,
            "snapshot set mismatch",
        ):
            live_contract_provenance.verify(self.contract, incomplete)

        unknown = dict(self.snapshots)
        unknown["other"] = unknown["sender"]
        with self.assertRaisesRegex(
            live_contract_provenance.LiveContractProvenanceError,
            "snapshot set mismatch",
        ):
            live_contract_provenance.verify(self.contract, unknown)


if __name__ == "__main__":
    unittest.main()
