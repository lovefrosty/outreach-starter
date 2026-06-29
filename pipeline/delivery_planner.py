#!/usr/bin/env python3
"""Build an explainable, non-executing outbound delivery plan.

The planner consumes synthetic or exported health/quota snapshots and observed
recipient MX hosts. It never performs DNS lookups, provider calls, CRM writes,
or sends. Batch usage updates exist only inside the returned simulation.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
REGISTRY_SCHEMA = "outreach.sender-registry.v1"
SEGMENTATION_SCHEMA = "outreach.recipient-mail-segmentation.v1"
INPUT_SCHEMA = "outreach.delivery-plan-input.v1"
OUTPUT_SCHEMA = "outreach.delivery-plan.v1"
DEFAULT_REGISTRY = REPO_ROOT / "workspace/config/sender_registry.json"
DEFAULT_SEGMENTATION = REPO_ROOT / "workspace/config/recipient_esp_rules.json"
FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "password",
    "secret",
    "token",
    "credential",
    "credentials",
    "private_key",
}


class DeliveryPlannerError(ValueError):
    """Raised when a delivery simulation cannot be evaluated safely."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeliveryPlannerError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DeliveryPlannerError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _parse_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise DeliveryPlannerError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryPlannerError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise DeliveryPlannerError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise DeliveryPlannerError(f"{context} requires {field}")
    return value


def _validate_no_secret_keys(value, path="root"):
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() in FORBIDDEN_SECRET_KEYS:
                raise DeliveryPlannerError(f"secret-bearing key is forbidden in planner input: {path}.{key}")
            _validate_no_secret_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_no_secret_keys(nested, f"{path}[{index}]")


def _validate_quota(record, context):
    try:
        quota = int(record.get("daily_quota"))
        used = int(record.get("sent_today"))
    except (TypeError, ValueError) as exc:
        raise DeliveryPlannerError(f"{context} requires integer daily_quota and sent_today") from exc
    if quota <= 0 or used < 0 or used > quota:
        raise DeliveryPlannerError(f"invalid quota snapshot for {context}")


def load_registry(path=DEFAULT_REGISTRY):
    registry = _read_json(path)
    if registry.get("schema_version") != REGISTRY_SCHEMA:
        raise DeliveryPlannerError("unsupported sender registry schema")
    _validate_no_secret_keys(registry)
    safety = registry.get("safety") or {}
    false_controls = (
        "external_send_allowed",
        "provider_api_allowed",
        "dns_lookup_allowed",
        "credential_material_allowed",
        "crm_mutation_allowed",
    )
    if any(safety.get(key) is not False for key in false_controls):
        raise DeliveryPlannerError("sender registry enables a forbidden external action")
    if safety.get("usage_updates_are_simulated_only") is not True:
        raise DeliveryPlannerError("sender registry does not constrain usage updates to simulation")

    approved_routes = set(registry.get("approved_campaign_routes") or [])
    if not approved_routes:
        raise DeliveryPlannerError("sender registry has no approved campaign routes")

    service_groups = {}
    for group in registry.get("service_groups") or []:
        group_id = _required_text(group, "service_group_id", "service group")
        if group_id in service_groups:
            raise DeliveryPlannerError(f"duplicate service_group_id: {group_id}")
        service_groups[group_id] = group

    domains = {}
    domain_names = set()
    for domain in registry.get("domains") or []:
        domain_id = _required_text(domain, "domain_id", "sender domain")
        domain_name = _required_text(domain, "domain", f"domain_id={domain_id}").casefold()
        if domain_id in domains or domain_name in domain_names:
            raise DeliveryPlannerError(f"duplicate sender domain: {domain_id}/{domain_name}")
        _validate_quota(domain, f"domain_id={domain_id}")
        routes = set(domain.get("approved_campaign_routes") or [])
        if not routes or not routes.issubset(approved_routes):
            raise DeliveryPlannerError(f"invalid campaign routes for domain_id={domain_id}")
        domains[domain_id] = domain
        domain_names.add(domain_name)

    identities = {}
    addresses = set()
    for identity in registry.get("identities") or []:
        identity_id = _required_text(identity, "sender_identity_id", "sender identity")
        address = _required_text(identity, "address", f"sender_identity_id={identity_id}").casefold()
        domain_id = _required_text(identity, "domain_id", f"sender_identity_id={identity_id}")
        group_id = _required_text(identity, "service_group_id", f"sender_identity_id={identity_id}")
        if identity_id in identities or address in addresses:
            raise DeliveryPlannerError(f"duplicate sender identity: {identity_id}/{address}")
        if domain_id not in domains:
            raise DeliveryPlannerError(f"sender identity references unknown domain: {identity_id}")
        if group_id not in service_groups:
            raise DeliveryPlannerError(f"sender identity references unknown service group: {identity_id}")
        expected_domain = str(domains[domain_id]["domain"]).casefold()
        if not address.endswith("@" + expected_domain):
            raise DeliveryPlannerError(f"sender address/domain mismatch: {identity_id}")
        _validate_quota(identity, f"sender_identity_id={identity_id}")
        routes = set(identity.get("approved_campaign_routes") or [])
        if not routes or not routes.issubset(approved_routes):
            raise DeliveryPlannerError(f"invalid campaign routes for sender_identity_id={identity_id}")
        segments = set(identity.get("allowed_recipient_segments") or [])
        affinities = identity.get("segment_affinity") or {}
        if not segments or not segments.issubset(set(affinities)):
            raise DeliveryPlannerError(f"missing segment affinity for sender_identity_id={identity_id}")
        identities[identity_id] = identity
        addresses.add(address)
    if not identities:
        raise DeliveryPlannerError("sender registry has no identities")
    return registry


def load_segmentation(path=DEFAULT_SEGMENTATION):
    config = _read_json(path)
    if config.get("schema_version") != SEGMENTATION_SCHEMA:
        raise DeliveryPlannerError("unsupported recipient segmentation schema")
    if int(config.get("max_mx_age_days", 0)) <= 0:
        raise DeliveryPlannerError("max_mx_age_days must be positive")
    if config.get("unknown_segment_policy") != "hold":
        raise DeliveryPlannerError("unknown recipient mail segments must fail closed")
    seen_ids = set()
    for dimension in ("mailbox_provider", "security_gateway"):
        rules = (config.get("dimensions") or {}).get(dimension)
        if not isinstance(rules, list):
            raise DeliveryPlannerError(f"missing segmentation dimension: {dimension}")
        for rule in rules:
            rule_id = _required_text(rule, "id", f"segmentation dimension={dimension}")
            if rule_id in seen_ids:
                raise DeliveryPlannerError(f"duplicate recipient segment id: {rule_id}")
            seen_ids.add(rule_id)
            suffixes = rule.get("mx_suffixes") or []
            if not suffixes or any(not str(item).startswith(".") for item in suffixes):
                raise DeliveryPlannerError(f"invalid MX suffix rules for segment: {rule_id}")
    mapping = config.get("routing_segments") or {}
    required = seen_ids | {"other", "unknown"}
    if not required.issubset(set(mapping)):
        raise DeliveryPlannerError("routing segment mapping is incomplete")
    return config


def _host_matches(host, suffix):
    bare = suffix.lstrip(".")
    return host == bare or host.endswith(suffix)


def classify_recipient(candidate, segmentation, plan_at):
    hosts = sorted(
        {
            str(host).strip().casefold().rstrip(".")
            for host in candidate.get("recipient_mx_hosts") or []
            if str(host).strip()
        }
    )
    observed_at = _parse_timestamp(
        candidate.get("mx_observed_at"),
        f"{candidate.get('decision_id', '<unknown>')}.mx_observed_at",
    )
    age_seconds = (plan_at - observed_at).total_seconds()
    if age_seconds < 0:
        raise DeliveryPlannerError(
            f"MX observation occurs after plan time: {candidate.get('decision_id')}"
        )
    age_days = age_seconds / 86400
    freshness = "fresh" if age_days <= int(segmentation["max_mx_age_days"]) else "stale"

    matches = {}
    matched_hosts = {}
    for dimension, rules in segmentation["dimensions"].items():
        found = set()
        evidence = {}
        for rule in rules:
            rule_hosts = [
                host
                for host in hosts
                if any(_host_matches(host, suffix.casefold()) for suffix in rule["mx_suffixes"])
            ]
            if rule_hosts:
                found.add(rule["id"])
                evidence[rule["id"]] = rule_hosts
        matches[dimension] = sorted(found)
        matched_hosts[dimension] = evidence

    conflict = any(len(values) > 1 for values in matches.values())
    mailbox = matches["mailbox_provider"][0] if len(matches["mailbox_provider"]) == 1 else None
    gateway = matches["security_gateway"][0] if len(matches["security_gateway"]) == 1 else None
    if conflict:
        status = "conflict"
        routing_source = "unknown"
    elif not hosts:
        status = "unknown"
        routing_source = "unknown"
    elif freshness == "stale":
        status = "stale"
        routing_source = gateway or mailbox or "other"
    else:
        status = "fresh"
        routing_source = gateway or mailbox or "other"
    routing_segment = segmentation["routing_segments"].get(routing_source, "unknown")
    return {
        "status": status,
        "mx_observed_at": _iso(observed_at),
        "mx_age_days": round(age_days, 6),
        "mx_hosts": hosts,
        "mailbox_provider": mailbox or ("other" if hosts and not gateway and not conflict else "unknown"),
        "security_gateway": gateway or "none",
        "routing_segment": routing_segment,
        "matched_hosts": matched_hosts,
        "inference_limit": (
            "A security gateway can hide the underlying mailbox provider; gateway and mailbox dimensions remain separate."
        ),
    }


def _deduplicate_candidates(candidates):
    by_decision = {}
    by_message = {}
    for raw in candidates:
        candidate = dict(raw)
        decision_id = _required_text(candidate, "decision_id", "delivery candidate")
        message_id = _required_text(
            candidate, "approved_message_id", f"decision_id={decision_id}"
        )
        canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        previous = by_decision.get(decision_id)
        if previous and previous[0] != canonical:
            raise DeliveryPlannerError(f"conflicting duplicate decision_id: {decision_id}")
        message_previous = by_message.get(message_id)
        if message_previous and message_previous[0] != canonical:
            raise DeliveryPlannerError(f"approved message appears in multiple decisions: {message_id}")
        by_decision[decision_id] = (canonical, candidate)
        by_message[message_id] = (canonical, candidate)
    return [item[1] for item in by_decision.values()]


def _global_holds(candidate, segment, approved_routes):
    holds = []
    if candidate["suppression_status"] != "clear":
        holds.append("recipient_suppressed")
    if candidate["human_approval_status"] != "approved":
        holds.append("human_approval_missing")
    if candidate["campaign_route"] not in approved_routes:
        holds.append("route_unapproved")
    if segment["status"] == "stale":
        holds.append("recipient_mx_stale")
    elif segment["status"] in {"unknown", "conflict"}:
        holds.append("recipient_segment_unknown")
    return sorted(set(holds))


def _identity_reasons(identity, domain, service_group, candidate, segment, usage):
    reasons = []
    identity_id = identity["sender_identity_id"]
    domain_id = domain["domain_id"]
    if domain.get("dns_ready") is not True:
        reasons.append("dns_not_ready")
    if domain.get("paused") is True:
        reasons.append("domain_paused")
    if domain.get("health") != "healthy":
        reasons.append("domain_unhealthy")
    if identity.get("paused") is True:
        reasons.append("mailbox_paused")
    if identity.get("health") != "healthy":
        reasons.append("mailbox_unhealthy")
    if service_group.get("paused") is True or service_group.get("health") != "healthy":
        reasons.append("provider_unhealthy")
    if usage["domains"][domain_id] >= int(domain["daily_quota"]):
        reasons.append("domain_quota_exhausted")
    if usage["identities"][identity_id] >= int(identity["daily_quota"]):
        reasons.append("mailbox_quota_exhausted")
    if candidate["campaign_route"] not in set(domain["approved_campaign_routes"]):
        reasons.append("route_unapproved_for_domain")
    if candidate["campaign_route"] not in set(identity["approved_campaign_routes"]):
        reasons.append("route_unapproved_for_mailbox")
    if segment["routing_segment"] not in set(identity["allowed_recipient_segments"]):
        reasons.append("recipient_segment_not_allowed")
    return sorted(set(reasons))


def _score(identity, domain, segment, usage):
    identity_id = identity["sender_identity_id"]
    domain_id = domain["domain_id"]
    affinity = int(identity["segment_affinity"][segment["routing_segment"]])
    domain_utilization = usage["domains"][domain_id] / int(domain["daily_quota"])
    mailbox_utilization = usage["identities"][identity_id] / int(identity["daily_quota"])
    return {
        "segment_affinity": affinity,
        "domain_utilization": round(domain_utilization, 6),
        "mailbox_utilization": round(mailbox_utilization, 6),
        "sort_key": [
            -affinity,
            round(domain_utilization, 12),
            round(mailbox_utilization, 12),
            domain_id,
            identity_id,
        ],
    }


def plan_batch(payload, registry, segmentation):
    if payload.get("schema_version") != INPUT_SCHEMA:
        raise DeliveryPlannerError("unsupported delivery plan input schema")
    _validate_no_secret_keys(payload)
    plan_at = _parse_timestamp(payload.get("plan_at"), "plan_at")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise DeliveryPlannerError("delivery plan input requires candidates")
    candidates = _deduplicate_candidates(raw_candidates)

    domains = {item["domain_id"]: item for item in registry["domains"]}
    groups = {item["service_group_id"]: item for item in registry["service_groups"]}
    identities = {item["sender_identity_id"]: item for item in registry["identities"]}
    approved_routes = set(registry["approved_campaign_routes"])
    usage = {
        "domains": {key: int(value["sent_today"]) for key, value in domains.items()},
        "identities": {
            key: int(value["sent_today"]) for key, value in identities.items()
        },
    }
    initial_usage = copy.deepcopy(usage)
    decisions = []
    for candidate in candidates:
        decision_id = _required_text(candidate, "decision_id", "delivery candidate")
        for field in (
            "lead_id",
            "approved_message_id",
            "campaign_route",
            "template_variant_id",
            "recipient_domain",
            "suppression_status",
            "human_approval_status",
        ):
            _required_text(candidate, field, f"decision_id={decision_id}")
        segment = classify_recipient(candidate, segmentation, plan_at)
        holds = _global_holds(candidate, segment, approved_routes)
        eligibility = []
        eligible = []
        for identity_id in sorted(identities):
            identity = identities[identity_id]
            domain = domains[identity["domain_id"]]
            group = groups[identity["service_group_id"]]
            reasons = _identity_reasons(identity, domain, group, candidate, segment, usage)
            score = None if reasons or holds else _score(identity, domain, segment, usage)
            row = {
                "sender_identity_id": identity_id,
                "domain_id": domain["domain_id"],
                "service_group_id": group["service_group_id"],
                "eligible": not reasons and not holds,
                "reasons": sorted(set(holds + reasons)),
                "score": score,
            }
            eligibility.append(row)
            if row["eligible"]:
                eligible.append((score["sort_key"], row, identity, domain, group))

        selected = None
        if eligible:
            _, row, identity, domain, group = sorted(eligible, key=lambda item: item[0])[0]
            before = {
                "domain_sent_today": usage["domains"][domain["domain_id"]],
                "mailbox_sent_today": usage["identities"][identity["sender_identity_id"]],
            }
            usage["domains"][domain["domain_id"]] += 1
            usage["identities"][identity["sender_identity_id"]] += 1
            selected = {
                "sender_identity_id": identity["sender_identity_id"],
                "sender_address": identity["address"],
                "domain_id": domain["domain_id"],
                "sender_domain": domain["domain"],
                "service_group_id": group["service_group_id"],
                "score": row["score"],
                "simulated_usage_before": before,
                "simulated_usage_after": {
                    "domain_sent_today": usage["domains"][domain["domain_id"]],
                    "mailbox_sent_today": usage["identities"][identity["sender_identity_id"]],
                },
            }
        decision_holds = holds
        if not selected and not decision_holds:
            decision_holds = ["no_eligible_sender_identity"]
        decisions.append(
            {
                "decision_id": decision_id,
                "lead_id": candidate["lead_id"],
                "approved_message_id": candidate["approved_message_id"],
                "campaign_route": candidate["campaign_route"],
                "template_variant_id": candidate["template_variant_id"],
                "recipient_domain": candidate["recipient_domain"],
                "recipient_segment": segment,
                "status": "planned_shadow" if selected else "held",
                "holds": decision_holds,
                "selected_sender": selected,
                "eligibility": eligibility,
                "send_authorized": False,
                "external_action_executed": False,
            }
        )

    status_counts = Counter(item["status"] for item in decisions)
    segment_counts = Counter(
        item["recipient_segment"]["routing_segment"] for item in decisions
    )
    selected_counts = Counter(
        item["selected_sender"]["sender_identity_id"]
        for item in decisions
        if item["selected_sender"]
    )
    return {
        "schema_version": OUTPUT_SCHEMA,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "plan_at": _iso(plan_at),
        "mode": "offline_shadow",
        "safety": {
            "external_send_allowed": False,
            "provider_api_allowed": False,
            "dns_lookup_allowed": False,
            "crm_mutation_allowed": False,
            "usage_updates_are_simulated_only": True,
        },
        "policy": {
            "rotation": "highest_segment_affinity_then_lowest_domain_and_mailbox_utilization",
            "tie_breaker": "domain_id_then_sender_identity_id",
            "unknown_recipient_segment": "hold",
            "approval_required": True,
            "suppression_clear_required": True,
        },
        "summary": {
            "candidates": len(decisions),
            "planned_shadow": status_counts["planned_shadow"],
            "held": status_counts["held"],
            "recipient_segments": dict(sorted(segment_counts.items())),
            "selected_identities": dict(sorted(selected_counts.items())),
        },
        "simulated_usage": {
            "initial": initial_usage,
            "final": usage,
        },
        "decisions": decisions,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--segmentation", default=str(DEFAULT_SEGMENTATION))
    return parser


def main():
    args = build_parser().parse_args()
    try:
        report = plan_batch(
            _read_json(args.input),
            load_registry(args.registry),
            load_segmentation(args.segmentation),
        )
        output = _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "planned_shadow": report["summary"]["planned_shadow"],
                    "held": report["summary"]["held"],
                    "external_send_allowed": report["safety"]["external_send_allowed"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except DeliveryPlannerError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
