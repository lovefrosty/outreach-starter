#!/usr/bin/env python3
"""Send plain-language operator reminder nudges over Telegram DM.

This script is intentionally narrow:
- It only sends outbound reminders.
- It prefers a remembered private Telegram chat id.
- It does not touch CRM/send state.
- It does not require slash commands or button flows.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import heartbeat_health
except Exception:
    heartbeat_health = None  # type: ignore[assignment]
try:
    import session_brief
except Exception:
    session_brief = None  # type: ignore[assignment]


ROOT = Path(os.environ.get("OUTREACH_ROOT", Path.home() / ".outreach")).resolve()
OPERATOR_CHATS_PATH = ROOT / "state/operator_chats.json"
LOG_PATH = ROOT / "state/operator_nudges.jsonl"
MESSAGE_LIMIT = 4096


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def cert_context():
    try:
        import certifi
    except Exception:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def remembered_operator_chat_id(path=OPERATOR_CHATS_PATH):
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return ""
    latest = str(data.get("latest_private_chat_id") or "").strip()
    if latest:
        return latest
    operators = data.get("operators") or {}
    newest_chat_id = ""
    newest_updated_at = ""
    for record in operators.values():
        if not isinstance(record, dict):
            continue
        chat_id = str(record.get("chat_id") or "").strip()
        updated_at = str(record.get("updated_at") or "")
        if chat_id and updated_at >= newest_updated_at:
            newest_chat_id = chat_id
            newest_updated_at = updated_at
    return newest_chat_id


def delivery_chat_id():
    return (
        os.environ.get("TELEGRAM_OPERATOR_DM_CHAT_ID", "").strip()
        or remembered_operator_chat_id()
    )


def append_log(event):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def telegram_send(token, chat_id, text):
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:MESSAGE_LIMIT],
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20, context=cert_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def social_research_message():
    return (
        "Quick check on the social research setup.\n\n"
        "Have you created the dedicated research account yet, or is something still blocking it?\n\n"
        "Reply normally. For example: done, blocked by naming, remind me tomorrow at 8, or "
        "I created it and followed 12 pages."
    )


def active_update_message(mode="general"):
    health = (
        heartbeat_health.format_text(heartbeat_health.snapshot())
        if heartbeat_health is not None
        else "Outreach health is unavailable in this runtime."
    )
    brief = (
        session_brief.build_brief(mode)
        if session_brief is not None
        else "Session brief is unavailable in this runtime."
    )
    return "\n\n".join([
        "Outreach proactive update",
        health,
        brief,
        "Reply normally, or use /review, /calls, /pipeline, /sending, or /brief.",
    ])[:MESSAGE_LIMIT]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--message",
        default="social_research_setup",
        help="Named built-in message or literal body when used with --literal",
    )
    parser.add_argument(
        "--literal",
        action="store_true",
        help="Treat --message as the exact text body to send.",
    )
    parser.add_argument(
        "--mode",
        default="general",
        help="Workflow mode used when building the active_update brief.",
    )
    return parser.parse_args(argv)


def resolve_message(args):
    if args.literal:
        return args.message.strip()
    if args.message == "social_research_setup":
        return social_research_message()
    if args.message == "active_update":
        return active_update_message(args.mode)
    raise SystemExit(f"unsupported message key: {args.message}")


def main(argv=None):
    args = parse_args(argv)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    chat_id = delivery_chat_id()
    if not chat_id:
        raise SystemExit(
            "No private operator chat id is available. DM the bot once so Outreach can remember your private chat."
        )

    message = resolve_message(args)
    try:
        response = telegram_send(token, chat_id, message)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        append_log({
            "type": "operator_nudge_failed",
            "sent_at": now_iso(),
            "chat_id": chat_id,
            "error": f"HTTP {exc.code}",
            "body": body[:500],
        })
        raise SystemExit(f"Telegram send failed: HTTP {exc.code}: {body[:200]}")
    except Exception as exc:
        append_log({
            "type": "operator_nudge_failed",
            "sent_at": now_iso(),
            "chat_id": chat_id,
            "error": str(exc),
        })
        raise SystemExit(f"Telegram send failed: {exc}")

    result = response.get("result") or {}
    message_id = result.get("message_id")
    append_log({
        "type": "operator_nudge_sent",
        "sent_at": now_iso(),
        "chat_id": chat_id,
        "message_id": message_id,
        "message_key": args.message if not args.literal else "literal",
    })
    print(json.dumps({
        "ok": bool(response.get("ok")),
        "chat_id": chat_id,
        "message_id": message_id,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
