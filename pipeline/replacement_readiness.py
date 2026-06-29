#!/usr/bin/env python3
"""Evaluate offline shadow completeness and fail-closed production readiness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
REQUIREMENTS_SCHEMA = "outreach.instantly-replacement-requirements.v1"
DEFAULT_REQUIREMENTS = REPO_ROOT / "workspace/config/replacement_requirements.json"
DEFAULT_CONTROL = REPO_ROOT / "workspace/config/outbound_control_plane.json"
DEFAULT_EVAL_CONFIG = REPO_ROOT / "workspace/config/eval_compounding.json"
DEFAULT_EVAL_STATE = REPO_ROOT / "workspace/evals/compounding/state.json"


class ReplacementReadinessError(ValueError):
    """Raised when replacement evidence is missing, inconsistent, or unsafe."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplacementReadinessError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReplacementReadinessError(f"invalid JSON in {path}: {exc}") from exc


def _resolve(path, root):
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate


def evaluate(requirements, control, eval_config, eval_state, root=REPO_ROOT):
    if requirements.get("schema_version") != REQUIREMENTS_SCHEMA:
        raise ReplacementReadinessError("unsupported replacement requirements schema")
    execution = control.get("execution") or {}
    unsafe_enabled = sorted(key for key, value in execution.items() if value is True)
    if unsafe_enabled:
        raise ReplacementReadinessError(
            f"shadow control plane enables execution: {', '.join(unsafe_enabled)}"
        )
    configured_evals = {item["id"] for item in eval_config.get("tests") or []}
    latest_results = {item["id"]: item["status"] for item in eval_state.get("results") or []}
    verification_results = {
        item["id"]: item["status"] for item in eval_state.get("verification_results") or []
    }
    layers = []
    for layer in requirements.get("layers") or []:
        layer_id = str(layer.get("id") or "").strip()
        if not layer_id:
            raise ReplacementReadinessError("replacement layer requires id")
        missing_files = [
            path for path in layer.get("evidence_files") or []
            if not _resolve(path, root).is_file()
        ]
        missing_evals = [item for item in layer.get("eval_ids") or [] if item not in configured_evals]
        failed_evals = [
            item for item in layer.get("eval_ids") or []
            if latest_results.get(item) != "pass" or verification_results.get(item) != "pass"
        ]
        passed = not missing_files and not missing_evals and not failed_evals
        layers.append({
            "id": layer_id,
            "offline_shadow_ready": passed,
            "missing_evidence_files": missing_files,
            "missing_eval_registrations": missing_evals,
            "nonpassing_evals": failed_evals,
        })
    if not layers:
        raise ReplacementReadinessError("replacement requirements contain no layers")
    gates = []
    for gate in requirements.get("production_gates") or []:
        gate_id = str(gate.get("id") or "").strip()
        satisfied = gate.get("satisfied") is True
        evidence_path = gate.get("evidence_path")
        evidence_exists = bool(evidence_path and _resolve(evidence_path, root).is_file())
        if satisfied and not evidence_exists:
            raise ReplacementReadinessError(
                f"production gate is marked satisfied without evidence: {gate_id}"
            )
        gates.append({
            "id": gate_id,
            "satisfied": satisfied and evidence_exists,
            "evidence_path": evidence_path,
        })
    if not gates:
        raise ReplacementReadinessError("replacement requirements contain no production gates")
    offline_ready = all(item["offline_shadow_ready"] for item in layers)
    production_ready = offline_ready and all(item["satisfied"] for item in gates)
    return {
        "schema_version": "outreach.instantly-replacement-readiness.v1",
        "offline_shadow_ready": offline_ready,
        "production_replacement_ready": production_ready,
        "decision": "production_replacement_ready" if production_ready else (
            "offline_shadow_ready_live_gates_blocked" if offline_ready else "offline_shadow_incomplete"
        ),
        "eval_run_id": eval_state.get("run_id"),
        "eval_verification_status": eval_state.get("verification_status"),
        "layers": layers,
        "production_gates": gates,
        "safety": {
            "email_send_allowed": False,
            "provider_api_calls_allowed": False,
            "crm_mutation_allowed": False,
            "production_routing_change_allowed": False,
        },
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", default=str(DEFAULT_REQUIREMENTS))
    parser.add_argument("--control", default=str(DEFAULT_CONTROL))
    parser.add_argument("--eval-config", default=str(DEFAULT_EVAL_CONFIG))
    parser.add_argument("--eval-state", default=str(DEFAULT_EVAL_STATE))
    return parser


def main():
    args = build_parser().parse_args()
    try:
        report = evaluate(
            _read_json(args.requirements),
            _read_json(args.control),
            _read_json(args.eval_config),
            _read_json(args.eval_state),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except ReplacementReadinessError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
