#!/usr/bin/env python3
"""Persist immutable sequence events and derive non-executing review actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
POLICY_SCHEMA = "outreach.sequence-state-policy.v1"
EVENT_SCHEMA = "outreach.sequence-event.v1"
BATCH_SCHEMA = "outreach.sequence-event-batch.v1"
REPORT_SCHEMA = "outreach.sequence-state-report.v1"
DEFAULT_POLICY = REPO_ROOT / "workspace/config/sequence_state_policy.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SequenceStateError(ValueError):
    """Raised when sequence state evidence is unsafe or inconsistent."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SequenceStateError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SequenceStateError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise SequenceStateError(f"{context} requires {field}")
    return value


def _timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SequenceStateError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SequenceStateError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise SequenceStateError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def load_policy(path=DEFAULT_POLICY):
    policy = _read_json(path)
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise SequenceStateError("unsupported sequence state policy schema")
    if policy.get("event_schema_version") != EVENT_SCHEMA:
        raise SequenceStateError("sequence policy has an unexpected event schema")
    safety = policy.get("safety") or {}
    if (
        safety.get("provider_api_calls_allowed") is not False
        or safety.get("email_send_allowed") is not False
        or safety.get("crm_mutation_allowed") is not False
        or safety.get("automatic_resume_allowed") is not False
        or safety.get("automatic_step_execution_allowed") is not False
        or safety.get("events_are_append_only") is not True
    ):
        raise SequenceStateError("sequence state policy enables unsafe execution")
    event_types = set(policy.get("event_types") or [])
    if not event_types:
        raise SequenceStateError("sequence state policy has no event types")
    sequence_ids = set()
    for sequence in policy.get("sequences") or []:
        sequence_id = _required_text(sequence, "sequence_id", "sequence definition")
        if sequence_id in sequence_ids:
            raise SequenceStateError(f"duplicate sequence definition: {sequence_id}")
        sequence_ids.add(sequence_id)
        steps = sequence.get("steps")
        if not isinstance(steps, list) or not steps:
            raise SequenceStateError(f"sequence requires steps: {sequence_id}")
        step_ids = set()
        for step in steps:
            step_id = _required_text(step, "step_id", f"sequence_id={sequence_id}")
            if step_id in step_ids:
                raise SequenceStateError(f"duplicate sequence step: {sequence_id}/{step_id}")
            step_ids.add(step_id)
            if int(step.get("delay_hours", -1)) < 0:
                raise SequenceStateError(f"sequence delay must be non-negative: {sequence_id}/{step_id}")
            _required_text(step, "template_variant_id", f"sequence_id={sequence_id}/{step_id}")
    if not sequence_ids:
        raise SequenceStateError("sequence state policy has no sequences")
    return policy


def _sequence_index(policy):
    return {item["sequence_id"]: {**item, "snapshot_sha256": _hash(item)} for item in policy["sequences"]}


def validate_event(raw, policy):
    event = dict(raw)
    if event.get("schema_version") != EVENT_SCHEMA:
        raise SequenceStateError("unsupported sequence event schema")
    event_id = _required_text(event, "event_id", "sequence event")
    for field in ("sequence_instance_id", "source_event_id", "sequence_id", "lead_id", "campaign_id"):
        _required_text(event, field, f"event_id={event_id}")
    if event["sequence_id"] not in _sequence_index(policy):
        raise SequenceStateError(f"unknown sequence: {event['sequence_id']}")
    event_type = _required_text(event, "event_type", f"event_id={event_id}")
    if event_type not in set(policy["event_types"]):
        raise SequenceStateError(f"unsupported sequence event type: {event_type}")
    occurred = _timestamp(event.get("occurred_at"), f"{event_id}.occurred_at")
    ingested = _timestamp(event.get("ingested_at"), f"{event_id}.ingested_at")
    if ingested < occurred:
        raise SequenceStateError(f"event ingestion precedes occurrence: {event_id}")
    payload_sha = _required_text(event, "payload_sha256", f"event_id={event_id}")
    if not SHA256_RE.fullmatch(payload_sha):
        raise SequenceStateError(f"invalid payload SHA-256: {event_id}")
    attributes = event.get("attributes")
    if not isinstance(attributes, dict):
        raise SequenceStateError(f"event attributes must be an object: {event_id}")
    if event_type == "human_approval_recorded":
        _required_text(attributes, "approval_id", f"event_id={event_id}.attributes")
        snapshot = _required_text(attributes, "approved_snapshot_sha256", f"event_id={event_id}.attributes")
        if not SHA256_RE.fullmatch(snapshot):
            raise SequenceStateError(f"invalid approved sequence snapshot: {event_id}")
    if event_type == "step_delivered":
        _required_text(attributes, "message_id", f"event_id={event_id}.attributes")
        _required_text(attributes, "step_id", f"event_id={event_id}.attributes")
        if not isinstance(attributes.get("step_index"), int) or attributes["step_index"] < 0:
            raise SequenceStateError(f"invalid delivered step index: {event_id}")
    if event_type == "reply_classified":
        intent = _required_text(attributes, "intent", f"event_id={event_id}.attributes")
        _required_text(attributes, "reply_event_id", f"event_id={event_id}.attributes")
        allowed_intents = set(policy["stop_intents"]) | set(policy["review_intents"]) | {policy["ooo_intent"]}
        if intent not in allowed_intents:
            raise SequenceStateError(f"unsupported reply intent: {intent}")
        if intent == policy["ooo_intent"]:
            return_at = _timestamp(attributes.get("return_at"), f"event_id={event_id}.attributes.return_at")
            if return_at <= occurred:
                raise SequenceStateError(f"OOO return must follow reply: {event_id}")
    if event_type == "bounced" and not isinstance(attributes.get("hard"), bool):
        raise SequenceStateError(f"bounce event requires boolean hard: {event_id}")
    event["occurred_at"] = _iso(occurred)
    event["ingested_at"] = _iso(ingested)
    return event


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sequence_events (
    event_id TEXT PRIMARY KEY,
    sequence_instance_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    sequence_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    canonical_sha256 TEXT NOT NULL,
    UNIQUE(sequence_instance_id, source_event_id)
);
CREATE TRIGGER IF NOT EXISTS sequence_events_no_update
BEFORE UPDATE ON sequence_events BEGIN SELECT RAISE(ABORT, 'sequence_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS sequence_events_no_delete
BEFORE DELETE ON sequence_events BEGIN SELECT RAISE(ABORT, 'sequence_events is append-only'); END;
"""


def connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def ingest_batch(conn, payload, policy):
    if payload.get("schema_version") != BATCH_SCHEMA:
        raise SequenceStateError("unsupported sequence event batch schema")
    events = payload.get("events")
    if not isinstance(events, list):
        raise SequenceStateError("sequence event batch requires events")
    inserted = 0
    duplicates = 0
    with conn:
        for raw in events:
            event = validate_event(raw, policy)
            canonical = _hash(event)
            existing = conn.execute(
                "SELECT canonical_sha256 FROM sequence_events WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if existing:
                if existing["canonical_sha256"] != canonical:
                    raise SequenceStateError(f"conflicting duplicate event_id: {event['event_id']}")
                duplicates += 1
                continue
            source_existing = conn.execute(
                "SELECT event_id FROM sequence_events WHERE sequence_instance_id = ? AND source_event_id = ?",
                (event["sequence_instance_id"], event["source_event_id"]),
            ).fetchone()
            if source_existing:
                raise SequenceStateError(
                    "source event already belongs to another sequence event: "
                    f"{event['sequence_instance_id']}/{event['source_event_id']}"
                )
            conn.execute(
                "INSERT INTO sequence_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["event_id"], event["sequence_instance_id"], event["source_event_id"],
                    event["event_type"], event["occurred_at"], event["ingested_at"],
                    event["sequence_id"], event["lead_id"], event["campaign_id"],
                    event["payload_sha256"], _canonical(event["attributes"]), canonical,
                ),
            )
            inserted += 1
    return {"submitted": len(events), "inserted": inserted, "duplicates": duplicates}


def _events(conn):
    rows = conn.execute(
        "SELECT * FROM sequence_events ORDER BY occurred_at, ingested_at, event_id"
    ).fetchall()
    result = []
    for row in rows:
        event = dict(row)
        event["attributes"] = json.loads(event.pop("attributes_json"))
        result.append(event)
    return result


def project(conn, policy, as_of):
    sequences = _sequence_index(policy)
    states = {}
    unresolved = []
    for event in _events(conn):
        instance_id = event["sequence_instance_id"]
        event_type = event["event_type"]
        state = states.get(instance_id)
        if event_type == "enrollment_created":
            if state:
                unresolved.append({"event_id": event["event_id"], "reason": "duplicate_enrollment"})
                continue
            definition = sequences[event["sequence_id"]]
            first_due = _timestamp(event["occurred_at"], "occurred_at") + timedelta(hours=int(definition["steps"][0]["delay_hours"]))
            states[instance_id] = {
                "sequence_instance_id": instance_id,
                "sequence_id": event["sequence_id"],
                "sequence_snapshot_sha256": definition["snapshot_sha256"],
                "lead_id": event["lead_id"],
                "campaign_id": event["campaign_id"],
                "status": "pending_approval",
                "approval_id": None,
                "next_step_index": 0,
                "next_due_at": _iso(first_due),
                "deferred_until": None,
                "stop_reason": None,
                "delivered_message_ids": [],
                "last_event_at": event["occurred_at"],
            }
            continue
        if not state:
            unresolved.append({"event_id": event["event_id"], "reason": "event_before_enrollment"})
            continue
        if any(state[field] != event[field] for field in ("sequence_id", "lead_id", "campaign_id")):
            unresolved.append({"event_id": event["event_id"], "reason": "sequence_attribution_conflict"})
            continue
        if state["status"] in {"stopped", "completed"}:
            unresolved.append({"event_id": event["event_id"], "reason": "event_after_terminal_state"})
            continue
        attributes = event["attributes"]
        if event_type == "human_approval_recorded":
            if attributes["approved_snapshot_sha256"] != state["sequence_snapshot_sha256"]:
                unresolved.append({"event_id": event["event_id"], "reason": "approval_snapshot_drift"})
                continue
            state["approval_id"] = attributes["approval_id"]
            state["status"] = "active"
        elif event_type == "step_delivered":
            definition = sequences[state["sequence_id"]]
            index = attributes["step_index"]
            if state["status"] != "active" or not state["approval_id"]:
                unresolved.append({"event_id": event["event_id"], "reason": "delivery_without_active_approval"})
                continue
            if index != state["next_step_index"] or index >= len(definition["steps"]):
                unresolved.append({"event_id": event["event_id"], "reason": "unexpected_step_index"})
                continue
            if definition["steps"][index]["step_id"] != attributes["step_id"]:
                unresolved.append({"event_id": event["event_id"], "reason": "step_definition_mismatch"})
                continue
            state["delivered_message_ids"].append(attributes["message_id"])
            state["next_step_index"] += 1
            if state["next_step_index"] >= len(definition["steps"]):
                state["status"] = "completed"
                state["next_due_at"] = None
            else:
                delay = int(definition["steps"][state["next_step_index"]]["delay_hours"])
                state["next_due_at"] = _iso(_timestamp(event["occurred_at"], "occurred_at") + timedelta(hours=delay))
        elif event_type == "reply_classified":
            intent = attributes["intent"]
            if intent in set(policy["stop_intents"]):
                state["status"] = "stopped"
                state["stop_reason"] = f"reply:{intent}"
                state["next_due_at"] = None
            elif intent == policy["ooo_intent"]:
                state["status"] = "deferred"
                state["deferred_until"] = _iso(_timestamp(attributes["return_at"], "return_at"))
            else:
                state["status"] = "paused_review"
                state["stop_reason"] = f"reply_review:{intent}"
        elif event_type == "paused":
            state["status"] = "paused_manual"
            state["stop_reason"] = str(attributes.get("reason") or "manual_pause")
        elif event_type == "resumed":
            if state["status"] not in {"paused_manual", "paused_review", "deferred"}:
                unresolved.append({"event_id": event["event_id"], "reason": "resume_without_pause"})
                continue
            if state["status"] == "deferred" and _timestamp(event["occurred_at"], "occurred_at") < _timestamp(state["deferred_until"], "deferred_until"):
                unresolved.append({"event_id": event["event_id"], "reason": "resume_before_ooo_return"})
                continue
            state["status"] = "active"
            state["deferred_until"] = None
            state["stop_reason"] = None
        elif event_type in {"unsubscribed", "complained", "cancelled"}:
            state["status"] = "stopped"
            state["stop_reason"] = event_type
            state["next_due_at"] = None
        elif event_type == "bounced":
            if attributes["hard"]:
                state["status"] = "stopped"
                state["stop_reason"] = "hard_bounce"
                state["next_due_at"] = None
            else:
                state["status"] = "paused_review"
                state["stop_reason"] = "soft_bounce_review"
        state["last_event_at"] = event["occurred_at"]

    as_of_time = _timestamp(as_of, "as_of")
    actions = []
    for instance_id, state in sorted(states.items()):
        definition = sequences[state["sequence_id"]]
        if state["status"] == "pending_approval":
            actions.append({"sequence_instance_id": instance_id, "action": "pending_human_enrollment_approval"})
        elif state["status"] == "active" and state["next_due_at"] and _timestamp(state["next_due_at"], "next_due_at") <= as_of_time:
            step = definition["steps"][state["next_step_index"]]
            actions.append({
                "sequence_instance_id": instance_id,
                "action": "pending_human_send_review",
                "step_index": state["next_step_index"],
                "step_id": step["step_id"],
                "template_variant_id": step["template_variant_id"],
                "resource_version_key": step.get("resource_version_key"),
                "due_at": state["next_due_at"],
                "automatic_execution_allowed": False,
            })
        elif state["status"] == "deferred" and _timestamp(state["deferred_until"], "deferred_until") <= as_of_time:
            actions.append({"sequence_instance_id": instance_id, "action": "pending_human_resume_review", "deferred_until": state["deferred_until"]})
        elif state["status"] == "paused_review":
            actions.append({"sequence_instance_id": instance_id, "action": "pending_human_reply_review", "reason": state["stop_reason"]})
    return {"states": states, "actions": actions, "unresolved": unresolved}


def build_report(conn, policy, as_of, ingestion=None):
    projection = project(conn, policy, as_of)
    return {
        "schema_version": REPORT_SCHEMA,
        "mode": "offline_shadow",
        "as_of": _iso(_timestamp(as_of, "as_of")),
        "safety": {
            **policy["safety"],
            "actions_are_review_proposals_only": True,
        },
        "ingestion": ingestion or {},
        "summary": {
            "events": len(_events(conn)),
            "instances": len(projection["states"]),
            "review_actions": len(projection["actions"]),
            "unresolved": len(projection["unresolved"]),
        },
        **projection,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    return parser


def main():
    args = build_parser().parse_args()
    conn = None
    try:
        policy = load_policy(args.policy)
        conn = connect(args.db)
        ingestion = ingest_batch(conn, _read_json(args.events), policy)
        report = build_report(conn, policy, args.as_of, ingestion)
        output = _write_json(args.output, report)
        print(json.dumps({"output": str(output), **report["summary"], "email_send_allowed": report["safety"]["email_send_allowed"]}, indent=2, sort_keys=True))
        return 0
    except (SequenceStateError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
