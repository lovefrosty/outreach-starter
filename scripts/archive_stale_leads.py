#!/usr/bin/env python3
"""
archive_stale_leads.py - dry-run cleanup for completed low-action rows.

What this program does
----------------------
The live SQLite database can hold a lot of history, but the active pipeline
should not carry stale enrichment forever. This script preserves dedupe identity
and useful contact history in a small archive table, then optionally clears
recyclable enrichment/draft fields from leads whose outreach loop is complete.

Safety model
------------
- Default mode is dry-run. It prints what would be archived and performs no
  writes, including no archive-table creation.
- `--execute` is required for writes.
- Sent rows are archived only after a conservative cadence-complete gate.
- Replied, interested, callback, and booked rows are excluded by default.
- Identity fields are preserved on the lead row and copied to `lead_archive`.

Main functions
--------------
- `ensure_archive_table(conn)`: create the compact archive identity table.
- `cadence_complete(row, now)`: decide whether outreach is finished enough.
- `candidate_rows(conn, days, limit)`: find rows eligible for cleanup review.
- `archive_rows(conn, rows, reason, execute)`: perform dry-run or execute mode.

Program entrypoint
------------------
Run `python3 workspace/scripts/archive_stale_leads.py` for a dry-run. Add
`--execute` only after reviewing the output.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import lead_store as ls  # noqa: E402


RECYCLABLE_COLUMNS = (
    "owner_name", "propensity", "pain_tier", "pain_theme", "tech_signals",
    "processor", "switch_window_score", "trigger", "template_key",
    "template_route", "sequence_key", "email_angle", "template_cta",
    "approved_at", "trigger_evidence", "trigger_source", "trigger_verified",
    "send_error",
)
PROTECTED_STAGES = ("queued", "replied", "personalized")
PROTECTED_STATUSES = ("interested", "callback", "booked")
CADENCE_COMPLETE_DAYS_AFTER_SENT = 10


def ensure_archive_table(conn):
    """Create or migrate the compact archive table used in execute mode."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            company TEXT,
            city_state TEXT,
            email TEXT,
            phone TEXT,
            website TEXT,
            processor TEXT,
            source TEXT,
            first_contacted_at TEXT,
            last_contacted_at TEXT,
            last_called_at TEXT,
            archived_at TEXT,
            reason TEXT,
            identity_json TEXT,
            UNIQUE(company, city_state, email)
        )
        """
    )
    have = {r["name"] for r in conn.execute("PRAGMA table_info(lead_archive)")}
    for col, decl in (
        ("processor", "TEXT"),
        ("source", "TEXT"),
        ("first_contacted_at", "TEXT"),
        ("last_contacted_at", "TEXT"),
        ("last_called_at", "TEXT"),
        ("identity_json", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE lead_archive ADD COLUMN {col} {decl}")
    conn.commit()


def connect_for_mode(execute=False):
    """
    Open the lead database for dry-run or execute mode.

    Dry-run uses SQLite read-only URI mode so the command cannot create tables,
    run migrations, or mutate the real database. Execute mode uses the canonical
    lead_store connection because it intentionally writes archive rows.
    """
    if execute:
        return ls.connect()
    db_path = str(ls.DB_PATH)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_dt(value):
    """Parse an ISO timestamp and return None when the value is empty/bad."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def cadence_complete(row, now=None):
    """
    Decide whether a lead's outreach loop is finished enough to archive.

    The current schema does not yet have explicit day-3/day-7 follow-up fields,
    so this function uses the strongest available facts:
    - explicit dead/not_interested status is complete;
    - sent leads need to be at least 10 days old and have a completed/recorded
      post-email call path;
    - old unsent rows can archive only when they have no contact path.
    """
    now = now or datetime.now(timezone.utc)
    status = (row["status"] or "").lower()
    stage = (row["stage"] or "").lower()
    sent_at = _parse_dt(row["sent_at"] if "sent_at" in row.keys() else None)
    call_due_at = _parse_dt(row["call_due_at"] if "call_due_at" in row.keys() else None)
    last_called_at = _parse_dt(row["last_called_at"] if "last_called_at" in row.keys() else None)
    has_contact_path = bool((row["email"] or "").strip() or (row["phone"] or "").strip())

    if status in {"dead", "not_interested"} or stage == "dead":
        return True, f"explicit_terminal_{status or stage}"
    if sent_at:
        age_days = (now - sent_at).days
        call_completed = bool(last_called_at and last_called_at >= sent_at)
        call_window_expired = bool(call_due_at and call_due_at <= now)
        if age_days >= CADENCE_COMPLETE_DAYS_AFTER_SENT and (call_completed or call_window_expired):
            return True, "sent_cadence_complete"
        return False, "sent_cadence_not_complete"
    if not has_contact_path:
        return True, "old_no_contact_path"
    return False, "needs_outreach_or_research"


def candidate_rows(conn, days=45, limit=100):
    """
    Find stale, low-action rows eligible for cleanup review.

    This first selects old low-action rows, then applies the cadence gate in
    Python so the business rule stays readable and easy to audit.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    protected_stage_marks = ",".join("?" for _ in PROTECTED_STAGES)
    protected_status_marks = ",".join("?" for _ in PROTECTED_STATUSES)
    params = [cutoff, *PROTECTED_STAGES, *PROTECTED_STATUSES, max(limit * 5, limit)]
    rows = conn.execute(
        f"""
        SELECT * FROM leads
        WHERE COALESCE(created_at, '') <= ?
          AND COALESCE(stage, '') NOT IN ({protected_stage_marks})
          AND COALESCE(status, '') NOT IN ({protected_status_marks})
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    candidates = []
    now = datetime.now(timezone.utc)
    for row in rows:
        ok, archive_reason = cadence_complete(row, now=now)
        if ok:
            candidates.append((row, archive_reason))
        if len(candidates) >= limit:
            break
    return candidates


def _now():
    """UTC timestamp for archive writes."""
    return datetime.now(timezone.utc).isoformat()


def archive_rows(conn, rows, reason, execute=False):
    """
    Archive identity fields and optionally clear recyclable enrichment fields.

    In execute mode, the original lead row remains present for dedupe/history,
    but its stage/status become `dead` and bulky/reusable pipeline fields are
    nulled so it no longer pollutes active workflow views.
    """
    archived = 0
    if execute:
        ensure_archive_table(conn)
    for item in rows:
        row, archive_reason = item if isinstance(item, tuple) else (item, reason)
        archived += 1
        if not execute:
            continue
        identity = {
            "lead_id": row["id"],
            "company": row["company"],
            "city_state": row["city_state"],
            "email": row["email"],
            "phone": row["phone"],
            "website": row["website"],
            "processor": row["processor"] if "processor" in row.keys() else None,
            "source": row["source"] if "source" in row.keys() else None,
            "status": row["status"],
            "stage": row["stage"],
            "archive_gate": archive_reason,
        }
        conn.execute(
            """
            INSERT OR IGNORE INTO lead_archive
              (lead_id, company, city_state, email, phone, website, processor,
               source, first_contacted_at, last_contacted_at, last_called_at,
               archived_at, reason, identity_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["id"], row["company"], row["city_state"], row["email"],
                row["phone"], row["website"],
                row["processor"] if "processor" in row.keys() else None,
                row["source"] if "source" in row.keys() else None,
                row["sent_at"] if "sent_at" in row.keys() else None,
                row["sent_at"] if "sent_at" in row.keys() else None,
                row["last_called_at"] if "last_called_at" in row.keys() else None,
                _now(), f"{reason}:{archive_reason}", json.dumps(identity, sort_keys=True),
            ),
        )
        # Clear only derived/recyclable fields. Company, city, phone, website,
        # and email remain available for dedupe and "do not contact again" use.
        sets = [f"{col}=NULL" for col in RECYCLABLE_COLUMNS]
        sets.extend(["stage='dead'", "status='dead'", "route='archived'"])
        conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", (row["id"],))
        conn.execute("DELETE FROM email_candidates WHERE business_id=?", (row["id"],))
        conn.execute("DELETE FROM reviews WHERE business_id=?", (row["id"],))
        conn.execute("DELETE FROM features WHERE business_id=?", (row["id"],))
    if execute:
        conn.commit()
    return archived


def main():
    """CLI entrypoint for dry-run or explicit cleanup execution."""
    ap = argparse.ArgumentParser(description="Dry-run archive cleanup for stale Outreach leads")
    ap.add_argument("--days", type=int, default=45, help="minimum lead age before cleanup review")
    ap.add_argument("--limit", type=int, default=100, help="max rows to inspect")
    ap.add_argument("--reason", default="stale_low_action_cleanup")
    ap.add_argument("--execute", action="store_true", help="actually write archive/cleanup changes")
    args = ap.parse_args()

    conn = connect_for_mode(execute=args.execute)
    try:
        rows = candidate_rows(conn, days=args.days, limit=args.limit)
        action = "ARCHIVE" if args.execute else "DRY RUN"
        print(f"{action}: {len(rows)} candidate lead(s)")
        for row, archive_reason in rows[:20]:
            print(f"  [{row['id']}] {row['company']} | {row['city_state'] or ''} | {row['email'] or ''} | stage={row['stage']} status={row['status']} gate={archive_reason}")
        if len(rows) > 20:
            print(f"  ... {len(rows) - 20} more")
        archived = archive_rows(conn, rows, args.reason, execute=args.execute)
        print(f"{'archived' if args.execute else 'would_archive'}={archived}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
