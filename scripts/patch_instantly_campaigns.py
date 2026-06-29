#!/usr/bin/env python3
"""Patch paused Instantly campaigns to the approved 80-word first-touch copy."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE_URL = "https://api.instantly.ai/api/v2"
ROBOTIC_PHRASES = (
    "I've been speaking",
    "I’ve been speaking",
    "many pharmacies",
    "several high-volume",
    "10,000+ merchants",
    "as partners not customers",
    "substantially less",
)

CAMPAIGN_DEFS = {
    "restaurant_default": {
        "env": "INSTANTLY_CAMPAIGN_ID_RESTAURANT_DEFAULT",
        "body": (
            "Your team in {{CityState}} has ordering, payments, rewards, and reporting "
            "touching speed and margin every day. When those workflows sit apart, fees "
            "are harder to compare and staff loses time. {{EmailAngle}} Open to a free "
            "savings quote and short demo?"
        ),
    },
    "pharmacy_default": {
        "env": "INSTANTLY_CAMPAIGN_ID_PHARMACY_DEFAULT",
        "body": (
            "Your pharmacy in {{CityState}} has checkout, inventory, patient data, and "
            "payments competing for attention at the counter. When those systems sit "
            "apart, fees are harder to compare and reporting takes longer. "
            "{{EmailAngle}} Open to a free savings quote and workflow demo?"
        ),
    },
    "dealership_default": {
        "env": "INSTANTLY_CAMPAIGN_ID_DEALERSHIP_DEFAULT",
        "body": (
            "Your dealership in {{CityState}} has customers, ROs, invoices, approvals, "
            "and payments moving through the same day. When collections and approvals "
            "sit in separate tools, closeouts slow down and reconciliation gets messy. "
            "{{EmailAngle}} Open to a free savings quote and demo?"
        ),
    },
}


def word_count(text):
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", text or ""))


def body_text(core):
    return f"Hi {{{{first_name}}}},\n\n{core}\n\nGabriella\nGreen PayTech"


def body_policy_errors(text):
    errors = []
    countable = "\n".join(
        line for line in text.splitlines()
        if line.strip()
        and not line.strip().lower().startswith("hi ")
        and line.strip().lower() not in {"gabriella", "green paytech"}
    )
    if word_count(countable) > 80:
        errors.append(f"body_plus_cta_words={word_count(countable)} > 80")
    if text.count("?") != 1:
        errors.append(f"question_count={text.count('?')}, expected 1")
    lowered = text.lower()
    for phrase in ROBOTIC_PHRASES:
        if phrase.lower() in lowered:
            errors.append(f"robotic_phrase={phrase}")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("•", "-", "*")):
            errors.append("bullet_list_detected")
    return errors


def cert_context():
    try:
        import certifi
    except Exception:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 OutreachLaunchPatch",
        "Accept": "application/json",
    }


def api(api_key, method, path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = headers(api_key)
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30, context=cert_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def backup_path(root):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / "backups" / f"instantly-campaign-patch-{ts}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def patch_campaign(api_key, label, config, daily_limit, backup_dir, dry_run=False):
    campaign_id = os.environ.get(config["env"], "").strip()
    if not campaign_id:
        return {"label": label, "error": f"missing {config['env']}"}

    before = api(api_key, "GET", f"/campaigns/{campaign_id}")
    (backup_dir / f"{label}.before.json").write_text(
        json.dumps(before, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    sequences = copy.deepcopy(before["sequences"])
    first_step = sequences[0]["steps"][0]
    first_step["delay"] = 0
    first_step["delay_unit"] = "days"
    first_step["pre_delay_unit"] = "days"
    if "pre_delay" in first_step:
        first_step["pre_delay"] = 0

    new_body = body_text(config["body"])
    for variant in first_step.get("variants", []):
        variant["subject"] = "Gabriella / {{company_name}} Connect"
        variant["body"] = new_body

    errors = body_policy_errors(new_body)
    if errors:
        return {"label": label, "id": campaign_id, "error": "policy_failed", "errors": errors}

    payload = {
        "sequences": sequences,
        "daily_limit": daily_limit,
        "stop_on_reply": True,
    }
    if dry_run:
        after = before
    else:
        after = api(api_key, "PATCH", f"/campaigns/{campaign_id}", payload)
        (backup_dir / f"{label}.after.json").write_text(
            json.dumps(after, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    after_first = (sequences if dry_run else after["sequences"])[0]["steps"][0]
    after_body = after_first["variants"][0]["body"]
    before_body = before["sequences"][0]["steps"][0]["variants"][0]["body"]
    return {
        "label": label,
        "id": campaign_id,
        "dry_run": dry_run,
        "status_before": before.get("status"),
        "status_after": before.get("status") if dry_run else after.get("status"),
        "daily_limit_before": before.get("daily_limit"),
        "daily_limit_after": before.get("daily_limit") if dry_run else after.get("daily_limit"),
        "first_delay_before": before["sequences"][0]["steps"][0].get("delay"),
        "first_delay_after": after_first.get("delay"),
        "old_body_had_robotic_phrase": any(phrase in before_body for phrase in ROBOTIC_PHRASES),
        "new_body_policy_errors": body_policy_errors(after_body),
        "new_body_plus_cta_words": word_count(
            " ".join(
                line for line in after_body.splitlines()
                if line.strip()
                and not line.strip().lower().startswith("hi ")
                and line.strip().lower() not in {"gabriella", "green paytech"}
            )
        ),
        "new_body": after_body,
    }


def verify_campaign(api_key, label, config):
    campaign_id = os.environ.get(config["env"], "").strip()
    if not campaign_id:
        return {"label": label, "error": f"missing {config['env']}"}
    data = api(api_key, "GET", f"/campaigns/{campaign_id}")
    first_step = data["sequences"][0]["steps"][0]
    body = first_step["variants"][0]["body"]
    countable = " ".join(
        line for line in body.splitlines()
        if line.strip()
        and not line.strip().lower().startswith("hi ")
        and line.strip().lower() not in {"gabriella", "green paytech"}
    )
    return {
        "label": label,
        "id": campaign_id,
        "status": data.get("status"),
        "daily_limit": data.get("daily_limit"),
        "first_delay": first_step.get("delay"),
        "questions": body.count("?"),
        "body_plus_cta_words": word_count(countable),
        "policy_errors": body_policy_errors(body),
        "old_phrase_present": [
            phrase for phrase in ROBOTIC_PHRASES
            if phrase.lower() in body.lower()
        ],
        "body": body,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-limit", type=int, default=15)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    api_key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("INSTANTLY_API_KEY is required")
    root = Path(os.environ.get("OUTREACH_ROOT", Path.home() / ".outreach")).resolve()
    if args.verify_only:
        results = [
            verify_campaign(api_key, label, config)
            for label, config in CAMPAIGN_DEFS.items()
        ]
        print(json.dumps({"results": results}, indent=2))
        if any(item.get("error") or item.get("policy_errors") or item.get("old_phrase_present") for item in results):
            return 1
        return 0
    backups = backup_path(root)
    results = [
        patch_campaign(api_key, label, config, args.daily_limit, backups, args.dry_run)
        for label, config in CAMPAIGN_DEFS.items()
    ]
    print(json.dumps({"backup_dir": str(backups), "results": results}, indent=2))
    if any(item.get("error") or item.get("new_body_policy_errors") for item in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
