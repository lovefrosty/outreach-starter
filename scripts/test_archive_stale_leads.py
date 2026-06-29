#!/usr/bin/env python3
"""
test_archive_stale_leads.py - focused checks for archive cleanup safety.

What this program tests
-----------------------
- Dry-run counts candidates but does not create or modify `lead_archive`.
- Execute mode archives only rows whose cadence is complete.
- Identity/history fields are preserved while recyclable fields are cleared.

Run from repo root:
`python3 workspace/scripts/test_archive_stale_leads.py`
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

tmp_db = Path(tempfile.gettempdir()) / "outreach_archive_test.db"
if tmp_db.exists():
    tmp_db.unlink()
os.environ["LEAD_DB"] = str(tmp_db)

import archive_stale_leads as archive  # noqa: E402
import lead_store as ls  # noqa: E402


def _iso(days_ago):
    """Return a UTC ISO timestamp `days_ago` days before now."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _insert_lead(conn, company, **fields):
    """Insert a minimal lead row for archive tests."""
    values = {
        "company": company,
        "phone": fields.get("phone", f"+1201555{len(company):04d}"[-12:]),
        "email": fields.get("email", f"{company.lower().replace(' ', '')}@example.com"),
        "city_state": fields.get("city_state", "Test, NJ"),
        "website": fields.get("website", "https://example.com"),
        "source": fields.get("source", "test"),
        "created_at": fields.get("created_at", _iso(80)),
        "stage": fields.get("stage"),
        "status": fields.get("status", "new"),
        "sent_at": fields.get("sent_at"),
        "call_due_at": fields.get("call_due_at"),
        "last_called_at": fields.get("last_called_at"),
        "owner_name": fields.get("owner_name", "Test Owner"),
        "processor": fields.get("processor", "Square"),
        "pain_theme": fields.get("pain_theme", "checkout_friction"),
        "template_key": fields.get("template_key", "restaurant_v1"),
    }
    conn.execute(
        """
        INSERT INTO leads
          (company, phone, email, city_state, website, source, created_at,
           stage, status, sent_at, call_due_at, last_called_at, owner_name,
           processor, pain_theme, template_key)
        VALUES
          (:company, :phone, :email, :city_state, :website, :source,
           :created_at, :stage, :status, :sent_at, :call_due_at,
           :last_called_at, :owner_name, :processor, :pain_theme,
           :template_key)
        """,
        values,
    )


def main():
    """Run archive safety assertions and print the proof lines."""
    conn = ls.connect()
    try:
        _insert_lead(
            conn,
            "Cadence Complete",
            stage="sent",
            sent_at=_iso(14),
            call_due_at=_iso(10),
            last_called_at=_iso(9),
        )
        _insert_lead(
            conn,
            "Still Active",
            stage="sent",
            sent_at=_iso(2),
            call_due_at=_iso(-1),
        )
        _insert_lead(
            conn,
            "No Contact",
            stage="scraped",
            email="",
            phone="",
        )
        conn.commit()

        rows = archive.candidate_rows(conn, days=45, limit=10)
        print(f"dry_run_candidates={len(rows)}")
        archived = archive.archive_rows(conn, rows, "test", execute=False)
        print(f"dry_run_would_archive={archived}")

        archive_table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lead_archive'"
        ).fetchone()
        assert archive_table_exists is None, "dry-run created lead_archive"

        archived = archive.archive_rows(conn, rows, "test", execute=True)
        print(f"execute_archived={archived}")
        archived_rows = conn.execute("SELECT * FROM lead_archive ORDER BY company").fetchall()
        print("archived_companies=" + ",".join(row["company"] for row in archived_rows))
        assert len(archived_rows) == 2

        active = conn.execute("SELECT * FROM leads WHERE company='Still Active'").fetchone()
        assert active["stage"] == "sent", "active sent lead should not be archived"
        cleaned = conn.execute("SELECT * FROM leads WHERE company='Cadence Complete'").fetchone()
        assert cleaned["stage"] == "dead"
        assert cleaned["processor"] is None
        assert cleaned["email"], "dedupe email should remain on lead row"
    finally:
        conn.close()
    print("archive_stale_leads_test=PASS")


if __name__ == "__main__":
    main()
