#!/usr/bin/env python3
"""Replay Maps provider events into a resumable, provenance-rich job state.

This controller does not invoke Apify, Google Places, DNS, or a database. It
separates timeout, retry, fallback, partial pagination, successful completion,
and proven territory exhaustion so an empty list cannot hide provider failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
POLICY_SCHEMA = "outreach.maps-job-policy.v1"
INPUT_SCHEMA = "outreach.maps-job-events.v1"
OUTPUT_SCHEMA = "outreach.maps-job-state.v1"
ENVELOPE_SCHEMA = "outreach.research-envelope.v1"
DEFAULT_POLICY = REPO_ROOT / "workspace/config/maps_job_policy.json"
OUTCOMES = {"success", "timeout", "error"}


class MapsJobError(ValueError):
    """Raised when Maps job evidence is inconsistent or unsafe."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MapsJobError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MapsJobError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _parse_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise MapsJobError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise MapsJobError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise MapsJobError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise MapsJobError(f"{context} requires {field}")
    return value


def _canonical_sha256(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_policy(path=DEFAULT_POLICY):
    policy = _read_json(path)
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise MapsJobError("unsupported Maps job policy schema")
    safety = policy.get("safety") or {}
    forbidden = (
        "external_api_calls_allowed",
        "database_writes_allowed",
        "automatic_fallback_calls_allowed",
        "automatic_retry_calls_allowed",
        "cost_commitment_allowed",
    )
    if any(safety.get(key) is not False for key in forbidden):
        raise MapsJobError("Maps job policy enables an external action")
    providers = policy.get("providers") or []
    roles = [item.get("role") for item in providers]
    if roles.count("primary") != 1 or roles.count("fallback") > 1:
        raise MapsJobError("Maps job policy requires one primary and at most one fallback")
    seen = set()
    for provider in providers:
        provider_id = _required_text(provider, "provider_id", "Maps provider")
        if provider_id in seen:
            raise MapsJobError(f"duplicate Maps provider: {provider_id}")
        seen.add(provider_id)
        attempts = int(provider.get("max_attempts", 0))
        timeout = int(provider.get("attempt_timeout_seconds", 0))
        backoff = provider.get("backoff_seconds") or []
        if attempts <= 0 or timeout <= 0 or len(backoff) != attempts:
            raise MapsJobError(f"invalid retry policy for provider: {provider_id}")
    return policy


def _provider_index(policy):
    return {item["provider_id"]: item for item in policy["providers"]}


def _provider_by_role(policy, role):
    return next((item for item in policy["providers"] if item["role"] == role), None)


def _deduplicate_events(raw_events):
    events = {}
    for raw in raw_events:
        event = dict(raw)
        event_id = _required_text(event, "event_id", "Maps event")
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
        previous = events.get(event_id)
        if previous and previous[0] != canonical:
            raise MapsJobError(f"conflicting duplicate Maps event: {event_id}")
        events[event_id] = (canonical, event)
    return [item[1] for item in events.values()]


def _maps_url(place_id):
    return "https://www.google.com/maps/place/?q=place_id:" + quote(place_id, safe="")


def _evidence(url, locator, value):
    return {
        "url": url,
        "locator": locator,
        "content_sha256": hashlib.sha256(str(value).encode("utf-8")).hexdigest(),
    }


def _research_envelope(item, provider_id, captured_at):
    place_id = _required_text(item, "place_id", "Maps item")
    company = _required_text(item, "company", f"place_id={place_id}")
    website = _required_text(item, "website", f"place_id={place_id}")
    if not website.startswith(("http://", "https://")):
        raise MapsJobError(f"Maps item website is invalid: {place_id}")
    source_url = _maps_url(place_id)
    raw_fingerprint = _canonical_sha256(item)
    return {
        "schema_version": ENVELOPE_SCHEMA,
        "source": {
            "adapter": provider_id,
            "source_url": source_url,
            "external_id": place_id,
            "captured_at": captured_at,
            "source_artifact_sha256": raw_fingerprint,
        },
        "entity": {
            "company": company,
            "website": website,
        },
        "observed": {
            "processor": "",
            "tech_signals": [],
        },
        "evidence": {
            "entity.company": [_evidence(source_url, "maps.name", company)],
            "entity.website": [_evidence(source_url, "maps.website", website)],
        },
    }


def _normalize_event(event, provider, expected_attempt):
    event_id = event["event_id"]
    if event.get("outcome") not in OUTCOMES:
        raise MapsJobError(f"unsupported Maps event outcome: {event_id}")
    try:
        attempt = int(event.get("attempt"))
        cost = float(event.get("cost_usd"))
    except (TypeError, ValueError) as exc:
        raise MapsJobError(f"invalid attempt or cost: {event_id}") from exc
    if attempt != expected_attempt or attempt > int(provider["max_attempts"]):
        raise MapsJobError(f"unexpected provider attempt number: {event_id}")
    if cost < 0:
        raise MapsJobError(f"negative provider cost: {event_id}")
    started = _parse_timestamp(event.get("started_at"), f"{event_id}.started_at")
    finished = _parse_timestamp(event.get("finished_at"), f"{event_id}.finished_at")
    if finished < started:
        raise MapsJobError(f"Maps event finishes before it starts: {event_id}")
    elapsed = (finished - started).total_seconds()
    if event["outcome"] == "timeout" and elapsed < int(provider["attempt_timeout_seconds"]):
        raise MapsJobError(f"timeout reported before configured deadline: {event_id}")
    items = event.get("items")
    if not isinstance(items, list):
        raise MapsJobError(f"Maps event requires an items list: {event_id}")
    if event["outcome"] != "success" and items:
        raise MapsJobError(f"failed Maps event cannot claim items: {event_id}")
    return attempt, cost, started, finished, items


def replay(payload, policy):
    if payload.get("schema_version") != INPUT_SCHEMA:
        raise MapsJobError("unsupported Maps job event schema")
    job = payload.get("job") or {}
    job_id = _required_text(job, "job_id", "Maps job")
    for field in ("vertical", "territory", "query"):
        _required_text(job, field, f"job_id={job_id}")
    try:
        max_results = int(job.get("max_results"))
        budget = float(job.get("budget_usd"))
    except (TypeError, ValueError) as exc:
        raise MapsJobError("Maps job requires numeric max_results and budget_usd") from exc
    if max_results <= 0 or budget <= 0:
        raise MapsJobError("Maps job max_results and budget_usd must be positive")

    providers = _provider_index(policy)
    primary = _provider_by_role(policy, "primary")
    fallback = _provider_by_role(policy, "fallback")
    active_provider = primary["provider_id"]
    attempts = Counter()
    total_cost = 0.0
    records = {}
    event_log = []
    state = "pending"
    next_cursor = ""
    next_action = {
        "type": "provider_attempt_required",
        "provider_id": active_provider,
        "attempt": 1,
        "automatic_execution_allowed": False,
    }
    terminal = False
    events = _deduplicate_events(payload.get("events") or [])
    last_finished = None
    for event in events:
        if terminal:
            raise MapsJobError(f"Maps event appears after terminal state: {event['event_id']}")
        provider_id = _required_text(event, "provider_id", f"event_id={event['event_id']}")
        if provider_id != active_provider or provider_id not in providers:
            raise MapsJobError(f"unexpected provider for current Maps state: {event['event_id']}")
        provider = providers[provider_id]
        expected_attempt = attempts[provider_id] + 1
        attempt, cost, started, finished, items = _normalize_event(
            event, provider, expected_attempt
        )
        if last_finished and started < last_finished:
            raise MapsJobError(f"Maps events overlap or are out of order: {event['event_id']}")
        last_finished = finished
        attempts[provider_id] += 1
        total_cost = round(total_cost + cost, 6)
        if total_cost > budget:
            state = "budget_hold"
            next_action = {
                "type": "human_budget_review_required",
                "automatic_execution_allowed": False,
            }
            terminal = True
        outcome = event["outcome"]
        error_code = str(event.get("error_code") or "none")
        if not terminal and outcome in {"timeout", "error"}:
            if error_code in set(policy.get("terminal_error_codes") or []):
                state = "failed_terminal"
                next_action = {"type": "human_investigation_required", "automatic_execution_allowed": False}
                terminal = True
            elif error_code not in set(provider.get("retryable_error_codes") or []):
                state = "failed_unclassified"
                next_action = {"type": "human_investigation_required", "automatic_execution_allowed": False}
                terminal = True
            elif attempt < int(provider["max_attempts"]):
                state = "retry_pending"
                next_cursor = str(event.get("next_cursor") or "")
                next_action = {
                    "type": "provider_retry_required",
                    "provider_id": provider_id,
                    "attempt": attempt + 1,
                    "resume_cursor": next_cursor,
                    "backoff_seconds": provider["backoff_seconds"][attempt],
                    "automatic_execution_allowed": False,
                }
            elif provider["role"] == "primary" and fallback:
                state = "fallback_pending"
                active_provider = fallback["provider_id"]
                next_cursor = ""
                next_action = {
                    "type": "fallback_provider_attempt_required",
                    "provider_id": active_provider,
                    "attempt": 1,
                    "automatic_execution_allowed": False,
                }
            else:
                state = "retry_exhausted"
                next_action = {"type": "human_investigation_required", "automatic_execution_allowed": False}
                terminal = True
        elif not terminal and outcome == "success":
            for item in items:
                place_id = _required_text(item, "place_id", f"event_id={event['event_id']}")
                envelope = _research_envelope(item, provider_id, _iso(finished))
                canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
                previous = records.get(place_id)
                if previous and previous[0] != canonical:
                    raise MapsJobError(f"conflicting duplicate Place ID: {place_id}")
                records[place_id] = (canonical, envelope)
                if len(records) > max_results:
                    raise MapsJobError("Maps job produced more than max_results")
            complete = event.get("complete") is True
            next_cursor = str(event.get("next_cursor") or "")
            if not complete:
                if not next_cursor:
                    raise MapsJobError(f"partial Maps success requires a resume cursor: {event['event_id']}")
                state = "resume_pending"
                next_action = {
                    "type": "provider_resume_required",
                    "provider_id": provider_id,
                    "attempt": attempt + 1,
                    "resume_cursor": next_cursor,
                    "automatic_execution_allowed": False,
                }
            elif records:
                state = "completed"
                next_action = {"type": "none", "automatic_execution_allowed": False}
                terminal = True
            elif provider["role"] == "primary" and fallback:
                state = "fallback_pending"
                active_provider = fallback["provider_id"]
                next_action = {
                    "type": "fallback_provider_attempt_required",
                    "provider_id": active_provider,
                    "attempt": 1,
                    "automatic_execution_allowed": False,
                }
            else:
                state = "territory_exhausted"
                next_action = {"type": "none", "automatic_execution_allowed": False}
                terminal = True
        event_log.append(
            {
                "event_id": event["event_id"],
                "provider_id": provider_id,
                "attempt": attempt,
                "outcome": outcome,
                "error_code": error_code,
                "finished_at": _iso(finished),
                "cost_usd": cost,
                "state_after": state,
                "records_after": len(records),
            }
        )

    if not events:
        state = "pending"
    return {
        "schema_version": OUTPUT_SCHEMA,
        "job_id": job_id,
        "mode": "offline_shadow",
        "state": state,
        "territory_exhausted": state == "territory_exhausted",
        "empty_result_interpretation": (
            "proven_empty_after_primary_and_fallback_success"
            if state == "territory_exhausted"
            else "not_proven_empty"
        ),
        "safety": {
            "external_api_calls_allowed": False,
            "database_writes_allowed": False,
            "automatic_retry_calls_allowed": False,
            "automatic_fallback_calls_allowed": False,
            "cost_commitment_allowed": False,
        },
        "job": job,
        "attempts": dict(sorted(attempts.items())),
        "cost": {
            "budget_usd": budget,
            "observed_cost_usd": total_cost,
            "remaining_budget_usd": round(max(0.0, budget - total_cost), 6),
        },
        "resume": {
            "active_provider_id": active_provider,
            "cursor": next_cursor,
            "next_action": next_action,
        },
        "event_log": event_log,
        "research_envelopes": [item[1] for item in records.values()],
        "record_count": len(records),
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    return parser


def main():
    args = build_parser().parse_args()
    try:
        state = replay(_read_json(args.events), load_policy(args.policy))
        output = _write_json(args.output, state)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "state": state["state"],
                    "record_count": state["record_count"],
                    "external_api_calls_allowed": state["safety"]["external_api_calls_allowed"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except MapsJobError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
