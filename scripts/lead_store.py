#!/usr/bin/env python3
"""
lead_store.py — the lead CRM for the Acme outreach OS.

A single SQLite database (~/.outreach/state/leads.db) that is the system of
record for lead state: who's been called, outcomes, notes, status. Robust,
instant, no external API. Telegram (outreach_bot.py) is the interface.

Importable (Outreach uses these functions) AND runnable from cron:
    python3 lead_store.py ingest <csv>     # load a list_builder CSV
    python3 lead_store.py stats            # counts by status
    python3 lead_store.py list [status]    # print leads
    python3 lead_store.py uncalled [days]  # leads sourced N+ days ago, never called

Stdlib only.
"""

import os
import re
import sys
import csv
import sqlite3
import difflib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

DB_PATH = Path(os.environ.get("LEAD_DB", str(Path.home() / ".outreach" / "state" / "leads.db")))

STATUSES = ("new", "called", "voicemail", "interested", "callback",
            "not_interested", "booked", "dead")
ET = ZoneInfo("America/New_York")


def _now():
    return datetime.now(timezone.utc).isoformat()


def format_et(value):
    """Render stored UTC timestamps in the operator's Eastern timezone."""
    if not value:
        return "unscheduled"
    try:
        return datetime.fromisoformat(value).astimezone(ET).strftime("%Y-%m-%d %I:%M %p ET")
    except ValueError:
        return value


def norm_phone(raw):
    """Normalize to +1XXXXXXXXXX so Telegram makes it tap-to-call."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return raw or ""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            company       TEXT,
            contact_name  TEXT,
            phone         TEXT,
            email         TEXT,
            vertical      TEXT,
            city_state    TEXT,
            website       TEXT,
            locations     TEXT,
            est_volume    TEXT,
            rating        REAL,
            review_count  INTEGER,
            price_level   TEXT,
            call_priority INTEGER DEFAULT 0,
            status        TEXT DEFAULT 'new',
            notes         TEXT DEFAULT '',
            lead_ref      TEXT,
            source        TEXT,
            created_at    TEXT,
            last_called_at TEXT,
            called_by     TEXT,
            UNIQUE(company, phone)
        )
    """)
    # Migrate older DBs in place — add columns if missing. Existing rows get
    # NULL (no DEFAULT) so the pipeline orchestrator ignores them as 'legacy';
    # only rows the C1 puller inserts with stage='pulled' enter the waterfall.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
    for col, decl in (
        # signal columns (existing)
        ("rating", "REAL"), ("review_count", "INTEGER"),
        ("price_level", "TEXT"), ("call_priority", "INTEGER DEFAULT 0"),
        # pipeline stage-machine columns (new)
        ("stage", "TEXT"), ("route", "TEXT"), ("owner_name", "TEXT"),
        ("propensity", "REAL"), ("pain_tier", "TEXT"), ("pain_theme", "TEXT"),
        ("tech_signals", "TEXT"), ("processor", "TEXT"),
        ("switch_window_score", "REAL"), ("filing_date", "TEXT"),
        ("normalized_domain", "TEXT"),
        # C6 personalizer output -> C7 sender; approval gate timestamp
        ("trigger", "TEXT"), ("template_key", "TEXT"),
        ("template_route", "TEXT"), ("sequence_key", "TEXT"),
        ("email_angle", "TEXT"), ("template_cta", "TEXT"),
        ("approved_at", "TEXT"),
        # auditable opener evidence + scheduled post-email cold-call follow-up
        ("trigger_evidence", "TEXT"), ("trigger_source", "TEXT"),
        ("trigger_verified", "INTEGER"),
        ("sent_at", "TEXT"), ("call_due_at", "TEXT"), ("call_reason", "TEXT"),
        ("send_provider", "TEXT"), ("gmail_message_id", "TEXT"),
        ("gmail_thread_id", "TEXT"), ("send_error", "TEXT"),
        ("send_claim_id", "TEXT"), ("send_claimed_at", "TEXT"),
        # recipient mail-infra segmentation (ESP/ESG) — set at c5 verify + backfill
        ("recipient_esp", "TEXT"), ("recipient_esg", "TEXT"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")

    # Pipeline support tables (all keyed on leads.id as business_id).
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER, text TEXT, rating REAL,
            source TEXT, fetched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_reviews_biz ON reviews(business_id);

        CREATE TABLE IF NOT EXISTS features (
            business_id INTEGER PRIMARY KEY,
            review_count INTEGER, rating REAL, pain_density REAL,
            review_velocity REAL, tech_score REAL, age_days INTEGER,
            computed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS email_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER, email TEXT, source TEXT,
            confidence REAL, mx_ok INTEGER, smtp_status TEXT,
            rank INTEGER, created_at TEXT,
            UNIQUE(business_id, email)
        );
        CREATE INDEX IF NOT EXISTS ix_cand_biz ON email_candidates(business_id);

        CREATE TABLE IF NOT EXISTS outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER, event TEXT, value TEXT, ts TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_outcomes_biz ON outcomes(business_id);

        CREATE TABLE IF NOT EXISTS cost_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT, source TEXT, units REAL, cost_usd REAL, ts TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node TEXT, business_id INTEGER, event_type TEXT,
            payload TEXT, ts TEXT, handled INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_events_unhandled ON events(handled, event_type);

        CREATE TABLE IF NOT EXISTS territories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            town TEXT, vertical TEXT, last_sourced TEXT,
            new_found_last_run INTEGER, status TEXT DEFAULT 'queued',
            priority INTEGER DEFAULT 100,
            UNIQUE(town, vertical)
        );
    """)
    conn.commit()
    return conn


def ingest_csv(path, source=None):
    """Load a list_builder-schema CSV. Upsert by (company, phone). Returns (added, skipped)."""
    conn = connect()
    added = skipped = 0
    source = source or os.path.basename(path)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            company = (row.get("CompanyName") or "").strip()
            phone = norm_phone(row.get("Phone"))
            if not company:
                continue
            name = " ".join(x for x in [row.get("FirstName"), row.get("LastName")] if x).strip()
            def _num(v, cast):
                v = (v or "").strip()
                try:
                    return cast(float(v)) if v else None
                except (TypeError, ValueError):
                    return None
            try:
                conn.execute("""
                    INSERT INTO leads (company, contact_name, phone, email, vertical,
                        city_state, website, lead_ref, rating, review_count, price_level,
                        call_priority, source, status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'new', ?)
                """, (company, name, phone, (row.get("Email") or "").strip(),
                      (row.get("Vertical") or "").strip(), (row.get("CityState") or "").strip(),
                      (row.get("Website") or "").strip(), (row.get("LeadId") or "").strip(),
                      _num(row.get("Rating"), float), _num(row.get("ReviewCount"), int),
                      (row.get("PriceLevel") or "").strip(), _num(row.get("CallPriority"), int) or 0,
                      source, _now()))
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1  # already in store
    conn.commit()
    conn.close()
    return added, skipped


def find_lead(query, limit=5):
    """Fuzzy-match a lead by company name. Returns list of Row, best first."""
    conn = connect()
    rows = conn.execute("SELECT * FROM leads").fetchall()
    conn.close()
    if not rows:
        return []
    q = query.lower().strip()
    # exact/substring first
    subs = [r for r in rows if q in (r["company"] or "").lower()]
    if subs:
        return subs[:limit]
    names = {r["company"].lower(): r for r in rows if r["company"]}
    close = difflib.get_close_matches(q, list(names.keys()), n=limit, cutoff=0.5)
    return [names[c] for c in close]


def update_lead(lead_id, status=None, note=None, called_by=None, mark_called=False):
    conn = connect()
    sets, vals = [], []
    if status:
        sets.append("status=?"); vals.append(status)
    if note:
        # append to existing notes
        cur = conn.execute("SELECT notes FROM leads WHERE id=?", (lead_id,)).fetchone()
        existing = (cur["notes"] if cur else "") or ""
        stamp = datetime.now(timezone.utc).strftime("%m/%d")
        sets.append("notes=?"); vals.append((existing + f"\n[{stamp}] {note}").strip())
    if mark_called:
        sets.append("last_called_at=?"); vals.append(_now())
    if called_by:
        sets.append("called_by=?"); vals.append(called_by)
    if not sets:
        conn.close(); return False
    vals.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit(); conn.close()
    return True


def list_leads(status=None, vertical=None, limit=25):
    conn = connect()
    q, vals = "SELECT * FROM leads WHERE 1=1", []
    if status:
        q += " AND status=?"; vals.append(status)
    if vertical:
        q += " AND vertical=?"; vals.append(vertical)
    q += " ORDER BY created_at DESC LIMIT ?"; vals.append(limit)
    rows = conn.execute(q, vals).fetchall()
    conn.close()
    return rows


def uncalled(days=2, limit=25):
    """Unscheduled leads created N+ days ago that have never been called."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    # Best-fit leads first (highest CallPriority), oldest as tiebreak — so a
    # high-volume single-location lead with no email never gets buried.
    rows = conn.execute("""
        SELECT * FROM leads
        WHERE last_called_at IS NULL AND created_at <= ?
          AND call_due_at IS NULL
        ORDER BY COALESCE(call_priority, 0) DESC, created_at ASC LIMIT ?
    """, (cutoff, limit)).fetchall()
    conn.close()
    return rows


def scheduled_calls(due_only=True, limit=25):
    """Post-email cold calls, due first; called-before-email leads stay eligible."""
    conn = connect()
    op = "<=" if due_only else ">"
    rows = conn.execute(f"""
        SELECT * FROM leads
        WHERE call_due_at IS NOT NULL
          AND call_due_at {op} ?
          AND (last_called_at IS NULL OR sent_at IS NULL OR last_called_at < sent_at)
        ORDER BY call_due_at ASC, COALESCE(call_priority, 0) DESC LIMIT ?
    """, (_now(), limit)).fetchall()
    conn.close()
    return rows


def stats():
    conn = connect()
    rows = conn.execute("SELECT status, COUNT(*) n FROM leads GROUP BY status").fetchall()
    total = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
    conn.close()
    return total, {r["status"]: r["n"] for r in rows}


def _has(row, key):
    """Safe column access — older rows may predate a migration."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def card(row):
    """Format a lead as a clean Telegram card. Phone is tap-to-call."""
    prio = _has(row, "call_priority") or 0
    reviews = _has(row, "review_count")
    # ⭐ flag the high-fit leads — especially the ones worth calling even with no email.
    star = ""
    if prio >= 60:
        star = "⭐ HIGH FIT" + ("" if row["email"] else " · no email, call this one")
        star = star + "\n"
    lines = [f"{star}🏪 *{row['company']}*  ·  {row['vertical'] or '—'}"]
    if row["city_state"]:
        loc = row["city_state"]
        if row["locations"]:
            loc += f" · {row['locations']}"
        lines.append(f"📍 {loc}")
    if reviews:
        rating = _has(row, "rating")
        lines.append(f"⭐ {rating or '?'} · {reviews} reviews")
    if row["phone"]:
        lines.append(f"📞 {row['phone']}")
    if row["est_volume"]:
        lines.append(f"💳 est. {row['est_volume']}")
    if row["email"]:
        lines.append(f"✉️ {row['email']}")
    if row["website"]:
        lines.append(f"🔗 {row['website']}")
    template_key = _has(row, "template_key")
    if template_key:
        lines.append(f"🧩 Template: {template_key}")
    email_angle = _has(row, "email_angle")
    if email_angle:
        lines.append(f"🧭 Angle: {email_angle}")
    trigger = _has(row, "trigger")
    if trigger:
        lines.append(f"✍️ Opener: {trigger.strip()}")
    trigger_verified = _has(row, "trigger_verified")
    trigger_evidence = _has(row, "trigger_evidence")
    trigger_source = _has(row, "trigger_source")
    if trigger_verified == 1:
        lines.append(f"🔎 Opener evidence: {trigger_evidence or 'verified public listing'}")
        if trigger_source:
            lines.append(f"   Source: {trigger_source}")
    elif trigger:
        lines.append("⚠️ Opener evidence: UNVERIFIED — do not approve this opener as written")
    elif template_key:
        lines.append("✅ Standard template body only — no custom opener")
    call_due_at = _has(row, "call_due_at")
    if call_due_at:
        lines.append(f"📅 Post-email call due: {format_et(call_due_at)}")
    badge = {"new": "🆕", "interested": "🔥", "callback": "📅", "booked": "✅",
             "not_interested": "❌", "dead": "⚰️", "called": "☑️", "voicemail": "📨"}.get(row["status"], "")
    lines.append(f"{badge} {row['status']}  ·  id {row['id']}")
    if row["notes"]:
        lines.append(f"📝 {row['notes'].strip()}")
    return "\n".join(lines)


# ─────────────────────────── CLI ───────────────────────────

def main():
    if len(sys.argv) < 2:
        print("usage: lead_store.py [ingest <csv> | stats | list [status] | uncalled [days]]")
        return 1
    cmd = sys.argv[1]
    if cmd == "ingest" and len(sys.argv) >= 3:
        for path in sys.argv[2:]:
            if not os.path.exists(path):
                print(f"[ingest] skip (not found): {path}")
                continue
            added, skipped = ingest_csv(path)
            print(f"[ingest] {path}: +{added} new, {skipped} already known")
    elif cmd == "stats":
        total, by = stats()
        print(f"total leads: {total}")
        for s, n in sorted(by.items(), key=lambda kv: -kv[1]):
            print(f"  {s}: {n}")
    elif cmd == "list":
        status = sys.argv[2] if len(sys.argv) >= 3 else None
        for r in list_leads(status=status):
            print(f"  [{r['id']}] {r['company']} | {r['phone']} | {r['vertical']} | {r['status']}")
    elif cmd == "uncalled":
        days = int(sys.argv[2]) if len(sys.argv) >= 3 else 2
        for r in uncalled(days=days):
            print(f"  [{r['id']}] {r['company']} | {r['phone']} | sourced {r['created_at'][:10]}")
    else:
        print("unknown command")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
