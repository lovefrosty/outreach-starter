#!/usr/bin/env python3
"""Check whether Outreach is ready for the next outbound send window."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("OUTREACH_ROOT", Path.home() / ".outreach")).resolve()
SCRIPTS = Path(__file__).resolve().parent
PIPELINE = SCRIPTS.parent / "pipeline"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(PIPELINE))

import ledger as L  # noqa: E402

try:
    import operator_nudge  # noqa: E402
except Exception:
    operator_nudge = None  # type: ignore[assignment]


APPROVED_VERTICAL_SEQUENCES = (
    "restaurant_default",
    "pharmacy_default",
    "dealership_default",
)
REQUIRED_CAMPAIGN_ENVS = {
    "restaurant_default": "INSTANTLY_CAMPAIGN_ID_RESTAURANT_DEFAULT",
    "pharmacy_default": "INSTANTLY_CAMPAIGN_ID_PHARMACY_DEFAULT",
    "dealership_default": "INSTANTLY_CAMPAIGN_ID_DEALERSHIP_DEFAULT",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _count(conn, stage, sequences=None):
    params = [stage]
    query = "SELECT COUNT(*) n FROM leads WHERE stage=?"
    if sequences:
        placeholders = ",".join("?" for _ in sequences)
        query += f" AND sequence_key IN ({placeholders})"
        params.extend(sequences)
    return conn.execute(query, params).fetchone()["n"]


def _sample_ids(conn, stage, sequences=None, limit=15):
    params = [stage]
    query = "SELECT id FROM leads WHERE stage=?"
    if sequences:
        placeholders = ",".join("?" for _ in sequences)
        query += f" AND sequence_key IN ({placeholders})"
        params.extend(sequences)
    query += " ORDER BY COALESCE(propensity,0) DESC, COALESCE(call_priority,0) DESC, id ASC LIMIT ?"
    params.append(limit)
    return [row["id"] for row in conn.execute(query, params).fetchall()]


def snapshot(target):
    conn = L.connect()
    stages = L.count_by_stage(conn)
    vertical_queued = _count(conn, "queued", APPROVED_VERTICAL_SEQUENCES)
    vertical_personalized = _count(conn, "personalized", APPROVED_VERTICAL_SEQUENCES)
    general_queued = _count(conn, "queued", ("general_standard",))
    queued_ids = _sample_ids(conn, "queued", APPROVED_VERTICAL_SEQUENCES, target)
    personalized_ids = _sample_ids(conn, "personalized", APPROVED_VERTICAL_SEQUENCES, max(0, target - len(queued_ids)))
    conn.close()

    provider = os.environ.get("SEND_PROVIDER", "").strip() or "gmail"
    cap = os.environ.get("PIPELINE_DAILY_SEND_CAP", "").strip()
    gate = os.environ.get("PIPELINE_SENDING_ENABLED", "").strip() == "1"
    campaign_env = {
        seq: bool(os.environ.get(name, "").strip())
        for seq, name in REQUIRED_CAMPAIGN_ENVS.items()
    }

    blockers = []
    if provider != "instantly":
        blockers.append(f"SEND_PROVIDER is {provider!r}, expected 'instantly'")
    if cap != str(target):
        blockers.append(f"PIPELINE_DAILY_SEND_CAP is {cap!r}, expected {target}")
    if vertical_queued < target:
        needed = target - vertical_queued
        blockers.append(f"{needed} more approved vertical leads needed in queued")
    if general_queued:
        blockers.append(f"{general_queued} general_standard queued lead(s) are held until fallback campaign is fixed")
    missing_campaigns = [seq for seq, present in campaign_env.items() if not present]
    if missing_campaigns:
        blockers.append("missing campaign envs for: " + ", ".join(missing_campaigns))

    return {
        "checked_at": _now(),
        "target": target,
        "provider": provider,
        "daily_cap": cap,
        "sending_enabled": gate,
        "stage_counts": stages,
        "vertical_queued": vertical_queued,
        "vertical_personalized": vertical_personalized,
        "general_queued": general_queued,
        "candidate_queued_ids": queued_ids,
        "candidate_personalized_ids_to_review": personalized_ids,
        "campaign_env_present": campaign_env,
        "blockers": blockers,
    }


def format_message(data):
    lines = [
        "Outreach launch readiness",
        f"target={data['target']} provider={data['provider']} cap={data['daily_cap']} gate={'ON' if data['sending_enabled'] else 'OFF'}",
        f"vertical queued={data['vertical_queued']} vertical personalized={data['vertical_personalized']} general queued={data['general_queued']}",
        f"queued ids={','.join(map(str, data['candidate_queued_ids'])) or 'none'}",
    ]
    review_ids = data["candidate_personalized_ids_to_review"]
    if review_ids:
        lines.append(f"review next ids={','.join(map(str, review_ids))}")
    if data["blockers"]:
        lines.append("Blockers:")
        lines.extend(f"- {item}" for item in data["blockers"])
    else:
        lines.append("No readiness blockers detected. Gate/canary approval is still required before live sends.")
    return "\n".join(lines)


def maybe_notify(message):
    if operator_nudge is None:
        raise RuntimeError("operator_nudge is unavailable")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    chat_id = operator_nudge.delivery_chat_id()
    if not chat_id:
        raise RuntimeError("operator chat id is unavailable")
    return operator_nudge.telegram_send(token, chat_id, message)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=15)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    data = snapshot(args.target)
    message = format_message(data)
    print(json.dumps(data, indent=2, sort_keys=True) if args.json else message)
    if args.notify and data["blockers"]:
        response = maybe_notify(message)
        print(json.dumps({"telegram_ok": bool(response.get("ok"))}, sort_keys=True))
    return 1 if data["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
