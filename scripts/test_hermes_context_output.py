#!/usr/bin/env python3
"""No-send Outreach context/output smoke test."""

import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/outreach/.outreach/scripts")
sys.path.insert(0, "workspace/scripts")
sys.path.insert(0, "workspace/pipeline")

import outreach_bot  # noqa: E402
import shared_context  # noqa: E402


def main():
    ctx = outreach_bot.load_context_pack()
    with tempfile.TemporaryDirectory() as td:
        context_path = Path(td) / "context.json"
        doc = shared_context.blank_context("2026-06-22T12:00:00Z")
        doc["research_signals"] = [{
            "observed": {
                "public_text_excerpt": "RAW SOCIAL SCRAPER DUMP SHOULD NOT APPEAR",
                "detected_intents": ["hiring"],
            },
            "inferred": ["unsafe hypothesis should not appear"],
        }]
        doc["handoff_summary"] = {
            "summary": "Safe compact research handoff.",
            "safe_items": ["Demo Co: hiring"],
            "updated_at": "2026-06-22T12:00:00Z",
        }
        shared_context.atomic_write(context_path, doc)
        old_shared = os.environ.get("OUTREACH_SHARED_CONTEXT")
        os.environ["OUTREACH_SHARED_CONTEXT"] = str(context_path)
        try:
            safe_shared = outreach_bot.load_operator_shared_context()
        finally:
            if old_shared is None:
                os.environ.pop("OUTREACH_SHARED_CONTEXT", None)
            else:
                os.environ["OUTREACH_SHARED_CONTEXT"] = old_shared
    checks = [
        ("context_has_soul", "pragmatic, proactive senior collaborator" in ctx),
        ("context_has_slack_retired", "fully retired" in ctx),
        ("context_has_router", "Task Router" in ctx),
        ("context_has_scale_priority", "safe outbound scale" in ctx),
        ("shared_context_has_safe_handoff", "Safe compact research handoff." in safe_shared),
        ("shared_context_hides_raw_research", "RAW SOCIAL SCRAPER DUMP" not in safe_shared),
        ("shared_context_hides_unsafe_hypothesis", "unsafe hypothesis" not in safe_shared),
        (
            "next_action_personalized",
            outreach_bot._pipeline_next_best_action({"personalized": 2}, 0)
            == "review personalized drafts with /review so approved sends can move.",
        ),
        (
            "next_action_due_calls",
            outreach_bot._pipeline_next_best_action({"personalized": 2}, 1).startswith(
                "handle due calls first"
            ),
        ),
        (
            "intent_fill_review_queue",
            outreach_bot.build_structured_action("Run personalized on the first 20 enriched leads now")["action"]
            == "fill_review_queue",
        ),
        (
            "intent_fill_review_queue_limit",
            outreach_bot.build_structured_action("Run personalized on the first 20 enriched leads now")["limit"]
            == 20,
        ),
        (
            "intent_get_review_cards",
            outreach_bot.build_structured_action("get 20 review cards ready")["action"]
            == "fill_review_queue",
        ),
        (
            "intent_personalized_to_read",
            outreach_bot.build_structured_action("I just want the leads to get personalized so I can read them")["action"]
            == "fill_review_queue",
        ),
        (
            "intent_yes_confirmation",
            outreach_bot.detect_action("Yes") == ("confirm_pending", "Yes"),
        ),
        (
            "intent_increase_supply",
            outreach_bot.build_structured_action("increase email supply")["action"]
            == "supply_autopilot",
        ),
        (
            "intent_enrichment_quality",
            outreach_bot.build_structured_action("why is enrichment quality low?")["action"]
            == "enrichment_quality",
        ),
        (
            "intent_no_queue_bottleneck",
            outreach_bot.build_structured_action("why is there no queue?")["action"]
            == "pipeline_bottleneck",
        ),
        (
            "intent_protected_send",
            outreach_bot.build_structured_action("turn sending on")["requires_confirmation"]
            is True,
        ),
        (
            "intent_more_confirmation",
            outreach_bot.detect_action("more") == ("confirm_pending", "more"),
        ),
    ]
    failed = False
    for name, ok in checks:
        print(f"{name}= {ok}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
