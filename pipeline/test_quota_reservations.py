#!/usr/bin/env python3

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import delivery_planner
import quota_reservations


class QuotaReservationTests(unittest.TestCase):
    def setUp(self):
        self.policy = quota_reservations.load_policy()
        self.registry = delivery_planner.load_registry()

    def request(self, number, identity="sender-east-1", domain="domain-east", at="2026-06-13T14:00:00Z", ttl=900):
        return {
            "idempotency_key": f"request-{number}",
            "message_id": f"message-{number}",
            "domain_id": domain,
            "sender_identity_id": identity,
            "requested_at": at,
            "ttl_seconds": ttl,
        }

    def test_reserves_domain_and_mailbox_capacity_atomically(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        result = quota_reservations.reserve(conn, self.request(1), self.policy, self.registry)
        self.assertEqual(result["status"], "active")
        report = quota_reservations.build_report(conn, self.policy, self.registry)
        self.assertEqual(report["domain_usage"]["domain-east"]["reserved_or_committed"], 1)
        self.assertEqual(report["identity_usage"]["sender-east-1"]["reserved_or_committed"], 1)
        self.assertFalse(report["safety"]["email_send_allowed"])

    def test_mailbox_quota_blocks_third_reservation(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        quota_reservations.reserve(conn, self.request(1), self.policy, self.registry)
        quota_reservations.reserve(conn, self.request(2), self.policy, self.registry)
        with self.assertRaisesRegex(quota_reservations.QuotaReservationError, "mailbox quota exhausted"):
            quota_reservations.reserve(conn, self.request(3), self.policy, self.registry)

    def test_two_connections_cannot_exceed_domain_quota(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "quota.sqlite"
            first = quota_reservations.connect(path)
            second = quota_reservations.connect(path)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            quota_reservations.reserve(first, self.request(1), self.policy, self.registry)
            quota_reservations.reserve(second, self.request(2), self.policy, self.registry)
            quota_reservations.reserve(first, self.request(3, identity="sender-east-2"), self.policy, self.registry)
            quota_reservations.reserve(second, self.request(4, identity="sender-east-2"), self.policy, self.registry)
            with self.assertRaisesRegex(quota_reservations.QuotaReservationError, "domain quota exhausted"):
                quota_reservations.reserve(first, self.request(5, identity="sender-east-2"), self.policy, self.registry)

    def test_identical_reservation_request_is_idempotent(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        first = quota_reservations.reserve(conn, self.request(1), self.policy, self.registry)
        second = quota_reservations.reserve(conn, self.request(1), self.policy, self.registry)
        self.assertEqual(first["reservation_id"], second["reservation_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(quota_reservations.build_report(conn, self.policy, self.registry)["summary"]["events"], 1)

    def test_conflicting_idempotency_key_is_rejected(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        quota_reservations.reserve(conn, self.request(1), self.policy, self.registry)
        conflict = self.request(1)
        conflict["message_id"] = "different-message"
        with self.assertRaisesRegex(quota_reservations.QuotaReservationError, "conflicting idempotent operation"):
            quota_reservations.reserve(conn, conflict, self.policy, self.registry)

    def test_commit_is_idempotent_and_continues_counting(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        reservation = quota_reservations.reserve(conn, self.request(1), self.policy, self.registry)
        committed = quota_reservations.transition(
            conn, reservation["reservation_id"], "committed", "commit-message-1",
            "2026-06-13T14:05:00Z", {"delivery_event_id": "delivery-1"},
        )
        duplicate = quota_reservations.transition(
            conn, reservation["reservation_id"], "committed", "commit-message-1",
            "2026-06-13T14:05:00Z", {"delivery_event_id": "delivery-1"},
        )
        self.assertEqual(committed["status"], "committed")
        self.assertTrue(duplicate["duplicate"])
        report = quota_reservations.build_report(conn, self.policy, self.registry)
        self.assertEqual(report["domain_usage"]["domain-east"]["reserved_or_committed"], 1)

    def test_release_frees_capacity_but_message_cannot_be_reused(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        reservation = quota_reservations.reserve(conn, self.request(1), self.policy, self.registry)
        quota_reservations.transition(
            conn, reservation["reservation_id"], "released", "release-message-1",
            "2026-06-13T14:05:00Z", {"reason": "review_rejected"},
        )
        report = quota_reservations.build_report(conn, self.policy, self.registry)
        self.assertEqual(report["domain_usage"]["domain-east"]["reserved_or_committed"], 0)
        retry = self.request("retry")
        retry["message_id"] = "message-1"
        with self.assertRaisesRegex(quota_reservations.QuotaReservationError, "message already has"):
            quota_reservations.reserve(conn, retry, self.policy, self.registry)

    def test_expiration_frees_capacity_and_blocks_commit(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        reservation = quota_reservations.reserve(conn, self.request(1, ttl=60), self.policy, self.registry)
        expired = quota_reservations.expire_due(conn, "2026-06-13T14:02:00Z")
        self.assertEqual(len(expired), 1)
        with self.assertRaisesRegex(quota_reservations.QuotaReservationError, "not active"):
            quota_reservations.transition(
                conn, reservation["reservation_id"], "committed", "late-commit",
                "2026-06-13T14:03:00Z", {"delivery_event_id": "late"},
            )

    def test_ineligible_sender_is_rejected(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(quota_reservations.QuotaReservationError, "ineligible sender"):
            quota_reservations.reserve(
                conn,
                self.request(1, identity="sender-held-1", domain="domain-held"),
                self.policy,
                self.registry,
            )

    def test_event_table_is_append_only(self):
        conn = quota_reservations.connect(":memory:")
        self.addCleanup(conn.close)
        quota_reservations.reserve(conn, self.request(1), self.policy, self.registry)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE quota_reservation_events SET event_type = 'changed'")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM quota_reservation_events")

    def test_unsafe_policy_is_rejected(self):
        policy = copy.deepcopy(self.policy)
        policy["safety"]["email_send_allowed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(quota_reservations.QuotaReservationError, "unsafe execution"):
                quota_reservations.load_policy(path)


if __name__ == "__main__":
    unittest.main()
