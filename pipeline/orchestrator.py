#!/usr/bin/env python3
"""
orchestrator.py - deterministic no-send pipeline runner for Outreach.

What this program does
----------------------
Outreach stores leads in SQLite and moves them through explicit pipeline stages.
This file is the missing "keep the pipeline moving" loop. It advances leads
from raw/source stages into Telegram-reviewable email drafts, but it never
sends email. Sending still happens only after a human taps Send in Telegram.

Stage flow handled here
-----------------------
1. `pulled`   -> C2 website scraper -> `scraped`
2. `scraped`  -> C3 analyzer        -> `analyzed`
3. `analyzed` -> email route check  -> `verified` or `call_list`
4. `verified` -> template routing   -> `personalized`

Main functions
--------------
- `run_once(stage_filter=None, limit=25)`: process one batch for each no-send
  stage and return counts of moved rows.
- `health_snapshot()`: read-only heartbeat status for stage bottlenecks.
- `_process_stage(...)`: dispatch one lead row to the correct stage handler.

Program entrypoint
------------------
Running `python3 workspace/pipeline/orchestrator.py` calls `main()`, which
either prints `run_once(...)` results or a read-only `health_snapshot()`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_PIPE = Path(__file__).resolve().parent
_SCRIPTS = _PIPE.parent / "scripts"
sys.path.insert(0, str(_PIPE))
sys.path.insert(0, str(_SCRIPTS))

import ledger as L  # noqa: E402
from nodes import c2_scraper, c3_analyzer  # noqa: E402

try:
    import outreach_templates  # noqa: E402
except Exception:
    outreach_templates = None


STAGE_ORDER = ("pulled", "scraped", "analyzed", "verified")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GENERIC_LOCAL = {
    "info", "contact", "hello", "support", "admin", "sales", "noreply",
    "no-reply", "webmaster", "postmaster", "abuse", "privacy", "legal",
    "billing", "careers", "jobs", "press", "media", "team", "service",
    "help", "feedback", "office", "events", "reservations", "catering",
    "marketing", "accounting", "hr", "general",
}


FIRST_TOUCH_ROUTE_DEFAULTS = {
    "restaurant_default": {
        "email_angle": "Green PayTech can review one recent statement to show how much you could save and show how Union brings ordering, payments, and guest data into one restaurant system.",
        "template_cta": "Open to a free savings quote and short demo?",
    },
    "pharmacy_default": {
        "email_angle": "Green PayTech can review one recent statement to show how much you could save and show how ExampleProduct brings payments, inventory, signatures, and history into one pharmacy workflow.",
        "template_cta": "Open to a free savings quote and workflow demo?",
    },
    "dealership_default": {
        "email_angle": "Green PayTech can review one recent statement to show how much you could save and show how ExampleProduct ties approvals, payments, and deposits into one dealership workflow.",
        "template_cta": "Open to a free savings quote and demo?",
    },
    "general_standard": {
        "email_angle": "Green PayTech can review one recent statement to show how much you could save.",
        "template_cta": "Open to a free savings quote to see whether we can lower your expected rate?",
    },
}


def _row_value(row, key, default=""):
    """Read a SQLite Row key safely, including older DBs missing a column."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _clean_email(value):
    """Normalize an email string and reject values that are not email-shaped."""
    email = (value or "").strip().lower()
    return email if EMAIL_RE.match(email) else ""


def _is_generic(email):
    local = email.split("@", 1)[0].lower() if "@" in email else ""
    return local in GENERIC_LOCAL


def _best_candidate(conn, lead_id):
    """
    Pick the best email candidate C2 already found on the website.

    Non-generic inboxes win. If only generic inboxes exist, keep one fallback
    instead of stranding the lead, because the operator can still reject it in
    `/review`.
    """
    rows = L.candidates_for(conn, lead_id)
    fallback = None
    for row in rows:
        email = _clean_email(row["email"])
        if not email:
            continue
        if fallback is None:
            fallback = email
        if not _is_generic(email):
            return email
    return fallback or ""


def _sequence_for_vertical(vertical):
    """
    Fallback route selector when the richer outreach template library is absent.

    This keeps the pipeline operational on the server even if template-routing
    config files are not deployed yet.
    """
    v = (vertical or "").strip().lower()
    if "pharma" in v:
        return {
            "template_key": "PharmacyExampleProductA",
            "template_route": "pharmacy:cold_fit:default",
            "sequence_key": "pharmacy_default",
            **FIRST_TOUCH_ROUTE_DEFAULTS["pharmacy_default"],
        }
    if "dealer" in v or "auto" in v:
        return {
            "template_key": "ExampleProductA",
            "template_route": "dealership:cold_fit:default",
            "sequence_key": "dealership_default",
            **FIRST_TOUCH_ROUTE_DEFAULTS["dealership_default"],
        }
    if "restaurant" in v or "bar" in v or "hospitality" in v:
        return {
            "template_key": "Restaurant2",
            "template_route": "restaurant:cold_fit:default",
            "sequence_key": "restaurant_default",
            **FIRST_TOUCH_ROUTE_DEFAULTS["restaurant_default"],
        }
    return {
        "template_key": "Standard1",
        "template_route": "general:cold_fit:default",
        "sequence_key": "general_standard",
        **FIRST_TOUCH_ROUTE_DEFAULTS["general_standard"],
    }


def _usable_route_sentence(value, default, require_question=False):
    value = " ".join((value or "").strip().split())
    if require_question:
        return value if value.endswith("?") and len(value.split()) >= 5 else default
    return value if value.endswith((".", "?")) and len(value.split()) >= 6 else default


def _route_for(row):
    """
    Return template metadata for a verified lead.

    Preferred path: use `scripts/outreach_templates.py`, which encodes the
    approved vertical routing logic. Fallback path: use local vertical keywords.
    """
    if outreach_templates is not None:
        try:
            route = outreach_templates.select_route(
                _row_value(row, "vertical"),
                _row_value(row, "pain_theme"),
                _row_value(row, "filing_date"),
            )
            return {
                "template_key": route.get("template_key") or "Standard1",
                "template_route": route.get("route_key") or "",
                "sequence_key": route.get("sequence") or "general_standard",
                "email_angle": _usable_route_sentence(
                    route.get("angle"),
                    FIRST_TOUCH_ROUTE_DEFAULTS.get(
                        route.get("sequence") or "general_standard",
                        FIRST_TOUCH_ROUTE_DEFAULTS["general_standard"],
                    )["email_angle"],
                ),
                "template_cta": _usable_route_sentence(
                    route.get("cta"),
                    FIRST_TOUCH_ROUTE_DEFAULTS.get(
                        route.get("sequence") or "general_standard",
                        FIRST_TOUCH_ROUTE_DEFAULTS["general_standard"],
                    )["template_cta"],
                    require_question=True,
                ),
            }
        except Exception:
            pass
    return _sequence_for_vertical(_row_value(row, "vertical"))


def _process_analyzed(conn, row):
    """
    Convert an analyzed lead into either email route or call route.

    This is intentionally conservative: it uses an existing `leads.email` or an
    onsite candidate already found by C2. It does not spend Hunter/API credits.
    """
    lead_id = row["id"]
    email = _clean_email(_row_value(row, "email")) or _best_candidate(conn, lead_id)
    if not email:
        L.divert_to_call_list(conn, lead_id, "no_verified_email_candidate")
        return True
    L.set_fields(conn, lead_id, email=email, route="email", stage="verified")
    L.log_event(conn, "orchestrator", lead_id, "email_verified", {"source": "existing_or_onsite_candidate"})
    return True


def _process_verified(conn, row):
    """
    Attach approved template metadata and make the row visible to `/review`.

    `trigger` is intentionally blank here. The current approved copy supports
    standard-template sends without generated openers unless a later stage
    records literal evidence for a custom opener.
    """
    route = _route_for(row)
    L.set_fields(
        conn,
        row["id"],
        route="email",
        template_key=route["template_key"],
        template_route=route["template_route"],
        sequence_key=route["sequence_key"],
        email_angle=route["email_angle"],
        template_cta=route["template_cta"],
        trigger="",
        trigger_evidence="",
        trigger_source="standard_template_only",
        trigger_verified=None,
        stage="personalized",
    )
    L.log_event(conn, "orchestrator", row["id"], "draft_ready", route)
    return True


def _process_stage(conn, stage, row):
    """Dispatch one row to the implementation for its current stage."""
    if stage == "pulled":
        return c2_scraper.process(conn, row)
    if stage == "scraped":
        return c3_analyzer.process(conn, row)
    if stage == "analyzed":
        return _process_analyzed(conn, row)
    if stage == "verified":
        return _process_verified(conn, row)
    raise ValueError(f"unsupported stage: {stage}")


def run_once(stage_filter=None, limit=25):
    """Run each no-send stage once. Returns a dict of moved row counts."""
    conn = L.connect()
    totals = {}
    stages = [stage_filter] if stage_filter else STAGE_ORDER
    try:
        for stage in stages:
            # Unsupported stages are reported as zero instead of crashing
            # Telegram commands that pass an unexpected value.
            if stage not in STAGE_ORDER:
                totals[stage] = 0
                continue
            rows = L.read_by_stage(conn, stage, limit=limit)
            moved = 0
            for row in rows:
                # Stage handlers commit their own updates through ledger helpers.
                # Re-read the stage afterward so the returned count reflects
                # actual movement, not just a function returning True.
                before = _row_value(row, "stage")
                ok = _process_stage(conn, stage, row)
                if ok:
                    refreshed = conn.execute("SELECT stage FROM leads WHERE id=?", (row["id"],)).fetchone()
                    after = refreshed["stage"] if refreshed else before
                    if after != before:
                        moved += 1
            totals[stage] = moved
    finally:
        conn.close()
    return totals


def health_snapshot():
    """
    Return read-only stage health for heartbeat output.

    This function must not mutate lead rows. It exists so morning/evening
    heartbeat checks can surface stuck stages without running the pipeline.
    """
    conn = L.connect()
    try:
        counts = L.count_by_stage(conn)
        issues = []
        if counts.get("pulled", 0):
            issues.append(f"{counts['pulled']} pulled need C2 scrape")
        if counts.get("scraped", 0):
            issues.append(f"{counts['scraped']} scraped need C3 analysis")
        if counts.get("analyzed", 0):
            issues.append(f"{counts['analyzed']} analyzed need email routing")
        if counts.get("verified", 0):
            issues.append(f"{counts['verified']} verified need template routing")
        if not counts.get("personalized", 0):
            issues.append("no personalized leads ready for /review")
        if counts.get("queued", 0) and os.environ.get("PIPELINE_SENDING_ENABLED", "").strip() != "1":
            issues.append(f"{counts['queued']} queued leads held by send gate")
        return {"counts": counts, "issues": issues}
    finally:
        conn.close()


def main():
    """CLI wrapper used by local checks and server cron/manual runs."""
    ap = argparse.ArgumentParser(description="Run the Outreach deterministic pipeline once")
    ap.add_argument("--stage", choices=STAGE_ORDER)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--health", action="store_true")
    args = ap.parse_args()
    if args.health:
        print(json.dumps(health_snapshot(), indent=2, sort_keys=True))
        return 0
    print(json.dumps(run_once(stage_filter=args.stage, limit=args.limit), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
