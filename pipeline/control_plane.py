#!/usr/bin/env python3
"""Auditable run state and human checkpoints for Outreach workflows.

This module is intentionally stdlib-only and does not execute external actions.
Workers record artifacts and verifier results here; a human records the decision.
The downstream executor remains a separate, deterministic component.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "outreach.control-plane.v1"
CHECKPOINT_DECISIONS = {"approve", "reject", "request_changes"}
STAGE_STATUSES = {"completed", "failed", "blocked", "skipped"}
VALUE_COMPARISON_SCHEMA = "outreach.value-comparison.v1"


class ControlPlaneError(RuntimeError):
    """Raised when run state, verification, or decision rules are violated."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return value or "run"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_ref(path: str | Path) -> dict:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ControlPlaneError(f"artifact does not exist: {resolved}")
    return {
        "path": str(resolved),
        "sha256": _sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ControlPlaneError(f"state file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ControlPlaneError(f"invalid JSON state: {path}: {exc}") from exc


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _key_values(values: list[str]) -> dict:
    parsed = {}
    for item in values:
        if "=" not in item:
            raise ControlPlaneError(f"expected KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ControlPlaneError(f"metric key is blank: {item}")
        try:
            parsed[key] = float(value)
        except ValueError:
            parsed[key] = value
    return parsed


class ControlPlane:
    """File-backed control plane for resumable, human-reviewed workflows."""

    def __init__(self, root: str | Path, config_path: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = _read_json(self.config_path)
        self._validate_config()

    def _validate_config(self) -> None:
        if self.config.get("schema_version") != "outreach.orchestration.v1":
            raise ControlPlaneError("unsupported orchestration config schema")
        if not self.config.get("lanes"):
            raise ControlPlaneError("orchestration config has no lanes")

    def _run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def _manifest_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "manifest.json"

    def _checkpoint_path(self, run_id: str, checkpoint_id: str) -> Path:
        return self._run_dir(run_id) / "checkpoints" / f"{checkpoint_id}.json"

    def _workflow(self, lane: str, workflow: str) -> dict:
        lane_config = (self.config.get("lanes") or {}).get(lane)
        if not lane_config:
            raise ControlPlaneError(f"unknown lane: {lane}")
        workflow_config = (lane_config.get("workflows") or {}).get(workflow)
        if not workflow_config:
            raise ControlPlaneError(f"unknown workflow for {lane}: {workflow}")
        return workflow_config

    def start_run(self, lane: str, workflow: str, objective: str) -> dict:
        workflow_config = self._workflow(lane, workflow)
        if not (objective or "").strip():
            raise ControlPlaneError("run objective is required")
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{_slug(lane)}-{uuid.uuid4().hex[:8]}"
        run_dir = self._run_dir(run_id)
        (run_dir / "checkpoints").mkdir(parents=True, exist_ok=False)
        stages = [stage["id"] for stage in workflow_config.get("stages", [])]
        if not stages:
            raise ControlPlaneError(f"workflow has no stages: {lane}/{workflow}")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "lane": lane,
            "workflow": workflow,
            "objective": objective.strip(),
            "status": "active",
            "current_stage": stages[0],
            "stages": stages,
            "created_at": _now(),
            "updated_at": _now(),
            "config": {
                "path": str(self.config_path),
                "sha256": _sha256(self.config_path),
            },
            "metrics": {},
        }
        _atomic_json(self._manifest_path(run_id), manifest)
        self._event(run_id, "run_started", {"lane": lane, "workflow": workflow})
        return manifest

    def load_run(self, run_id: str) -> dict:
        return _read_json(self._manifest_path(run_id))

    def _save_run(self, manifest: dict) -> None:
        manifest["updated_at"] = _now()
        _atomic_json(self._manifest_path(manifest["run_id"]), manifest)

    def _event(self, run_id: str, event_type: str, payload: dict) -> None:
        _append_jsonl(
            self._run_dir(run_id) / "events.jsonl",
            {"ts": _now(), "event_type": event_type, "payload": payload},
        )

    def record_stage(
        self,
        run_id: str,
        stage: str,
        status: str,
        summary: str,
        metrics: dict | None = None,
        artifacts: list[str] | None = None,
    ) -> dict:
        manifest = self.load_run(run_id)
        if stage not in manifest["stages"]:
            raise ControlPlaneError(f"stage is not in this workflow: {stage}")
        if status not in STAGE_STATUSES:
            raise ControlPlaneError(f"invalid stage status: {status}")
        if not (summary or "").strip():
            raise ControlPlaneError("stage summary is required")
        result = {
            "ts": _now(),
            "stage": stage,
            "status": status,
            "summary": summary.strip(),
            "metrics": metrics or {},
            "artifacts": [_artifact_ref(path) for path in (artifacts or [])],
        }
        _append_jsonl(self._run_dir(run_id) / "stage_results.jsonl", result)
        manifest["metrics"].update(metrics or {})
        if status == "completed":
            index = manifest["stages"].index(stage)
            if index + 1 < len(manifest["stages"]):
                manifest["current_stage"] = manifest["stages"][index + 1]
                manifest["status"] = "active"
            else:
                manifest["current_stage"] = stage
                manifest["status"] = "completed"
        else:
            manifest["current_stage"] = stage
            manifest["status"] = "blocked"
        self._save_run(manifest)
        self._event(run_id, "stage_recorded", {"stage": stage, "status": status})
        return result

    def create_checkpoint(
        self,
        run_id: str,
        kind: str,
        title: str,
        summary: str,
        risk: str,
        recommendation: str,
        rollback: str,
        artifact_paths: list[str],
        required_checks: list[str] | None = None,
    ) -> dict:
        manifest = self.load_run(run_id)
        required = list(dict.fromkeys(required_checks or []))
        if not artifact_paths:
            raise ControlPlaneError("a checkpoint requires at least one review artifact")
        for label, value in {
            "kind": kind,
            "title": title,
            "summary": summary,
            "risk": risk,
            "recommendation": recommendation,
            "rollback": rollback,
        }.items():
            if not (value or "").strip():
                raise ControlPlaneError(f"checkpoint {label} is required")
        checkpoint_id = f"cp-{uuid.uuid4().hex[:10]}"
        checkpoint = {
            "schema_version": SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id,
            "run_id": run_id,
            "kind": kind.strip(),
            "title": title.strip(),
            "summary": summary.strip(),
            "risk": risk.strip(),
            "recommendation": recommendation.strip(),
            "rollback": rollback.strip(),
            "artifacts": [_artifact_ref(path) for path in artifact_paths],
            "required_checks": required,
            "checks": {},
            "status": "pending_verification" if required else "pending_human",
            "decision": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        _atomic_json(self._checkpoint_path(run_id, checkpoint_id), checkpoint)
        manifest["status"] = checkpoint["status"]
        self._save_run(manifest)
        self._event(run_id, "checkpoint_created", {"checkpoint_id": checkpoint_id, "kind": kind})
        return checkpoint

    def create_value_promotion_checkpoint(
        self,
        run_id: str,
        comparison_path: str | Path,
        title: str,
        rollback: str,
        artifact_paths: list[str] | None = None,
    ) -> dict:
        """Create a promotion review only after value guardrails pass."""
        comparison_file = Path(comparison_path).expanduser().resolve()
        comparison = _read_json(comparison_file)
        if comparison.get("schema_version") != VALUE_COMPARISON_SCHEMA:
            raise ControlPlaneError("promotion comparison uses an unsupported schema")
        if not comparison.get("promotion_eligible"):
            failed = ", ".join(comparison.get("failed_checks") or []) or "unknown checks"
            raise ControlPlaneError(f"candidate is not eligible for promotion: {failed}")
        if any(check.get("passed") is not True for check in comparison.get("checks") or []):
            raise ControlPlaneError("promotion comparison contains a failed or incomplete check")
        baseline = comparison.get("baseline") or {}
        candidate = comparison.get("candidate") or {}
        economics = comparison.get("economics") or {}
        summary = (
            f"{candidate.get('strategy_id', 'candidate')} passed value-weighted guardrails against "
            f"{baseline.get('strategy_id', 'baseline')}. Critical catch-rate delta: "
            f"{economics.get('critical_catch_rate_delta', 0):+.3f}; weighted-value delta: "
            f"{economics.get('weighted_value_capture_delta', 0):+.3f}; net expected-value delta/run: "
            f"${economics.get('net_expected_value_delta_per_run_usd', 0):+.2f}."
        )
        risks = comparison.get("remaining_risks") or [
            "Benchmark performance may not transfer to production data."
        ]
        artifacts = [str(comparison_file)] + list(artifact_paths or [])
        checkpoint = self.create_checkpoint(
            run_id=run_id,
            kind="value_based_promotion",
            title=title,
            summary=summary,
            risk=" ".join(risks),
            recommendation=comparison.get("recommendation") or "Review the candidate for promotion.",
            rollback=rollback,
            artifact_paths=artifacts,
            required_checks=[],
        )
        checkpoint["value_guardrails"] = comparison.get("checks") or []
        checkpoint["economics"] = economics
        checkpoint["updated_at"] = _now()
        _atomic_json(self._checkpoint_path(run_id, checkpoint["checkpoint_id"]), checkpoint)
        return checkpoint

    def load_checkpoint(self, run_id: str, checkpoint_id: str) -> dict:
        return _read_json(self._checkpoint_path(run_id, checkpoint_id))

    def record_check(
        self,
        run_id: str,
        checkpoint_id: str,
        name: str,
        passed: bool,
        details: str,
        evidence_path: str = "",
    ) -> dict:
        checkpoint = self.load_checkpoint(run_id, checkpoint_id)
        if checkpoint["status"] not in {"pending_verification", "pending_human"}:
            raise ControlPlaneError("checkpoint is already decided")
        if name not in checkpoint["required_checks"]:
            raise ControlPlaneError(f"check is not required by this checkpoint: {name}")
        if not (details or "").strip():
            raise ControlPlaneError("check details are required")
        result = {"passed": bool(passed), "details": details.strip(), "checked_at": _now()}
        if evidence_path:
            result["evidence"] = _artifact_ref(evidence_path)
        checkpoint["checks"][name] = result
        if all(
            checkpoint["checks"].get(check, {}).get("passed") is True
            for check in checkpoint["required_checks"]
        ):
            checkpoint["status"] = "pending_human"
            manifest = self.load_run(run_id)
            manifest["status"] = "pending_human"
            self._save_run(manifest)
        else:
            checkpoint["status"] = "pending_verification"
        checkpoint["updated_at"] = _now()
        _atomic_json(self._checkpoint_path(run_id, checkpoint_id), checkpoint)
        self._event(
            run_id,
            "verification_recorded",
            {"checkpoint_id": checkpoint_id, "name": name, "passed": bool(passed)},
        )
        return checkpoint

    def _verify_artifacts_unchanged(self, checkpoint: dict) -> None:
        stale = []
        for artifact in checkpoint["artifacts"]:
            path = Path(artifact["path"])
            if not path.is_file() or _sha256(path) != artifact["sha256"]:
                stale.append(artifact["path"])
        if stale:
            raise ControlPlaneError("checkpoint artifacts changed after review was requested: " + ", ".join(stale))

    def decide(
        self,
        run_id: str,
        checkpoint_id: str,
        decision: str,
        actor: str,
        note: str,
    ) -> dict:
        checkpoint = self.load_checkpoint(run_id, checkpoint_id)
        if decision not in CHECKPOINT_DECISIONS:
            raise ControlPlaneError(f"invalid decision: {decision}")
        if checkpoint["status"] != "pending_human":
            raise ControlPlaneError("checkpoint is not ready for human decision")
        if not (actor or "").strip() or not (note or "").strip():
            raise ControlPlaneError("decision actor and note are required")
        self._verify_artifacts_unchanged(checkpoint)
        checkpoint["status"] = {
            "approve": "approved",
            "reject": "rejected",
            "request_changes": "changes_requested",
        }[decision]
        checkpoint["decision"] = {
            "value": decision,
            "actor": actor.strip(),
            "note": note.strip(),
            "decided_at": _now(),
            "artifact_fingerprint": hashlib.sha256(
                "|".join(item["sha256"] for item in checkpoint["artifacts"]).encode("utf-8")
            ).hexdigest(),
        }
        checkpoint["updated_at"] = _now()
        _atomic_json(self._checkpoint_path(run_id, checkpoint_id), checkpoint)
        manifest = self.load_run(run_id)
        manifest["status"] = "active" if decision == "approve" else "blocked"
        self._save_run(manifest)
        _append_jsonl(self._run_dir(run_id) / "decisions.jsonl", checkpoint["decision"] | {
            "checkpoint_id": checkpoint_id,
            "kind": checkpoint["kind"],
        })
        self._event(run_id, "human_decision_recorded", {"checkpoint_id": checkpoint_id, "decision": decision})
        return checkpoint

    def dashboard(self, run_id: str) -> str:
        manifest = self.load_run(run_id)
        checkpoint_dir = self._run_dir(run_id) / "checkpoints"
        checkpoints = [_read_json(path) for path in sorted(checkpoint_dir.glob("*.json"))]
        pending = [item for item in checkpoints if item["status"] in {"pending_verification", "pending_human"}]
        lines = [
            f"Run: {manifest['run_id']}",
            f"Lane / workflow: {manifest['lane']} / {manifest['workflow']}",
            f"Status: {manifest['status']}",
            f"Current stage: {manifest['current_stage']}",
            f"Objective: {manifest['objective']}",
            "",
            f"Pending decisions: {len(pending)}",
        ]
        for checkpoint in pending:
            passed = sum(
                1 for check in checkpoint["required_checks"]
                if checkpoint["checks"].get(check, {}).get("passed") is True
            )
            lines.extend([
                f"- {checkpoint['checkpoint_id']} [{checkpoint['status']}] {checkpoint['title']}",
                f"  Summary: {checkpoint['summary']}",
                f"  Risk: {checkpoint['risk']}",
                f"  Recommendation: {checkpoint['recommendation']}",
                (
                    f"  Value guardrails: "
                    f"{sum(1 for check in checkpoint.get('value_guardrails', []) if check.get('passed') is True)}"
                    f"/{len(checkpoint.get('value_guardrails', []))} passed"
                    if checkpoint.get("value_guardrails") else
                    f"  Checks: {passed}/{len(checkpoint['required_checks'])} passed"
                ),
                f"  Artifact digest: {checkpoint['artifacts'][0]['sha256'][:12]}",
            ])
        if not pending:
            lines.append("- None")
        return "\n".join(lines)


def _default_config() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "orchestration.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Outreach human decision control plane")
    parser.add_argument(
        "--root",
        default=os.environ.get("OUTREACH_CONTROL_ROOT", str(Path.home() / ".outreach" / "state" / "control_plane")),
    )
    parser.add_argument("--config", default=str(_default_config()))
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start a resumable workflow run")
    start.add_argument("--lane", required=True)
    start.add_argument("--workflow", required=True)
    start.add_argument("--objective", required=True)

    stage = sub.add_parser("stage", help="record a worker stage result")
    stage.add_argument("--run", required=True)
    stage.add_argument("--stage", required=True)
    stage.add_argument("--status", required=True, choices=sorted(STAGE_STATUSES))
    stage.add_argument("--summary", required=True)
    stage.add_argument("--metric", action="append", default=[])
    stage.add_argument("--artifact", action="append", default=[])

    checkpoint = sub.add_parser("checkpoint", help="create an evidence-backed human checkpoint")
    checkpoint.add_argument("--run", required=True)
    checkpoint.add_argument("--kind", required=True)
    checkpoint.add_argument("--title", required=True)
    checkpoint.add_argument("--summary", required=True)
    checkpoint.add_argument("--risk", required=True)
    checkpoint.add_argument("--recommendation", required=True)
    checkpoint.add_argument("--rollback", required=True)
    checkpoint.add_argument("--artifact", action="append", required=True)
    checkpoint.add_argument("--check", action="append", default=[])

    promotion = sub.add_parser(
        "promotion-checkpoint",
        help="create a human promotion review from an eligible value comparison",
    )
    promotion.add_argument("--run", required=True)
    promotion.add_argument("--comparison", required=True)
    promotion.add_argument("--title", required=True)
    promotion.add_argument("--rollback", required=True)
    promotion.add_argument("--artifact", action="append", default=[])

    check = sub.add_parser("check", help="record a verifier result")
    check.add_argument("--run", required=True)
    check.add_argument("--checkpoint", required=True)
    check.add_argument("--name", required=True)
    outcome = check.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--pass", dest="passed", action="store_true")
    outcome.add_argument("--fail", dest="passed", action="store_false")
    check.add_argument("--details", required=True)
    check.add_argument("--evidence", default="")

    decide = sub.add_parser("decide", help="record the human decision")
    decide.add_argument("--run", required=True)
    decide.add_argument("--checkpoint", required=True)
    decide.add_argument("--decision", required=True, choices=sorted(CHECKPOINT_DECISIONS))
    decide.add_argument("--actor", required=True)
    decide.add_argument("--note", required=True)

    dashboard = sub.add_parser("dashboard", help="show the next decision in plain language")
    dashboard.add_argument("--run", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    control = ControlPlane(args.root, args.config)
    try:
        if args.command == "start":
            result = control.start_run(args.lane, args.workflow, args.objective)
        elif args.command == "stage":
            result = control.record_stage(
                args.run, args.stage, args.status, args.summary,
                metrics=_key_values(args.metric), artifacts=args.artifact,
            )
        elif args.command == "checkpoint":
            result = control.create_checkpoint(
                args.run, args.kind, args.title, args.summary, args.risk,
                args.recommendation, args.rollback, args.artifact, args.check,
            )
        elif args.command == "promotion-checkpoint":
            result = control.create_value_promotion_checkpoint(
                args.run, args.comparison, args.title, args.rollback, args.artifact,
            )
        elif args.command == "check":
            result = control.record_check(
                args.run, args.checkpoint, args.name, args.passed,
                args.details, args.evidence,
            )
        elif args.command == "decide":
            result = control.decide(
                args.run, args.checkpoint, args.decision, args.actor, args.note,
            )
        else:
            print(control.dashboard(args.run))
            return 0
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ControlPlaneError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
