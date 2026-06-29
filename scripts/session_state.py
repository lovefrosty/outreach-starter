#!/usr/bin/env python3
"""
session_state.py - lightweight persistent workflow state for Outreach.

What this program does
----------------------
Outreach needs to remember the current operator workflow across Telegram messages:
reviewing drafts, making calls, moving the pipeline, cleaning data, or doing
general planning. This module stores that small state in the live SQLite DB so
the bot can behave like a collaborator instead of treating every message as a
fresh command.

Stored state
------------
- `mode`: current workflow, such as `review`, `calls`, `pipeline`, `cleanup`.
- `objective`: optional plain-English goal for the session.
- `pending_action`: optional JSON action Outreach asked permission to run.
- `updated_by`: Telegram first name or process label that changed the state.
- `updated_at`: UTC timestamp.

Main functions
--------------
- `ensure_table(conn)`: create the `outreach_session_state` table.
- `get_state(conn)`: read the current state as a dict.
- `set_state(conn, mode, objective, updated_by)`: update the state.
- `note_action(conn, action, mode, updated_by)`: record lightweight activity.
- `set_pending_action(conn, action, updated_by)`: remember an action awaiting yes/no.
- `pop_pending_action(conn)`: return and clear the pending action.

Program entrypoint
------------------
Run `python3 workspace/scripts/session_state.py` to print current session state.
Use `--set-mode review --objective "review emails"` to update it manually.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import lead_store as ls  # noqa: E402


VALID_MODES = {"general", "review", "calls", "pipeline", "cleanup", "research", "sending"}
DEFAULT_STATE = {
    "id": 1,
    "mode": "general",
    "objective": "",
    "updated_by": "system",
    "updated_at": "",
    "last_action": "",
    "last_action_at": "",
    "pending_action": "",
}


def _now():
    """UTC timestamp used for session state writes."""
    return datetime.now(timezone.utc).isoformat()


def normalize_mode(mode):
    """Map loose user language into a small set of session modes."""
    raw = (mode or "general").strip().lower()
    aliases = {
        "approve": "review",
        "approvals": "review",
        "email": "review",
        "emails": "review",
        "call": "calls",
        "enrich": "pipeline",
        "orchestrator": "pipeline",
        "archive": "cleanup",
        "clean": "cleanup",
        "status": "general",
    }
    mode = aliases.get(raw, raw)
    return mode if mode in VALID_MODES else "general"


def ensure_table(conn):
    """Create the single-row session table if it does not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outreach_session_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mode TEXT,
            objective TEXT,
            updated_by TEXT,
            updated_at TEXT,
            last_action TEXT,
            last_action_at TEXT,
            pending_action TEXT
        )
        """
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(outreach_session_state)")}
    if "pending_action" not in cols:
        conn.execute("ALTER TABLE outreach_session_state ADD COLUMN pending_action TEXT")
    conn.execute(
        """
        INSERT OR IGNORE INTO outreach_session_state
          (id, mode, objective, updated_by, updated_at, last_action, last_action_at, pending_action)
        VALUES (1, 'general', '', 'system', '', '', '', '')
        """
    )
    conn.commit()


def get_state(conn=None):
    """
    Return the current session state.

    Accepts an optional existing connection so Outreach command handlers can avoid
    opening extra DB handles. If no connection is supplied, this function opens
    and closes one itself.
    """
    close = False
    if conn is None:
        conn = ls.connect()
        close = True
    try:
        ensure_table(conn)
        row = conn.execute("SELECT * FROM outreach_session_state WHERE id=1").fetchone()
        return dict(row) if row else dict(DEFAULT_STATE)
    finally:
        if close:
            conn.close()


def set_state(conn, mode="general", objective="", updated_by="operator"):
    """Persist the operator's current workflow mode and optional objective."""
    ensure_table(conn)
    mode = normalize_mode(mode)
    conn.execute(
        """
        UPDATE outreach_session_state
        SET mode=?, objective=?, updated_by=?, updated_at=?
        WHERE id=1
        """,
        (mode, (objective or "").strip(), updated_by, _now()),
    )
    conn.commit()
    return get_state(conn)


def note_action(conn, action, mode="", updated_by="operator"):
    """
    Record lightweight activity without necessarily changing the objective.

    If `mode` is supplied, it also updates the session mode. This lets simple
    commands like `/review` or `/calls` keep Outreach' session awareness current.
    """
    ensure_table(conn)
    state = get_state(conn)
    next_mode = normalize_mode(mode or state.get("mode") or "general")
    conn.execute(
        """
        UPDATE outreach_session_state
        SET mode=?, updated_by=?, updated_at=?, last_action=?, last_action_at=?
        WHERE id=1
        """,
        (next_mode, updated_by, _now(), action, _now()),
    )
    conn.commit()
    return get_state(conn)


def set_pending_action(conn, action, updated_by="operator"):
    """Persist one structured action for a later short confirmation."""
    ensure_table(conn)
    conn.execute(
        """
        UPDATE outreach_session_state
        SET pending_action=?, updated_by=?, updated_at=?
        WHERE id=1
        """,
        (json.dumps(action or {}, sort_keys=True), updated_by, _now()),
    )
    conn.commit()
    return get_state(conn)


def clear_pending_action(conn):
    """Clear the pending action without changing the workflow mode."""
    ensure_table(conn)
    conn.execute(
        """
        UPDATE outreach_session_state
        SET pending_action='', updated_at=?
        WHERE id=1
        """,
        (_now(),),
    )
    conn.commit()
    return get_state(conn)


def pop_pending_action(conn):
    """Return the pending action dict and clear it."""
    ensure_table(conn)
    state = get_state(conn)
    raw = (state.get("pending_action") or "").strip()
    clear_pending_action(conn)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main():
    """CLI entrypoint for manual inspection or setting the session mode."""
    ap = argparse.ArgumentParser(description="Inspect or update Outreach session state")
    ap.add_argument("--set-mode", choices=sorted(VALID_MODES))
    ap.add_argument("--objective", default="")
    ap.add_argument("--by", default="cli")
    args = ap.parse_args()
    conn = ls.connect()
    try:
        if args.set_mode:
            state = set_state(conn, args.set_mode, args.objective, args.by)
        else:
            state = get_state(conn)
        print(json.dumps(state, indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
