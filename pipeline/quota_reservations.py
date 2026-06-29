#!/usr/bin/env python3
"""Atomically reserve synthetic sender quotas without sending email."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import delivery_planner


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
POLICY_SCHEMA = "outreach.quota-reservation-policy.v1"
DEFAULT_POLICY = REPO_ROOT / "workspace/config/quota_reservation_policy.json"


class QuotaReservationError(ValueError):
    """Raised when quota cannot be reserved or reconciled safely."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QuotaReservationError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise QuotaReservationError(f"invalid JSON in {path}: {exc}") from exc


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise QuotaReservationError(f"{context} requires {field}")
    return value


def _timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise QuotaReservationError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaReservationError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise QuotaReservationError(f"{field} must include a timezone")
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
        raise QuotaReservationError("unsupported quota reservation policy schema")
    safety = policy.get("safety") or {}
    if (
        safety.get("provider_api_calls_allowed") is not False
        or safety.get("email_send_allowed") is not False
        or safety.get("crm_mutation_allowed") is not False
        or safety.get("production_registry_writes_allowed") is not False
        or safety.get("events_are_append_only") is not True
    ):
        raise QuotaReservationError("quota reservation policy enables unsafe execution")
    if int(policy.get("reservation_ttl_seconds") or 0) <= 0:
        raise QuotaReservationError("reservation TTL must be positive")
    if policy.get("day_boundary") != "UTC":
        raise QuotaReservationError("only UTC quota boundaries are supported")
    return policy


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quota_reservation_events (
    event_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    reservation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    canonical_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS quota_events_no_update
BEFORE UPDATE ON quota_reservation_events BEGIN SELECT RAISE(ABORT, 'quota events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS quota_events_no_delete
BEFORE DELETE ON quota_reservation_events BEGIN SELECT RAISE(ABORT, 'quota events are append-only'); END;
"""


def connect(path):
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _load_events(conn):
    rows = conn.execute(
        "SELECT * FROM quota_reservation_events ORDER BY occurred_at, event_id"
    ).fetchall()
    result = []
    for row in rows:
        event = dict(row)
        event["data"] = json.loads(event.pop("data_json"))
        result.append(event)
    return result


def project(conn):
    states = {}
    for event in _load_events(conn):
        reservation_id = event["reservation_id"]
        if event["event_type"] == "reserved":
            states[reservation_id] = {**event["data"], "reservation_id": reservation_id, "status": "active"}
        elif reservation_id in states:
            states[reservation_id]["status"] = event["event_type"]
            states[reservation_id]["finalized_at"] = event["occurred_at"]
            states[reservation_id]["finalization_evidence"] = event["data"]
    return states


def _registry_index(registry):
    domains = {item["domain_id"]: item for item in registry["domains"]}
    identities = {item["sender_identity_id"]: item for item in registry["identities"]}
    groups = {item["service_group_id"]: item for item in registry["service_groups"]}
    return domains, identities, groups


def _eligible_sender(request, registry):
    domains, identities, groups = _registry_index(registry)
    domain_id = _required_text(request, "domain_id", "quota request")
    identity_id = _required_text(request, "sender_identity_id", "quota request")
    domain = domains.get(domain_id)
    identity = identities.get(identity_id)
    if not domain or not identity or identity["domain_id"] != domain_id:
        raise QuotaReservationError("quota request references an unknown domain/identity pair")
    group = groups.get(identity["service_group_id"])
    if (
        domain.get("paused") is not False
        or domain.get("health") != "healthy"
        or domain.get("dns_ready") is not True
        or identity.get("paused") is not False
        or identity.get("health") != "healthy"
        or not group
        or group.get("paused") is not False
        or group.get("health") != "healthy"
    ):
        raise QuotaReservationError("quota request references an ineligible sender")
    return domain, identity


def _event(operation_id, reservation_id, event_type, occurred_at, data):
    body = {
        "operation_id": operation_id,
        "reservation_id": reservation_id,
        "event_type": event_type,
        "occurred_at": _iso(occurred_at),
        "data": data,
    }
    return {
        **body,
        "event_id": f"quotaevt-{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()[:24]}",
        "canonical_sha256": _hash(body),
    }


def _append_event(conn, event):
    existing = conn.execute(
        "SELECT canonical_sha256 FROM quota_reservation_events WHERE operation_id = ?",
        (event["operation_id"],),
    ).fetchone()
    if existing:
        if existing["canonical_sha256"] != event["canonical_sha256"]:
            raise QuotaReservationError(f"conflicting idempotent operation: {event['operation_id']}")
        return False
    conn.execute(
        "INSERT INTO quota_reservation_events VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event["event_id"], event["operation_id"], event["reservation_id"],
            event["event_type"], event["occurred_at"], _canonical(event["data"]),
            event["canonical_sha256"],
        ),
    )
    return True


def reserve(conn, request, policy, registry):
    operation_id = f"reserve:{_required_text(request, 'idempotency_key', 'quota request')}"
    message_id = _required_text(request, "message_id", "quota request")
    requested_at = _timestamp(request.get("requested_at"), "requested_at")
    domain, identity = _eligible_sender(request, registry)
    request_fingerprint = _hash(request)
    reservation_id = f"quota-{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()[:24]}"
    ttl = int(request.get("ttl_seconds") or policy["reservation_ttl_seconds"])
    if ttl <= 0 or ttl > 86400:
        raise QuotaReservationError("reservation TTL must be between 1 and 86400 seconds")
    day_start = requested_at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    expires_at = min(requested_at + timedelta(seconds=ttl), day_end)
    data = {
        "request_fingerprint": request_fingerprint,
        "message_id": message_id,
        "domain_id": domain["domain_id"],
        "sender_identity_id": identity["sender_identity_id"],
        "quota_date": day_start.date().isoformat(),
        "reserved_at": _iso(requested_at),
        "expires_at": _iso(expires_at),
    }
    event = _event(operation_id, reservation_id, "reserved", requested_at, data)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT canonical_sha256 FROM quota_reservation_events WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if existing:
            if existing["canonical_sha256"] != event["canonical_sha256"]:
                raise QuotaReservationError(f"conflicting idempotent operation: {operation_id}")
            current = project(conn).get(reservation_id) or {}
            conn.commit()
            return {"reservation_id": reservation_id, "status": current.get("status", "active"), "duplicate": True}
        states = project(conn)
        if any(state["message_id"] == message_id for state in states.values()):
            raise QuotaReservationError(f"message already has a quota reservation: {message_id}")
        counting = set(policy["counting_statuses"])
        domain_used = int(domain["sent_today"]) + sum(
            1 for state in states.values()
            if state["quota_date"] == data["quota_date"] and state["domain_id"] == domain["domain_id"] and state["status"] in counting
        )
        identity_used = int(identity["sent_today"]) + sum(
            1 for state in states.values()
            if state["quota_date"] == data["quota_date"] and state["sender_identity_id"] == identity["sender_identity_id"] and state["status"] in counting
        )
        if domain_used >= int(domain["daily_quota"]):
            raise QuotaReservationError(f"domain quota exhausted: {domain['domain_id']}")
        if identity_used >= int(identity["daily_quota"]):
            raise QuotaReservationError(f"mailbox quota exhausted: {identity['sender_identity_id']}")
        _append_event(conn, event)
        conn.commit()
        return {"reservation_id": reservation_id, "status": "active", "duplicate": False, **data}
    except Exception:
        conn.rollback()
        raise


def transition(conn, reservation_id, event_type, operation_id, occurred_at, evidence=None):
    if event_type not in {"committed", "released", "expired"}:
        raise QuotaReservationError(f"unsupported quota transition: {event_type}")
    occurred = _timestamp(occurred_at, "occurred_at")
    operation = _required_text({"operation_id": operation_id}, "operation_id", "quota transition")
    data = {"evidence": evidence or {}}
    event = _event(operation, reservation_id, event_type, occurred, data)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing_operation = conn.execute(
            "SELECT canonical_sha256 FROM quota_reservation_events WHERE operation_id = ?",
            (operation,),
        ).fetchone()
        if existing_operation:
            if existing_operation["canonical_sha256"] != event["canonical_sha256"]:
                raise QuotaReservationError(f"conflicting idempotent operation: {operation}")
            current = project(conn).get(reservation_id) or {}
            conn.commit()
            return {"reservation_id": reservation_id, "status": current.get("status", event_type), "duplicate": True}
        states = project(conn)
        state = states.get(reservation_id)
        if not state:
            raise QuotaReservationError(f"unknown quota reservation: {reservation_id}")
        if state["status"] != "active":
            raise QuotaReservationError(f"quota reservation is not active: {reservation_id}/{state['status']}")
        if event_type == "committed" and occurred > _timestamp(state["expires_at"], "expires_at"):
            raise QuotaReservationError(f"cannot commit expired quota reservation: {reservation_id}")
        inserted = _append_event(conn, event)
        conn.commit()
        return {"reservation_id": reservation_id, "status": event_type, "duplicate": not inserted}
    except Exception:
        conn.rollback()
        raise


def expire_due(conn, as_of):
    now = _timestamp(as_of, "as_of")
    expired = []
    for reservation_id, state in project(conn).items():
        if state["status"] == "active" and _timestamp(state["expires_at"], "expires_at") <= now:
            result = transition(
                conn,
                reservation_id,
                "expired",
                f"expire:{reservation_id}:{_iso(now)}",
                _iso(now),
                {"reason": "reservation_ttl_elapsed"},
            )
            expired.append(result)
    return expired


def build_report(conn, policy, registry):
    states = project(conn)
    domains, identities, _ = _registry_index(registry)
    counting = set(policy["counting_statuses"])
    domain_usage = {}
    for domain_id, domain in domains.items():
        reserved = sum(1 for state in states.values() if state["domain_id"] == domain_id and state["status"] in counting)
        domain_usage[domain_id] = {"baseline_sent": int(domain["sent_today"]), "reserved_or_committed": reserved, "daily_quota": int(domain["daily_quota"])}
    identity_usage = {}
    for identity_id, identity in identities.items():
        reserved = sum(1 for state in states.values() if state["sender_identity_id"] == identity_id and state["status"] in counting)
        identity_usage[identity_id] = {"baseline_sent": int(identity["sent_today"]), "reserved_or_committed": reserved, "daily_quota": int(identity["daily_quota"])}
    return {
        "schema_version": "outreach.quota-reservation-report.v1",
        "mode": "offline_shadow",
        "safety": policy["safety"],
        "summary": {"events": len(_load_events(conn)), "reservations": len(states)},
        "domain_usage": domain_usage,
        "identity_usage": identity_usage,
        "reservations": states,
    }
