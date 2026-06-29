#!/usr/bin/env python3
"""Build machine-readable cross-channel campaign learning from inert sidecars.

The evaluator is read-only with respect to CRM and delivery systems. It accepts
explicitly attributed offline events, rejects unknown channel/asset mappings,
deduplicates events by event_id, and computes rates from distinct businesses.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "outreach.campaign-learning.v1"
EVENT_SCHEMA_VERSION = "outreach.campaign-events.v1"
SIDECAR_SCHEMA_VERSION = "outreach.campaign-sidecar.v1"
MIN_TRUST = 5
EVENT_TYPES = {"exposure", "reply", "reply_intent", "booked", "verified_value"}


class CampaignLearningError(ValueError):
    """Raised when offline campaign evidence cannot be attributed safely."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CampaignLearningError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CampaignLearningError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(value, encoding="utf-8")


def _load_sidecars(directory):
    root = Path(directory)
    if not root.is_dir():
        raise CampaignLearningError(f"sidecar directory does not exist: {root}")
    sidecars = {}
    for path in sorted(root.glob("*.json")):
        sidecar = _read_json(path)
        if sidecar.get("schema_version") != SIDECAR_SCHEMA_VERSION:
            raise CampaignLearningError(f"unsupported sidecar schema: {path}")
        business_id = str(sidecar.get("business_id") or "").strip()
        if not business_id:
            raise CampaignLearningError(f"sidecar has no business_id: {path}")
        if business_id in sidecars:
            raise CampaignLearningError(f"duplicate sidecar business_id: {business_id}")
        sidecars[business_id] = sidecar
    if not sidecars:
        raise CampaignLearningError(f"no sidecars found in: {root}")
    return sidecars


def _variant_index(sidecar):
    variants = defaultdict(set)
    email_mapping = sidecar.get("email_mapping") or {}
    template_key = email_mapping.get("email_template_key")
    if template_key:
        variants["email"].add(str(template_key))
    for item in sidecar.get("channel_variants") or []:
        channel = str(item.get("channel") or "").strip()
        variant_id = str(item.get("variant_id") or "").strip()
        if channel and variant_id:
            variants[channel].add(variant_id)
    return variants


def _normalize_events(payload):
    if payload.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise CampaignLearningError("unsupported campaign event schema")
    events = payload.get("events")
    if not isinstance(events, list):
        raise CampaignLearningError("campaign event payload requires an events list")
    deduplicated = {}
    for raw in events:
        event = dict(raw)
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            raise CampaignLearningError("every campaign event requires event_id")
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
        previous = deduplicated.get(event_id)
        if previous and previous[0] != canonical:
            raise CampaignLearningError(f"conflicting duplicate event_id: {event_id}")
        deduplicated[event_id] = (canonical, event)
    return [item[1] for item in deduplicated.values()]


def _validate_event(event, sidecars):
    event_type = str(event.get("event") or "").strip()
    business_id = str(event.get("business_id") or "").strip()
    channel = str(event.get("channel") or "").strip()
    variant_id = str(event.get("asset_variant_id") or "").strip()
    if event_type not in EVENT_TYPES:
        raise CampaignLearningError(f"unsupported campaign event: {event_type}")
    if business_id not in sidecars:
        raise CampaignLearningError(f"event has no matching sidecar: {business_id}")
    if not channel:
        raise CampaignLearningError(f"event has no channel: {event.get('event_id')}")
    allowed = _variant_index(sidecars[business_id])
    if channel not in allowed:
        raise CampaignLearningError(
            f"channel is not declared by sidecar for {business_id}: {channel}"
        )
    if not variant_id or variant_id not in allowed[channel]:
        raise CampaignLearningError(
            f"asset variant is not declared for {business_id}/{channel}: {variant_id or '<blank>'}"
        )
    if event_type == "reply_intent" and not str(event.get("value") or "").strip():
        raise CampaignLearningError(f"reply_intent requires a value: {event.get('event_id')}")
    if event_type == "verified_value":
        try:
            value = float(event.get("value"))
        except (TypeError, ValueError) as exc:
            raise CampaignLearningError(
                f"verified_value must be numeric: {event.get('event_id')}"
            ) from exc
        if value < 0:
            raise CampaignLearningError(
                f"verified_value cannot be negative: {event.get('event_id')}"
            )


def evaluate(sidecars, event_payload, min_trust=MIN_TRUST):
    events = _normalize_events(event_payload)
    for event in events:
        _validate_event(event, sidecars)

    groups = {}
    for event in events:
        business_id = str(event["business_id"])
        sidecar = sidecars[business_id]
        style = sidecar.get("style") or {}
        key = (style.get("style_id"), event["channel"])
        if not key[0]:
            raise CampaignLearningError(f"sidecar has no style_id: {business_id}")
        group = groups.setdefault(
            key,
            {
                "style_id": key[0],
                "style_version": style.get("version"),
                "channel": key[1],
                "assigned_business_ids": set(),
                "exposed_business_ids": set(),
                "reply_business_ids": set(),
                "booked_business_ids": set(),
                "reply_intents": Counter(),
                "verified_value_usd": 0.0,
                "event_count": 0,
            },
        )
        group["assigned_business_ids"].add(business_id)
        group["event_count"] += 1
        event_type = event["event"]
        if event_type == "exposure":
            group["exposed_business_ids"].add(business_id)
        elif event_type == "reply":
            group["reply_business_ids"].add(business_id)
        elif event_type == "booked":
            group["booked_business_ids"].add(business_id)
        elif event_type == "reply_intent":
            group["reply_intents"][str(event["value"])] += 1
        elif event_type == "verified_value":
            group["verified_value_usd"] += float(event["value"])

    metrics = []
    for key in sorted(groups):
        group = groups[key]
        exposed = len(group["exposed_business_ids"])
        replies = len(group["reply_business_ids"])
        booked = len(group["booked_business_ids"])
        # Outcomes without an explicit exposure remain visible but never create
        # a denominator or an invented rate.
        metrics.append(
            {
                "style_id": group["style_id"],
                "style_version": group["style_version"],
                "channel": group["channel"],
                "assigned_businesses": len(group["assigned_business_ids"]),
                "exposed_businesses": exposed,
                "reply_businesses": replies,
                "reply_rate": round(replies / exposed, 6) if exposed else None,
                "booked_businesses": booked,
                "booking_rate": round(booked / exposed, 6) if exposed else None,
                "verified_value_usd": round(group["verified_value_usd"], 2),
                "value_per_exposure_usd": (
                    round(group["verified_value_usd"] / exposed, 2) if exposed else None
                ),
                "reply_intents": dict(sorted(group["reply_intents"].items())),
                "event_count": group["event_count"],
                "sample_status": "trusted" if exposed >= int(min_trust) else "too_early",
                "minimum_trusted_sample": int(min_trust),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "attribution": {
            "join_key": "business_id",
            "rates_use_distinct_businesses": True,
            "events_deduplicated_by": "event_id",
            "unattributed_events_allowed": False,
        },
        "input": {
            "sidecar_businesses": len(sidecars),
            "unique_events": len(events),
        },
        "metrics": metrics,
    }


def render_markdown(report):
    lines = [
        "# Campaign Style Learning",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "Rates use distinct businesses. Events are deduplicated by `event_id`.",
        "",
        "| style | channel | exposed | replies | reply rate | booked | booking rate | value/exposure | sample |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for metric in report["metrics"]:
        reply_rate = "n/a" if metric["reply_rate"] is None else f"{metric['reply_rate'] * 100:.1f}%"
        booking_rate = "n/a" if metric["booking_rate"] is None else f"{metric['booking_rate'] * 100:.1f}%"
        value = (
            "n/a"
            if metric["value_per_exposure_usd"] is None
            else f"${metric['value_per_exposure_usd']:.2f}"
        )
        lines.append(
            f"| {metric['style_id']} | {metric['channel']} | {metric['exposed_businesses']} | "
            f"{metric['reply_businesses']} | {reply_rate} | {metric['booked_businesses']} | "
            f"{booking_rate} | {value} | {metric['sample_status']} |"
        )
        if metric["reply_intents"]:
            intents = ", ".join(f"{key}={value}" for key, value in metric["reply_intents"].items())
            lines.append(f"\nReply intents for `{metric['style_id']} / {metric['channel']}`: {intents}")
    if not report["metrics"]:
        lines.append("| none | none | 0 | 0 | n/a | 0 | n/a | n/a | too_early |")
    return "\n".join(lines).rstrip() + "\n"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecars", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--markdown-output", required=True)
    parser.add_argument("--min-trust", type=int, default=MIN_TRUST)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        sidecars = _load_sidecars(args.sidecars)
        report = evaluate(sidecars, _read_json(args.events), min_trust=args.min_trust)
        _write_json(args.json_output, report)
        _write_text(args.markdown_output, render_markdown(report))
        print(
            json.dumps(
                {
                    "json_output": str(Path(args.json_output).resolve()),
                    "markdown_output": str(Path(args.markdown_output).resolve()),
                    "groups": len(report["metrics"]),
                    "unique_events": report["input"]["unique_events"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except CampaignLearningError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
