#!/usr/bin/env python3

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sequence_state


ROOT = Path(__file__).resolve().parents[2]
EVENTS_PATH = ROOT / "workspace/campaigns/examples/sequence-events.json"
AS_OF = "2026-06-14T12:00:00Z"


class SequenceStateTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        self.policy = sequence_state.load_policy()

    def report(self, payload=None, as_of=AS_OF):
        conn = sequence_state.connect(":memory:")
        ingestion = sequence_state.ingest_batch(conn, payload or self.payload, self.policy)
        return conn, sequence_state.build_report(conn, self.policy, as_of, ingestion)

    def test_projects_positive_stop_ooo_defer_and_due_review(self):
        conn, report = self.report()
        self.addCleanup(conn.close)
        self.assertEqual(report["summary"], {
            "events": 10,
            "instances": 3,
            "review_actions": 1,
            "unresolved": 0,
        })
        self.assertEqual(report["states"]["seqinst-positive"]["status"], "stopped")
        self.assertEqual(report["states"]["seqinst-positive"]["stop_reason"], "reply:positive_interest")
        self.assertEqual(report["states"]["seqinst-ooo"]["status"], "deferred")
        self.assertEqual(report["actions"][0]["action"], "pending_human_send_review")
        self.assertEqual(report["actions"][0]["sequence_instance_id"], "seqinst-due")

    def test_due_action_is_review_only_and_contains_no_message_body(self):
        conn, report = self.report()
        self.addCleanup(conn.close)
        action = report["actions"][0]
        self.assertFalse(action["automatic_execution_allowed"])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn('"body"', serialized)
        self.assertFalse(report["safety"]["email_send_allowed"])
        self.assertFalse(report["safety"]["automatic_step_execution_allowed"])

    def test_ooo_requires_human_resume_after_return(self):
        conn, report = self.report(as_of="2026-06-21T12:00:00Z")
        self.addCleanup(conn.close)
        action = next(item for item in report["actions"] if item["sequence_instance_id"] == "seqinst-ooo")
        self.assertEqual(action["action"], "pending_human_resume_review")
        self.assertEqual(report["states"]["seqinst-ooo"]["status"], "deferred")

    def test_identical_event_replay_is_idempotent(self):
        conn = sequence_state.connect(":memory:")
        self.addCleanup(conn.close)
        first = sequence_state.ingest_batch(conn, self.payload, self.policy)
        second = sequence_state.ingest_batch(conn, self.payload, self.policy)
        self.assertEqual(first["inserted"], 10)
        self.assertEqual(second["duplicates"], 10)
        self.assertEqual(sequence_state.build_report(conn, self.policy, AS_OF)["summary"]["events"], 10)

    def test_conflicting_duplicate_event_id_is_rejected(self):
        conn = sequence_state.connect(":memory:")
        self.addCleanup(conn.close)
        sequence_state.ingest_batch(conn, self.payload, self.policy)
        conflict = copy.deepcopy(self.payload)
        conflict["events"][0]["lead_id"] = "different-lead"
        with self.assertRaisesRegex(sequence_state.SequenceStateError, "conflicting duplicate event_id"):
            sequence_state.ingest_batch(conn, conflict, self.policy)

    def test_source_event_cannot_map_to_two_sequence_events(self):
        payload = copy.deepcopy(self.payload)
        duplicate = copy.deepcopy(payload["events"][1])
        duplicate["event_id"] = "seqevt-source-conflict"
        duplicate["payload_sha256"] = "f" * 64
        payload["events"].append(duplicate)
        conn = sequence_state.connect(":memory:")
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(sequence_state.SequenceStateError, "source event already belongs"):
            sequence_state.ingest_batch(conn, payload, self.policy)

    def test_stale_approval_snapshot_blocks_activation(self):
        payload = {"schema_version": self.payload["schema_version"], "events": copy.deepcopy(self.payload["events"][8:10])}
        payload["events"][1]["attributes"]["approved_snapshot_sha256"] = "f" * 64
        conn, report = self.report(payload)
        self.addCleanup(conn.close)
        self.assertEqual(report["states"]["seqinst-due"]["status"], "pending_approval")
        self.assertEqual(report["unresolved"][0]["reason"], "approval_snapshot_drift")
        self.assertEqual(report["actions"][0]["action"], "pending_human_enrollment_approval")

    def test_delivery_before_approval_is_unresolved(self):
        payload = {"schema_version": self.payload["schema_version"], "events": [copy.deepcopy(self.payload["events"][0]), copy.deepcopy(self.payload["events"][2])]}
        conn, report = self.report(payload)
        self.addCleanup(conn.close)
        self.assertEqual(report["unresolved"][0]["reason"], "delivery_without_active_approval")
        self.assertEqual(report["states"]["seqinst-positive"]["next_step_index"], 0)

    def test_unsubscribe_stops_and_later_delivery_is_rejected(self):
        payload = {"schema_version": self.payload["schema_version"], "events": copy.deepcopy(self.payload["events"][8:10])}
        unsubscribe = {
            **copy.deepcopy(payload["events"][1]),
            "event_id": "seqevt-unsubscribe",
            "source_event_id": "unsubscribe-source",
            "event_type": "unsubscribed",
            "occurred_at": "2026-06-13T11:02:00Z",
            "ingested_at": "2026-06-13T11:02:01Z",
            "payload_sha256": "d" * 64,
            "attributes": {},
        }
        late_delivery = {
            **copy.deepcopy(unsubscribe),
            "event_id": "seqevt-late-delivery",
            "source_event_id": "late-message",
            "event_type": "step_delivered",
            "occurred_at": "2026-06-13T11:03:00Z",
            "ingested_at": "2026-06-13T11:03:01Z",
            "payload_sha256": "e" * 64,
            "attributes": {"message_id": "late-message", "step_index": 0, "step_id": "intro"},
        }
        payload["events"].extend([unsubscribe, late_delivery])
        conn, report = self.report(payload)
        self.addCleanup(conn.close)
        self.assertEqual(report["states"]["seqinst-due"]["stop_reason"], "unsubscribed")
        self.assertEqual(report["unresolved"][0]["reason"], "event_after_terminal_state")
        self.assertEqual(report["actions"], [])

    def test_sequence_event_table_is_append_only(self):
        conn, _ = self.report()
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE sequence_events SET lead_id = 'changed' WHERE event_id = 'seqevt-001'")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM sequence_events WHERE event_id = 'seqevt-001'")

    def test_unsafe_policy_is_rejected(self):
        policy = copy.deepcopy(self.policy)
        policy["safety"]["automatic_step_execution_allowed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(sequence_state.SequenceStateError, "unsafe execution"):
                sequence_state.load_policy(path)


if __name__ == "__main__":
    unittest.main()
