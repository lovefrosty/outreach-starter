#!/usr/bin/env python3
"""Research-only social intent lane.

Normalizes public observations and appends them to shared_context.research_signals.
It deliberately has no CRM, Telegram, queue, send, or approval integration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(PIPELINE_DIR / "sources"))

import shared_context  # noqa: E402
from social_intent_adapter import SocialIntentAdapter, SocialIntentError  # noqa: E402


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SocialIntentError(f"input file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SocialIntentError(f"invalid JSON input: {exc}") from exc


def run(input_path, context_path=None):
    payload = _read_json(input_path)
    rows = payload.get("signals") if isinstance(payload, dict) else payload
    adapter = SocialIntentAdapter()
    signals = adapter.fetch({"signals": rows})
    shared_context.append_research_signals(
        context_path,
        signals,
        writer="social_intent",
        source_path=str(Path(input_path).resolve()),
    )
    handoff = {
        "summary": f"{len(signals)} public social/web research signal(s) captured. Research-only; no outreach actions changed.",
        "safe_items": [
            f"{signal.get('company') or 'Unknown'}: {', '.join(signal.get('observed', {}).get('detected_intents') or ['context_only'])}"
            for signal in signals[:5]
        ],
        "updated_at": shared_context.now_utc(),
    }
    shared_context.write_section(
        context_path,
        "handoff_summary",
        handoff,
        writer="social_intent",
        source_path=str(Path(input_path).resolve()),
        evidence_refs=[ref for signal in signals for ref in signal.get("evidence_refs", [])],
    )
    return {"signals_written": len(signals), "context_path": str(shared_context.context_path(context_path))}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="JSON list or {'signals': [...]} of public observations")
    parser.add_argument("--context", default="", help="shared context path; defaults to OUTREACH_SHARED_CONTEXT or VPS path")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run(args.input, args.context or None)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (SocialIntentError, shared_context.SharedContextError, shared_context.SharedContextPermissionError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
