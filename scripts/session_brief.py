#!/usr/bin/env python3
"""
session_brief.py - compact Outreach collaborator brief.

What this program does
----------------------
Outreach should feel less like a command bot and more like an operating partner.
This script creates a short session brief for the current work mode: review,
calls, pipeline, cleanup, or general. It reads SQLite facts, interprets the
bottleneck, and asks one useful question instead of dumping a dense status file.

Main functions
--------------
- `snapshot()`: collect compact live facts from SQLite and environment flags.
- `analyst_summary(data, mode)`: explain bottleneck, quality, and risk.
- `tool_summary(data, mode)`: show what deterministic tools/subagents can use.
- `proactive_question(data, mode, state)`: ask one useful workflow question.
- `infer_mode(...)`: choose the likely operator workflow if none is supplied.
- `build_brief(...)`: render the short analyst-style session brief.

Program entrypoint
------------------
Running `python3 workspace/scripts/session_brief.py` calls `main()`. Use
`--mode review|calls|pipeline|cleanup|general` to bias the brief.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_PIPE = _SCRIPTS.parent / "pipeline"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_PIPE))

import lead_store as ls  # noqa: E402
import ledger as L  # noqa: E402
import session_state  # noqa: E402
from ds import qualification  # noqa: E402


MODE_ALIASES = {
    "review": "review",
    "approve": "review",
    "approval": "review",
    "calls": "calls",
    "call": "calls",
    "pipeline": "pipeline",
    "enrich": "pipeline",
    "cleanup": "cleanup",
    "archive": "cleanup",
    "general": "general",
}


def _rows(conn, query, params=()):
    """Run a small read-only query and return sqlite Row objects."""
    return conn.execute(query, params).fetchall()


def snapshot():
    """
    Collect only the facts the session brief needs.

    This intentionally avoids loading full lead cards or long notes. The goal
    is a compact collaborator prompt, not another dense context pack.
    """
    conn = L.connect()
    try:
        counts = L.count_by_stage(conn)
        due_calls = ls.scheduled_calls(due_only=True, limit=8)
        personalized = _rows(
            conn,
            """
            SELECT id, company, vertical, city_state, pain_tier, pain_theme,
                   email, phone, owner_name, contact_name, processor,
                   tech_signals, review_count, locations, filing_date,
                   switch_window_score, propensity, route, template_key
            FROM leads
            WHERE stage='personalized'
            ORDER BY COALESCE(propensity,0) DESC, COALESCE(call_priority,0) DESC, id ASC
            LIMIT 5
            """,
        )
        queued = _rows(
            conn,
            """
            SELECT id, company, vertical, city_state, email, sequence_key
            FROM leads
            WHERE stage='queued'
            ORDER BY id ASC
            LIMIT 5
            """,
        )
        call_list = _rows(
            conn,
            """
            SELECT id, company, vertical, city_state, email, phone,
                   owner_name, contact_name, processor, tech_signals,
                   review_count, locations, filing_date, switch_window_score,
                   pain_tier, pain_theme, call_priority
            FROM leads
            WHERE stage='call_list'
            ORDER BY COALESCE(call_priority,0) DESC, id ASC
            LIMIT 5
            """,
        )
        stage_quality = _rows(
            conn,
            """
            SELECT
              SUM(CASE WHEN stage='personalized' AND email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) email_ready,
              SUM(CASE WHEN stage='personalized' AND (email IS NULL OR email='') THEN 1 ELSE 0 END) review_without_email,
              SUM(CASE WHEN stage='personalized' AND COALESCE(pain_tier,'')='HOT' THEN 1 ELSE 0 END) hot_review,
              SUM(CASE WHEN stage='personalized' AND COALESCE(pain_tier,'')='WARM' THEN 1 ELSE 0 END) warm_review
            FROM leads
            """
        )[0]
        state = session_state.get_state(conn)
        stuck = {
            "pulled": counts.get("pulled", 0),
            "scraped": counts.get("scraped", 0),
            "analyzed": counts.get("analyzed", 0),
            "verified": counts.get("verified", 0),
        }
        return {
            "counts": counts,
            "due_calls": due_calls,
            "personalized": personalized,
            "queued": queued,
            "call_list": call_list,
            "quality": dict(stage_quality),
            "stuck": stuck,
            "sending_enabled": os.environ.get("PIPELINE_SENDING_ENABLED", "").strip() == "1",
            "session_state": state,
        }
    finally:
        conn.close()


def infer_mode(data, requested="general"):
    """Pick the most useful workflow mode when the operator did not specify one."""
    requested = MODE_ALIASES.get((requested or "general").lower(), "general")
    if requested != "general":
        return requested
    state_mode = session_state.normalize_mode((data.get("session_state") or {}).get("mode"))
    if state_mode != "general":
        return state_mode
    if data["due_calls"]:
        return "calls"
    if data["personalized"]:
        return "review"
    if any(data["stuck"].values()):
        return "pipeline"
    if data["counts"].get("queued", 0):
        return "review"
    return "general"


def _stage_line(counts):
    """Format the pipeline stages that matter for the brief."""
    order = ("pulled", "scraped", "analyzed", "verified", "personalized", "queued", "sent", "call_list")
    parts = [f"{stage} {counts.get(stage, 0)}" for stage in order if counts.get(stage, 0)]
    return ", ".join(parts) if parts else "empty"


def _lead_labels(rows):
    """Render a compact preview of lead names without full cards or notes."""
    labels = []
    for row in rows:
        vertical = row["vertical"] or "unknown"
        city = row["city_state"] or "unknown market"
        labels.append(f"{row['company']} ({vertical}, {city})")
    return "; ".join(labels)


def bottleneck_summary(data):
    """
    Explain where the workflow is currently constrained.

    This stays deterministic and compact: no LLM and no long historical context.
    """
    counts = data["counts"]
    stuck = data["stuck"]
    if data["due_calls"]:
        return "Bottleneck: due calls are closest to revenue, so they outrank new sourcing."
    if counts.get("personalized", 0):
        return "Bottleneck: review capacity. There are drafts ready for human judgment."
    if stuck.get("verified", 0):
        return "Bottleneck: template routing. Verified leads need to become review cards."
    if stuck.get("analyzed", 0):
        return "Bottleneck: email routing. Analyzed leads need a reachable contact or call route."
    if stuck.get("scraped", 0):
        return "Bottleneck: scoring. Scraped leads need C3 analysis before outreach decisions."
    if stuck.get("pulled", 0):
        return "Bottleneck: scraping. Raw pulled leads need website facts before more volume helps."
    if counts.get("queued", 0) and not data["sending_enabled"]:
        return "Bottleneck: send gate. Approved leads are queued but sending is off."
    return "Bottleneck: no active constraint is obvious from compact counts."


def quality_summary(data):
    """Summarize review queue quality without loading full lead cards."""
    q = data.get("quality") or {}
    hot = q.get("hot_review") or 0
    warm = q.get("warm_review") or 0
    email_ready = q.get("email_ready") or 0
    review_without_email = q.get("review_without_email") or 0
    if email_ready or review_without_email:
        return f"Quality: {email_ready} review rows have email; {hot} HOT and {warm} WARM; {review_without_email} need call-first handling."
    if data["call_list"]:
        return "Quality: email supply is thin; best available work is likely call-first."
    return "Quality: no reviewable email set is available yet."


def qualification_summary(data):
    """
    Summarize what the system knows about statement-review readiness.

    This uses the deterministic qualification module against the compact sample
    rows already loaded by `snapshot()`. It does not perform new scraping.
    """
    rows = list(data.get("personalized") or []) + list(data.get("call_list") or [])
    if not rows:
        return "Qualification: no sampled leads available for statement-review routing."
    next_steps = {}
    for row in rows:
        result = qualification.qualify(row)
        step = result.get("next_step", "research_more")
        next_steps[step] = next_steps.get(step, 0) + 1
    ordered = ", ".join(f"{key} {value}" for key, value in sorted(next_steps.items()))
    return f"Qualification: sampled next steps -> {ordered}."


def risk_summary(data):
    """Surface the main operational risk for the current session."""
    counts = data["counts"]
    if counts.get("queued", 0) and not data["sending_enabled"]:
        return f"Risk: {counts.get('queued', 0)} queued approvals will not send while the send gate is off."
    if counts.get("personalized", 0) == 0 and any(data["stuck"].values()):
        return "Risk: review stays empty unless the no-send pipeline runs."
    if counts.get("call_list", 0) >= 25:
        return "Risk: call-list volume can hide high-fit leads unless calls are prioritized."
    return "Risk: no immediate safety issue from compact checks."


def analyst_summary(data, mode):
    """
    Return three short analyst lines: bottleneck, quality, and risk.

    These are the reusable analyst summary functions Outreach can call in briefs,
    heartbeats, and future proactive messages without loading dense context.
    """
    return [
        bottleneck_summary(data),
        quality_summary(data),
        qualification_summary(data),
        risk_summary(data),
    ]


def tool_summary(data, mode):
    """
    Summarize the concrete tools available for the current workflow.

    This answers "what can the subagent use?" without loading a large tool
    registry. The list is intentionally operational: each item names the local
    script or command surface that can be invoked for this session.
    """
    counts = data["counts"]
    tools = []
    if mode == "pipeline" or any(data["stuck"].values()):
        tools.append("orchestrator.py for no-send stage movement")
        tools.append("C2 scraper + C3 analyzer for website facts and scoring")
    if mode == "review" or counts.get("personalized", 0):
        tools.append("/review and /drafts for approval-card QA")
    if mode == "calls" or data["due_calls"] or counts.get("call_list", 0):
        tools.append("/calls plus SQLite call_due_at/call_priority")
    if mode == "cleanup":
        tools.append("archive_stale_leads.py dry-run before cleanup")
    if mode == "sending" or counts.get("queued", 0):
        tools.append("/sending, /readiness, and C7 sender gate checks")
    if mode == "research":
        tools.append("/research plus C2/C3 evidence and lead_store lookup")
    if not tools:
        tools.append("/brief, /pipeline, and heartbeat_health.py for orientation")
    return "Tools: " + "; ".join(tools[:4]) + "."


def proactive_question(data, mode, state):
    """
    Generate one context-aware question that helps choose the next workflow.

    The question depends on the current session mode and facts. It is designed
    to make Outreach collaborative while still keeping control with the operator.
    """
    counts = data["counts"]
    objective = (state or {}).get("objective") or ""
    if mode == "calls":
        return "Question: do you want call notes for the due leads, or should I first surface the highest-priority call-list targets?"
    if mode == "review":
        return "Question: should I optimize for more approved sends, or be stricter and push weak/generic emails to calls?"
    if mode == "pipeline":
        if counts.get("personalized", 0):
            return "Question: review cards are available now. Should I pause pipeline movement and switch you to approvals?"
        return "Question: should I run the no-send pipeline toward review cards, or inspect why enrichment quality is thin first?"
    if mode == "cleanup":
        return "Question: should I dry-run archive stale low-action leads while preserving company, location, and email?"
    if objective:
        return f"Question: are we still optimizing for '{objective}', or should I switch modes?"
    return "Question: do you want to review emails, make calls, move the pipeline, or clean stale data?"


def build_brief(mode="general"):
    """
    Build a short analyst-style session brief.

    The final line is always a question. That is the collaboration hook: Outreach
    should help the operator choose the next workflow, not just report counts.
    """
    data = snapshot()
    mode = infer_mode(data, mode)
    state = data.get("session_state") or {}
    counts = data["counts"]
    lines = [f"Session brief: {mode}", f"Pipeline: {_stage_line(counts)}"]
    objective = (state.get("objective") or "").strip()
    if objective:
        lines.append(f"Objective: {objective}")
    lines.extend(analyst_summary(data, mode))
    lines.append(tool_summary(data, mode))

    if mode == "calls":
        lines.append(f"Calls due now: {len(data['due_calls'])}")
        if data["due_calls"]:
            lines.append("First call targets: " + _lead_labels(data["due_calls"][:3]))
        lines.append(proactive_question(data, mode, state))
        return "\n".join(lines)

    if mode == "review":
        lines.append(f"Review-ready leads: {len(data['personalized'])}")
        if data["personalized"]:
            lines.append("Best review targets: " + _lead_labels(data["personalized"][:3]))
        if counts.get("queued", 0):
            gate = "on" if data["sending_enabled"] else "off"
            lines.append(f"Queued approvals: {counts.get('queued', 0)}; send gate is {gate}.")
        lines.append(proactive_question(data, mode, state))
        return "\n".join(lines)

    if mode == "pipeline":
        stuck = ", ".join(f"{k} {v}" for k, v in data["stuck"].items() if v) or "none"
        lines.append(f"Pipeline work waiting: {stuck}")
        lines.append(proactive_question(data, mode, state))
        return "\n".join(lines)

    if mode == "cleanup":
        inactive = counts.get("call_list", 0) + counts.get("skipped", 0) + counts.get("dead", 0)
        lines.append(f"Cleanup candidates to inspect: {inactive}")
        lines.append(proactive_question(data, mode, state))
        return "\n".join(lines)

    lines.append("No urgent workflow is obvious from the compact counts.")
    lines.append(proactive_question(data, mode, state))
    return "\n".join(lines)


def main():
    """CLI entrypoint used by Outreach and by manual local checks."""
    ap = argparse.ArgumentParser(description="Print a compact Outreach session brief")
    ap.add_argument("--mode", default="general", choices=sorted(set(MODE_ALIASES.values())))
    args = ap.parse_args()
    print(build_brief(args.mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
