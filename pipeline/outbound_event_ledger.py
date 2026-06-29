#!/usr/bin/env python3
"""Persist minimized outbound events and compute provider-independent metrics.

The SQLite ledger is append-only and idempotent. It stores payload fingerprints
and secure references, never raw message bodies. It has no provider client,
sender, scheduler, or live CRM integration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
POLICY_SCHEMA = "outreach.outbound-event-ledger-policy.v1"
EVENT_SCHEMA = "outreach.outbound-event.v1"
BATCH_SCHEMA = "outreach.outbound-event-batch.v1"
REPORT_SCHEMA = "outreach.outbound-ledger-report.v1"
DEFAULT_POLICY = REPO_ROOT / "workspace/config/outbound_event_ledger.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACTIONABLE_INTENTS = {"booking", "positive_interest", "question", "objection"}


class OutboundLedgerError(ValueError):
    """Raised when canonical event evidence is unsafe or inconsistent."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OutboundLedgerError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OutboundLedgerError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _parse_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise OutboundLedgerError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutboundLedgerError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise OutboundLedgerError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise OutboundLedgerError(f"{context} requires {field}")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _canonical_hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def load_policy(path=DEFAULT_POLICY):
    policy = _read_json(path)
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise OutboundLedgerError("unsupported outbound event ledger policy schema")
    safety = policy.get("safety") or {}
    if (
        safety.get("provider_api_calls_allowed") is not False
        or safety.get("email_send_allowed") is not False
        or safety.get("live_crm_writes_allowed") is not False
        or safety.get("raw_message_body_storage_allowed") is not False
        or safety.get("events_are_append_only") is not True
    ):
        raise OutboundLedgerError("outbound event ledger policy violates safety controls")
    if policy.get("event_schema_version") != EVENT_SCHEMA:
        raise OutboundLedgerError("outbound event policy has an unexpected event schema")
    if not policy.get("event_types") or not policy.get("required_message_fields"):
        raise OutboundLedgerError("outbound event policy is incomplete")
    return policy


def _forbidden_key_paths(value, forbidden, path="attributes"):
    found = []
    if isinstance(value, dict):
        for key, nested in value.items():
            next_path = f"{path}.{key}"
            if str(key).casefold() in forbidden:
                found.append(next_path)
            found.extend(_forbidden_key_paths(nested, forbidden, next_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_forbidden_key_paths(nested, forbidden, f"{path}[{index}]"))
    return found


def validate_event(raw, policy):
    event = dict(raw)
    if event.get("schema_version") != EVENT_SCHEMA:
        raise OutboundLedgerError("unsupported outbound event schema")
    event_id = _required_text(event, "event_id", "outbound event")
    provider = _required_text(event, "provider", f"event_id={event_id}")
    provider_event_id = _required_text(
        event, "provider_event_id", f"event_id={event_id}"
    )
    event_type = _required_text(event, "event_type", f"event_id={event_id}")
    if event_type not in set(policy["event_types"]):
        raise OutboundLedgerError(f"unsupported outbound event type: {event_type}")
    occurred = _parse_timestamp(event.get("occurred_at"), f"{event_id}.occurred_at")
    ingested = _parse_timestamp(event.get("ingested_at"), f"{event_id}.ingested_at")
    if ingested < occurred:
        raise OutboundLedgerError(f"event ingestion precedes occurrence: {event_id}")
    for field in policy["required_message_fields"]:
        _required_text(event, field, f"event_id={event_id}")
    payload_sha = _required_text(event, "payload_sha256", f"event_id={event_id}")
    if not SHA256_RE.fullmatch(payload_sha):
        raise OutboundLedgerError(f"invalid payload SHA-256: {event_id}")
    payload_ref = _required_text(event, "payload_ref", f"event_id={event_id}")
    attributes = event.get("attributes")
    if not isinstance(attributes, dict):
        raise OutboundLedgerError(f"event attributes must be an object: {event_id}")
    forbidden = {str(item).casefold() for item in policy["forbidden_attribute_keys"]}
    paths = _forbidden_key_paths(attributes, forbidden)
    if paths:
        raise OutboundLedgerError(
            f"raw content or secret-bearing attributes are forbidden: {event_id}/{paths[0]}"
        )
    if event_type == "provider_accepted":
        _required_text(attributes, "provider_message_id", f"event_id={event_id}.attributes")
    if event_type == "reply_received":
        _parse_timestamp(
            attributes.get("webhook_received_at"),
            f"{event_id}.attributes.webhook_received_at",
        )
        if not payload_ref.startswith("secure://"):
            raise OutboundLedgerError(f"reply payload_ref must use secure://: {event_id}")
    if event_type == "draft_ready":
        _required_text(attributes, "source_event_id", f"event_id={event_id}.attributes")
        _required_text(attributes, "intent", f"event_id={event_id}.attributes")
        _required_text(attributes, "workflow_id", f"event_id={event_id}.attributes")
    if event_type == "booking_recorded":
        _required_text(attributes, "source_event_id", f"event_id={event_id}.attributes")
    event["provider"] = provider
    event["provider_event_id"] = provider_event_id
    event["occurred_at"] = _iso(occurred)
    event["ingested_at"] = _iso(ingested)
    return event


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_events (
    event_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    message_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    template_variant_id TEXT NOT NULL,
    sender_identity_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    canonical_sha256 TEXT NOT NULL,
    UNIQUE(provider, provider_event_id)
);

CREATE TRIGGER IF NOT EXISTS outbound_events_no_update
BEFORE UPDATE ON outbound_events
BEGIN
    SELECT RAISE(ABORT, 'outbound_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS outbound_events_no_delete
BEFORE DELETE ON outbound_events
BEGIN
    SELECT RAISE(ABORT, 'outbound_events is append-only');
END;
"""


def connect(path):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(destination)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO ledger_metadata(key, value) VALUES('schema_version', ?)",
        ("outreach.outbound-ledger-sqlite.v1",),
    )
    conn.commit()
    return conn


def _event_storage_tuple(event):
    canonical_hash = _canonical_hash(event)
    return (
        event["event_id"],
        event["provider"],
        event["provider_event_id"],
        event["event_type"],
        event["occurred_at"],
        event["ingested_at"],
        event["message_id"],
        event["lead_id"],
        event["thread_id"],
        event["campaign_id"],
        event["template_variant_id"],
        event["sender_identity_id"],
        event["payload_sha256"],
        event["payload_ref"],
        _canonical(event["attributes"]),
        canonical_hash,
    )


def ingest_batch(conn, payload, policy):
    if payload.get("schema_version") != BATCH_SCHEMA:
        raise OutboundLedgerError("unsupported outbound event batch schema")
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise OutboundLedgerError("outbound event batch requires events")
    events = [validate_event(item, policy) for item in raw_events]
    inserted = 0
    duplicates = 0
    with conn:
        for event in events:
            canonical_hash = _canonical_hash(event)
            existing = conn.execute(
                "SELECT canonical_sha256 FROM outbound_events WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if existing:
                if existing["canonical_sha256"] != canonical_hash:
                    raise OutboundLedgerError(
                        f"conflicting duplicate event_id: {event['event_id']}"
                    )
                duplicates += 1
                continue
            provider_existing = conn.execute(
                "SELECT event_id, canonical_sha256 FROM outbound_events WHERE provider = ? AND provider_event_id = ?",
                (event["provider"], event["provider_event_id"]),
            ).fetchone()
            if provider_existing:
                raise OutboundLedgerError(
                    "provider event id already belongs to a different event: "
                    f"{event['provider']}/{event['provider_event_id']}"
                )
            conn.execute(
                """
                INSERT INTO outbound_events(
                    event_id, provider, provider_event_id, event_type, occurred_at,
                    ingested_at, message_id, lead_id, thread_id, campaign_id,
                    template_variant_id, sender_identity_id, payload_sha256,
                    payload_ref, attributes_json, canonical_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _event_storage_tuple(event),
            )
            inserted += 1
    return {"submitted": len(events), "inserted": inserted, "duplicates": duplicates}


def _load_events(conn):
    rows = conn.execute(
        "SELECT * FROM outbound_events ORDER BY occurred_at, ingested_at, event_id"
    ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["attributes"] = json.loads(event.pop("attributes_json"))
        events.append(event)
    return events


def project(conn):
    messages = {}
    replies = {}
    drafts = {}
    bookings = []
    unresolved = []
    events = _load_events(conn)
    for event in events:
        event_type = event["event_type"]
        message_id = event["message_id"]
        if event_type == "message_registered":
            existing = messages.get(message_id)
            identity = {
                "message_id": message_id,
                "lead_id": event["lead_id"],
                "thread_id": event["thread_id"],
                "campaign_id": event["campaign_id"],
                "template_variant_id": event["template_variant_id"],
                "sender_identity_id": event["sender_identity_id"],
                "provider": event["provider"],
            }
            if existing and any(existing[key] != value for key, value in identity.items()):
                unresolved.append(
                    {"event_id": event["event_id"], "reason": "conflicting_message_registration"}
                )
                continue
            if not existing:
                messages[message_id] = {
                    **identity,
                    "status": "registered",
                    "provider_message_id": None,
                    "registered_at": event["occurred_at"],
                    "accepted_at": None,
                    "delivered_at": None,
                    "bounced_at": None,
                    "complained_at": None,
                    "unsubscribed_at": None,
                }
            continue
        message = messages.get(message_id)
        if not message:
            unresolved.append({"event_id": event["event_id"], "reason": "missing_message_registration"})
            continue
        attribution = (
            "lead_id",
            "thread_id",
            "campaign_id",
            "template_variant_id",
            "sender_identity_id",
        )
        if any(message[field] != event[field] for field in attribution):
            unresolved.append({"event_id": event["event_id"], "reason": "message_attribution_conflict"})
            continue
        if event_type == "provider_accepted":
            message["provider_message_id"] = event["attributes"]["provider_message_id"]
            message["accepted_at"] = event["occurred_at"]
            message["status"] = "accepted"
        elif event_type == "delivered":
            if not message["accepted_at"]:
                unresolved.append({"event_id": event["event_id"], "reason": "delivered_without_provider_acceptance"})
            message["delivered_at"] = event["occurred_at"]
            message["status"] = "delivered"
        elif event_type == "bounced":
            message["bounced_at"] = event["occurred_at"]
            message["status"] = "bounced"
        elif event_type == "complained":
            message["complained_at"] = event["occurred_at"]
        elif event_type == "unsubscribed":
            message["unsubscribed_at"] = event["occurred_at"]
        elif event_type == "reply_received":
            if not message["delivered_at"]:
                unresolved.append({"event_id": event["event_id"], "reason": "reply_without_delivery"})
                continue
            replies[event["event_id"]] = {
                "event_id": event["event_id"],
                "message_id": message_id,
                "lead_id": event["lead_id"],
                "template_variant_id": event["template_variant_id"],
                "occurred_at": event["occurred_at"],
                "webhook_received_at": event["attributes"]["webhook_received_at"],
                "payload_ref": event["payload_ref"],
                "intent": None,
                "draft_ready_at": None,
                "workflow_id": None,
            }
        elif event_type == "draft_ready":
            source_event_id = event["attributes"]["source_event_id"]
            reply = replies.get(source_event_id)
            if not reply:
                unresolved.append({"event_id": event["event_id"], "reason": "draft_without_attributed_reply"})
                continue
            if reply["message_id"] != message_id:
                unresolved.append({"event_id": event["event_id"], "reason": "draft_reply_message_conflict"})
                continue
            reply["intent"] = event["attributes"]["intent"]
            reply["draft_ready_at"] = event["occurred_at"]
            reply["workflow_id"] = event["attributes"]["workflow_id"]
            drafts[event["event_id"]] = source_event_id
        elif event_type == "booking_recorded":
            source_event_id = event["attributes"]["source_event_id"]
            if source_event_id not in replies:
                unresolved.append({"event_id": event["event_id"], "reason": "booking_without_attributed_reply"})
                continue
            bookings.append(
                {
                    "event_id": event["event_id"],
                    "source_event_id": source_event_id,
                    "message_id": message_id,
                    "lead_id": event["lead_id"],
                }
            )
    return {
        "events": events,
        "messages": messages,
        "replies": replies,
        "drafts": drafts,
        "bookings": bookings,
        "unresolved": unresolved,
    }


def _rate(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def build_report(conn, ingestion=None):
    projection = project(conn)
    events = projection["events"]
    delivered = {
        key: value for key, value in projection["messages"].items() if value["delivered_at"]
    }
    delivered_messages = set(delivered)
    delivered_leads = {item["lead_id"] for item in delivered.values()}
    replies = list(projection["replies"].values())
    replied_messages = {item["message_id"] for item in replies}
    replied_leads = {item["lead_id"] for item in replies}
    classified = [item for item in replies if item["intent"]]
    non_ooo = [item for item in classified if item["intent"] != "out_of_office"]
    non_ooo_messages = {item["message_id"] for item in non_ooo}
    non_ooo_leads = {item["lead_id"] for item in non_ooo}
    actionable_leads = {item["lead_id"] for item in classified if item["intent"] in ACTIONABLE_INTENTS}
    booking_leads = {item["lead_id"] for item in projection["bookings"]}
    speeds = []
    for reply in replies:
        if not reply["draft_ready_at"]:
            continue
        start = _parse_timestamp(reply["webhook_received_at"], "webhook_received_at")
        end = _parse_timestamp(reply["draft_ready_at"], "draft_ready_at")
        seconds = (end - start).total_seconds()
        if seconds < 0:
            projection["unresolved"].append(
                {"event_id": reply["event_id"], "reason": "draft_ready_before_webhook"}
            )
            continue
        speeds.append(seconds)

    variants = []
    grouped_messages = defaultdict(dict)
    for message_id, message in delivered.items():
        grouped_messages[message["template_variant_id"]][message_id] = message
    grouped_replies = defaultdict(list)
    for reply in replies:
        grouped_replies[reply["template_variant_id"]].append(reply)
    for variant_id in sorted(grouped_messages):
        messages = grouped_messages[variant_id]
        variant_replies = grouped_replies.get(variant_id, [])
        reply_message_ids = {item["message_id"] for item in variant_replies}
        reply_lead_ids = {item["lead_id"] for item in variant_replies}
        adjusted = [item for item in variant_replies if item["intent"] and item["intent"] != "out_of_office"]
        adjusted_message_ids = {item["message_id"] for item in adjusted}
        adjusted_lead_ids = {item["lead_id"] for item in adjusted}
        leads = {item["lead_id"] for item in messages.values()}
        variants.append(
            {
                "template_variant_id": variant_id,
                "delivered_emails": len(messages),
                "contacted_leads": len(leads),
                "emails_with_reply": len(reply_message_ids),
                "leads_with_reply": len(reply_lead_ids),
                "emails_with_non_ooo_reply": len(adjusted_message_ids),
                "leads_with_non_ooo_reply": len(adjusted_lead_ids),
                "per_email_reply_rate": _rate(len(reply_message_ids), len(messages)),
                "per_lead_reply_rate": _rate(len(reply_lead_ids), len(leads)),
                "per_email_ooo_adjusted_reply_rate": _rate(len(adjusted_message_ids), len(messages)),
                "per_lead_ooo_adjusted_reply_rate": _rate(len(adjusted_lead_ids), len(leads)),
                "reply_intents": dict(sorted(Counter(item["intent"] or "unclassified" for item in variant_replies).items())),
            }
        )
    return {
        "schema_version": REPORT_SCHEMA,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "mode": "offline_shadow",
        "safety": {
            "provider_api_calls_allowed": False,
            "email_send_allowed": False,
            "live_crm_writes_allowed": False,
            "raw_message_body_stored": False,
            "events_append_only": True,
        },
        "ingestion": ingestion or {},
        "ledger": {
            "event_count": len(events),
            "event_types": dict(sorted(Counter(item["event_type"] for item in events).items())),
            "unresolved_count": len(projection["unresolved"]),
            "unresolved": projection["unresolved"],
        },
        "definitions": {
            "ooo_adjusted": "Only classified non-OOO replies enter the adjusted numerator; unclassified replies are excluded rather than guessed.",
            "speed_to_draft": "draft_ready occurred_at minus reply webhook_received_at",
            "per_email": "distinct delivered message IDs",
            "per_lead": "distinct lead IDs with delivered messages",
        },
        "funnel_metrics": {
            "delivered_emails": len(delivered_messages),
            "contacted_leads": len(delivered_leads),
            "emails_with_reply": len(replied_messages),
            "leads_with_reply": len(replied_leads),
            "emails_with_non_ooo_reply": len(non_ooo_messages),
            "leads_with_non_ooo_reply": len(non_ooo_leads),
            "leads_with_actionable_reply": len(actionable_leads),
            "leads_with_booking": len(booking_leads),
            "per_email_reply_rate": _rate(len(replied_messages), len(delivered_messages)),
            "per_lead_reply_rate": _rate(len(replied_leads), len(delivered_leads)),
            "per_email_ooo_adjusted_reply_rate": _rate(len(non_ooo_messages), len(delivered_messages)),
            "per_lead_ooo_adjusted_reply_rate": _rate(len(non_ooo_leads), len(delivered_leads)),
            "per_lead_actionable_conversion_rate": _rate(len(actionable_leads), len(delivered_leads)),
            "per_lead_booking_conversion_rate": _rate(len(booking_leads), len(delivered_leads)),
        },
        "speed_to_draft": {
            "measured_replies": len(speeds),
            "average_seconds": round(sum(speeds) / len(speeds), 3) if speeds else None,
            "maximum_seconds": max(speeds) if speeds else None,
        },
        "template_variants": variants,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--events", required=True)
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
        report = build_report(conn, ingestion)
        output = _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "db": str(Path(args.db).expanduser().resolve()),
                    "inserted": ingestion["inserted"],
                    "duplicates": ingestion["duplicates"],
                    "events": report["ledger"]["event_count"],
                    "unresolved": report["ledger"]["unresolved_count"],
                    "email_send_allowed": report["safety"]["email_send_allowed"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (OutboundLedgerError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
