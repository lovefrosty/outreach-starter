#!/usr/bin/env python3

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import suppression_registry


EMAIL_HASH = hashlib.sha256(b"person@example.test").hexdigest()
DOMAIN_HASH = hashlib.sha256(b"example.test").hexdigest()


class SuppressionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.policy = suppression_registry.load_policy()

    def batch(self, *events):
        return {"schema_version": suppression_registry.BATCH_SCHEMA, "events": list(events)}

    def applied(self, event_id="sup-1", source="reply-unsubscribe-1", subject_type="email_sha256", subject_id=EMAIL_HASH, reason="unsubscribe"):
        return suppression_registry.suppression_event(
            event_id, source, "2026-06-13T14:00:00Z",
            subject_type, subject_id, reason,
        )

    def test_email_suppression_holds_candidate(self):
        conn = suppression_registry.connect(":memory:")
        self.addCleanup(conn.close)
        suppression_registry.ingest_batch(conn, self.batch(self.applied()), self.policy)
        decision = suppression_registry.evaluate(conn, {"email_sha256": EMAIL_HASH})
        self.assertTrue(decision["suppressed"])
        self.assertEqual(decision["decision"], "hold_suppressed")
        self.assertFalse(decision["email_send_allowed"])

    def test_domain_and_lead_scopes_are_evaluated_together(self):
        conn = suppression_registry.connect(":memory:")
        self.addCleanup(conn.close)
        domain = self.applied("sup-domain", "legal-domain", "domain_sha256", DOMAIN_HASH, "legal")
        lead = self.applied("sup-lead", "manual-lead", "lead_id", "lead-7", "manual_review")
        suppression_registry.ingest_batch(conn, self.batch(domain, lead), self.policy)
        decision = suppression_registry.evaluate(conn, {
            "email_sha256": hashlib.sha256(b"other@example.test").hexdigest(),
            "domain_sha256": DOMAIN_HASH,
            "lead_id": "lead-7",
        })
        self.assertEqual({match["reason"] for match in decision["matches"]}, {"legal", "manual_review"})

    def test_unmatched_candidate_remains_eligible_for_other_checks(self):
        conn = suppression_registry.connect(":memory:")
        self.addCleanup(conn.close)
        suppression_registry.ingest_batch(conn, self.batch(self.applied()), self.policy)
        decision = suppression_registry.evaluate(conn, {
            "email_sha256": hashlib.sha256(b"different@example.test").hexdigest()
        })
        self.assertFalse(decision["suppressed"])
        self.assertEqual(decision["decision"], "eligible_for_other_checks")

    def test_identical_event_replay_is_idempotent(self):
        conn = suppression_registry.connect(":memory:")
        self.addCleanup(conn.close)
        first = suppression_registry.ingest_batch(conn, self.batch(self.applied()), self.policy)
        second = suppression_registry.ingest_batch(conn, self.batch(self.applied()), self.policy)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["duplicates"], 1)

    def test_conflicting_duplicate_event_is_rejected(self):
        conn = suppression_registry.connect(":memory:")
        self.addCleanup(conn.close)
        suppression_registry.ingest_batch(conn, self.batch(self.applied()), self.policy)
        conflict = self.applied()
        conflict["reason"] = "complaint"
        with self.assertRaisesRegex(suppression_registry.SuppressionRegistryError, "conflicting duplicate"):
            suppression_registry.ingest_batch(conn, self.batch(conflict), self.policy)

    def test_raw_email_subject_and_attributes_are_rejected(self):
        raw = self.applied(subject_id="person@example.test")
        with self.assertRaisesRegex(suppression_registry.SuppressionRegistryError, "subject hash is invalid"):
            suppression_registry.validate_event(raw, self.policy)
        attributes = self.applied()
        attributes["attributes"] = {"raw_body": "private"}
        with self.assertRaisesRegex(suppression_registry.SuppressionRegistryError, "raw or secret-bearing"):
            suppression_registry.validate_event(attributes, self.policy)

    def test_human_reinstatement_clears_exact_reason_only(self):
        conn = suppression_registry.connect(":memory:")
        self.addCleanup(conn.close)
        unsubscribe = self.applied()
        complaint = self.applied("sup-2", "complaint-1", reason="complaint")
        reinstate = {
            "schema_version": suppression_registry.EVENT_SCHEMA,
            "event_id": "reinstate-1",
            "source_event_id": "operator-reinstate-1",
            "event_type": "reinstatement_recorded",
            "occurred_at": "2026-06-13T15:00:00Z",
            "subject_type": "email_sha256",
            "subject_id": EMAIL_HASH,
            "reason": "unsubscribe",
            "attributes": {
                "approval_id": "approval-1",
                "basis": "verified_consent",
                "supersedes_source_event_id": "reply-unsubscribe-1"
            }
        }
        suppression_registry.ingest_batch(conn, self.batch(unsubscribe, complaint, reinstate), self.policy)
        decision = suppression_registry.evaluate(conn, {"email_sha256": EMAIL_HASH})
        self.assertTrue(decision["suppressed"])
        self.assertEqual([match["reason"] for match in decision["matches"]], ["complaint"])

    def test_mismatched_reinstatement_is_unresolved_not_applied(self):
        conn = suppression_registry.connect(":memory:")
        self.addCleanup(conn.close)
        applied = self.applied()
        reinstate = {
            "schema_version": suppression_registry.EVENT_SCHEMA,
            "event_id": "reinstate-mismatch",
            "source_event_id": "operator-reinstate-mismatch",
            "event_type": "reinstatement_recorded",
            "occurred_at": "2026-06-13T15:00:00Z",
            "subject_type": "email_sha256",
            "subject_id": EMAIL_HASH,
            "reason": "complaint",
            "attributes": {
                "approval_id": "approval-2",
                "basis": "provider_correction",
                "supersedes_source_event_id": "reply-unsubscribe-1"
            }
        }
        suppression_registry.ingest_batch(conn, self.batch(applied, reinstate), self.policy)
        decision = suppression_registry.evaluate(conn, {"email_sha256": EMAIL_HASH})
        self.assertTrue(decision["suppressed"])
        self.assertEqual(decision["unresolved_count"], 1)

    def test_reinstatement_requires_human_approval_and_basis(self):
        event = {
            "schema_version": suppression_registry.EVENT_SCHEMA,
            "event_id": "reinstate-invalid",
            "source_event_id": "operator-invalid",
            "event_type": "reinstatement_recorded",
            "occurred_at": "2026-06-13T15:00:00Z",
            "subject_type": "email_sha256",
            "subject_id": EMAIL_HASH,
            "reason": "unsubscribe",
            "attributes": {"supersedes_source_event_id": "reply-unsubscribe-1"}
        }
        with self.assertRaisesRegex(suppression_registry.SuppressionRegistryError, "approval_id"):
            suppression_registry.validate_event(event, self.policy)

    def test_event_table_is_append_only(self):
        conn = suppression_registry.connect(":memory:")
        self.addCleanup(conn.close)
        suppression_registry.ingest_batch(conn, self.batch(self.applied()), self.policy)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE suppression_events SET reason = 'changed'")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM suppression_events")

    def test_unsafe_policy_is_rejected(self):
        policy = copy.deepcopy(self.policy)
        policy["safety"]["automatic_reinstatement_allowed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(suppression_registry.SuppressionRegistryError, "unsafe execution"):
                suppression_registry.load_policy(path)


if __name__ == "__main__":
    unittest.main()
