#!/usr/bin/env python3
"""Value-weighted evaluation for layered agent strategies.

The evaluator measures repeated runs against explicit ground-truth findings.
It intentionally separates run price, finding volume, and the economic value of
the specific failures a strategy detects.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path


BENCHMARK_SCHEMA = "outreach.value-benchmark.v1"
BUNDLE_SCHEMA = "outreach.value-run-bundle.v1"
POLICY_SCHEMA = "outreach.value-policy.v1"
EVALUATION_SCHEMA = "outreach.value-evaluation.v1"
COMPARISON_SCHEMA = "outreach.value-comparison.v1"
SEVERITIES = {"critical", "high", "medium", "low"}


class EvaluationError(RuntimeError):
    """Raised for invalid evaluation inputs or unsafe comparisons."""


def load_json(path: str | Path) -> dict:
    path = Path(path).expanduser().resolve()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid JSON: {path}: {exc}") from exc


def write_json(path: str | Path, payload: dict) -> None:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _number(value, label: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(f"{label} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise EvaluationError(f"{label} must be at least {minimum}")
    return value


def _rate(value, label: str) -> float:
    value = _number(value, label)
    if value > 1:
        raise EvaluationError(f"{label} must be between 0 and 1")
    return value


def _finding_key(case_id: str, finding_id: str) -> str:
    return f"{case_id}::{finding_id}"


def _validate_benchmark(benchmark: dict) -> tuple[dict, list[str]]:
    if benchmark.get("schema_version") != BENCHMARK_SCHEMA:
        raise EvaluationError("unsupported benchmark schema")
    truth = {}
    class_order = []
    for case in benchmark.get("cases") or []:
        case_id = (case.get("case_id") or "").strip()
        if not case_id:
            raise EvaluationError("benchmark case_id is required")
        for finding in case.get("truth") or []:
            finding_id = (finding.get("finding_id") or "").strip()
            finding_class = (finding.get("class") or "").strip()
            severity = (finding.get("severity") or "").strip()
            if not finding_id or not finding_class:
                raise EvaluationError(f"finding id and class are required in case {case_id}")
            if severity not in SEVERITIES:
                raise EvaluationError(f"invalid severity for {case_id}/{finding_id}: {severity}")
            key = _finding_key(case_id, finding_id)
            if key in truth:
                raise EvaluationError(f"duplicate benchmark finding: {key}")
            value_usd = _number(finding.get("value_usd"), f"{key}.value_usd")
            realization = _rate(
                finding.get("realization_probability", 1.0),
                f"{key}.realization_probability",
            )
            truth[key] = {
                "case_id": case_id,
                "finding_id": finding_id,
                "class": finding_class,
                "severity": severity,
                "value_usd": value_usd,
                "realization_probability": realization,
                "expected_value_usd": value_usd * realization,
                "description": (finding.get("description") or "").strip(),
            }
            if finding_class not in class_order:
                class_order.append(finding_class)
    if not truth:
        raise EvaluationError("benchmark has no ground-truth findings")
    return truth, class_order


def _validate_bundle(bundle: dict) -> None:
    if bundle.get("schema_version") != BUNDLE_SCHEMA:
        raise EvaluationError("unsupported run bundle schema")
    if not (bundle.get("strategy_id") or "").strip():
        raise EvaluationError("strategy_id is required")
    layer_order = bundle.get("layer_order") or []
    if not layer_order or len(layer_order) != len(set(layer_order)):
        raise EvaluationError("layer_order must contain unique layer ids")
    runs = bundle.get("runs") or []
    if not runs:
        raise EvaluationError("run bundle has no runs")
    seen_runs = set()
    for run in runs:
        run_id = (run.get("run_id") or "").strip()
        if not run_id or run_id in seen_runs:
            raise EvaluationError(f"run_id must be unique and non-blank: {run_id}")
        seen_runs.add(run_id)
        _number(run.get("cost_usd"), f"{run_id}.cost_usd")
        _number(run.get("human_review_minutes", 0), f"{run_id}.human_review_minutes")
        for layer, cost in (run.get("layer_costs") or {}).items():
            if layer not in layer_order:
                raise EvaluationError(f"unknown layer in {run_id}: {layer}")
            _number(cost, f"{run_id}.layer_costs.{layer}")
        for finding in run.get("findings") or []:
            if not (finding.get("case_id") and finding.get("finding_id")):
                raise EvaluationError(f"reported finding missing ids in {run_id}")
            if finding.get("layer") not in layer_order:
                raise EvaluationError(f"reported finding has unknown layer in {run_id}")


def _validate_policy(policy: dict) -> None:
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise EvaluationError("unsupported value policy schema")


def evaluate(benchmark: dict, bundle: dict, policy: dict) -> dict:
    truth, class_order = _validate_benchmark(benchmark)
    _validate_bundle(bundle)
    _validate_policy(policy)
    guardrails = policy.get("guardrails") or {}
    review_rate = _number(guardrails.get("review_hourly_rate_usd", 0), "review_hourly_rate_usd")
    false_positive_penalty = _number(
        guardrails.get("false_positive_penalty_usd", 0),
        "false_positive_penalty_usd",
    )
    layer_order = bundle["layer_order"]
    layer_rank = {layer: index for index, layer in enumerate(layer_order)}
    detection_counts = {key: 0 for key in truth}
    per_run = []
    layer_stats = {
        layer: {
            "cost_usd": 0.0,
            "first_true_findings": 0,
            "first_expected_value_usd": 0.0,
            "false_findings": 0,
        }
        for layer in layer_order
    }
    total_model_cost = 0.0
    total_review_cost = 0.0
    total_false_reports = 0
    total_true_reports = 0
    total_value_captured = 0.0
    total_duplicate_reports = 0

    for run in bundle["runs"]:
        run_cost = _number(run["cost_usd"], f"{run['run_id']}.cost_usd")
        review_cost = _number(run.get("human_review_minutes", 0), "human_review_minutes") / 60 * review_rate
        total_model_cost += run_cost
        total_review_cost += review_cost
        for layer, cost in (run.get("layer_costs") or {}).items():
            layer_stats[layer]["cost_usd"] += float(cost)

        reports_by_key = {}
        duplicate_reports = 0
        for report in run.get("findings") or []:
            key = _finding_key(report["case_id"], report["finding_id"])
            existing = reports_by_key.get(key)
            if existing is None or layer_rank[report["layer"]] < layer_rank[existing["layer"]]:
                if existing is not None:
                    duplicate_reports += 1
                reports_by_key[key] = report
            else:
                duplicate_reports += 1
        total_duplicate_reports += duplicate_reports
        true_keys = set(reports_by_key) & set(truth)
        false_keys = set(reports_by_key) - set(truth)
        for key in true_keys:
            detection_counts[key] += 1
            layer = reports_by_key[key]["layer"]
            layer_stats[layer]["first_true_findings"] += 1
            layer_stats[layer]["first_expected_value_usd"] += truth[key]["expected_value_usd"]
        for key in false_keys:
            layer_stats[reports_by_key[key]["layer"]]["false_findings"] += 1

        value_captured = sum(truth[key]["expected_value_usd"] for key in true_keys)
        critical_total = sum(1 for item in truth.values() if item["severity"] == "critical")
        critical_found = sum(1 for key in true_keys if truth[key]["severity"] == "critical")
        total_true_reports += len(true_keys)
        total_false_reports += len(false_keys)
        total_value_captured += value_captured
        report_count = len(reports_by_key)
        per_run.append({
            "run_id": run["run_id"],
            "model_cost_usd": run_cost,
            "review_cost_usd": review_cost,
            "reported_findings": report_count,
            "verified_true_findings": len(true_keys),
            "false_findings": len(false_keys),
            "duplicate_reports": duplicate_reports,
            "precision": len(true_keys) / report_count if report_count else 1.0,
            "critical_catch_rate": critical_found / critical_total if critical_total else 1.0,
            "expected_value_captured_usd": value_captured,
        })

    attempts = len(per_run)
    total_truth_value = sum(item["expected_value_usd"] for item in truth.values())
    total_operating_cost = total_model_cost + total_review_cost
    false_positive_cost = total_false_reports * false_positive_penalty
    net_value = total_value_captured - total_operating_cost - false_positive_cost
    total_report_count = total_true_reports + total_false_reports
    critical_opportunities = attempts * sum(1 for item in truth.values() if item["severity"] == "critical")
    critical_detections = sum(
        detection_counts[key]
        for key, item in truth.items()
        if item["severity"] == "critical"
    )

    finding_metrics = []
    for key, item in truth.items():
        detections = detection_counts[key]
        rate = detections / attempts
        finding_metrics.append({
            **item,
            "detections": detections,
            "attempts": attempts,
            "detection_rate": rate,
            "expected_spend_to_surface_once_usd": (
                total_operating_cost / detections if detections else None
            ),
        })
    finding_metrics.sort(key=lambda item: (-item["expected_value_usd"], item["case_id"], item["finding_id"]))

    class_metrics = {}
    for finding_class in class_order:
        keys = [key for key, item in truth.items() if item["class"] == finding_class]
        opportunities = attempts * len(keys)
        detected = sum(detection_counts[key] for key in keys)
        available_value = attempts * sum(truth[key]["expected_value_usd"] for key in keys)
        captured_value = sum(
            truth[key]["expected_value_usd"] * detection_counts[key] for key in keys
        )
        class_metrics[finding_class] = {
            "finding_count": len(keys),
            "detection_rate": detected / opportunities if opportunities else 1.0,
            "weighted_value_capture_rate": captured_value / available_value if available_value else 1.0,
            "expected_value_captured_usd": captured_value,
        }

    summary = {
        "attempts": attempts,
        "median_model_cost_per_run_usd": statistics.median(item["model_cost_usd"] for item in per_run),
        "model_cost_range_usd": [
            min(item["model_cost_usd"] for item in per_run),
            max(item["model_cost_usd"] for item in per_run),
        ],
        "median_distinct_findings_per_run": statistics.median(item["reported_findings"] for item in per_run),
        "verified_true_findings_per_run": total_true_reports / attempts,
        "precision": total_true_reports / total_report_count if total_report_count else 1.0,
        "false_positive_rate": total_false_reports / total_report_count if total_report_count else 0.0,
        "unweighted_recall": sum(detection_counts.values()) / (attempts * len(truth)),
        "critical_catch_rate": critical_detections / critical_opportunities if critical_opportunities else 1.0,
        "weighted_value_capture_rate": total_value_captured / (attempts * total_truth_value),
        "model_cost_usd": total_model_cost,
        "human_review_cost_usd": total_review_cost,
        "false_positive_penalty_usd": false_positive_cost,
        "total_operating_cost_usd": total_operating_cost,
        "cost_per_verified_true_finding_usd": (
            total_operating_cost / total_true_reports if total_true_reports else None
        ),
        "expected_value_captured_per_run_usd": total_value_captured / attempts,
        "net_expected_value_per_run_usd": net_value / attempts,
        "value_return_multiple": (
            total_value_captured / (total_operating_cost + false_positive_cost)
            if total_operating_cost + false_positive_cost else None
        ),
        "duplicate_reports": total_duplicate_reports,
    }
    return {
        "schema_version": EVALUATION_SCHEMA,
        "benchmark_id": benchmark.get("benchmark_id"),
        "strategy_id": bundle["strategy_id"],
        "summary": summary,
        "finding_metrics": finding_metrics,
        "class_metrics": class_metrics,
        "layer_metrics": layer_stats,
        "per_run": per_run,
    }


def compare(baseline: dict, candidate: dict, policy: dict) -> dict:
    if baseline.get("schema_version") != EVALUATION_SCHEMA:
        raise EvaluationError("baseline is not a value evaluation")
    if candidate.get("schema_version") != EVALUATION_SCHEMA:
        raise EvaluationError("candidate is not a value evaluation")
    if baseline.get("benchmark_id") != candidate.get("benchmark_id"):
        raise EvaluationError("evaluations use different benchmarks")
    _validate_policy(policy)
    b = baseline["summary"]
    c = candidate["summary"]
    guardrails = policy.get("guardrails") or {}
    checks = []

    def add(name: str, passed: bool, baseline_value, candidate_value, rule: str) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "baseline": baseline_value,
            "candidate": candidate_value,
            "rule": rule,
        })

    minimum_critical = _rate(
        guardrails.get("minimum_critical_catch_rate", 0),
        "minimum_critical_catch_rate",
    )
    max_critical_regression = _rate(
        guardrails.get("maximum_critical_regression", 0),
        "maximum_critical_regression",
    )
    max_value_regression = _rate(
        guardrails.get("maximum_weighted_value_capture_regression", 0),
        "maximum_weighted_value_capture_regression",
    )
    max_false_positive = _rate(
        guardrails.get("maximum_false_positive_rate", 1),
        "maximum_false_positive_rate",
    )
    add(
        "critical_floor",
        c["critical_catch_rate"] >= minimum_critical,
        b["critical_catch_rate"], c["critical_catch_rate"],
        f"candidate >= {minimum_critical}",
    )
    add(
        "critical_non_regression",
        c["critical_catch_rate"] + max_critical_regression >= b["critical_catch_rate"],
        b["critical_catch_rate"], c["critical_catch_rate"],
        f"candidate regression <= {max_critical_regression}",
    )
    add(
        "weighted_value_non_regression",
        c["weighted_value_capture_rate"] + max_value_regression >= b["weighted_value_capture_rate"],
        b["weighted_value_capture_rate"], c["weighted_value_capture_rate"],
        f"candidate regression <= {max_value_regression}",
    )
    add(
        "false_positive_guardrail",
        c["false_positive_rate"] <= max_false_positive,
        b["false_positive_rate"], c["false_positive_rate"],
        f"candidate <= {max_false_positive}",
    )
    if guardrails.get("require_positive_net_value_delta", False):
        add(
            "positive_net_value_delta",
            c["net_expected_value_per_run_usd"] > b["net_expected_value_per_run_usd"],
            b["net_expected_value_per_run_usd"], c["net_expected_value_per_run_usd"],
            "candidate must exceed baseline",
        )

    mandatory_failures = []
    for finding_class in policy.get("mandatory_classes") or []:
        baseline_class = baseline.get("class_metrics", {}).get(finding_class)
        candidate_class = candidate.get("class_metrics", {}).get(finding_class)
        if candidate_class and candidate_class["detection_rate"] < 1.0:
            mandatory_failures.append(finding_class)
        if baseline_class or candidate_class:
            add(
                f"mandatory_class:{finding_class}",
                bool(candidate_class and candidate_class["detection_rate"] == 1.0),
                baseline_class["detection_rate"] if baseline_class else None,
                candidate_class["detection_rate"] if candidate_class else None,
                "candidate detection rate must equal 1.0",
            )

    eligible = all(check["passed"] for check in checks)
    failed = [check["name"] for check in checks if not check["passed"]]
    cost_ratio = (
        c["median_model_cost_per_run_usd"] / b["median_model_cost_per_run_usd"]
        if b["median_model_cost_per_run_usd"] else None
    )
    return {
        "schema_version": COMPARISON_SCHEMA,
        "benchmark_id": baseline["benchmark_id"],
        "baseline": {
            "strategy_id": baseline["strategy_id"],
            "summary": b,
        },
        "candidate": {
            "strategy_id": candidate["strategy_id"],
            "summary": c,
        },
        "checks": checks,
        "promotion_eligible": eligible,
        "failed_checks": failed,
        "mandatory_class_failures": mandatory_failures,
        "economics": {
            "model_cost_ratio": cost_ratio,
            "critical_catch_rate_delta": c["critical_catch_rate"] - b["critical_catch_rate"],
            "weighted_value_capture_delta": c["weighted_value_capture_rate"] - b["weighted_value_capture_rate"],
            "net_expected_value_delta_per_run_usd": (
                c["net_expected_value_per_run_usd"] - b["net_expected_value_per_run_usd"]
            ),
        },
        "recommendation": (
            f"Eligible for human promotion review: {candidate['strategy_id']} improves value economics "
            "without violating critical-quality guardrails."
            if eligible else
            f"Do not promote {candidate['strategy_id']}; failed checks: {', '.join(failed)}."
        ),
        "remaining_risks": [
            "Benchmark performance may not transfer to new source distributions.",
            "Finding values and realization probabilities require periodic human recalibration.",
            "Correlated repeated runs can overstate the benefit of retrying the same strategy."
        ],
    }


def escalation_decision(policy: dict, finding_class: str, signals: list[str] | None = None) -> dict:
    _validate_policy(policy)
    config = (policy.get("escalation_classes") or {}).get(finding_class)
    if not config:
        raise EvaluationError(f"no escalation calibration for finding class: {finding_class}")
    signals = list(dict.fromkeys(signals or []))
    scout = _rate(config["scout_detection_rate"], "scout_detection_rate")
    specialist = _rate(config["specialist_detection_rate"], "specialist_detection_rate")
    realization = _rate(config.get("realization_probability", 1), "realization_probability")
    miss_impact = _number(config["miss_impact_usd"], "miss_impact_usd")
    model_cost = _number(config["incremental_model_cost_usd"], "incremental_model_cost_usd")
    review_minutes = _number(config.get("incremental_review_minutes", 0), "incremental_review_minutes")
    review_rate = _number(
        (policy.get("guardrails") or {}).get("review_hourly_rate_usd", 0),
        "review_hourly_rate_usd",
    )
    incremental_cost = model_cost + review_minutes / 60 * review_rate
    incremental_detection = max(0.0, specialist - scout)
    expected_value = incremental_detection * miss_impact * realization
    matched_signals = sorted(set(signals) & set(config.get("always_escalate_signals") or []))
    mandatory = finding_class in set(policy.get("mandatory_classes") or [])
    escalate = mandatory or bool(matched_signals) or expected_value > incremental_cost
    return {
        "schema_version": "outreach.escalation-decision.v1",
        "finding_class": finding_class,
        "signals": signals,
        "matched_mandatory_signals": matched_signals,
        "mandatory_class": mandatory,
        "incremental_detection_probability": incremental_detection,
        "expected_incremental_value_usd": expected_value,
        "incremental_cost_usd": incremental_cost,
        "expected_value_to_cost_ratio": expected_value / incremental_cost if incremental_cost else None,
        "decision": "escalate" if escalate else "stop_after_scout",
        "reason": (
            "mandatory safety class" if mandatory else
            "mandatory risk signal present" if matched_signals else
            "expected value exceeds incremental cost" if escalate else
            "incremental cost exceeds expected value"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate layered agents by verified business value")
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate_cmd = sub.add_parser("evaluate")
    evaluate_cmd.add_argument("--benchmark", required=True)
    evaluate_cmd.add_argument("--bundle", required=True)
    evaluate_cmd.add_argument("--policy", required=True)
    evaluate_cmd.add_argument("--output")
    compare_cmd = sub.add_parser("compare")
    compare_cmd.add_argument("--baseline", required=True)
    compare_cmd.add_argument("--candidate", required=True)
    compare_cmd.add_argument("--policy", required=True)
    compare_cmd.add_argument("--output")
    route_cmd = sub.add_parser("route")
    route_cmd.add_argument("--policy", required=True)
    route_cmd.add_argument("--finding-class", required=True)
    route_cmd.add_argument("--signal", action="append", default=[])
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "evaluate":
            result = evaluate(load_json(args.benchmark), load_json(args.bundle), load_json(args.policy))
        elif args.command == "compare":
            result = compare(load_json(args.baseline), load_json(args.candidate), load_json(args.policy))
        else:
            result = escalation_decision(load_json(args.policy), args.finding_class, args.signal)
        if getattr(args, "output", None):
            write_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except EvaluationError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
