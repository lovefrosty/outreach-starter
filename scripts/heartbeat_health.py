#!/usr/bin/env python3
"""
heartbeat_health.py - no-mutation Outreach health block.

What this program does
----------------------
The morning/evening heartbeat needs a concise operational health summary:
stage counts, stuck pipeline stages, due calls, sender gate state, and the next
best action. This script reads SQLite and environment flags, formats that
summary, and does not change lead or sender state.

Main functions
--------------
- `snapshot()`: collect raw health facts as a dict.
- `next_best_action(...)`: choose one operator action from the current counts.
- `format_text(...)`: render the heartbeat-friendly text block.

Program entrypoint
------------------
Running `python3 workspace/scripts/heartbeat_health.py` calls `main()` and
prints text. Passing `--json` prints the same facts as JSON for automation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_PIPE = _SCRIPTS.parent / "pipeline"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_PIPE))

import lead_store as ls  # noqa: E402
import orchestrator  # noqa: E402


def snapshot():
    """Collect the read-only health facts used by heartbeat output."""
    health = orchestrator.health_snapshot()
    due_calls = ls.scheduled_calls(due_only=True, limit=100)
    upcoming_calls = ls.scheduled_calls(due_only=False, limit=100)
    sending_enabled = os.environ.get("PIPELINE_SENDING_ENABLED", "").strip() == "1"
    counts = health["counts"]
    issues = list(health["issues"])
    if due_calls:
        issues.insert(0, f"{len(due_calls)} post-email calls due now")
    return {
        "stage_counts": counts,
        "due_calls": len(due_calls),
        "upcoming_calls": len(upcoming_calls),
        "sending_enabled": sending_enabled,
        "issues": issues,
        "next_best_action": next_best_action(counts, len(due_calls)),
    }


def next_best_action(counts, due_call_count):
    """
    Pick one action, ordered by proximity to revenue and operational risk.

    Calls and review-ready drafts outrank sourcing because they are closer to
    booked meetings. Raw lead volume is only recommended when downstream stages
    are empty.
    """
    if due_call_count:
        return "Handle due calls first."
    if counts.get("personalized", 0):
        return "Review personalized drafts in Telegram."
    if counts.get("pulled", 0) or counts.get("scraped", 0) or counts.get("analyzed", 0) or counts.get("verified", 0):
        return "Run the no-send pipeline until review cards are available."
    if counts.get("queued", 0):
        return "Check sender readiness and send gate before adding more approvals."
    if counts.get("call_list", 0):
        return "Work the call list while email supply is empty."
    return "Source a small targeted batch, then enrich it before adding more volume."


def format_text(data):
    """Render `snapshot()` output as a concise Telegram-friendly text block."""
    counts = data["stage_counts"]
    count_text = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "none"
    send_state = "ON" if data["sending_enabled"] else "OFF"
    issues = data["issues"] or ["no material health issues detected"]
    return "\n".join([
        "Outreach health",
        f"Stages: {count_text}",
        f"Calls: {data['due_calls']} due, {data['upcoming_calls']} upcoming",
        f"Sending gate: {send_state}",
        "Issues: " + "; ".join(issues[:5]),
        "Next: " + data["next_best_action"],
    ])


def main():
    """CLI entrypoint for heartbeat jobs or manual local checks."""
    data = snapshot()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(format_text(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
