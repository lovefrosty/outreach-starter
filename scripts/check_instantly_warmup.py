#!/usr/bin/env python3
"""
check_instantly_warmup.py - read-only Instantly warmup status check.

What this program does
----------------------
Outreach needs a safe way to answer "how long until warmup is done?" without
printing API keys or guessing from memory. This script reads the Instantly API
key from the normal server environment file, fetches account metadata and
warmup analytics, then estimates remaining warmup days.

The estimate uses Instantly's current public guidance:
- standard connected accounts: at least 14 days of warmup;
- managed/DFY/AirMail-style accounts: about 21 days of warmup.

Main functions
--------------
- `load_env(path)`: parse a simple KEY=VALUE env file.
- `api_json(method, url, key, body)`: make a JSON Instantly API request.
- `summarize_account(account, analytics)`: convert API fields into a safe row.

Program entrypoint
------------------
Run on the VPS:
`python3 scripts/check_instantly_warmup.py`
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path


DEFAULT_ENV = Path.home() / ".outreach" / "secrets" / "instantly.env"
ACCOUNTS_URL = "https://api.instantly.ai/api/v2/accounts"
WARMUP_ANALYTICS_URL = "https://api.instantly.ai/api/v2/accounts/warmup-analytics"


def load_env(path):
    """Load KEY=VALUE pairs from a secrets file without printing values."""
    values = {}
    path = Path(path)
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def api_json(method, url, key, body=None):
    """Call Instantly and return `(status_code, decoded_json)`."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            payload = {"error": str(exc)}
        return exc.code, payload


def parse_dt(value):
    """Parse an Instantly timestamp to aware UTC datetime when possible."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def warmup_activity_dates(email, analytics):
    """Return dates where Instantly reports warmup sent or received activity."""
    date_data = analytics.get("email_date_data", {}) if isinstance(analytics, dict) else {}
    per_email = date_data.get(email, {}) if isinstance(date_data, dict) else {}
    return sorted(
        day for day, values in per_email.items()
        if (values or {}).get("sent", 0) or (values or {}).get("received", 0)
    )


def summarize_account(account, analytics):
    """
    Build one safe account summary row.

    `warmup_status` is Instantly's read-only status field. The script does not
    assume completion only from that field; it also checks actual warmup
    activity and account age.
    """
    email = account.get("email", "")
    now = datetime.now(timezone.utc)
    created = account.get("timestamp_created") or account.get("created_at") or ""
    created_dt = parse_dt(created)
    account_age_days = (now - created_dt).days if created_dt else None
    dates = warmup_activity_dates(email, analytics)
    first_warmup = dates[0] if dates else None
    last_warmup = dates[-1] if dates else None
    warmup_activity_days = None
    if first_warmup:
        try:
            warmup_activity_days = (date.today() - date.fromisoformat(first_warmup)).days + 1
        except ValueError:
            warmup_activity_days = None

    aggregate = analytics.get("aggregate_data", {}) if isinstance(analytics, dict) else {}
    totals = aggregate.get(email, {}) if isinstance(aggregate, dict) else {}
    is_managed = bool(account.get("is_managed_account"))
    recommended_days = 21 if is_managed else 14
    basis_days = warmup_activity_days if warmup_activity_days is not None else account_age_days
    days_left = None if basis_days is None else max(0, recommended_days - basis_days)
    warmup = account.get("warmup") or {}
    return {
        "email": email,
        "warmup_status": account.get("warmup_status"),
        "account_status": account.get("status"),
        "setup_pending": account.get("setup_pending"),
        "managed_or_dfy": is_managed,
        "account_age_days": account_age_days,
        "first_warmup_activity": first_warmup,
        "last_warmup_activity": last_warmup,
        "warmup_activity_days": warmup_activity_days,
        "recommended_warmup_days": recommended_days,
        "estimated_days_left": days_left,
        "warmup_limit": warmup.get("limit"),
        "warmup_sent_total": totals.get("sent"),
        "warmup_received_total": totals.get("received"),
        "landed_inbox": totals.get("landed_inbox"),
        "landed_spam": totals.get("landed_spam"),
        "health_score": totals.get("health_score"),
        "health_score_label": totals.get("health_score_label"),
    }


def main():
    """CLI entrypoint for a safe warmup summary."""
    parser = argparse.ArgumentParser(description="Read-only Instantly warmup check")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--json", action="store_true", help="print JSON instead of compact text")
    args = parser.parse_args()

    env = load_env(args.env_file)
    key = env.get("INSTANTLY_API_KEY") or os.environ.get("INSTANTLY_API_KEY", "")
    if not key:
        print("error=INSTANTLY_API_KEY not found")
        return 1

    status, accounts_payload = api_json("GET", ACCOUNTS_URL, key)
    if status != 200:
        print(json.dumps({"accounts_status": status, "payload": accounts_payload}, indent=2))
        return 1

    accounts = accounts_payload.get("items", [])
    emails = [account.get("email") for account in accounts if account.get("email")]
    analytics = {}
    if emails:
        status, analytics_payload = api_json(
            "POST",
            WARMUP_ANALYTICS_URL,
            key,
            {"emails": emails[:100]},
        )
        if status == 200:
            analytics = analytics_payload
        else:
            analytics = {"error_status": status, "payload": analytics_payload}

    rows = [summarize_account(account, analytics) for account in accounts]
    result = {"account_count": len(rows), "accounts": rows}
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"account_count={len(rows)}")
    for row in rows:
        print(
            " | ".join([
                f"email={row['email']}",
                f"warmup_status={row['warmup_status']}",
                f"activity_days={row['warmup_activity_days']}",
                f"recommended_days={row['recommended_warmup_days']}",
                f"estimated_days_left={row['estimated_days_left']}",
                f"health={row['health_score_label'] or row['health_score']}",
                f"sent={row['warmup_sent_total']}",
                f"received={row['warmup_received_total']}",
            ])
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
