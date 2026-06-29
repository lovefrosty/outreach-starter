#!/usr/bin/env python3
"""Store and evaluate minimized global suppression evidence offline."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
POLICY_SCHEMA = "outreach.suppression-policy.v1"
EVENT_SCHEMA = "outreach.suppression-event.v1"
BATCH_SCHEMA = "outreach.suppression-event-batch.v1"
DEFAULT_POLICY = REPO_ROOT / "workspace/config/suppression_policy.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SuppressionRegistryError(ValueError):
    """Raised when suppression evidence is unsafe or inconsistent."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SuppressionRegistryError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SuppressionRegistryError(f"invalid JSON in {path}: {exc}") from exc


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise SuppressionRegistryError(f"{context} requires {field}")
    return value


def _timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SuppressionRegistryError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SuppressionRegistryError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise SuppressionRegistryError(f"{field} must include a timezone")
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
        raise SuppressionRegistryError("unsupported suppression policy schema")
    safety = policy.get("safety") or {}
    if (
        safety.get("provider_api_calls_allowed") is not False
        or safety.get("email_send_allowed") is not False
        or safety.get("crm_mutation_allowed") is not False
        or safety.get("raw_email_storage_allowed") is not False
        or safety.get("automatic_reinstatement_allowed") is not False
        or safety.get("events_are_append_only") is not True
    ):
        raise SuppressionRegistryError("suppression policy enables unsafe execution")
    if not policy.get("subject_types") or not policy.get("reasons") or not policy.get("event_types"):
        raise SuppressionRegistryError("suppression policy is incomplete")
    return policy


def validate_event(raw, policy):
    event = dict(raw)
    if event.get("schema_version") != EVENT_SCHEMA:
        raise SuppressionRegistryError("unsupported suppression event schema")
    event_id = _required_text(event, "event_id", "suppression event")
    _required_text(event, "source_event_id", f"event_id={event_id}")
    event_type = _required_text(event, "event_type", f"event_id={event_id}")
    if event_type not in set(policy["event_types"]):
        raise SuppressionRegistryError(f"unsupported suppression event type: {event_type}")
    subject_type = _required_text(event, "subject_type", f"event_id={event_id}")
    if subject_type not in set(policy["subject_types"]):
        raise SuppressionRegistryError(f"unsupported suppression subject type: {subject_type}")
    subject_id = _required_text(event, "subject_id", f"event_id={event_id}")
    if subject_type.endswith("_sha256") and not SHA256_RE.fullmatch(subject_id):
        raise SuppressionRegistryError(f"suppression subject hash is invalid: {event_id}")
    if "@" in subject_id:
        raise SuppressionRegistryError(f"raw email is forbidden in suppression subject: {event_id}")
    reason = _required_text(event, "reason", f"event_id={event_id}")
    if reason not in set(policy["reasons"]):
        raise SuppressionRegistryError(f"unsupported suppression reason: {reason}")
    occurred = _timestamp(event.get("occurred_at"), f"{event_id}.occurred_at")
    attributes = event.get("attributes")
    if not isinstance(attributes, dict):
        raise SuppressionRegistryError(f"suppression attributes must be an object: {event_id}")
    serialized = _canonical(attributes).casefold()
    if "@" in serialized or any(key in serialized for key in ('"email"', '"raw_body"', '"token"', '"secret"')):
        raise SuppressionRegistryError(f"raw or secret-bearing suppression attributes are forbidden: {event_id}")
    if event_type == "reinstatement_recorded":
        _required_text(attributes, "approval_id", f"event_id={event_id}.attributes")
        basis = _required_text(attributes, "basis", f"event_id={event_id}.attributes")
        if basis not in set(policy["reinstatement_bases"]):
            raise SuppressionRegistryError(f"unsupported reinstatement basis: {basis}")
        _required_text(attributes, "supersedes_source_event_id", f"event_id={event_id}.attributes")
    event["occurred_at"] = _iso(occurred)
    return event


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS suppression_events (
    event_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    canonical_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS suppression_events_no_update
BEFORE UPDATE ON suppression_events BEGIN SELECT RAISE(ABORT, 'suppression events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS suppression_events_no_delete
BEFORE DELETE ON suppression_events BEGIN SELECT RAISE(ABORT, 'suppression events are append-only'); END;
"""


def connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def ingest_batch(conn, payload, policy):
    if payload.get("schema_version") != BATCH_SCHEMA:
        raise SuppressionRegistryError("unsupported suppression batch schema")
    events = payload.get("events")
    if not isinstance(events, list):
        raise SuppressionRegistryError("suppression batch requires events")
    inserted = 0
    duplicates = 0
    with conn:
        for raw in events:
            event = validate_event(raw, policy)
            canonical = _hash(event)
            existing = conn.execute(
                "SELECT canonical_sha256 FROM suppression_events WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if existing:
                if existing["canonical_sha256"] != canonical:
                    raise SuppressionRegistryError(f"conflicting duplicate event_id: {event['event_id']}")
                duplicates += 1
                continue
            source = conn.execute(
                "SELECT event_id FROM suppression_events WHERE source_event_id = ?",
                (event["source_event_id"],),
            ).fetchone()
            if source:
                raise SuppressionRegistryError(f"source event already used: {event['source_event_id']}")
            conn.execute(
                "INSERT INTO suppression_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["event_id"], event["source_event_id"], event["event_type"],
                    event["occurred_at"], event["subject_type"], event["subject_id"],
                    event["reason"], _canonical(event["attributes"]), canonical,
                ),
            )
            inserted += 1
    return {"submitted": len(events), "inserted": inserted, "duplicates": duplicates}


def _events(conn):
    rows = conn.execute("SELECT * FROM suppression_events ORDER BY occurred_at, event_id").fetchall()
    result = []
    for row in rows:
        event = dict(row)
        event["attributes"] = json.loads(event.pop("attributes_json"))
        result.append(event)
    return result


def project(conn):
    records = {}
    unresolved = []
    source_to_key = {}
    for event in _events(conn):
        key = (event["subject_type"], event["subject_id"], event["reason"])
        if event["event_type"] == "suppression_applied":
            records[key] = {
                "subject_type": event["subject_type"],
                "subject_id": event["subject_id"],
                "reason": event["reason"],
                "status": "active",
                "applied_at": event["occurred_at"],
                "source_event_id": event["source_event_id"],
                "reinstatement": None,
            }
            source_to_key[event["source_event_id"]] = key
            continue
        supersedes = event["attributes"]["supersedes_source_event_id"]
        target_key = source_to_key.get(supersedes)
        if not target_key:
            unresolved.append({"event_id": event["event_id"], "reason": "reinstatement_target_missing"})
            continue
        target = records[target_key]
        if target["status"] != "active":
            unresolved.append({"event_id": event["event_id"], "reason": "suppression_already_reinstated"})
            continue
        if target_key != key:
            unresolved.append({"event_id": event["event_id"], "reason": "reinstatement_scope_mismatch"})
            continue
        target["status"] = "reinstated"
        target["reinstatement"] = {
            "at": event["occurred_at"],
            "approval_id": event["attributes"]["approval_id"],
            "basis": event["attributes"]["basis"],
            "source_event_id": event["source_event_id"],
        }
    return {"records": list(records.values()), "unresolved": unresolved}


def evaluate(conn, candidate):
    supplied = {
        subject_type: str(candidate.get(subject_type) or "").strip()
        for subject_type in ("email_sha256", "domain_sha256", "lead_id")
    }
    for subject_type, subject_id in supplied.items():
        if subject_id and subject_type.endswith("_sha256") and not SHA256_RE.fullmatch(subject_id):
            raise SuppressionRegistryError(f"candidate {subject_type} is invalid")
    projection = project(conn)
    matches = [
        record for record in projection["records"]
        if record["status"] == "active" and supplied.get(record["subject_type"]) == record["subject_id"]
    ]
    return {
        "suppressed": bool(matches),
        "decision": "hold_suppressed" if matches else "eligible_for_other_checks",
        "matches": matches,
        "unresolved_count": len(projection["unresolved"]),
        "email_send_allowed": False,
    }


def suppression_event(event_id, source_event_id, occurred_at, subject_type, subject_id, reason):
    return {
        "schema_version": EVENT_SCHEMA,
        "event_id": event_id,
        "source_event_id": source_event_id,
        "event_type": "suppression_applied",
        "occurred_at": occurred_at,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "reason": reason,
        "attributes": {},
    }
