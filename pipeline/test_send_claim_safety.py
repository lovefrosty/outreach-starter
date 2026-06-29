#!/usr/bin/env python3
"""Safety tests for WAL and C7 send claiming."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = PIPELINE_DIR.parent / "scripts"
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PIPELINE_DIR / "nodes"))

import ledger as L  # noqa: E402
import lead_store as ls  # noqa: E402
import c7_sender  # noqa: E402


class SendClaimSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "leads.db"
        self.old_db = ls.DB_PATH
        self.old_env = os.environ.copy()
        ls.DB_PATH = self.db_path

    def tearDown(self):
        ls.DB_PATH = self.old_db
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tempdir.cleanup()

    def _insert_queued(self, conn, email="owner@example.com", sequence="restaurant_default"):
        conn.execute(
            """
            INSERT INTO leads
              (company, phone, email, vertical, city_state, website, stage, route,
               sequence_key, trigger, trigger_verified, status, created_at)
            VALUES
              ('Race Cafe', '+12015550100', ?, 'restaurant', 'Hoboken, NJ',
               'https://race.example', 'queued', 'email', ?, '', NULL, 'new', ?)
            """,
            (email, sequence, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return conn.execute("SELECT * FROM leads WHERE email=?", (email,)).fetchone()

    def test_connect_enables_wal_and_busy_timeout(self):
        conn = L.connect()
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
        conn.close()

        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(busy_timeout, 30000)
        self.assertIn("send_claim_id", columns)
        self.assertIn("send_claimed_at", columns)

    def test_two_connections_cannot_claim_same_queued_lead(self):
        first = L.connect()
        row = self._insert_queued(first)
        second = L.connect()

        self.assertTrue(L.claim_send(first, row["id"], "instantly", "claim-1"))
        self.assertFalse(L.claim_send(second, row["id"], "instantly", "claim-2"))

        final = first.execute("SELECT stage, send_claim_id FROM leads WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(final["stage"], "sending")
        self.assertEqual(final["send_claim_id"], "claim-1")
        first.close()
        second.close()

    def test_failed_provider_send_releases_claim_to_queued(self):
        conn = L.connect()
        row = self._insert_queued(conn)
        self.assertTrue(L.claim_send(conn, row["id"], "instantly", "claim-fail"))

        self.assertTrue(L.release_send_claim(conn, row["id"], "claim-fail", "provider_failed"))

        final = conn.execute("SELECT stage, send_error, send_claim_id FROM leads WHERE id=?", (row["id"],)).fetchone()
        conn.close()
        self.assertEqual(final["stage"], "queued")
        self.assertEqual(final["send_error"], "provider_failed")
        self.assertIsNone(final["send_claim_id"])

    def test_successful_claim_records_one_sent_outcome(self):
        conn = L.connect()
        row = self._insert_queued(conn)
        self.assertTrue(L.claim_send(conn, row["id"], "instantly", "claim-ok"))
        self.assertTrue(
            L.mark_send_claim_sent(
                conn,
                row["id"],
                "claim-ok",
                {
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "send_provider": "instantly",
                    "send_error": "",
                },
            )
        )
        L.record_outcome(conn, row["id"], "sent", "instantly")

        final = conn.execute("SELECT stage, send_claim_id FROM leads WHERE id=?", (row["id"],)).fetchone()
        outcomes = conn.execute(
            "SELECT COUNT(*) n FROM outcomes WHERE business_id=? AND event='sent'",
            (row["id"],),
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(final["stage"], "sent")
        self.assertIsNone(final["send_claim_id"])
        self.assertEqual(outcomes, 1)

    def test_stale_sending_claim_requeues(self):
        conn = L.connect()
        row = self._insert_queued(conn)
        stale = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        self.assertTrue(L.claim_send(conn, row["id"], "instantly", "claim-stale", stale))

        self.assertEqual(L.requeue_stale_send_claims(conn, ttl_minutes=30), 1)

        final = conn.execute("SELECT stage, send_error FROM leads WHERE id=?", (row["id"],)).fetchone()
        conn.close()
        self.assertEqual(final["stage"], "queued")
        self.assertEqual(final["send_error"], "stale_send_claim_requeued")

    def test_sender_race_uses_one_provider_call_for_stale_duplicate_row(self):
        conn = L.connect()
        row = self._insert_queued(conn)
        stale_copy = conn.execute("SELECT * FROM leads WHERE id=?", (row["id"],)).fetchone()
        calls = []

        os.environ.update(
            {
                "PIPELINE_SENDING_ENABLED": "1",
                "SEND_PROVIDER": "instantly",
                "PIPELINE_DAILY_SEND_CAP": "30",
                "INSTANTLY_API_KEY": "test-key",
                "INSTANTLY_CAMPAIGN_ID_RESTAURANT_DEFAULT": "campaign-1",
            }
        )
        c7_sender.load_suppression = lambda: (set(), set())
        c7_sender.load_seen_uploaded = lambda: set()
        c7_sender.commit_uploaded = lambda emails: None

        def fake_push(_key, _campaign, leads):
            calls.append(leads[0]["email"])
            return 1

        c7_sender.push_to_instantly = fake_push

        self.assertTrue(c7_sender.process(conn, stale_copy, dry_run=False))
        self.assertFalse(c7_sender.process(conn, stale_copy, dry_run=False))

        final = conn.execute("SELECT stage FROM leads WHERE id=?", (row["id"],)).fetchone()
        outcomes = conn.execute(
            "SELECT COUNT(*) n FROM outcomes WHERE business_id=? AND event='sent'",
            (row["id"],),
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(calls, ["owner@example.com"])
        self.assertEqual(final["stage"], "sent")
        self.assertEqual(outcomes, 1)


if __name__ == "__main__":
    unittest.main()
