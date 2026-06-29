#!/usr/bin/env python3

import copy
import json
import tempfile
import unittest
from pathlib import Path

import campaign_research_contract
import directory_snapshot_compiler


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOTS_PATH = ROOT / "workspace/campaigns/examples/directory-snapshots.json"


class DirectorySnapshotCompilerTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(SNAPSHOTS_PATH.read_text(encoding="utf-8"))
        self.manifest = directory_snapshot_compiler.load_manifest()

    def compile(self, payload=None, manifest=None):
        return directory_snapshot_compiler.compile_snapshots(
            payload or self.payload,
            manifest or self.manifest,
        )

    def test_compiles_two_sources_into_seed_and_research_lanes(self):
        report = self.compile()
        self.assertEqual(report["summary"]["snapshots"], 2)
        self.assertEqual(report["summary"]["records"], 4)
        self.assertEqual(report["summary"]["sources"]["nj_construction_permits"], 2)
        self.assertEqual(report["summary"]["sources"]["gloucester_trade_names"], 2)
        self.assertEqual(report["summary"]["stages"]["research_envelope_ready"], 2)
        self.assertEqual(report["summary"]["stages"]["seeded_research_needed"], 2)

    def test_website_records_emit_campaign_compatible_envelopes(self):
        report = self.compile()
        ready = [item for item in report["records"] if item["research_envelope"]]
        self.assertEqual(len(ready), 2)
        for record in ready:
            compiled = campaign_research_contract.compile_envelope(record["research_envelope"])
            self.assertEqual(compiled["company"], record["fields"]["company"])
            self.assertTrue(compiled["research_envelope"]["values_minimized"])

    def test_no_website_records_remain_non_promotable_seeds(self):
        report = self.compile()
        seeds = [item for item in report["records"] if not item["fields"]["website"]]
        self.assertEqual(len(seeds), 2)
        for seed in seeds:
            self.assertEqual(seed["stage"], "seeded_research_needed")
            self.assertIsNone(seed["research_envelope"])
            self.assertFalse(seed["automatic_lead_promotion_allowed"])

    def test_snapshot_row_and_external_id_provenance_is_durable(self):
        report = self.compile()
        for record in report["records"]:
            self.assertEqual(len(record["snapshot_sha256"]), 64)
            self.assertEqual(len(record["row_sha256"]), 64)
            self.assertEqual(len(record["external_id_sha256"]), 64)
            self.assertTrue(record["source_url"].startswith("https://"))

    def test_identical_duplicate_row_is_deduplicated(self):
        payload = copy.deepcopy(self.payload)
        payload["snapshots"][0]["rows"].append(
            copy.deepcopy(payload["snapshots"][0]["rows"][0])
        )
        report = self.compile(payload)
        self.assertEqual(report["summary"]["records"], 4)
        self.assertEqual(report["summary"]["duplicate_rows"], 1)

    def test_conflicting_duplicate_external_id_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        conflicting = copy.deepcopy(payload["snapshots"][0]["rows"][0])
        conflicting["contractor_name"] = "Different Contractor"
        payload["snapshots"][0]["rows"].append(conflicting)
        with self.assertRaisesRegex(
            directory_snapshot_compiler.DirectoryCompilerError,
            "conflicting duplicate directory external ID",
        ):
            self.compile(payload)

    def test_private_or_inferred_fields_are_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["snapshots"][0]["rows"][0]["pain_theme"] = "not allowed"
        with self.assertRaisesRegex(
            directory_snapshot_compiler.DirectoryCompilerError,
            "prohibited inferred/private fields",
        ):
            self.compile(payload)

    def test_invalid_website_is_rejected_not_guessed(self):
        payload = copy.deepcopy(self.payload)
        payload["snapshots"][0]["rows"][0]["website"] = "not a URL"
        with self.assertRaisesRegex(
            directory_snapshot_compiler.DirectoryCompilerError,
            "website is invalid",
        ):
            self.compile(payload)

    def test_unknown_source_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["snapshots"][0]["source_id"] = "unknown_directory"
        with self.assertRaisesRegex(
            directory_snapshot_compiler.DirectoryCompilerError,
            "unknown directory source",
        ):
            self.compile(payload)

    def test_unsafe_manifest_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["safety"]["external_fetch_allowed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                directory_snapshot_compiler.DirectoryCompilerError,
                "enables an external action",
            ):
                directory_snapshot_compiler.load_manifest(path)

    def test_output_cannot_fetch_write_or_promote(self):
        report = self.compile()
        self.assertFalse(report["safety"]["external_fetch_allowed"])
        self.assertFalse(report["safety"]["database_writes_allowed"])
        self.assertFalse(report["safety"]["automatic_lead_promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
