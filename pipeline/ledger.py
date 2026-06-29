#!/usr/bin/env python3
"""
ledger.py — the stage-machine helper layer for the lead pipeline.

The DB schema + connection live in scripts/lead_store.py (single source of
truth). This module sits on top and provides the stage-advancement, event,
cost, candidate, review, feature, and outcome helpers every pipeline node uses.

Design rules (from the build plan):
  - Deterministic. No LLM here.
  - Idempotent. A node only ever acts on rows at its exact input stage, so
    re-running a node is a no-op for already-advanced rows.
  - The "event cascade" (email verified -> notify Telegram) is just rows in the
    `events` table that a downstream node drains. No spawned agents.

Stdlib only. Import from anywhere on the path:
    import ledger as L
    conn = L.connect()
    for row in L.read_by_stage(conn, "pulled", limit=50):
        ...
        L.advance(conn, row["id"], "scraped", owner_name="Jane Doe")
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Reuse the canonical DB layer in scripts/ (schema + migration live there).
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import lead_store as ls  # noqa: E402

# Ordered email-enrichment waterfall. `route='call_list'` diverts a lead out of
# this path (it stays callable via the existing /calls queue).
STAGES = ["pulled", "scraped", "analyzed", "guessed", "verified",
          "personalized", "queued", "sending", "sent", "replied", "skipped"]
TERMINAL = {"sent", "replied", "skipped"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    """Open the shared leads.db (runs schema migration on first call)."""
    return ls.connect()


# ── Stage machine ──────────────────────────────────────────────────────────

def read_by_stage(conn, stage, limit=100, vertical=None, route=None):
    """Rows currently at `stage`. The orchestrator feeds these to the node."""
    q = "SELECT * FROM leads WHERE stage=?"
    params = [stage]
    if vertical:
        q += " AND vertical=?"; params.append(vertical)
    if route:
        q += " AND route=?"; params.append(route)
    q += " ORDER BY COALESCE(propensity,0) DESC, COALESCE(call_priority,0) DESC, id ASC LIMIT ?"
    params.append(limit)
    return conn.execute(q, params).fetchall()


def count_by_stage(conn):
    rows = conn.execute(
        "SELECT stage, COUNT(*) n FROM leads WHERE stage IS NOT NULL GROUP BY stage"
    ).fetchall()
    return {r["stage"]: r["n"] for r in rows}


def set_fields(conn, lead_id, **fields):
    """Generic, safe column setter (whitelisted to real leads columns)."""
    if not fields:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in cols:
            raise KeyError(f"unknown leads column: {k}")
        sets.append(f"{k}=?"); vals.append(v)
    vals.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()


def advance(conn, lead_id, to_stage, **fields):
    """Advance a lead to `to_stage`, optionally writing fields in the same txn."""
    if to_stage not in STAGES:
        raise ValueError(f"unknown stage: {to_stage}")
    fields["stage"] = to_stage
    set_fields(conn, lead_id, **fields)


def claim_send(conn, lead_id, provider, claim_id, claimed_at=None):
    """Atomically claim a queued lead for exactly one live sender."""
    claimed_at = claimed_at or _now()
    cur = conn.execute(
        """
        UPDATE leads
           SET stage='sending',
               send_provider=?,
               send_claim_id=?,
               send_claimed_at=?,
               send_error=''
         WHERE id=?
           AND stage='queued'
        """,
        (provider, claim_id, claimed_at, lead_id),
    )
    conn.commit()
    return cur.rowcount == 1


def release_send_claim(conn, lead_id, claim_id, error=""):
    """Return a failed claimed send to queued, but only for the same claim."""
    cur = conn.execute(
        """
        UPDATE leads
           SET stage='queued',
               send_claim_id=NULL,
               send_claimed_at=NULL,
               send_error=?
         WHERE id=?
           AND stage='sending'
           AND send_claim_id=?
        """,
        (error, lead_id, claim_id),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_send_claim_sent(conn, lead_id, claim_id, fields):
    """Move one claimed send to sent; stale duplicate workers cannot win."""
    if not fields:
        fields = {}
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
    sets, vals = [], []
    for key, value in fields.items():
        if key not in cols:
            raise KeyError(f"unknown leads column: {key}")
        sets.append(f"{key}=?")
        vals.append(value)
    sets.extend(["stage='sent'", "send_claim_id=NULL", "send_claimed_at=NULL"])
    vals.extend([lead_id, claim_id])
    cur = conn.execute(
        f"""
        UPDATE leads
           SET {', '.join(sets)}
         WHERE id=?
           AND stage='sending'
           AND send_claim_id=?
        """,
        vals,
    )
    conn.commit()
    return cur.rowcount == 1


def requeue_stale_send_claims(conn, ttl_minutes=30):
    """Recover rows abandoned at sending before any provider success was recorded."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)).isoformat()
    cur = conn.execute(
        """
        UPDATE leads
           SET stage='queued',
               send_claim_id=NULL,
               send_claimed_at=NULL,
               send_error='stale_send_claim_requeued'
         WHERE stage='sending'
           AND send_claimed_at IS NOT NULL
           AND send_claimed_at < ?
        """,
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def divert_to_call_list(conn, lead_id, reason=""):
    """Take a lead out of the email waterfall (terminal stage 'call_list') —
    it stays callable via the existing /calls queue (which ranks by
    call_priority, independent of stage)."""
    set_fields(conn, lead_id, route="call_list", stage="call_list")
    log_event(conn, "router", lead_id, "routed_call_list", {"reason": reason})


def recycle_no_email(conn, lead_id, reason=""):
    """Remove a lead from the email waterfall when no usable email path exists."""
    set_fields(conn, lead_id, route="recycle", stage="skipped")
    log_event(conn, "router", lead_id, "recycled_no_email", {"reason": reason})


# ── Events (the deterministic 'cascade') ─────────────────────────────────────

def log_event(conn, node, business_id, event_type, payload=None):
    conn.execute(
        "INSERT INTO events (node, business_id, event_type, payload, ts, handled) "
        "VALUES (?,?,?,?,?,0)",
        (node, business_id, event_type,
         json.dumps(payload or {}), _now()))
    conn.commit()


def drain_events(conn, event_type, limit=100):
    """Return unhandled events of a type. Caller marks them handled when done."""
    return conn.execute(
        "SELECT * FROM events WHERE handled=0 AND event_type=? ORDER BY id ASC LIMIT ?",
        (event_type, limit)).fetchall()


def mark_event_handled(conn, event_id):
    conn.execute("UPDATE events SET handled=1 WHERE id=?", (event_id,))
    conn.commit()


# ── Cost log (economics; replaces token accounting) ──────────────────────────

def log_cost(conn, stage, source, units, cost_usd):
    conn.execute(
        "INSERT INTO cost_log (stage, source, units, cost_usd, ts) VALUES (?,?,?,?,?)",
        (stage, source, units, cost_usd, _now()))
    conn.commit()


# ── Email candidates (C4 output -> C5 input) ─────────────────────────────────

def add_candidate(conn, business_id, email, source, confidence,
                  mx_ok=None, smtp_status=None, rank=0):
    conn.execute(
        "INSERT OR IGNORE INTO email_candidates "
        "(business_id, email, source, confidence, mx_ok, smtp_status, rank, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (business_id, email.lower().strip(), source, confidence,
         (1 if mx_ok else 0) if mx_ok is not None else None,
         smtp_status, rank, _now()))
    conn.commit()


def candidates_for(conn, business_id):
    return conn.execute(
        "SELECT * FROM email_candidates WHERE business_id=? ORDER BY rank ASC, confidence DESC",
        (business_id,)).fetchall()


# ── Reviews / features / outcomes (DS plumbing) ──────────────────────────────

def add_review(conn, business_id, text, rating=None, source="scrape"):
    conn.execute(
        "INSERT INTO reviews (business_id, text, rating, source, fetched_at) VALUES (?,?,?,?,?)",
        (business_id, text, rating, source, _now()))
    conn.commit()


def reviews_for(conn, business_id):
    return conn.execute(
        "SELECT * FROM reviews WHERE business_id=?", (business_id,)).fetchall()


def upsert_features(conn, business_id, **feats):
    feats["business_id"] = business_id
    feats["computed_at"] = _now()
    cols = ", ".join(feats.keys())
    ph = ", ".join("?" for _ in feats)
    updates = ", ".join(f"{k}=excluded.{k}" for k in feats if k != "business_id")
    conn.execute(
        f"INSERT INTO features ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(business_id) DO UPDATE SET {updates}",
        list(feats.values()))
    conn.commit()


def record_outcome(conn, business_id, event, value=""):
    """Label store for the v3 supervised model (sent/opened/replied/called/booked/closed)."""
    conn.execute(
        "INSERT INTO outcomes (business_id, event, value, ts) VALUES (?,?,?,?)",
        (business_id, event, str(value), _now()))
    conn.commit()


if __name__ == "__main__":
    # Smoke: open DB, print stage counts.
    c = connect()
    print("stages:", count_by_stage(c))
    print("ledger OK")
