#!/usr/bin/env python3
"""Build a human review packet for an inert Campaign Genome email mapping.

This module validates and describes a proposed metadata promotion. It cannot
approve a checkpoint, write live metadata, send email, render, or publish.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

import campaign_studio  # noqa: E402
import control_plane  # noqa: E402


SCHEMA_VERSION = "outreach.campaign-email-promotion-review.v1"
DEFAULT_LIVE_SEND_CONTRACT = REPO_ROOT / "workspace/config/live_send_contract.json"


class CampaignPromotionReviewError(ValueError):
    """Raised when a draft mapping is not eligible for human review."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CampaignPromotionReviewError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CampaignPromotionReviewError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, payload):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_live_send_contract(contract_path=DEFAULT_LIVE_SEND_CONTRACT):
    contract = _read_json(contract_path)
    if contract.get("schema_version") != "outreach.live-send-contract.v1":
        raise CampaignPromotionReviewError("unsupported live send contract schema")
    approval = contract.get("human_approval") or {}
    live_send = contract.get("live_send") or {}
    automation = contract.get("automation_boundary") or {}
    enabler = live_send.get("non_dry_run_enabler") or {}
    legacy = contract.get("legacy_gates_policy") or {}
    if approval.get("approved_value") != "queued" or approval.get("does_not_send") is not True:
        raise CampaignPromotionReviewError("live send contract does not separate approval from send")
    blocker_ids = {
        item.get("id") for item in approval.get("observed_security_blockers") or []
    }
    required_blockers = {
        "telegram_operator_allowlist_missing",
        "telegram_callback_origin_unverified",
        "telegram_actor_attribution_unverified",
        "telegram_allsend_unbounded",
        "telegram_skip_audit_missing",
    }
    if approval.get("campaign_genome_integration_allowed") is not False:
        raise CampaignPromotionReviewError("unsafe live approval integration is enabled")
    if not required_blockers.issubset(blocker_ids):
        raise CampaignPromotionReviewError("live approval security blockers are incomplete")
    if live_send.get("candidate_stage") != "queued":
        raise CampaignPromotionReviewError("live send contract has an unexpected candidate stage")
    if (
        enabler.get("environment_variable") != "PIPELINE_SENDING_ENABLED"
        or enabler.get("operator") != "exact_string_equal"
        or enabler.get("value") != "1"
    ):
        raise CampaignPromotionReviewError("live send contract has an unexpected send enabler")
    if live_send.get("provider_success_required_before_sent") is not True:
        raise CampaignPromotionReviewError("live send contract does not require provider success")
    expected_stage_order = ["pulled", "scraped", "analyzed", "guessed", "verified"]
    if (
        automation.get("status") != "verified_no_automatic_queue_or_send"
        or automation.get("orchestrator_stage_order") != expected_stage_order
        or automation.get("highest_automated_output_stage") != "personalized"
        or automation.get("orchestrator_can_target_personalized") is not False
        or automation.get("orchestrator_can_target_queued") is not False
        or automation.get("orchestrator_imports_c7") is not False
        or automation.get("scheduled_pipeline_invokes_c7") is not False
        or automation.get("scheduled_pipeline_calls_orchestrator") is not True
        or automation.get("cron_invokes_c7") is not False
    ):
        raise CampaignPromotionReviewError("automated queue/send boundary is unsafe or incomplete")
    if set(automation.get("queued_writers") or []) != {
        "outreach_bot.appr_send",
        "outreach_bot.appr_allsend",
    }:
        raise CampaignPromotionReviewError("queued writer inventory is unexpected")
    if automation.get("live_c7_caller") != "outreach_bot.cmd_send_queued":
        raise CampaignPromotionReviewError("live C7 caller inventory is unexpected")
    if legacy.get("runtime_enforced") is not False or legacy.get("use") != "policy_provenance_only":
        raise CampaignPromotionReviewError("legacy gates policy is incorrectly marked as live")
    return contract


def build_review_packet(
    sidecar_path,
    registry_path=campaign_studio.DEFAULT_EMAIL_REGISTRY,
    send_contract_path=DEFAULT_LIVE_SEND_CONTRACT,
):
    sidecar_file = Path(sidecar_path).expanduser().resolve()
    registry_file = Path(registry_path).expanduser().resolve()
    send_contract_file = Path(send_contract_path).expanduser().resolve()
    sidecar = _read_json(sidecar_file)
    verification = campaign_studio.verify_sidecar(sidecar, registry_file)
    if not verification["passed"]:
        raise CampaignPromotionReviewError(
            "sidecar verification failed: " + "; ".join(verification["failures"])
        )

    mapping = sidecar["email_mapping"]
    if mapping.get("status") != "proposed" or mapping.get("promotable") is not True:
        raise CampaignPromotionReviewError(
            "email mapping must be proposed and router-compatible before human review"
        )
    if mapping.get("live_metadata_written") is not False:
        raise CampaignPromotionReviewError("sidecar is not an inert draft")

    registry = campaign_studio.load_email_template_registry(registry_file)
    send_contract = load_live_send_contract(send_contract_file)
    automation = send_contract["automation_boundary"]
    manifest_path = Path(sidecar["manifest"]["path"]).expanduser().resolve()
    requested_fields = {
        "template_key": mapping["email_template_key"],
        "sequence_key": mapping["email_sequence"],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pending_human",
        "business_id": sidecar["business_id"],
        "campaign_id": sidecar["campaign_id"],
        "requested_change": {
            "scope": "email_metadata_mapping_only",
            "fields": requested_fields,
            "eligible_routes": mapping["eligible_routes"],
            "executor_implemented": False,
        },
        "cross_channel_style": sidecar["style"],
        "review_evidence": {
            "sidecar": {
                "path": str(sidecar_file),
                "registry_sha256": mapping["registry_sha256"],
            },
            "campaign_manifest": {
                "path": str(manifest_path),
                "sha256": sidecar["manifest"]["sha256"],
            },
            "email_registry": {
                "path": str(registry_file),
                "source": registry["source"],
            },
            "live_send_contract": {
                "path": str(send_contract_file),
                "sha256": _sha256(send_contract_file),
                "source_fingerprints": send_contract["sources"],
            },
        },
        "deterministic_checks": {
            "sidecar_integrity": "PASS",
            "router_pair_compatibility": "PASS",
            "approved_sequence": "PASS",
            "live_metadata_already_written": False,
        },
        "human_questions": [
            "Is this the correct business and campaign?",
            "Does the eligible vertical/variant route match the intended audience?",
            "Is the approved sequence body appropriate for this campaign and its verified claims?",
            "Should only template_key and sequence_key be proposed for a later deterministic write?",
        ],
        "human_decision_interface": {
            "scope": "one_business_one_mapping",
            "target_business_id": sidecar["business_id"],
            "delivery_surface": "local_file_checkpoint",
            "allowed_decisions": ["approve_metadata_only", "request_changes", "reject"],
            "batch_approval_allowed": False,
            "telegram_mutation_allowed": False,
            "approval_and_send_are_separate": True,
            "fresh_state_recheck_required_before_execution": True,
            "artifact_change_invalidates_review": True,
            "confirmation_summary": (
                f"Review metadata only for {sidecar['business_id']}: "
                f"template_key={mapping['email_template_key']}, "
                f"sequence_key={mapping['email_sequence']}."
            ),
        },
        "does_not_authorize": [
            "live_metadata_write",
            "email_send_or_queue",
            "reply_send",
            "higgsfield_render",
            "media_spend",
            "campaign_publish",
            "crm_stage_advance",
            "telegram_state_mutation",
        ],
        "live_human_approval_surface": {
            "status": "blocked_security_review",
            "campaign_genome_integration_allowed": False,
            "observed_security_blockers": send_contract["human_approval"][
                "observed_security_blockers"
            ],
            "required_remediation": [
                "immutable Telegram user-id allowlist for every mutating command and callback",
                "configured-chat validation for callback origin",
                "actor audit by authorized immutable user id",
                "snapshot-bound, limited, explicitly confirmed batch approval",
                "audit event for every state mutation including skip",
            ],
        },
        "live_send_gate_compatibility": {
            "status": "not_evaluated_review_only",
            "approval_signal": send_contract["human_approval"],
            "required_at_execution": send_contract["live_send"],
            "legacy_gates_policy": send_contract["legacy_gates_policy"],
            "send_ready": False,
            "note": "Metadata review is not approval or send readiness; current live signals must be evaluated by C7 at execution time.",
        },
        "live_automation_boundary": {
            "status": automation["status"],
            "highest_automated_output_stage": automation["highest_automated_output_stage"],
            "orchestrator_can_target_queued": automation["orchestrator_can_target_queued"],
            "orchestrator_imports_c7": automation["orchestrator_imports_c7"],
            "scheduled_pipeline_invokes_c7": automation["scheduled_pipeline_invokes_c7"],
            "cron_invokes_c7": automation["cron_invokes_c7"],
            "queued_writers": automation["queued_writers"],
            "live_c7_caller": automation["live_c7_caller"],
            "known_redeployment_risks": automation["known_redeployment_risks"],
        },
        "rollback": "Discard the packet and sidecar. No live metadata or external action has occurred.",
    }


def create_checkpoint(
    packet_path,
    sidecar_path,
    control_root,
    config_path,
    run_id,
    registry_path=campaign_studio.DEFAULT_EMAIL_REGISTRY,
    send_contract_path=DEFAULT_LIVE_SEND_CONTRACT,
):
    packet = build_review_packet(sidecar_path, registry_path, send_contract_path)
    packet_file = _write_json(packet_path, packet)
    sidecar = _read_json(sidecar_path)
    control = control_plane.ControlPlane(control_root, config_path)
    return control.create_checkpoint(
        run_id=run_id,
        kind="campaign_email_metadata_promotion",
        title=f"Review email mapping for {packet['business_id']}",
        summary=(
            f"Propose template_key={packet['requested_change']['fields']['template_key']} and "
            f"sequence_key={packet['requested_change']['fields']['sequence_key']} for one business. "
            "No executor is implemented."
        ),
        risk=(
            "A wrong mapping can select an inappropriate approved email body or corrupt attribution. "
            "This is a single-business metadata review; it does not authorize a batch action, "
            "Telegram mutation, write, queue transition, or send."
        ),
        recommendation="Review the exact business, campaign, router evidence, and approved sequence.",
        rollback=packet["rollback"],
        artifact_paths=[
            str(packet_file),
            str(Path(sidecar_path).expanduser().resolve()),
            sidecar["manifest"]["path"],
            str(Path(registry_path).expanduser().resolve()),
            str(Path(send_contract_path).expanduser().resolve()),
        ],
        required_checks=[],
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--registry", default=str(campaign_studio.DEFAULT_EMAIL_REGISTRY))
    parser.add_argument("--send-contract", default=str(DEFAULT_LIVE_SEND_CONTRACT))
    parser.add_argument("--control-root")
    parser.add_argument("--config", default=str(REPO_ROOT / "workspace/config/orchestration.json"))
    parser.add_argument("--run-id")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if bool(args.control_root) != bool(args.run_id):
            raise CampaignPromotionReviewError(
                "--control-root and --run-id must be supplied together"
            )
        if args.control_root:
            result = create_checkpoint(
                args.output,
                args.sidecar,
                args.control_root,
                args.config,
                args.run_id,
                args.registry,
                args.send_contract,
            )
        else:
            result = build_review_packet(args.sidecar, args.registry, args.send_contract)
            _write_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (CampaignPromotionReviewError, control_plane.ControlPlaneError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
