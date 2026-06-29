#!/usr/bin/env python3
"""Run the workspace eval suite and persist pass/fail transitions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "workspace/config/eval_compounding.json"
STATE_START = "<!-- nightly-eval-compounding:start -->"
STATE_END = "<!-- nightly-eval-compounding:end -->"
TELEGRAM_MESSAGE_LIMIT = 4096


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_path(root, value):
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_config(config_path, root=REPO_ROOT):
    config = load_json(config_path)
    if not isinstance(config, dict) or not isinstance(config.get("tests"), list):
        raise ValueError(f"Invalid eval config: {config_path}")
    config = dict(config)
    for key in ("state_path", "digest_path", "state_doc_path"):
        config[key] = resolve_path(root, config[key])
    delivery = config.get("delivery") or {}
    config["delivery"] = {
        "target": str(delivery.get("target") or "local_digest"),
        "destination": str(delivery.get("destination") or config["digest_path"]),
        "mode": str(delivery.get("mode") or "local_file"),
    }
    return config


def output_tail(stdout, stderr, limit=4000):
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    return combined[-limit:]


def telegram_ssl_context():
    try:
        import certifi
    except Exception:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def run_test(test, root):
    started = time.monotonic()
    command = test["command"]
    timeout = int(test.get("timeout_seconds", 300))
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code = completed.returncode
        output = output_tail(completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        output = output_tail(exc.stdout or "", exc.stderr or "")
        output = f"Timed out after {timeout}s\n{output}".strip()
    duration = round(time.monotonic() - started, 3)
    return {
        "id": test["id"],
        "description": test.get("description", ""),
        "command": command,
        "owner_skill": test.get("owner_skill", ""),
        "status": "pass" if exit_code == 0 else "fail",
        "exit_code": exit_code,
        "duration_seconds": duration,
        "output_tail": output,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def classify_transitions(previous, current):
    previous_results = {
        item["id"]: item for item in (previous or {}).get("results", [])
    }
    newly_passed = []
    newly_failed = []
    added = []
    for result in current:
        old = previous_results.get(result["id"])
        if old is None:
            added.append(result["id"])
        elif old.get("status") != "pass" and result["status"] == "pass":
            newly_passed.append(result["id"])
        elif old.get("status") == "pass" and result["status"] != "pass":
            newly_failed.append(result["id"])
    current_ids = {item["id"] for item in current}
    removed = sorted(set(previous_results) - current_ids)
    return {
        "newly_passed": newly_passed,
        "newly_failed": newly_failed,
        "added": added,
        "removed": removed,
    }


def format_list(values, empty):
    if not values:
        return f"- {empty}"
    return "\n".join(f"- `{value}`" for value in values)


def build_digest(state):
    results = state["results"]
    passed = sum(item["status"] == "pass" for item in results)
    failed = len(results) - passed
    transitions = state["transitions"]
    lines = [
        "# Daily Eval Compounding Digest",
        "",
        f"Run: `{state['run_id']}`",
        f"Completed: {state['completed_at']}",
        f"Result: **{passed}/{len(results)} passed; {failed} failed**",
        f"Independent verification: **{state.get('verification_status', 'pending')}**",
        "",
        "## Transitions",
        "",
        "Newly passed:",
        format_list(transitions["newly_passed"], "None"),
        "",
        "Newly failed:",
        format_list(transitions["newly_failed"], "None"),
        "",
        "Suite changes:",
        format_list(transitions["added"], "No tests added"),
        format_list(transitions["removed"], "No tests removed"),
        "",
        "## Skill Distillation",
        "",
    ]
    if state.get("distillations"):
        for item in state["distillations"]:
            lines.append(f"- `{item['test_id']}` -> `{item['skill_path']}`: {item['note']}")
    elif transitions["newly_passed"]:
        lines.append("- Pending causal verification and workspace-skill update.")
    else:
        lines.append("- No newly passing tests require distillation.")
    lines.extend(["", "## Failures", ""])
    failing_results = [item for item in results if item["status"] != "pass"]
    if not failing_results:
        lines.append("- None.")
    else:
        for item in failing_results:
            excerpt = item["output_tail"] or "No output captured."
            lines.extend(
                [
                    f"### {item['id']}",
                    "",
                    f"Command: `{' '.join(item['command'])}`",
                    f"Exit: `{item['exit_code']}`",
                    "",
                    "```text",
                    excerpt,
                    "```",
                    "",
                ]
            )
    lines.extend(["", "## Failure Investigations", ""])
    if state.get("investigations"):
        for item in state["investigations"]:
            lines.append(f"- `{item['test_id']}`: {item['note']}")
    elif transitions["newly_failed"]:
        lines.append("- Pending evidence-based investigation.")
    else:
        lines.append("- No newly failing tests require investigation.")
    delivery = state.get("delivery", {})
    target = delivery.get("target", "engineering_digest")
    lines.extend(
        [
            "",
            "## Delivery",
            "",
            f"Target: `{target}`",
            f"Status: **{delivery.get('status', 'pending')}**",
        ]
    )
    if delivery.get("destination"):
        lines.append(f"Destination: `{delivery['destination']}`")
    if delivery.get("receipt"):
        lines.append(f"Receipt: {delivery['receipt']}")
    if delivery.get("reason"):
        lines.append(f"Blocker: {delivery['reason']}")
    return "\n".join(lines).rstrip() + "\n"


def build_state_section(state):
    transitions = state["transitions"]
    delivery = state.get("delivery", {})
    target = delivery.get("target", "engineering_digest")
    failing = [item for item in state["results"] if item["status"] != "pass"]
    lines = [
        STATE_START,
        "## Nightly Eval Compounding",
        "",
        f"- Last run: `{state['run_id']}` at {state['completed_at']}",
        f"- Passing: {sum(item['status'] == 'pass' for item in state['results'])}/{len(state['results'])}",
        f"- Verification pass: {state.get('verification_status', 'pending')}",
        f"- Digest target: {target}",
        f"- Digest delivery: {delivery.get('status', 'pending')}",
    ]
    if delivery.get("destination"):
        lines.append(f"- Digest destination: `{delivery['destination']}`")
    if delivery.get("receipt"):
        lines.append(f"- Delivery receipt: {delivery['receipt']}")
    if delivery.get("reason"):
        lines.append(f"- Delivery blocker: {delivery['reason']}")
    lines.extend(
        [
            "",
            "### Newly Passing",
            "",
            format_list(transitions["newly_passed"], "None"),
            "",
            "### Newly Failing",
            "",
            format_list(transitions["newly_failed"], "None"),
            "",
            "### Failure Investigation",
            "",
        ]
    )
    investigations = {item["test_id"]: item for item in state.get("investigations", [])}
    if not failing:
        lines.append("- No current failures.")
    else:
        for item in failing:
            investigation = investigations.get(item["id"])
            if investigation:
                lines.append(f"- `{item['id']}`: {investigation['note']}")
            else:
                lines.append(
                    f"- `{item['id']}` exits `{item['exit_code']}`. Investigation required; "
                    f"output fingerprint `{item['output_sha256'][:12]}`."
                )
    lines.extend(["", "### Skill Distillation", ""])
    if state.get("distillations"):
        for item in state["distillations"]:
            lines.append(f"- `{item['test_id']}` -> `{item['skill_path']}`: {item['note']}")
    elif transitions["newly_passed"]:
        lines.append("- Pending causal verification.")
    else:
        lines.append("- No newly passing tests require distillation.")
    lines.extend([STATE_END, ""])
    return "\n".join(lines)


def build_telegram_digest(state):
    results = state["results"]
    passed = sum(item["status"] == "pass" for item in results)
    transitions = state["transitions"]
    distillations = state.get("distillations", [])
    investigations = state.get("investigations", [])
    next_action = "No repair action required."
    if investigations:
        next_action = investigations[-1]["note"]
    elif transitions["newly_failed"]:
        next_action = "Complete the pending failure investigations."
    elif transitions["newly_passed"] and not distillations:
        next_action = "Complete the pending skill distillations."
    lines = [
        "Nightly eval compounding",
        f"Run ID: {state['run_id']}",
        f"Result: {passed}/{len(results)} passed; {len(results) - passed} failed",
        f"Verification: {state.get('verification_status', 'pending')}",
        "Transitions: "
        f"newly passed={len(transitions['newly_passed'])}, "
        f"newly failed={len(transitions['newly_failed'])}, "
        f"added={len(transitions['added'])}, removed={len(transitions['removed'])}",
        f"Distillations: {len(distillations)}",
        f"Investigations: {len(investigations)}",
        f"Next action: {next_action}",
    ]
    return "\n".join(lines)


def write_state_doc(path, state):
    section = build_state_section(state)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    else:
        existing = "# System State\n\n"
    if STATE_START in existing and STATE_END in existing:
        before, remainder = existing.split(STATE_START, 1)
        _, after = remainder.split(STATE_END, 1)
        content = before.rstrip() + "\n\n" + section + after.lstrip("\n")
    else:
        content = existing.rstrip() + "\n\n" + section
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def persist(config, state):
    write_json(config["state_path"], state)
    config["digest_path"].parent.mkdir(parents=True, exist_ok=True)
    config["digest_path"].write_text(build_digest(state), encoding="utf-8")
    write_state_doc(config["state_doc_path"], state)


def execute_run(config_path=DEFAULT_CONFIG, root=REPO_ROOT):
    config = load_config(Path(config_path), root)
    previous = load_json(config["state_path"], default={})
    results = [run_test(test, root) for test in config["tests"]]
    transitions = classify_transitions(previous, results)
    completed_at = utc_now()
    history = list(previous.get("history", []))
    history.append(
        {
            "run_id": completed_at,
            "completed_at": completed_at,
            "statuses": {item["id"]: item["status"] for item in results},
            "transitions": transitions,
        }
    )
    history = history[-int(config.get("history_limit", 30)) :]
    state = {
        "schema_version": int(config.get("schema_version", 1)),
        "run_id": completed_at,
        "completed_at": completed_at,
        "results": results,
        "transitions": transitions,
        "distillations": [],
        "investigations": [],
        "verification_status": "pending",
        "delivery": {
            "status": "pending",
            "target": config["delivery"]["target"],
            "destination": config["delivery"]["destination"],
            "mode": config["delivery"]["mode"],
        },
        "history": history,
    }
    persist(config, state)
    return state


def verify_run(config_path=DEFAULT_CONFIG, root=REPO_ROOT):
    config = load_config(Path(config_path), root)
    state = load_json(config["state_path"])
    if not state:
        raise ValueError("No eval state exists; run the suite first")
    verification_results = [run_test(test, root) for test in config["tests"]]
    expected = {item["id"]: item["status"] for item in state["results"]}
    observed = {item["id"]: item["status"] for item in verification_results}
    state["verification_status"] = "passed" if observed == expected else "mismatch"
    state["verified_at"] = utc_now()
    state["verification_results"] = verification_results
    persist(config, state)
    return state


def update_state(config_path, root, mutator):
    config = load_config(Path(config_path), root)
    state = load_json(config["state_path"])
    if not state:
        raise ValueError("No eval state exists; run the suite first")
    mutator(state)
    persist(config, state)
    return state


def mark_delivery_posted(state, receipt):
    validate_delivery_ready(state)
    prior = state.get("delivery", {})
    state["delivery"] = {
        "status": "posted",
        "posted_at": utc_now(),
        "target": prior.get("target", "engineering_digest"),
        "destination": prior.get("destination", ""),
        "mode": prior.get("mode", "external_append"),
        "receipt": receipt,
    }


def validate_delivery_ready(state):
    if state.get("verification_status") != "passed":
        raise ValueError("Cannot mark delivery before independent verification passes")
    distilled = {item["test_id"] for item in state.get("distillations", [])}
    investigated = {item["test_id"] for item in state.get("investigations", [])}
    missing_distillations = set(state["transitions"]["newly_passed"]) - distilled
    missing_investigations = set(state["transitions"]["newly_failed"]) - investigated
    if missing_distillations:
        raise ValueError(f"Missing distillations: {sorted(missing_distillations)}")
    if missing_investigations:
        raise ValueError(f"Missing investigations: {sorted(missing_investigations)}")


def post_telegram_delivery(state, token, chat_id, urlopen=urllib.request.urlopen):
    validate_delivery_ready(state)
    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    text = build_telegram_digest(state)
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        raise ValueError("Telegram digest exceeds the 4096-character message limit")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15, context=telegram_ssl_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram delivery failed: {exc}") from exc
    message_id = (payload.get("result") or {}).get("message_id")
    if payload.get("ok") is not True or message_id is None:
        description = payload.get("description") or "response lacked a message_id"
        raise RuntimeError(f"Telegram delivery failed: {description}")
    receipt = f"telegram-message:{message_id}"
    mark_delivery_posted(state, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--verification", action="store_true")
    parser.add_argument("--post-delivery", action="store_true")
    parser.add_argument("--mark-delivery-posted", metavar="RECEIPT")
    parser.add_argument("--mark-delivery-blocked", metavar="REASON")
    parser.add_argument("--record-distillation", metavar="TEST_ID")
    parser.add_argument("--distillation-note", default="")
    parser.add_argument("--record-investigation", metavar="TEST_ID")
    parser.add_argument("--investigation-note", default="")
    args = parser.parse_args()

    posted_receipt = args.mark_delivery_posted
    blocked_reason = args.mark_delivery_blocked
    if args.post_delivery:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

        def post_delivery(state):
            post_telegram_delivery(state, token, chat_id)

        state = update_state(args.config, REPO_ROOT, post_delivery)
    elif posted_receipt:
        def mark_delivery(state):
            mark_delivery_posted(state, posted_receipt)

        state = update_state(args.config, REPO_ROOT, mark_delivery)
    elif blocked_reason:
        def mark_blocked(state):
            prior = state.get("delivery", {})
            state["delivery"] = {
                "status": "blocked",
                "recorded_at": utc_now(),
                "target": prior.get("target", "engineering_digest"),
                "destination": prior.get("destination", ""),
                "mode": prior.get("mode", "external_append"),
                "reason": blocked_reason,
            }

        state = update_state(args.config, REPO_ROOT, mark_blocked)
    elif args.record_distillation:
        def record_distillation(state):
            if args.record_distillation not in state["transitions"]["newly_passed"]:
                raise ValueError("Distillations may only be recorded for newly passing tests")
            result = next(
                item for item in state["results"] if item["id"] == args.record_distillation
            )
            state.setdefault("distillations", []).append(
                {
                    "test_id": args.record_distillation,
                    "skill_path": result["owner_skill"],
                    "note": args.distillation_note or "Verified and distilled.",
                    "recorded_at": utc_now(),
                }
            )

        state = update_state(args.config, REPO_ROOT, record_distillation)
    elif args.record_investigation:
        def record_investigation(state):
            if args.record_investigation not in state["transitions"]["newly_failed"]:
                raise ValueError("Investigations may only be recorded for newly failing tests")
            state.setdefault("investigations", []).append(
                {
                    "test_id": args.record_investigation,
                    "note": args.investigation_note or "Failure reproduced; repair pending.",
                    "recorded_at": utc_now(),
                }
            )

        state = update_state(args.config, REPO_ROOT, record_investigation)
    elif args.verification:
        state = verify_run(args.config, REPO_ROOT)
    else:
        state = execute_run(args.config, REPO_ROOT)

    print(json.dumps(state, indent=2, sort_keys=True))
    tests_pass = all(item["status"] == "pass" for item in state["results"])
    verification_pass = state.get("verification_status") != "mismatch"
    return 0 if tests_pass and verification_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
