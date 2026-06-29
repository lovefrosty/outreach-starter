#!/usr/bin/env python3
"""Compile reply intelligence into human-reviewed draft workflow packets.

Assignments are stable by lead and experiment. Resources are fingerprinted and
versioned. Every subsequence step remains blocked pending a human decision; this
module has no scheduler, CRM writer, provider adapter, or sender.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
PLAYBOOK_SCHEMA = "outreach.reply-playbooks.v1"
RESOURCE_SCHEMA = "outreach.custom-resource-registry.v1"
INPUT_SCHEMA = "outreach.reply-intelligence.v1"
OUTPUT_SCHEMA = "outreach.reply-workflow.v1"
DEFAULT_PLAYBOOKS = REPO_ROOT / "workspace/config/reply_playbooks.json"
DEFAULT_RESOURCES = REPO_ROOT / "workspace/config/custom_resource_registry.json"


class ReplyWorkflowError(ValueError):
    """Raised when a reply workflow cannot be compiled safely."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplyWorkflowError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReplyWorkflowError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _parse_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ReplyWorkflowError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplyWorkflowError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise ReplyWorkflowError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise ReplyWorkflowError(f"{context} requires {field}")
    return value


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_playbooks(path=DEFAULT_PLAYBOOKS):
    config = _read_json(path)
    if config.get("schema_version") != PLAYBOOK_SCHEMA:
        raise ReplyWorkflowError("unsupported reply playbook schema")
    safety = config.get("safety") or {}
    forbidden = (
        "external_send_allowed",
        "automatic_subsequence_allowed",
        "automatic_scheduling_allowed",
        "crm_mutation_allowed",
    )
    if any(safety.get(key) is not False for key in forbidden):
        raise ReplyWorkflowError("reply playbooks enable an external action")
    if safety.get("human_review_required") is not True:
        raise ReplyWorkflowError("reply playbooks do not require human review")
    assignment = config.get("assignment") or {}
    if (
        assignment.get("unit") != "lead_id"
        or assignment.get("method") != "sha256_mod_100"
        or assignment.get("cross_reply_stickiness") is not True
    ):
        raise ReplyWorkflowError("reply experiment assignment is not lead-stable")

    experiments = {}
    experiment_intents = set()
    variant_ids = set()
    for experiment in config.get("experiments") or []:
        experiment_id = _required_text(experiment, "experiment_id", "reply experiment")
        intent = _required_text(experiment, "intent", f"experiment_id={experiment_id}")
        if experiment_id in experiments or intent in experiment_intents:
            raise ReplyWorkflowError(f"duplicate reply experiment or intent: {experiment_id}/{intent}")
        variants = experiment.get("variants") or []
        if len(variants) < 2:
            raise ReplyWorkflowError(f"reply experiment requires at least two variants: {experiment_id}")
        total = 0
        for variant in variants:
            variant_id = _required_text(variant, "variant_id", f"experiment_id={experiment_id}")
            _required_text(variant, "draft", f"variant_id={variant_id}")
            try:
                weight = int(variant.get("weight"))
            except (TypeError, ValueError) as exc:
                raise ReplyWorkflowError(f"invalid weight for variant_id={variant_id}") from exc
            if weight <= 0 or variant_id in variant_ids:
                raise ReplyWorkflowError(f"invalid or duplicate variant_id: {variant_id}")
            total += weight
            variant_ids.add(variant_id)
        if total != 100:
            raise ReplyWorkflowError(f"experiment weights must total 100: {experiment_id}")
        experiments[experiment_id] = experiment
        experiment_intents.add(intent)

    playbooks = config.get("playbooks") or {}
    required_intents = {
        "booking",
        "positive_interest",
        "question",
        "objection",
        "out_of_office",
        "not_interested",
        "unsubscribe",
        "ambiguous",
    }
    if set(playbooks) != required_intents:
        raise ReplyWorkflowError("reply playbooks must define every supported intent")
    for intent, playbook in playbooks.items():
        steps = playbook.get("steps") or []
        if not steps or any(not str(step).strip() for step in steps):
            raise ReplyWorkflowError(f"reply playbook has no valid steps: {intent}")
        experiment_id = playbook.get("experiment_id")
        if experiment_id:
            experiment = experiments.get(experiment_id)
            if not experiment or experiment["intent"] != intent:
                raise ReplyWorkflowError(f"reply playbook experiment mismatch: {intent}")
    return config


def load_resources(path=DEFAULT_RESOURCES):
    registry = _read_json(path)
    if registry.get("schema_version") != RESOURCE_SCHEMA:
        raise ReplyWorkflowError("unsupported custom resource registry schema")
    safety = registry.get("safety") or {}
    if (
        safety.get("external_distribution_allowed") is not False
        or safety.get("automatic_attachment_allowed") is not False
        or safety.get("automatic_publish_allowed") is not False
        or safety.get("human_promotion_required") is not True
    ):
        raise ReplyWorkflowError("custom resource registry enables external use")
    resources = {}
    for item in registry.get("resources") or []:
        resource_id = _required_text(item, "resource_id", "custom resource")
        version = _required_text(item, "version", f"resource_id={resource_id}")
        if resource_id in resources:
            raise ReplyWorkflowError(f"duplicate custom resource: {resource_id}")
        path_value = _required_text(item, "path", f"resource_id={resource_id}")
        resource_path = (REPO_ROOT / path_value).resolve()
        workspace_root = (REPO_ROOT / "workspace").resolve()
        if not resource_path.is_relative_to(workspace_root):
            raise ReplyWorkflowError(f"custom resource escapes workspace: {resource_id}")
        if not resource_path.is_file():
            raise ReplyWorkflowError(f"custom resource file missing: {resource_id}")
        expected = _required_text(item, "sha256", f"resource_id={resource_id}")
        observed = _sha256(resource_path)
        if observed != expected:
            raise ReplyWorkflowError(f"custom resource fingerprint drift: {resource_id}")
        if item.get("external_use_allowed") is not False:
            raise ReplyWorkflowError(f"draft resource unexpectedly enables external use: {resource_id}")
        _parse_timestamp(item.get("expires_at"), f"resource_id={resource_id}.expires_at")
        normalized = dict(item)
        normalized["absolute_path"] = str(resource_path)
        normalized["version_key"] = f"{resource_id}@{version}"
        resources[resource_id] = normalized
    if not resources:
        raise ReplyWorkflowError("custom resource registry is empty")
    return resources


def assign_variant(experiment, lead_id):
    experiment_id = experiment["experiment_id"]
    digest = hashlib.sha256(f"{experiment_id}:{lead_id}".encode("utf-8")).hexdigest()
    bucket = int(digest[:16], 16) % 100
    upper = 0
    for variant in experiment["variants"]:
        upper += int(variant["weight"])
        if bucket < upper:
            return {
                "experiment_id": experiment_id,
                "variant_id": variant["variant_id"],
                "assignment_unit": "lead_id",
                "assignment_key_sha256": hashlib.sha256(lead_id.encode("utf-8")).hexdigest(),
                "bucket": bucket,
                "draft_text": variant["draft"],
            }
    raise ReplyWorkflowError(f"experiment assignment failed: {experiment_id}")


def _validate_reply_report(report):
    if report.get("schema_version") != INPUT_SCHEMA:
        raise ReplyWorkflowError("unsupported reply intelligence schema")
    safety = report.get("safety") or {}
    forbidden = (
        "external_send_allowed",
        "crm_mutation_allowed",
        "provider_api_allowed",
        "automatic_subsequence_allowed",
    )
    if any(safety.get(key) is not False for key in forbidden):
        raise ReplyWorkflowError("reply intelligence report enables an external action")
    seen_events = set()
    seen_provider_messages = set()
    for item in report.get("reply_results") or []:
        event_id = _required_text(item, "event_id", "reply result")
        provider_message_id = _required_text(
            item, "provider_message_id", f"event_id={event_id}"
        )
        if event_id in seen_events or provider_message_id in seen_provider_messages:
            raise ReplyWorkflowError("reply workflow input contains duplicate event attribution")
        seen_events.add(event_id)
        seen_provider_messages.add(provider_message_id)
        proposal = item.get("proposal") or {}
        if (
            proposal.get("automatic_send_allowed") is not False
            or proposal.get("crm_mutation_allowed") is not False
            or proposal.get("external_action_executed") is not False
        ):
            raise ReplyWorkflowError(f"unsafe reply proposal: {event_id}")


def _resource_preview(resource_id, intent, resources, generated_at):
    if resource_id == "none":
        return None
    resource = resources.get(resource_id)
    if not resource:
        raise ReplyWorkflowError(f"playbook references unknown resource: {resource_id}")
    if intent not in set(resource.get("supported_intents") or []):
        raise ReplyWorkflowError(f"resource does not support intent: {resource_id}/{intent}")
    expires_at = _parse_timestamp(resource["expires_at"], f"resource_id={resource_id}.expires_at")
    availability = "preview_only" if generated_at < expires_at else "expired_hold"
    return {
        "resource_id": resource_id,
        "version": resource["version"],
        "version_key": resource["version_key"],
        "title": resource["title"],
        "path": resource["path"],
        "sha256": resource["sha256"],
        "status": resource["status"],
        "availability": availability,
        "external_use_allowed": False,
        "automatic_attachment_allowed": False,
        "expires_at": resource["expires_at"],
        "claims_policy": resource["claims_policy"],
    }


def _workflow_state(intent, resource_preview):
    if intent in {"unsubscribe", "not_interested"}:
        return "suppression_review_pending"
    if intent == "out_of_office":
        return "defer_review_pending"
    if intent == "ambiguous":
        return "manual_triage_pending"
    if resource_preview and resource_preview["availability"] == "expired_hold":
        return "resource_review_blocked"
    return "draft_pending_human"


def _allowed_decisions(state):
    if state == "suppression_review_pending":
        return ["confirm_suppression", "hold_for_compliance_review"]
    if state == "defer_review_pending":
        return ["approve_defer_date", "edit_defer_date", "close_without_followup"]
    if state == "manual_triage_pending":
        return ["correct_intent", "hold", "close_without_followup"]
    if state == "resource_review_blocked":
        return ["replace_resource", "renew_resource_review", "hold"]
    return ["approve_draft_only", "edit_draft", "correct_intent", "hold", "close_without_followup"]


def compile_workflows(report, playbooks, resources, generated_at=None):
    _validate_reply_report(report)
    generated = generated_at or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        raise ReplyWorkflowError("generated_at must include a timezone")
    generated = generated.astimezone(timezone.utc)
    experiments = {item["experiment_id"]: item for item in playbooks["experiments"]}
    workflows = []
    for item in report.get("reply_results") or []:
        event_id = item["event_id"]
        lead_id = _required_text(item, "lead_id", f"event_id={event_id}")
        intent = _required_text(item.get("classification") or {}, "intent", f"event_id={event_id}")
        playbook = playbooks["playbooks"].get(intent)
        if not playbook:
            raise ReplyWorkflowError(f"no playbook for intent: {intent}")
        resource = _resource_preview(playbook.get("resource_id", "none"), intent, resources, generated)
        assignment = None
        if playbook.get("experiment_id"):
            assignment = assign_variant(experiments[playbook["experiment_id"]], lead_id)
        state = _workflow_state(intent, resource)
        not_before_date = (
            (item.get("proposal") or {}).get("subsequence") or {}
        ).get("not_before_date")
        steps = []
        for index, name in enumerate(playbook["steps"], start=1):
            steps.append(
                {
                    "step_id": f"{event_id}:step-{index}",
                    "name": name,
                    "status": "blocked_pending_human",
                    "automatic_execution_allowed": False,
                    "not_before_date": not_before_date if intent == "out_of_office" else None,
                }
            )
        workflows.append(
            {
                "workflow_id": f"reply-workflow:{event_id}",
                "source_event_id": event_id,
                "lead_id": lead_id,
                "thread_id": item["thread_id"],
                "template_variant_id": item["template_variant_id"],
                "intent": intent,
                "classification_evidence": item["classification"]["evidence"],
                "state": state,
                "experiment_assignment": assignment,
                "resource_preview": resource,
                "subsequence_steps": steps,
                "human_decision": {
                    "required": True,
                    "allowed_decisions": _allowed_decisions(state),
                    "decision_recorded": False,
                },
                "learning": {
                    "assignment_recordable": assignment is not None,
                    "exposure_recordable": False,
                    "reason": "A draft assignment is not a delivered experiment exposure.",
                },
                "external_action_executed": False,
                "send_authorized": False,
                "crm_mutation_authorized": False,
            }
        )

    states = Counter(item["state"] for item in workflows)
    assignments = Counter(
        item["experiment_assignment"]["variant_id"]
        for item in workflows
        if item["experiment_assignment"]
    )
    return {
        "schema_version": OUTPUT_SCHEMA,
        "generated_at": _iso(generated),
        "mode": "offline_shadow",
        "safety": {
            "external_send_allowed": False,
            "automatic_subsequence_allowed": False,
            "automatic_scheduling_allowed": False,
            "crm_mutation_allowed": False,
            "resource_attachment_allowed": False,
        },
        "measurement": {
            "assignment_unit": "lead_id",
            "assignment_is_not_exposure": True,
            "outcomes_require_delivered_message_attribution": True,
        },
        "summary": {
            "workflows": len(workflows),
            "states": dict(sorted(states.items())),
            "draft_assignments": dict(sorted(assignments.items())),
        },
        "workflows": workflows,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reply-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--playbooks", default=str(DEFAULT_PLAYBOOKS))
    parser.add_argument("--resources", default=str(DEFAULT_RESOURCES))
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
        output = compile_workflows(
            _read_json(args.reply_report),
            load_playbooks(args.playbooks),
            load_resources(args.resources),
            generated_at=generated,
        )
        destination = _write_json(args.output, output)
        print(
            json.dumps(
                {
                    "output": str(destination),
                    "workflows": output["summary"]["workflows"],
                    "external_send_allowed": output["safety"]["external_send_allowed"],
                    "resource_attachment_allowed": output["safety"]["resource_attachment_allowed"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except ReplyWorkflowError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
