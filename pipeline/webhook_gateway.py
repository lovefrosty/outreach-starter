#!/usr/bin/env python3
"""Authenticate and normalize provider webhook payloads without side effects."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
POLICY_SCHEMA = "outreach.webhook-gateway-policy.v1"
FIXTURE_SCHEMA = "outreach.webhook-request-fixtures.v1"
EVENT_SCHEMA = "outreach.outbound-event.v1"
BATCH_SCHEMA = "outreach.outbound-event-batch.v1"
REPORT_SCHEMA = "outreach.webhook-normalization-report.v1"
DEFAULT_POLICY = REPO_ROOT / "workspace/config/webhook_gateway.json"


class WebhookGatewayError(ValueError):
    """Raised when webhook authentication or normalization is unsafe."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WebhookGatewayError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WebhookGatewayError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise WebhookGatewayError(f"{context} requires {field}")
    return value


def _timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise WebhookGatewayError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise WebhookGatewayError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise WebhookGatewayError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_body(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _path(value, dotted):
    current = value
    for segment in str(dotted).split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def load_policy(path=DEFAULT_POLICY):
    policy = _read_json(path)
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise WebhookGatewayError("unsupported webhook gateway policy schema")
    if policy.get("event_schema_version") != EVENT_SCHEMA:
        raise WebhookGatewayError("webhook gateway has an unexpected event schema")
    safety = policy.get("safety") or {}
    required_false = (
        "http_listener_enabled",
        "network_calls_allowed",
        "ledger_writes_allowed",
        "workflow_invocation_allowed",
        "email_send_allowed",
        "crm_mutation_allowed",
        "raw_body_output_allowed",
    )
    if any(safety.get(key) is not False for key in required_false):
        raise WebhookGatewayError("webhook gateway policy enables an external action")
    if int(policy.get("max_clock_skew_seconds") or 0) <= 0:
        raise WebhookGatewayError("webhook replay window must be positive")
    profile_ids = set()
    canonical_types = set()
    for profile in policy.get("profiles") or []:
        profile_id = _required_text(profile, "profile_id", "webhook profile")
        if profile_id in profile_ids:
            raise WebhookGatewayError(f"duplicate webhook profile: {profile_id}")
        profile_ids.add(profile_id)
        _required_text(profile, "provider", f"profile_id={profile_id}")
        auth = profile.get("authentication") or {}
        if auth.get("method") != "hmac_sha256":
            raise WebhookGatewayError(f"unsupported webhook authentication: {profile_id}")
        for field in ("signature_header", "timestamp_header", "signature_prefix"):
            _required_text(auth, field, f"profile_id={profile_id}.authentication")
        if auth.get("signed_content") != "timestamp_dot_raw_body":
            raise WebhookGatewayError(f"unsupported signed content: {profile_id}")
        paths = profile.get("paths") or {}
        for field in (
            "provider_event_id",
            "provider_event_type",
            "occurred_at",
            *policy.get("required_attribution_fields", []),
        ):
            _required_text(paths, field, f"profile_id={profile_id}.paths")
        for provider_type, mapping in (profile.get("event_types") or {}).items():
            if not str(provider_type).strip():
                raise WebhookGatewayError(f"empty provider event type: {profile_id}")
            canonical = _required_text(mapping, "canonical", f"event_type={provider_type}")
            canonical_types.add(canonical)
            if not isinstance(mapping.get("attributes"), dict):
                raise WebhookGatewayError(f"event attributes mapping must be an object: {profile_id}/{provider_type}")
    if not profile_ids or not canonical_types:
        raise WebhookGatewayError("webhook gateway policy has no provider profiles")
    return policy


def sign_request(secret, timestamp, raw_body, prefix="v1="):
    material = f"{timestamp}.{raw_body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()
    return f"{prefix}{digest}"


def _authenticate(profile, headers, raw_body, secret, received_at, max_skew):
    if not secret:
        raise WebhookGatewayError("webhook secret is required")
    normalized_headers = {str(key).casefold(): str(value).strip() for key, value in headers.items()}
    auth = profile["authentication"]
    signature_name = auth["signature_header"].casefold()
    timestamp_name = auth["timestamp_header"].casefold()
    signature = normalized_headers.get(signature_name)
    signed_at_raw = normalized_headers.get(timestamp_name)
    if not signature or not signed_at_raw:
        raise WebhookGatewayError("webhook signature or timestamp header is missing")
    signed_at = _timestamp(signed_at_raw, "signature timestamp")
    skew = abs((received_at - signed_at).total_seconds())
    if skew > max_skew:
        raise WebhookGatewayError(f"webhook signature timestamp outside replay window: {int(skew)}s")
    expected = sign_request(secret, signed_at_raw, raw_body, auth["signature_prefix"])
    if not hmac.compare_digest(signature, expected):
        raise WebhookGatewayError("webhook signature mismatch")
    return signed_at


def normalize_request(profile_id, headers, raw_body, secret, received_at, policy=None):
    policy = policy or load_policy()
    profiles = {item["profile_id"]: item for item in policy["profiles"]}
    profile = profiles.get(profile_id)
    if not profile:
        raise WebhookGatewayError(f"unknown webhook profile: {profile_id}")
    received = _timestamp(received_at, "received_at")
    _authenticate(
        profile,
        headers,
        raw_body,
        secret,
        received,
        int(policy["max_clock_skew_seconds"]),
    )
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise WebhookGatewayError(f"invalid webhook JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise WebhookGatewayError("webhook body must be a JSON object")
    paths = profile["paths"]
    provider_event_id = str(_path(body, paths["provider_event_id"]) or "").strip()
    provider_type = str(_path(body, paths["provider_event_type"]) or "").strip()
    if not provider_event_id or not provider_type:
        return {"status": "quarantined", "reason": "missing_provider_identity"}
    mapping = profile["event_types"].get(provider_type)
    if not mapping:
        return {
            "status": "quarantined",
            "reason": "unsupported_provider_event_type",
            "provider": profile["provider"],
            "provider_event_id": provider_event_id,
            "provider_event_type": provider_type,
        }
    extracted = {}
    for field in policy["required_attribution_fields"]:
        extracted[field] = str(_path(body, paths[field]) or "").strip()
    missing = sorted(field for field, value in extracted.items() if not value)
    if missing:
        return {
            "status": "quarantined",
            "reason": "missing_attribution",
            "missing_fields": missing,
            "provider": profile["provider"],
            "provider_event_id": provider_event_id,
        }
    occurred_raw = str(_path(body, paths["occurred_at"]) or "").strip()
    try:
        occurred = _timestamp(occurred_raw, "occurred_at")
    except WebhookGatewayError:
        return {
            "status": "quarantined",
            "reason": "invalid_occurred_at",
            "provider": profile["provider"],
            "provider_event_id": provider_event_id,
        }
    if occurred > received:
        return {
            "status": "quarantined",
            "reason": "event_occurs_after_ingestion",
            "provider": profile["provider"],
            "provider_event_id": provider_event_id,
        }
    attributes = {}
    for target, source_path in mapping["attributes"].items():
        value = _path(body, source_path)
        if value not in (None, ""):
            attributes[target] = value
    if mapping["canonical"] == "reply_received":
        attributes["webhook_received_at"] = _iso(received)
        if not extracted["payload_ref"].startswith("secure://"):
            return {
                "status": "quarantined",
                "reason": "reply_payload_ref_not_secure",
                "provider": profile["provider"],
                "provider_event_id": provider_event_id,
            }
    if mapping["canonical"] == "provider_accepted" and not attributes.get("provider_message_id"):
        return {
            "status": "quarantined",
            "reason": "missing_provider_message_id",
            "provider": profile["provider"],
            "provider_event_id": provider_event_id,
        }
    deterministic = hashlib.sha256(
        f"{profile['provider']}:{provider_event_id}".encode("utf-8")
    ).hexdigest()[:24]
    event = {
        "schema_version": EVENT_SCHEMA,
        "event_id": f"evt-webhook-{deterministic}",
        "provider": profile["provider"],
        "provider_event_id": provider_event_id,
        "event_type": mapping["canonical"],
        "occurred_at": _iso(occurred),
        "ingested_at": _iso(received),
        "message_id": extracted["message_id"],
        "lead_id": extracted["lead_id"],
        "thread_id": extracted["thread_id"],
        "campaign_id": extracted["campaign_id"],
        "template_variant_id": extracted["template_variant_id"],
        "sender_identity_id": extracted["sender_identity_id"],
        "payload_sha256": hashlib.sha256(raw_body.encode("utf-8")).hexdigest(),
        "payload_ref": extracted["payload_ref"],
        "attributes": attributes,
    }
    return {"status": "accepted", "event": event}


def normalize_fixtures(payload, policy=None):
    policy = policy or load_policy()
    if payload.get("schema_version") != FIXTURE_SCHEMA:
        raise WebhookGatewayError("unsupported webhook fixture schema")
    secret = _required_text(payload, "synthetic_secret", "webhook fixtures")
    accepted = []
    quarantined = []
    seen = {}
    duplicates = 0
    for request in payload.get("requests") or []:
        fixture_id = _required_text(request, "fixture_id", "webhook fixture")
        profile_id = _required_text(request, "profile_id", f"fixture_id={fixture_id}")
        raw_body = _canonical_body(request.get("body"))
        signed_at = _required_text(request, "signature_timestamp", f"fixture_id={fixture_id}")
        profile = next((item for item in policy["profiles"] if item["profile_id"] == profile_id), None)
        if not profile:
            raise WebhookGatewayError(f"unknown webhook profile: {profile_id}")
        auth = profile["authentication"]
        headers = {
            auth["timestamp_header"]: signed_at,
            auth["signature_header"]: sign_request(secret, signed_at, raw_body, auth["signature_prefix"]),
        }
        result = normalize_request(
            profile_id,
            headers,
            raw_body,
            secret,
            request.get("received_at"),
            policy,
        )
        if result["status"] == "quarantined":
            quarantined.append({"fixture_id": fixture_id, **result})
            continue
        event = result["event"]
        key = (event["provider"], event["provider_event_id"])
        canonical = _canonical_body(event)
        if key in seen:
            if seen[key] != canonical:
                raise WebhookGatewayError(
                    f"conflicting duplicate provider event: {event['provider']}/{event['provider_event_id']}"
                )
            duplicates += 1
            continue
        seen[key] = canonical
        accepted.append(event)
    return {
        "schema_version": REPORT_SCHEMA,
        "mode": "offline_shadow",
        "safety": {
            **policy["safety"],
            "raw_body_emitted": False,
        },
        "summary": {
            "submitted": len(payload.get("requests") or []),
            "accepted": len(accepted),
            "quarantined": len(quarantined),
            "duplicates": duplicates,
        },
        "canonical_batch": {"schema_version": BATCH_SCHEMA, "events": accepted},
        "quarantine": quarantined,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    return parser


def main():
    args = build_parser().parse_args()
    try:
        report = normalize_fixtures(_read_json(args.fixtures), load_policy(args.policy))
        output = _write_json(args.output, report)
        print(json.dumps({"output": str(output), **report["summary"], "network_calls_allowed": report["safety"]["network_calls_allowed"]}, indent=2, sort_keys=True))
        return 0
    except WebhookGatewayError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
