#!/usr/bin/env python3
"""Compile attributed reply events into offline intent and funnel intelligence.

This module is deliberately unable to send, schedule, call provider APIs, or
mutate CRM state. It validates an explicit event envelope, classifies replies
with inspectable phrase evidence, prepares human-review proposals, and reports
per-email, per-lead, OOO-adjusted, speed-to-draft, and variant metrics.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
EVENT_SCHEMA_VERSION = "outreach.reply-events.v1"
REPORT_SCHEMA_VERSION = "outreach.reply-intelligence.v1"
POLICY_SCHEMA_VERSION = "outreach.reply-intelligence-policy.v1"
MARKETING_SCHEMA_VERSION = "outreach.marketing-team.v1"
DEFAULT_POLICY = REPO_ROOT / "workspace/config/reply_intelligence.json"
DEFAULT_MARKETING_TEAM = REPO_ROOT / "workspace/config/marketing_team.json"


class ReplyIntelligenceError(ValueError):
    """Raised when reply evidence cannot be handled safely."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplyIntelligenceError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReplyIntelligenceError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _parse_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ReplyIntelligenceError(f"{field} requires an ISO-8601 timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReplyIntelligenceError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise ReplyIntelligenceError(f"{field} must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise ReplyIntelligenceError(f"{context} requires {field}")
    return value


def _deduplicate(records, primary_key, secondary_key=None):
    by_primary = {}
    by_secondary = {}
    for raw in records:
        record = dict(raw)
        key = _required_text(record, primary_key, "event record")
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        previous = by_primary.get(key)
        if previous and previous[0] != canonical:
            raise ReplyIntelligenceError(f"conflicting duplicate {primary_key}: {key}")
        if secondary_key:
            secondary = _required_text(record, secondary_key, f"{primary_key}={key}")
            secondary_previous = by_secondary.get(secondary)
            if secondary_previous and secondary_previous[0] != canonical:
                raise ReplyIntelligenceError(
                    f"conflicting duplicate {secondary_key}: {secondary}"
                )
            by_secondary[secondary] = (canonical, record)
        by_primary[key] = (canonical, record)
    return [item[1] for item in by_primary.values()]


def load_policy(path=DEFAULT_POLICY):
    policy = _read_json(path)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ReplyIntelligenceError("unsupported reply intelligence policy schema")
    safety = policy.get("safety") or {}
    forbidden = (
        safety.get("external_send_allowed"),
        safety.get("crm_mutation_allowed"),
        safety.get("provider_api_allowed"),
        safety.get("automatic_subsequence_allowed"),
    )
    if any(value is not False for value in forbidden):
        raise ReplyIntelligenceError("reply intelligence policy enables an external action")
    classification = policy.get("classification") or {}
    precedence = classification.get("precedence") or []
    intents = classification.get("intents") or {}
    if not precedence or set(precedence) != set(intents):
        raise ReplyIntelligenceError("classification precedence must list every intent exactly once")
    if precedence[-1] != "ambiguous":
        raise ReplyIntelligenceError("ambiguous must be the final classification fallback")
    return policy


def load_marketing_team(path=DEFAULT_MARKETING_TEAM):
    marketing = _read_json(path)
    if marketing.get("schema_version") != MARKETING_SCHEMA_VERSION:
        raise ReplyIntelligenceError("unsupported marketing team schema")
    return marketing


def _phrase_matches(body, phrase):
    pattern = r"(?<!\w)" + re.escape(phrase.casefold()) + r"(?!\w)"
    return bool(re.search(pattern, body.casefold()))


def classify_reply(body, policy):
    normalized = " ".join(str(body or "").split())
    if not normalized:
        raise ReplyIntelligenceError("reply body cannot be blank")
    classification = policy["classification"]
    intents = classification["intents"]
    for intent in classification["precedence"]:
        rule = intents[intent]
        matched = [phrase for phrase in rule.get("phrases") or [] if _phrase_matches(normalized, phrase)]
        if matched or (rule.get("match_question_mark") is True and "?" in normalized):
            evidence = [f"phrase:{phrase}" for phrase in matched]
            if rule.get("match_question_mark") is True and "?" in normalized:
                evidence.append("punctuation:question_mark")
            return {
                "intent": intent,
                "confidence": "deterministic_rule",
                "evidence": evidence,
                "proposed_action": rule["proposed_action"],
                "draft_allowed": rule["draft_allowed"],
            }
    fallback = intents["ambiguous"]
    return {
        "intent": "ambiguous",
        "confidence": "fallback",
        "evidence": ["no_configured_rule_matched"],
        "proposed_action": fallback["proposed_action"],
        "draft_allowed": fallback["draft_allowed"],
    }


def _extract_return_date(body):
    month = (
        r"January|February|March|April|May|June|July|August|September|"
        r"October|November|December"
    )
    candidates = re.findall(
        rf"\b(?:back|returning|returns?|until)\s+(?:on\s+)?(({month})\s+\d{{1,2}},?\s+\d{{4}})\b",
        body,
        flags=re.IGNORECASE,
    )
    for full, _ in candidates:
        for pattern in ("%B %d, %Y", "%B %d %Y"):
            try:
                return datetime.strptime(full.title(), pattern).date().isoformat()
            except ValueError:
                continue
    iso_match = re.search(
        r"\b(?:back|returning|returns?|until)\s+(?:on\s+)?(\d{4}-\d{2}-\d{2})\b",
        body,
        flags=re.IGNORECASE,
    )
    if iso_match:
        try:
            return datetime.strptime(iso_match.group(1), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    return None


def _validate_outbound(payload):
    records = payload.get("outbound_messages")
    if not isinstance(records, list):
        raise ReplyIntelligenceError("reply event payload requires outbound_messages")
    messages = _deduplicate(records, "message_id")
    indexed = {}
    for message in messages:
        message_id = _required_text(message, "message_id", "outbound message")
        _required_text(message, "lead_id", f"message_id={message_id}")
        _required_text(message, "thread_id", f"message_id={message_id}")
        _required_text(message, "template_variant_id", f"message_id={message_id}")
        _required_text(message, "provider", f"message_id={message_id}")
        _parse_timestamp(message.get("sent_at"), f"{message_id}.sent_at")
        if message.get("delivered") is not True:
            raise ReplyIntelligenceError(
                f"offline metrics require delivered=true for outbound message: {message_id}"
            )
        indexed[message_id] = message
    return indexed


def _validate_inbound(payload, outbound):
    records = payload.get("inbound_events")
    if not isinstance(records, list):
        raise ReplyIntelligenceError("reply event payload requires inbound_events")
    events = _deduplicate(records, "event_id", "provider_message_id")
    for event in events:
        event_id = _required_text(event, "event_id", "inbound event")
        lead_id = _required_text(event, "lead_id", f"event_id={event_id}")
        thread_id = _required_text(event, "thread_id", f"event_id={event_id}")
        reply_to = _required_text(
            event, "in_reply_to_message_id", f"event_id={event_id}"
        )
        _required_text(event, "body", f"event_id={event_id}")
        received_at = _parse_timestamp(event.get("received_at"), f"{event_id}.received_at")
        webhook_at = _parse_timestamp(
            event.get("webhook_received_at"), f"{event_id}.webhook_received_at"
        )
        if webhook_at < received_at:
            raise ReplyIntelligenceError(f"webhook precedes provider receipt: {event_id}")
        message = outbound.get(reply_to)
        if not message:
            raise ReplyIntelligenceError(f"reply references unknown outbound message: {reply_to}")
        if message["lead_id"] != lead_id or message["thread_id"] != thread_id:
            raise ReplyIntelligenceError(f"reply attribution conflicts with outbound message: {event_id}")
    return events


def _build_proposal(event, classification, marketing, draft_ready_at, policy):
    intent = classification["intent"]
    configured = (marketing.get("reply_intents") or {}).get(intent) or {}
    resource_id = configured.get("resource", "none")
    resources = marketing.get("resource_library") or {}
    if resource_id != "none" and resource_id not in resources:
        raise ReplyIntelligenceError(f"reply intent references unknown resource: {intent}/{resource_id}")
    speed_seconds = int(
        (draft_ready_at - _parse_timestamp(event["webhook_received_at"], "webhook_received_at"))
        .total_seconds()
    )
    if speed_seconds < 0:
        raise ReplyIntelligenceError(f"draft readiness precedes webhook: {event['event_id']}")
    proposal = {
        "status": "pending_human" if classification["draft_allowed"] else "review_only",
        "proposed_action": classification["proposed_action"],
        "external_action_executed": False,
        "automatic_send_allowed": False,
        "crm_mutation_allowed": False,
        "human_review_required": bool(
            configured.get("human_review_required", True)
            or classification["draft_allowed"]
        ),
        "response_brief": configured.get(
            "next_action", "Review the reply manually; no response has been drafted."
        ),
        "resource_id": resource_id,
        "resource_summary": resources.get(resource_id),
        "subsequence": {
            "status": "proposed_only",
            "automatic_execution_allowed": False,
            "next_step": classification["proposed_action"],
        },
        "draft_ready_at": _iso(draft_ready_at),
        "speed_to_draft_seconds": speed_seconds,
        "within_speed_target": speed_seconds
        <= int(policy["measurement"]["speed_to_draft_target_seconds"]),
    }
    if intent == "out_of_office":
        proposal["subsequence"]["not_before_date"] = _extract_return_date(event["body"])
    return proposal


def _rate(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def evaluate(payload, policy, marketing, generated_at=None):
    if payload.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise ReplyIntelligenceError("unsupported reply event schema")
    generated = generated_at or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        raise ReplyIntelligenceError("generated_at must include a timezone")
    generated = generated.astimezone(timezone.utc)
    outbound = _validate_outbound(payload)
    inbound = _validate_inbound(payload, outbound)

    results = []
    for event in inbound:
        classification = classify_reply(event["body"], policy)
        proposal = _build_proposal(event, classification, marketing, generated, policy)
        message = outbound[event["in_reply_to_message_id"]]
        results.append(
            {
                "event_id": event["event_id"],
                "provider_message_id": event["provider_message_id"],
                "lead_id": event["lead_id"],
                "thread_id": event["thread_id"],
                "in_reply_to_message_id": event["in_reply_to_message_id"],
                "template_variant_id": message["template_variant_id"],
                "received_at": _iso(_parse_timestamp(event["received_at"], "received_at")),
                "classification": classification,
                "proposal": proposal,
            }
        )

    ooo_intent = policy["measurement"]["ooo_intent"]
    actionable = set(policy["measurement"]["actionable_intents"])
    booking_intent = policy["measurement"]["booking_intent"]
    delivered_messages = set(outbound)
    contacted_leads = {message["lead_id"] for message in outbound.values()}
    replied_messages = {item["in_reply_to_message_id"] for item in results}
    replied_leads = {item["lead_id"] for item in results}
    non_ooo = [item for item in results if item["classification"]["intent"] != ooo_intent]
    non_ooo_messages = {item["in_reply_to_message_id"] for item in non_ooo}
    non_ooo_leads = {item["lead_id"] for item in non_ooo}
    actionable_leads = {
        item["lead_id"] for item in results if item["classification"]["intent"] in actionable
    }
    booking_leads = {
        item["lead_id"] for item in results if item["classification"]["intent"] == booking_intent
    }

    variant_messages = defaultdict(set)
    variant_leads = defaultdict(set)
    for message_id, message in outbound.items():
        variant = message["template_variant_id"]
        variant_messages[variant].add(message_id)
        variant_leads[variant].add(message["lead_id"])
    variant_replies = defaultdict(list)
    for item in results:
        variant_replies[item["template_variant_id"]].append(item)
    variants = []
    for variant in sorted(variant_messages):
        replies = variant_replies.get(variant, [])
        reply_message_ids = {item["in_reply_to_message_id"] for item in replies}
        reply_lead_ids = {item["lead_id"] for item in replies}
        adjusted = [item for item in replies if item["classification"]["intent"] != ooo_intent]
        adjusted_message_ids = {item["in_reply_to_message_id"] for item in adjusted}
        adjusted_lead_ids = {item["lead_id"] for item in adjusted}
        variants.append(
            {
                "template_variant_id": variant,
                "delivered_emails": len(variant_messages[variant]),
                "contacted_leads": len(variant_leads[variant]),
                "emails_with_reply": len(reply_message_ids),
                "leads_with_reply": len(reply_lead_ids),
                "emails_with_non_ooo_reply": len(adjusted_message_ids),
                "leads_with_non_ooo_reply": len(adjusted_lead_ids),
                "per_email_reply_rate": _rate(
                    len(reply_message_ids), len(variant_messages[variant])
                ),
                "per_lead_reply_rate": _rate(
                    len(reply_lead_ids), len(variant_leads[variant])
                ),
                "per_email_ooo_adjusted_reply_rate": _rate(
                    len(adjusted_message_ids), len(variant_messages[variant])
                ),
                "per_lead_ooo_adjusted_reply_rate": _rate(
                    len(adjusted_lead_ids), len(variant_leads[variant])
                ),
                "reply_intents": dict(
                    sorted(Counter(item["classification"]["intent"] for item in replies).items())
                ),
            }
        )

    speeds = [item["proposal"]["speed_to_draft_seconds"] for item in results]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": _iso(generated),
        "mode": "offline_shadow",
        "safety": {
            "external_send_allowed": False,
            "crm_mutation_allowed": False,
            "provider_api_allowed": False,
            "automatic_subsequence_allowed": False,
        },
        "definitions": {
            "per_email_reply_rate": "distinct outbound messages receiving at least one attributed reply / delivered outbound messages",
            "per_lead_reply_rate": "distinct leads replying at least once / distinct leads with a delivered outbound message",
            "ooo_adjusted_reply_rate": "same denominator as the corresponding gross rate, excluding replies classified as out_of_office from the numerator",
            "conversion_deduplication": "multiple replies from one lead or to one email count once in lead-level or email-level rates",
        },
        "input": {
            "delivered_outbound_messages": len(delivered_messages),
            "contacted_leads": len(contacted_leads),
            "unique_inbound_events": len(results),
        },
        "funnel_metrics": {
            "emails_with_reply": len(replied_messages),
            "leads_with_reply": len(replied_leads),
            "emails_with_non_ooo_reply": len(non_ooo_messages),
            "leads_with_non_ooo_reply": len(non_ooo_leads),
            "leads_with_actionable_reply": len(actionable_leads),
            "leads_with_booking_intent": len(booking_leads),
            "per_email_reply_rate": _rate(len(replied_messages), len(delivered_messages)),
            "per_lead_reply_rate": _rate(len(replied_leads), len(contacted_leads)),
            "per_email_ooo_adjusted_reply_rate": _rate(
                len(non_ooo_messages), len(delivered_messages)
            ),
            "per_lead_ooo_adjusted_reply_rate": _rate(
                len(non_ooo_leads), len(contacted_leads)
            ),
            "per_lead_actionable_conversion_rate": _rate(
                len(actionable_leads), len(contacted_leads)
            ),
            "per_lead_booking_conversion_rate": _rate(
                len(booking_leads), len(contacted_leads)
            ),
        },
        "speed_to_draft": {
            "target_seconds": int(policy["measurement"]["speed_to_draft_target_seconds"]),
            "measured_replies": len(speeds),
            "within_target": sum(
                1
                for item in results
                if item["proposal"]["within_speed_target"] is True
            ),
            "average_seconds": round(sum(speeds) / len(speeds), 3) if speeds else None,
            "maximum_seconds": max(speeds) if speeds else None,
        },
        "template_variants": variants,
        "reply_results": results,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--marketing-team", default=str(DEFAULT_MARKETING_TEAM))
    parser.add_argument("--generated-at")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        generated = (
            _parse_timestamp(args.generated_at, "generated_at")
            if args.generated_at
            else datetime.now(timezone.utc)
        )
        report = evaluate(
            _read_json(args.events),
            load_policy(args.policy),
            load_marketing_team(args.marketing_team),
            generated_at=generated,
        )
        output = _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "replies": report["input"]["unique_inbound_events"],
                    "mode": report["mode"],
                    "external_send_allowed": report["safety"]["external_send_allowed"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except ReplyIntelligenceError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
