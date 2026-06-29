#!/usr/bin/env python3

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import outbound_event_ledger


ROOT = Path(__file__).resolve().parents[2]
EVENTS_PATH = ROOT / "workspace/campaigns/examples/outbound-events.json"


class OutboundEventLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "events.db"
        self.conn = outbound_event_ledger.connect(self.db_path)
        self.policy = outbound_event_ledger.load_policy()
        self.payload = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def ingest(self, payload=None):
        return outbound_event_ledger.ingest_batch(
            self.conn,
            payload or self.payload,
            self.policy,
        )

    def test_ingestion_and_metrics_use_distinct_email_and_lead_denominators(self):
        ingestion = self.ingest()
        report = outbound_event_ledger.build_report(self.conn, ingestion)
        metrics = report["funnel_metrics"]
        self.assertEqual(ingestion["inserted"], 21)
        self.assertEqual(report["ledger"]["unresolved_count"], 0)
        self.assertEqual(metrics["delivered_emails"], 4)
        self.assertEqual(metrics["contacted_leads"], 3)
        self.assertEqual(metrics["emails_with_reply"], 3)
        self.assertEqual(metrics["leads_with_reply"], 2)
        self.assertEqual(metrics["per_email_reply_rate"], 0.75)
        self.assertEqual(metrics["per_lead_reply_rate"], 0.666667)
        self.assertEqual(metrics["per_email_ooo_adjusted_reply_rate"], 0.5)
        self.assertEqual(metrics["per_lead_ooo_adjusted_reply_rate"], 0.666667)
        self.assertEqual(metrics["per_lead_actionable_conversion_rate"], 0.666667)
        self.assertEqual(metrics["per_lead_booking_conversion_rate"], 0.333333)

    def test_speed_to_draft_uses_webhook_receipt(self):
        self.ingest()
        report = outbound_event_ledger.build_report(self.conn)
        self.assertEqual(report["speed_to_draft"]["measured_replies"], 4)
        self.assertEqual(report["speed_to_draft"]["average_seconds"], 45.0)
        self.assertEqual(report["speed_to_draft"]["maximum_seconds"], 90.0)

    def test_reingestion_is_idempotent(self):
        first = self.ingest()
        second = self.ingest()
        self.assertEqual(first["inserted"], 21)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 21)
        count = self.conn.execute("SELECT COUNT(*) FROM outbound_events").fetchone()[0]
        self.assertEqual(count, 21)

    def test_conflicting_duplicate_event_id_is_rejected(self):
        self.ingest()
        payload = {"schema_version": self.payload["schema_version"], "events": [copy.deepcopy(self.payload["events"][0])]}
        payload["events"][0]["payload_sha256"] = "9" * 64
        with self.assertRaisesRegex(
            outbound_event_ledger.OutboundLedgerError,
            "conflicting duplicate event_id",
        ):
            self.ingest(payload)

    def test_provider_event_id_cannot_alias_another_event(self):
        self.ingest()
        event = copy.deepcopy(self.payload["events"][0])
        event["event_id"] = "different-event-id"
        payload = {"schema_version": self.payload["schema_version"], "events": [event]}
        with self.assertRaisesRegex(
            outbound_event_ledger.OutboundLedgerError,
            "provider event id already belongs",
        ):
            self.ingest(payload)

    def test_raw_reply_body_is_rejected(self):
        event = copy.deepcopy(
            next(item for item in self.payload["events"] if item["event_type"] == "reply_received")
        )
        event["attributes"]["body"] = "raw content must not enter this ledger"
        payload = {"schema_version": self.payload["schema_version"], "events": [event]}
        with self.assertRaisesRegex(
            outbound_event_ledger.OutboundLedgerError,
            "raw content or secret-bearing attributes are forbidden",
        ):
            self.ingest(payload)

    def test_database_triggers_enforce_append_only_events(self):
        self.ingest()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.conn.execute(
                "UPDATE outbound_events SET event_type='bounced' WHERE event_id='evt-register-001'"
            )
        self.conn.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.conn.execute("DELETE FROM outbound_events WHERE event_id='evt-register-001'")
        self.conn.rollback()

    def test_projection_is_independent_of_batch_order(self):
        payload = copy.deepcopy(self.payload)
        payload["events"].reverse()
        self.ingest(payload)
        report = outbound_event_ledger.build_report(self.conn)
        self.assertEqual(report["ledger"]["unresolved_count"], 0)
        self.assertEqual(report["funnel_metrics"]["emails_with_reply"], 3)

    def test_missing_registration_is_visible_not_silently_dropped(self):
        delivered = copy.deepcopy(
            next(item for item in self.payload["events"] if item["event_type"] == "delivered")
        )
        payload = {"schema_version": self.payload["schema_version"], "events": [delivered]}
        self.ingest(payload)
        report = outbound_event_ledger.build_report(self.conn)
        self.assertEqual(report["ledger"]["unresolved_count"], 1)
        self.assertEqual(
            report["ledger"]["unresolved"][0]["reason"],
            "missing_message_registration",
        )

    def test_unclassified_reply_is_excluded_from_adjusted_numerator(self):
        payload = copy.deepcopy(self.payload)
        payload["events"] = [
            item for item in payload["events"] if item["event_id"] != "evt-draft-002"
        ]
        self.ingest(payload)
        report = outbound_event_ledger.build_report(self.conn)
        metrics = report["funnel_metrics"]
        self.assertEqual(metrics["per_email_reply_rate"], 0.75)
        self.assertEqual(metrics["per_email_ooo_adjusted_reply_rate"], 0.25)
        self.assertEqual(metrics["per_lead_ooo_adjusted_reply_rate"], 0.333333)

    def test_report_contains_no_raw_message_content_and_cannot_send(self):
        self.ingest()
        report = outbound_event_ledger.build_report(self.conn)
        serialized = json.dumps(report)
        self.assertNotIn("secure://synthetic/reply", serialized)
        self.assertFalse(report["safety"]["provider_api_calls_allowed"])
        self.assertFalse(report["safety"]["email_send_allowed"])
        self.assertFalse(report["safety"]["live_crm_writes_allowed"])
        self.assertFalse(report["safety"]["raw_message_body_stored"])
        self.assertTrue(report["safety"]["events_append_only"])


if __name__ == "__main__":
    unittest.main()
