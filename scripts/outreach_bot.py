#!/usr/bin/env python3
"""
outreach_bot.py — Outreach, the Telegram operating system for Acme outreach.

Stdlib only. Long-polling Telegram bot. The whole outreach OS lives here:
leads, calls, logging, pipeline — all from your phone.

WHAT YOU CAN SAY (DM freely; in a group, prefix /command or @mention):
  /onboarding             -> short interactive walkthrough for a new operator
  /leads                  → today's fresh leads as call cards (tap # to dial)
  /calls  /uncalled       → due post-email follow-ups, then unscheduled calls
  /status  /pipeline      → lead counts + live Instantly email stats
  show <name>             → pull a specific lead's card
  called <name> <outcome> → log a call in plain English, e.g.
        "called Leo's, no answer"
        "Komishane's interested, call back Thursday"
        "talked to Bayonne Auto, not interested"
  <anything else>         → DeepSeek answers from your playbook + live facts

ENV (secrets loaded by systemd EnvironmentFile lines):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   required
    DEEPSEEK_API_KEY                        freeform Q&A + call parsing
    INSTANTLY_API_KEY                       email stats (optional)
    OUTREACH_KNOWLEDGE                        knowledge pack path (optional)

DESIGN RULE: code fetches facts + owns the database; DeepSeek only phrases
and parses language into structured updates. It never invents a number.
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ── pipeline layer (ledger lives one level up in pipeline/) ───────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
try:
    import ledger as L
except Exception as _le:
    L = None  # type: ignore[assignment]
    print(f"[outreach] ledger not importable: {_le}", file=sys.stderr, flush=True)

try:
    import daily_brief as db
except Exception:
    db = None
try:
    import orchestrator
except Exception as _orch:
    orchestrator = None  # type: ignore[assignment]
    print(f"[outreach] orchestrator not importable: {_orch}", file=sys.stderr, flush=True)
try:
    import daily_pipeline
except Exception as _daily_pipeline:
    daily_pipeline = None  # type: ignore[assignment]
    print(f"[outreach] daily_pipeline not importable: {_daily_pipeline}", file=sys.stderr, flush=True)
try:
    import shared_context
except Exception as _shared_context:
    shared_context = None  # type: ignore[assignment]
    print(f"[outreach] shared_context not importable: {_shared_context}", file=sys.stderr, flush=True)
try:
    import session_brief
except Exception as _brief:
    session_brief = None  # type: ignore[assignment]
    print(f"[outreach] session_brief not importable: {_brief}", file=sys.stderr, flush=True)
try:
    import session_state
except Exception as _session:
    session_state = None  # type: ignore[assignment]
    print(f"[outreach] session_state not importable: {_session}", file=sys.stderr, flush=True)
try:
    from nodes import c7_sender
except Exception as _c7:
    c7_sender = None  # type: ignore[assignment]
    print(f"[outreach] c7_sender not importable: {_c7}", file=sys.stderr, flush=True)
try:
    from ds import qualification
except Exception as _qualification:
    qualification = None  # type: ignore[assignment]
    print(f"[outreach] qualification not importable: {_qualification}", file=sys.stderr, flush=True)
import lead_store as ls
from approved_email_templates import APPROVED_SEQUENCES, first_touch_body

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
TG_BASE      = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30
MSG_LIMIT    = 4000
ET           = ZoneInfo("America/New_York")
APPROVED_VERTICAL_SEQUENCES = ("restaurant_default", "pharmacy_default", "dealership_default")
PIPELINE_WATERFALL_STAGES = ("pulled", "scraped", "analyzed", "verified")

KNOWLEDGE_PATH = os.environ.get(
    "OUTREACH_KNOWLEDGE", str(Path.home() / ".outreach" / "state" / "outreach_knowledge.md"))
_DEFAULT_CONTEXT_ROOT = Path.home() / ".outreach"
if "OUTREACH_CONTEXT_ROOT" not in os.environ and not (_DEFAULT_CONTEXT_ROOT / "SOUL.md").exists():
    _DEFAULT_CONTEXT_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_ROOT = Path(os.environ.get("OUTREACH_CONTEXT_ROOT", str(_DEFAULT_CONTEXT_ROOT)))
OPERATOR_CHATS_PATH = Path(os.environ.get(
    "OUTREACH_OPERATOR_CHATS_PATH",
    str(Path.home() / ".outreach" / "state" / "operator_chats.json"),
))

SYSTEM_PROMPT = (
    "You are Outreach, the operations partner for Green PayTech's 2-person outbound team "
    "(Operator, Kasey). You live in Telegram. Direct, concise, honest — a business operator, "
    "not a chatbot. Lead with the actionable answer, then brief detail. No filler, no hype.\n"
    "CRITICAL: Use ONLY the facts in the CONTEXT block for any number or lead detail. NEVER "
    "invent a pipeline number — if it's not in the context, say so. Telegram inline-card "
    "approvals are the only approval surface. Slack and #approvals are retired. You do not "
    "claim an email was sent unless the Gmail or Instantly sender recorded it in the database."
)


# ── Telegram I/O ─────────────────────────────────────────────────────────────

def tg(token, method, **params):
    url = TG_BASE.format(token=token, method=method)
    req = urllib.request.Request(url, data=json.dumps(params).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[tg] {method} error: {e}", flush=True)
        return {}


# Persistent button keyboard — stays pinned at the bottom of the chat for both
# Operator and Kasey. Tapping a button sends that command. No memorizing needed.
KEYBOARD = {
    "keyboard": [["📞 /calls", "🆕 /leads"], ["✅ /review", "📈 /pipeline"],
                 ["📊 /status", "🚦 /sending"], ["🧭 /onboarding", "❓ /help"]],
    "resize_keyboard": True,
    "is_persistent": True,
}

HELP_TEXT = (
    "*⚡ Outreach — your outreach OS*\n\n"
    "Tap a button below, or just talk to me:\n"
    "🧭 `/onboarding` — short walkthrough for a new operator\n"
    "📞 `/calls` — due post-email follow-ups, then the unscheduled call queue\n"
    "🆕 `/leads` — fresh leads, tap # to dial\n"
    "📊 `/status` — pipeline + email stats\n"
    "📝 `/drafts` — show five exact standard-template previews\n"
    "✅ `/review` — approve / call / skip personalized leads (the email queue)\n"
    "📈 `/pipeline` — the email funnel (how many at each stage)\n"
    "🚦 `/sending` — is cold email live? + Instantly stats\n"
    "🧭 `/brief` — short workflow brief + one useful question\n"
    "🗂 `/context` — operator-safe shared context summary\n"
    "🎯 `/session review` — set current workflow mode/objective\n"
    "🧪 `/readiness` — validate Instantly routes + launch cap\n"
    "🔎 `/research <lead id or name>` — run the pipeline on an existing lead\n"
    "⚙️ `/runpipeline` — advance the deterministic pipeline once\n"
    "✍️ `get 20 review cards ready` — fill the personalized review queue without sending\n"
    "🌱 `increase email supply` — source more leads through the no-send pipeline\n"
    "📉 `why is enrichment low?` — diagnose email find-rate and quality\n"
    "🧭 `why is there no queue?` — explain the bottleneck and run the safe next step\n"
    "📤 `/sendgmail [lead id]` — send approved queued leads through Gmail\n"
    "`show <name>` — pull a lead's card\n"
    "`called Leo's, no answer` — log a call in plain English\n"
    "…or say the same things naturally, like `research lead 545` or "
    "`move the pipeline forward`."
)

ONBOARDING_MESSAGES = [
    (
        "*🧭 Acme Outreach onboarding*\n"
        "I help you run Acme outbound from Telegram: review leads, approve drafts, "
        "check sending, and work call follow-ups."
    ),
    (
        "*1. Start with status*\n"
        "Use `/pipeline` to see where leads are stuck.\n"
        "Use `/sending` to see whether email is live or safely holding."
    ),
    (
        "*2. Review before anything sends*\n"
        "Use `/review` to see personalized drafts.\n"
        "Tap Send only when you approve. `queued` means approved, not delivered."
    ),
    (
        "*3. Calls come after sends or call routing*\n"
        "Use `/calls` for post-email follow-ups first, then unscheduled call-list leads.\n"
        "Log calls naturally: `called Leo's, no answer`."
    ),
    (
        "*4. Ask questions normally*\n"
        "Ask things like: `what should I do next?`, `why is sending off?`, or "
        "`how do I improve bounce rate?`\n"
        "Use `/research 545` only when you want me to run lead research on a specific CRM lead."
    ),
    (
        "*5. Safety rule*\n"
        "I do not send external email unless a human approval exists and the send gate is live.\n"
        "For a quick next step, use `/brief`."
    ),
]


def send(token, chat_id, text, keyboard=False, markdown=True):
    parts = [text[i:i+MSG_LIMIT] for i in range(0, len(text), MSG_LIMIT)]
    for i, chunk in enumerate(parts):
        params = dict(chat_id=chat_id, text=chunk, disable_web_page_preview=True)
        if markdown:
            params["parse_mode"] = "Markdown"
        if keyboard and i == len(parts) - 1:
            params["reply_markup"] = KEYBOARD
        tg(token, "sendMessage", **params)


def _record_private_operator_chat_fields(chat, sender):
    """Remember private Telegram chat ids so scheduled jobs can DM operators."""
    if chat.get("type") != "private" or not chat.get("id"):
        return
    user_id = str(sender.get("id") or chat.get("id"))
    record = {
        "chat_id": str(chat["id"]),
        "first_name": sender.get("first_name", ""),
        "username": sender.get("username", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        OPERATOR_CHATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if OPERATOR_CHATS_PATH.exists():
            data = json.loads(OPERATOR_CHATS_PATH.read_text(encoding="utf-8") or "{}")
        data.setdefault("operators", {})[user_id] = record
        data["latest_private_chat_id"] = record["chat_id"]
        temporary = OPERATOR_CHATS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, OPERATOR_CHATS_PATH)
    except Exception as exc:
        print(f"[outreach] operator chat record error: {exc}", file=sys.stderr, flush=True)


def _record_private_operator_chat(message):
    _record_private_operator_chat_fields(message.get("chat") or {}, message.get("from") or {})


# ── DeepSeek ─────────────────────────────────────────────────────────────────

def load_knowledge(limit=12000):
    p = Path(KNOWLEDGE_PATH)
    return p.read_text()[:limit] if p.exists() else "(no knowledge pack found)"


def _read_context_file(rel_path, limit=5000):
    p = CONTEXT_ROOT / rel_path
    try:
        if p.exists():
            return f"## {rel_path}\n" + p.read_text()[:limit]
    except Exception as e:
        return f"## {rel_path}\n(unavailable: {e})"
    return ""


def load_context_pack(limit=14000):
    parts = [
        _read_context_file("SOUL.md", 3500),
        _read_context_file("AGENTS.md", 5000),
        _read_context_file("USER.md", 2500),
        _read_context_file("MEMORY.md", 2500),
        _read_context_file("context/ctx-index.md", 3000),
    ]
    text = "\n\n".join(part for part in parts if part.strip())
    return text[:limit] if text else "(no Outreach context pack found)"


def load_operator_shared_context():
    """Return only the Telegram-safe shared context projection."""
    if shared_context is None:
        return "Shared context unavailable: module not importable."
    try:
        snapshot = shared_context.operator_safe_snapshot()
        return shared_context.format_operator_safe_snapshot(snapshot)
    except Exception as exc:
        return f"Shared context unavailable: {exc}"


def deepseek(messages, max_tokens=500):
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return None
    body = {"model": "deepseek-chat", "max_tokens": max_tokens, "temperature": 0.3,
            "messages": messages}
    req = urllib.request.Request(DEEPSEEK_URL, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(DeepSeek error: {e})"


def parse_call(text):
    """Use DeepSeek to turn a freeform call note into {company, status, note}."""
    out = deepseek([
        {"role": "system", "content":
            "Extract a sales call log into JSON with keys: company (the business name "
            "mentioned), status (one of: called, voicemail, interested, callback, "
            "not_interested, booked, dead), note (any extra detail, may be empty). "
            "Map phrasing: 'no answer'/'didn't pick up'->called; 'left a vm'/'voicemail'->voicemail; "
            "'wants to talk'/'interested'->interested; 'call back'/'follow up'->callback; "
            "'not interested'/'no'->not_interested; 'booked'/'meeting set'->booked; "
            "'wrong number'/'closed'->dead. Reply with ONLY the JSON object."},
        {"role": "user", "content": text},
    ], max_tokens=150)
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


# ── Instantly facts ──────────────────────────────────────────────────────────

def instantly_summary():
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not db or not key:
        return "Email: no Instantly data yet (warming up)."
    try:
        a = db.fetch_analytics(key, [])
    except Exception as e:
        return f"Email: couldn't reach Instantly ({e})."
    sent    = sum(db._int(x.get("emails_sent_count")) for x in a)
    opens   = sum(db._int(x.get("open_count"))        for x in a)
    replies = sum(db._int(x.get("reply_count"))       for x in a)
    bounced = sum(db._int(x.get("bounced_count"))     for x in a)
    return f"Email — Sent: {sent} · Opens: {opens} · Replies: {replies} · Bounces: {bounced}"


# ── Command handlers ─────────────────────────────────────────────────────────

def cmd_leads(token, chat_id):
    rows = ls.list_leads(status="new", limit=8)
    if not rows:
        send(token, chat_id, "No new leads yet. The 8 AM sourcing run will add them.")
        return
    send(token, chat_id, f"*🆕 {len(rows)} fresh leads — tap a number to call:*")
    for r in rows:
        send(token, chat_id, ls.card(r))


def cmd_uncalled(token, chat_id):
    due = ls.scheduled_calls(due_only=True, limit=8)
    upcoming = ls.scheduled_calls(due_only=False, limit=5)
    unscheduled = ls.uncalled(days=0, limit=max(0, 8 - len(due)))
    if not due and not unscheduled and not upcoming:
        send(token, chat_id, "✅ Nothing in the call queue — everyone sourced has been called.")
        return
    if due:
        send(token, chat_id, f"*📞 {len(due)} post-email cold call(s) due now:*")
    for r in due:
        send(token, chat_id, ls.card(r))
    if unscheduled:
        send(token, chat_id, f"*🗂 {len(unscheduled)} unscheduled call-list lead(s):*")
    for r in unscheduled:
        send(token, chat_id, ls.card(r))
    if upcoming:
        labels = [
            f"• {r['company']} — {_format_et(r['call_due_at'])}"
            for r in upcoming
        ]
        send(token, chat_id, "*📅 Upcoming post-email calls:*\n" + "\n".join(labels))


def cmd_status(token, chat_id):
    total, by = ls.stats()
    parts = [f"*📊 Pipeline* — {total} leads"]
    if by:
        parts.append(" · ".join(f"{s}: {n}" for s, n in sorted(by.items(), key=lambda kv: -kv[1])))
    parts.append(instantly_summary())
    send(token, chat_id, "\n".join(parts))


def cmd_onboarding(token, chat_id, who="operator"):
    """Send a short operator walkthrough as separate Telegram-sized prompts."""
    _note_session("onboarding", "general", who)
    for message in ONBOARDING_MESSAGES:
        send(token, chat_id, message, keyboard=True)


def cmd_context(token, chat_id, who="operator"):
    """Show the operator-safe shared context view."""
    _note_session("shared_context", "general", who)
    send(token, chat_id, load_operator_shared_context(), markdown=False)


def _format_et(value):
    if not value:
        return "unscheduled"
    try:
        return datetime.fromisoformat(value).astimezone(ET).strftime("%Y-%m-%d %I:%M %p ET")
    except ValueError:
        return value


def cmd_show(token, chat_id, query):
    hits = ls.find_lead(query)
    if not hits:
        send(token, chat_id, f"No lead matching '{query}'.")
    elif len(hits) == 1:
        send(token, chat_id, ls.card(hits[0]))
    else:
        send(token, chat_id, "Multiple matches — say the full name or `show id <n>`:\n" +
             "\n".join(f"• {h['company']} (id {h['id']})" for h in hits))


def cmd_log_call(token, chat_id, text, who):
    parsed = parse_call(text)
    if not parsed or not parsed.get("company"):
        send(token, chat_id, "Didn't catch which business. Try: `called Leo's, no answer`")
        return
    hits = ls.find_lead(parsed["company"])
    if not hits:
        send(token, chat_id, f"No lead matching '{parsed['company']}'. Sourced yet?")
        return
    if len(hits) > 1:
        send(token, chat_id, f"Which '{parsed['company']}'? " +
             ", ".join(f"{h['company']} (id {h['id']})" for h in hits) +
             " — repeat with the exact name.")
        return
    lead = hits[0]
    status = parsed.get("status") if parsed.get("status") in ls.STATUSES else "called"
    ls.update_lead(lead["id"], status=status, note=parsed.get("note") or "",
                   called_by=who, mark_called=True)
    send(token, chat_id, f"✅ Logged *{lead['company']}* → {status}.\n" +
         ls.card(ls.find_lead(lead["company"])[0]))


def _ledger_conn():
    """Open a pipeline DB connection. Returns None if ledger is not available."""
    if L is None:
        return None
    try:
        return L.connect()
    except Exception as e:
        print(f"[outreach] ledger connect error: {e}", file=sys.stderr, flush=True)
        return None


def _note_session(action, mode="", who="operator"):
    """
    Update lightweight session state after deterministic commands.

    This is intentionally best-effort. Outreach should not fail a command because
    the collaborator-state table is unavailable.
    """
    if session_state is None:
        return
    conn = _ledger_conn()
    if conn is None:
        return
    try:
        session_state.note_action(conn, action, mode=mode, updated_by=who)
    except Exception as exc:
        print(f"[outreach] session note error: {exc}", file=sys.stderr, flush=True)
    finally:
        conn.close()


def _set_session(mode, objective="", who="operator"):
    """Persist an explicit operator session mode/objective."""
    if session_state is None:
        return None
    conn = _ledger_conn()
    if conn is None:
        return None
    try:
        return session_state.set_state(conn, mode, objective=objective, updated_by=who)
    except Exception as exc:
        print(f"[outreach] session set error: {exc}", file=sys.stderr, flush=True)
        return None
    finally:
        conn.close()


def _set_pending_action(action, who="operator"):
    """Remember one structured action so short replies like 'yes' can run it."""
    if session_state is None:
        return None
    conn = _ledger_conn()
    if conn is None:
        return None
    try:
        return session_state.set_pending_action(conn, action, updated_by=who)
    except Exception as exc:
        print(f"[outreach] pending action set error: {exc}", file=sys.stderr, flush=True)
        return None
    finally:
        conn.close()


def _pop_pending_action():
    """Return and clear the currently pending action, if any."""
    if session_state is None:
        return None
    conn = _ledger_conn()
    if conn is None:
        return None
    try:
        return session_state.pop_pending_action(conn)
    except Exception as exc:
        print(f"[outreach] pending action pop error: {exc}", file=sys.stderr, flush=True)
        return None
    finally:
        conn.close()


def _current_session_state():
    """Best-effort read of Outreach' lightweight workflow state."""
    if session_state is None:
        return {}
    conn = _ledger_conn()
    if conn is None:
        return {}
    try:
        return session_state.get_state(conn)
    except Exception as exc:
        print(f"[outreach] session read error: {exc}", file=sys.stderr, flush=True)
        return {}
    finally:
        conn.close()


def _get_lead_by_id(conn, lead_id):
    return conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def _find_pipeline_leads(query, limit=5):
    conn = _ledger_conn()
    if conn is None:
        return []
    try:
        if re.fullmatch(r"\d+", query.strip()):
            row = _get_lead_by_id(conn, int(query.strip()))
            return [row] if row else []
        return ls.find_lead(query, limit=limit)
    finally:
        conn.close()


def _pipeline_stage_for(row):
    try:
        return row["stage"] or ""
    except (IndexError, KeyError):
        return ""


def _run_orchestrator_once(limit=25, stage=None):
    if orchestrator is None:
        return None, "orchestrator is not importable on this runtime"
    try:
        return orchestrator.run_once(stage_filter=stage, limit=limit), ""
    except TypeError:
        try:
            return orchestrator.run_once(stage, limit), ""
        except Exception as exc:
            return None, str(exc)
    except Exception as exc:
        return None, str(exc)


def _format_action_result(title, did, changed="", blocked="", next_step=""):
    lines = [f"*{title}*"]
    if did:
        lines.append(f"Did: {did}")
    if changed:
        lines.append(f"Changed: {changed}")
    if blocked:
        lines.append(f"Blocked: {blocked}")
    if next_step:
        lines.append(f"Next: {next_step}")
    return "\n".join(lines)


def cmd_run_pipeline(token, chat_id, stage=None, limit=25, who="operator"):
    """Advance the deterministic pipeline once from Telegram."""
    _note_session("run_pipeline", "pipeline", who)
    totals, err = _run_orchestrator_once(limit=limit, stage=stage)
    if err:
        send(token, chat_id, _format_action_result(
            "⚙️ Pipeline action",
            "Tried to run the deterministic orchestrator.",
            blocked=err,
            next_step="Check the VPS pipeline modules before retrying.",
        ))
        return
    changed = ", ".join(f"{k}: {v}" for k, v in (totals or {}).items()) or "no staged rows moved"
    send(token, chat_id, _format_action_result(
        "⚙️ Pipeline action",
        f"Ran orchestrator once{f' for stage {stage}' if stage else ''}.",
        changed=changed,
        next_step="Use /review if leads reached personalized.",
    ))


def _personalized_count(conn):
    counts = L.count_by_stage(conn) if L is not None else {}
    return counts.get("personalized", 0), counts


def _stage_counts(conn):
    return L.count_by_stage(conn) if L is not None else {}


def _stage_delta(before, after):
    keys = sorted(set(before or {}) | set(after or {}))
    return {key: (after or {}).get(key, 0) - (before or {}).get(key, 0) for key in keys}


def _format_counts_delta(delta):
    parts = [f"{key}: {value:+d}" for key, value in (delta or {}).items() if value]
    return ", ".join(parts) if parts else "no stage-count change"


def _vertical_reserve(conn):
    placeholders = ",".join("?" for _ in APPROVED_VERTICAL_SEQUENCES)
    rows = conn.execute(
        f"""
        SELECT stage, COUNT(*) n
        FROM leads
        WHERE stage IN ('personalized','queued')
          AND sequence_key IN ({placeholders})
        GROUP BY stage
        """,
        APPROVED_VERTICAL_SEQUENCES,
    ).fetchall()
    counts = {row["stage"]: row["n"] for row in rows}
    return counts.get("personalized", 0) + counts.get("queued", 0), counts


def _target_review_limit(limit=None):
    """Cap review-ready drafts separately from live send execution."""
    if limit is not None:
        return max(1, min(int(limit), 50))
    raw = os.environ.get("PIPELINE_REVIEW_READY_CAP") or os.environ.get("PIPELINE_PERSONALIZED_CAP") or "20"
    try:
        return max(1, min(int(raw), 50))
    except ValueError:
        return 20


def _daily_send_cap(default=15):
    raw = os.environ.get("PIPELINE_DAILY_SEND_CAP", str(default)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _reserve_target():
    raw = os.environ.get("PIPELINE_REVIEW_RESERVE_TARGET", "").strip()
    if raw:
        try:
            return max(1, min(int(raw), 100))
        except ValueError:
            pass
    return min(100, max(_daily_send_cap() * 2, _target_review_limit()))


def _extract_limit(text, default=None):
    m = re.search(r"\b(?:first|next|up to|limit|cap|make|get|prepare|personalize|personalise)?\s*(\d{1,2})\b", text or "", re.I)
    if not m:
        return default
    return _target_review_limit(int(m.group(1)))


def _extract_supply_limit(text, default=60):
    m = re.search(r"\b(\d{1,3})\b", text or "")
    if not m:
        return default
    return max(1, min(int(m.group(1)), 250))


def _count_query(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return int(row["n"] if row and "n" in row.keys() else row[0] if row else 0)


def _top_event_reasons(conn, limit=4):
    rows = conn.execute(
        """
        SELECT event_type, payload, COUNT(*) n
        FROM events
        WHERE event_type IN ('routed_call_list','email_verified','draft_ready')
        GROUP BY event_type, payload
        ORDER BY n DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        reason = row["event_type"]
        try:
            payload = json.loads(row["payload"] or "{}")
            reason = payload.get("reason") or payload.get("source") or reason
        except Exception:
            pass
        out.append(f"{reason}: {row['n']}")
    return out


def cmd_fill_review_queue(token, chat_id, limit=None, who="operator"):
    """
    Move no-send pipeline rows toward `personalized` review cards.

    This never approves, queues, or sends email. It only runs the deterministic
    orchestrator until the review-ready cap is reached or no more rows move.
    """
    _note_session("fill_review_queue", "pipeline", who)
    cap = _target_review_limit(limit)
    if L is None:
        send(token, chat_id, _format_action_result(
            "✍️ Review queue action",
            "Tried to inspect the pipeline.",
            blocked="Ledger is not importable in Outreach.",
            next_step="Fix the pipeline import path, then retry.",
        ))
        return

    conn = _ledger_conn()
    if conn is None:
        send(token, chat_id, "⚠️ Pipeline DB not available.")
        return
    try:
        before_personalized, before_counts = _personalized_count(conn)
    finally:
        conn.close()

    if before_personalized >= cap:
        send(token, chat_id, _format_action_result(
            "✍️ Review queue action",
            f"Checked the review-ready queue against cap {cap}.",
            changed=f"personalized already at {before_personalized}/{cap}",
            next_step="Use /review to read and approve, call, or skip drafts.",
        ))
        return

    if orchestrator is None:
        send(token, chat_id, _format_action_result(
            "✍️ Review queue action",
            "Checked the review-ready queue.",
            blocked="Orchestrator is not importable in Outreach.",
            next_step="Fix the pipeline import path, then retry.",
        ))
        return

    totals_accum = {}
    last_err = ""
    # Verified rows are the direct route to personalized. If that does not fill
    # the cap, the full no-send pipeline can advance upstream rows one step.
    for _ in range(5):
        remaining = max(0, cap - before_personalized)
        if remaining == 0:
            break
        totals, err = _run_orchestrator_once(stage="verified", limit=remaining)
        if err:
            last_err = err
            break
        for key, value in (totals or {}).items():
            totals_accum[key] = totals_accum.get(key, 0) + value

        conn = _ledger_conn()
        if conn is None:
            last_err = "Pipeline DB unavailable after orchestrator run."
            break
        try:
            current_personalized, current_counts = _personalized_count(conn)
        finally:
            conn.close()
        if current_personalized >= cap:
            before_personalized = current_personalized
            before_counts = current_counts
            break

        remaining = max(1, cap - current_personalized)
        totals, err = _run_orchestrator_once(limit=remaining)
        if err:
            last_err = err
            break
        moved = sum((totals or {}).values())
        for key, value in (totals or {}).items():
            totals_accum[key] = totals_accum.get(key, 0) + value

        conn = _ledger_conn()
        if conn is None:
            last_err = "Pipeline DB unavailable after orchestrator run."
            break
        try:
            next_personalized, next_counts = _personalized_count(conn)
        finally:
            conn.close()
        if next_personalized == current_personalized and moved == 0:
            before_personalized = next_personalized
            before_counts = next_counts
            break
        before_personalized = next_personalized
        before_counts = next_counts

    changed = ", ".join(f"{k}: {v}" for k, v in sorted(totals_accum.items())) or "no staged rows moved"
    blocked = last_err
    if not blocked and before_personalized < cap:
        blocked = f"Review-ready queue stopped at {before_personalized}/{cap}; upstream rows may need source data, email candidates, or scraper fixes."
    send(token, chat_id, _format_action_result(
        "✍️ Review queue action",
        f"Ran the no-send pipeline toward {cap} personalized review card(s).",
        changed=f"{changed}; personalized: {before_personalized}/{cap}",
        blocked=blocked,
        next_step="Use /review to read drafts. Nothing was queued or sent.",
    ))


def cmd_supply_autopilot(token, chat_id, limit=60, passes=3, who="operator"):
    """
    Source more leads through the approved free pipeline until reserve improves.

    This never approves, queues, sends, buys enrichment, or lowers verification
    quality. It only runs daily_pipeline/orchestrator no-send stages.
    """
    _note_session("supply_autopilot", "pipeline", who)
    target = _reserve_target()
    limit = max(1, min(int(limit or 60), 250))
    passes = max(1, min(int(passes or 3), 6))
    if L is None:
        send(token, chat_id, _format_action_result(
            "🌱 Lead supply action",
            "Tried to inspect lead reserve.",
            blocked="Ledger is not importable in Outreach.",
            next_step="Fix the pipeline import path, then retry.",
        ))
        return
    conn = _ledger_conn()
    if conn is None:
        send(token, chat_id, "⚠️ Pipeline DB not available.")
        return
    try:
        before_counts = _stage_counts(conn)
        before_reserve, before_reserve_parts = _vertical_reserve(conn)
    finally:
        conn.close()

    if before_reserve >= target:
        send(token, chat_id, _format_action_result(
            "🌱 Lead supply action",
            f"Checked vertical review reserve against target {target}.",
            changed=f"reserve already {before_reserve}/{target} ({before_reserve_parts})",
            next_step="Use /review to approve cards, or ask for an enrichment quality report.",
        ))
        return
    if daily_pipeline is None:
        send(token, chat_id, _format_action_result(
            "🌱 Lead supply action",
            "Tried to run the approved free sourcing pipeline.",
            blocked="daily_pipeline is not importable in this runtime.",
            next_step="Deploy the VPS pipeline module or run /runpipeline for existing staged rows.",
        ))
        return

    run_notes = []
    blocked = ""
    for idx in range(passes):
        conn = _ledger_conn()
        if conn is None:
            blocked = "Pipeline DB unavailable during supply run."
            break
        try:
            current_reserve, _parts = _vertical_reserve(conn)
        finally:
            conn.close()
        if current_reserve >= target:
            break
        try:
            daily_pipeline.run(limit=limit, dry_run=False)
            run_notes.append(f"pass {idx + 1}: daily_pipeline --limit {limit}")
        except Exception as exc:
            blocked = f"supply pass {idx + 1} failed: {exc}"
            break

    conn = _ledger_conn()
    if conn is None:
        send(token, chat_id, _format_action_result(
            "🌱 Lead supply action",
            "Ran sourcing but could not inspect final counts.",
            changed="; ".join(run_notes),
            blocked="Pipeline DB unavailable after supply run.",
        ))
        return
    try:
        after_counts = _stage_counts(conn)
        after_reserve, after_reserve_parts = _vertical_reserve(conn)
        reasons = _top_event_reasons(conn)
    finally:
        conn.close()
    delta = _format_counts_delta(_stage_delta(before_counts, after_counts))
    next_step = "Use /review if personalized increased; otherwise ask `why is enrichment low` for the bottleneck report."
    send(token, chat_id, _format_action_result(
        "🌱 Lead supply action",
        f"Ran approved no-send sourcing because reserve was {before_reserve}/{target}.",
        changed=f"{'; '.join(run_notes) or 'no sourcing pass ran'}; {delta}; reserve {after_reserve}/{target} ({after_reserve_parts})",
        blocked=blocked,
        next_step=next_step + (f"\nReasons: {', '.join(reasons)}" if reasons else ""),
    ))


def cmd_enrichment_quality(token, chat_id, who="operator"):
    """Report why email/review supply is weak using live SQLite evidence."""
    _note_session("enrichment_quality", "pipeline", who)
    conn = _ledger_conn()
    if conn is None:
        send(token, chat_id, "⚠️ Pipeline DB not available.")
        return
    try:
        totals = _stage_counts(conn)
        total_email_path = _count_query(conn, "SELECT COUNT(*) n FROM leads WHERE stage IS NOT NULL")
        candidate_total = _count_query(conn, "SELECT COUNT(*) n FROM email_candidates")
        onsite_candidates = _count_query(conn, "SELECT COUNT(*) n FROM email_candidates WHERE source='onsite'")
        verified_or_better = _count_query(
            conn,
            "SELECT COUNT(*) n FROM leads WHERE stage IN ('verified','personalized','queued','sent','replied')",
        )
        personalized_or_better = _count_query(
            conn,
            "SELECT COUNT(*) n FROM leads WHERE stage IN ('personalized','queued','sent','replied')",
        )
        generic_candidates = _count_query(
            conn,
            """
            SELECT COUNT(*) n FROM email_candidates
            WHERE lower(substr(email, 1, instr(email, '@') - 1)) IN
              ('info','contact','hello','support','admin','sales','office','team','service')
            """,
        )
        by_vertical = conn.execute(
            """
            SELECT COALESCE(vertical,'unknown') vertical,
                   COUNT(*) total,
                   SUM(CASE WHEN email IS NOT NULL AND TRIM(email)<>'' THEN 1 ELSE 0 END) with_email,
                   SUM(CASE WHEN stage IN ('personalized','queued','sent','replied') THEN 1 ELSE 0 END) review_ready,
                   SUM(CASE WHEN stage='call_list' THEN 1 ELSE 0 END) call_list,
                   SUM(CASE WHEN stage='skipped' THEN 1 ELSE 0 END) skipped
            FROM leads
            WHERE stage IS NOT NULL
            GROUP BY COALESCE(vertical,'unknown')
            ORDER BY total DESC
            LIMIT 8
            """
        ).fetchall()
        reasons = _top_event_reasons(conn, limit=5)
    finally:
        conn.close()

    def pct(part, whole):
        return "0.0%" if not whole else f"{part / whole * 100:.1f}%"

    lines = ["*📉 Enrichment quality report*"]
    lines.append(
        f"Email-path rows: {total_email_path} · candidates: {candidate_total} "
        f"(onsite {onsite_candidates}) · verified+ {verified_or_better} ({pct(verified_or_better, total_email_path)})"
    )
    lines.append(
        f"Review-ready+ {personalized_or_better} ({pct(personalized_or_better, total_email_path)}) · "
        f"generic candidate inboxes: {generic_candidates}"
    )
    lines.append("\n*By vertical*")
    for row in by_vertical:
        lines.append(
            f"- {row['vertical']}: total {row['total']} · email {row['with_email']} ({pct(row['with_email'], row['total'])}) · "
            f"review-ready+ {row['review_ready']} · call-list {row['call_list']} · skipped {row['skipped']}"
        )
    lines.append("\n*Diagnosis*")
    if totals.get("call_list", 0) or totals.get("skipped", 0):
        lines.append("- The main bottleneck is email discovery: many sourced businesses do not expose a usable onsite email.")
    if generic_candidates:
        lines.append("- Some found emails are generic inboxes, so decision-maker confidence is low even when deliverability is acceptable.")
    lines.append("- Free path stays conservative: scrape more qualified businesses and keep the verification gate intact.")
    lines.append("- Paid enrichment remains Phase 2: Apollo/Hunter/A-Leads only after explicit approval.")
    if reasons:
        lines.append("\n*Recent routed reasons*: " + ", ".join(reasons))
    lines.append("\nNext: say `increase email supply` to source more, or `get review cards ready` to personalize verified leads.")
    send(token, chat_id, "\n".join(lines))


def cmd_pipeline_bottleneck(token, chat_id, who="operator"):
    """Explain why review/queue is empty and run the safe no-send next action."""
    _note_session("pipeline_bottleneck", "pipeline", who)
    conn = _ledger_conn()
    if conn is None:
        send(token, chat_id, "⚠️ Pipeline DB not available.")
        return
    try:
        counts = _stage_counts(conn)
        reserve, reserve_parts = _vertical_reserve(conn)
    finally:
        conn.close()
    lines = ["*🧭 Pipeline bottleneck*"]
    lines.append(
        f"personalized={counts.get('personalized', 0)} · queued={counts.get('queued', 0)} · "
        f"verified={counts.get('verified', 0)} · waterfall={sum(counts.get(s, 0) for s in PIPELINE_WATERFALL_STAGES)}"
    )
    if counts.get("personalized", 0):
        lines.append("There is a review queue. Use /review to approve, call, or skip cards.")
        send(token, chat_id, "\n".join(lines)); return
    if counts.get("verified", 0) or counts.get("enriched", 0):
        lines.append("No review cards because verified/enriched leads have not been converted to personalized drafts. I am running that no-send step now.")
        send(token, chat_id, "\n".join(lines))
        cmd_fill_review_queue(token, chat_id, limit=_target_review_limit(), who=who)
        return
    if any(counts.get(s, 0) for s in ("pulled", "scraped", "analyzed", "guessed")):
        lines.append("No review cards because leads are still upstream. I am advancing the no-send pipeline now.")
        send(token, chat_id, "\n".join(lines))
        cmd_run_pipeline(token, chat_id, limit=25, who=who)
        return
    lines.append(f"Vertical reserve is {reserve}/{_reserve_target()} ({reserve_parts}). I am sourcing more leads through the free no-send path.")
    send(token, chat_id, "\n".join(lines))
    cmd_supply_autopilot(token, chat_id, limit=60, passes=2, who=who)


def cmd_send_queued(token, chat_id, provider="gmail", lead_id=None, limit=6, who="operator"):
    """Send queued leads through the configured side rail without changing C7 semantics."""
    _note_session("send_queued", "sending", who)
    if c7_sender is None:
        send(token, chat_id, _format_action_result(
            "📤 Send action",
            "Tried to load C7 sender.",
            blocked="C7 sender is not importable in Outreach.",
            next_step="Fix the pipeline import path, then retry.",
        ))
        return
    if os.environ.get("PIPELINE_SENDING_ENABLED", "").strip() != "1":
        send(token, chat_id, _format_action_result(
            "📤 Send action",
            "Checked the send gate.",
            blocked="PIPELINE_SENDING_ENABLED is not 1, so queued leads will hold.",
            next_step="Enable sending only when you are ready for live external email.",
        ))
        return

    old_provider = os.environ.get("SEND_PROVIDER")
    os.environ["SEND_PROVIDER"] = provider
    try:
        c7_sender.run_batch(dry_run=False, limit=limit, lead_ids=[lead_id] if lead_id else [])
    finally:
        if old_provider is None:
            os.environ.pop("SEND_PROVIDER", None)
        else:
            os.environ["SEND_PROVIDER"] = old_provider

    conn = _ledger_conn()
    counts = L.count_by_stage(conn) if conn is not None else {}
    if conn is not None:
        conn.close()
    send(token, chat_id, _format_action_result(
        "📤 Send action",
        f"Ran C7 with provider `{provider}`" + (f" for lead {lead_id}" if lead_id else f" up to {limit} lead(s)") + ".",
        changed=f"sent: {counts.get('sent', 0)} · queued: {counts.get('queued', 0)}",
        next_step="Use /calls to see post-email follow-ups after sent rows schedule call_due_at.",
    ))


def cmd_research(token, chat_id, query, who="operator"):
    """Run the existing pipeline on an existing lead, then return its latest card."""
    _note_session("research", "research", who)
    q = (query or "").strip()
    q = re.sub(r"^(research|scrape|enrich|diligence|run diligence on|execute diligence on|run)\s+", "", q, flags=re.I).strip()
    q = re.sub(r"^lead\s+", "", q, flags=re.I).strip()
    if not q:
        send(token, chat_id, "Tell me which existing lead to research. Example: `/research 545`.")
        return

    hits = _find_pipeline_leads(q, limit=5)
    if not hits:
        send(token, chat_id, _format_action_result(
            "🔎 Research action",
            f"Searched the local CRM for `{q}`.",
            blocked="No matching lead exists in SQLite yet.",
            next_step="Add/source the lead first, then say `research <id or name>`.",
        ))
        return
    if len(hits) > 1:
        send(token, chat_id, "Multiple matches — rerun with the id:\n" +
             "\n".join(f"• {h['company']} (id {h['id']}, stage {_pipeline_stage_for(h) or 'none'})" for h in hits))
        return

    before = hits[0]
    lead_id = before["id"]
    before_stage = _pipeline_stage_for(before) or "none"
    if before_stage in ("personalized", "queued", "sent", "replied", "call_list"):
        send(token, chat_id, _format_action_result(
            "🔎 Research action",
            f"Checked lead {lead_id} — {before['company']}.",
            changed=f"Already at stage `{before_stage}`.",
            next_step="Use /review for personalized leads or /calls for call-list leads.",
        ) + "\n\n" + ls.card(before))
        return

    totals, err = _run_orchestrator_once(limit=50)
    if err:
        send(token, chat_id, _format_action_result(
            "🔎 Research action",
            f"Found lead {lead_id} at stage `{before_stage}`.",
            blocked=err,
            next_step="Fix orchestrator availability, then retry.",
        ))
        return

    refreshed = _find_pipeline_leads(str(lead_id), limit=1)
    after = refreshed[0] if refreshed else before
    after_stage = _pipeline_stage_for(after) or "none"
    changed = f"lead {lead_id}: `{before_stage}` -> `{after_stage}`"
    if totals:
        changed += " | " + ", ".join(f"{k}: {v}" for k, v in totals.items())
    next_step = "Use /review if this reached personalized."
    if after_stage in ("pulled", "scraped", "analyzed", "guessed", "verified"):
        next_step = "Run `continue pipeline` if more stage work is still pending."
    send(token, chat_id, _format_action_result(
        "🔎 Research action",
        f"Ran the deterministic pipeline for existing lead {lead_id} — {after['company']}.",
        changed=changed,
        next_step=next_step,
    ) + "\n\n" + ls.card(after))


def cmd_pipeline(token, chat_id, who="operator"):
    """📈 The email funnel — how many leads sit at each pipeline stage."""
    _note_session("pipeline_status", "pipeline", who)
    conn = _ledger_conn()
    if conn is None:
        send(token, chat_id, "⚠️ Pipeline DB not available."); return
    try:
        counts = L.count_by_stage(conn)
    except Exception as e:
        send(token, chat_id, f"⚠️ Could not read pipeline: {e}"); return
    order = [("pulled", "🌱 sourced"), ("scraped", "🔍 scraped"),
             ("analyzed", "🧮 scored"), ("guessed", "📧 email-guessed"),
             ("verified", "✅ verified"), ("personalized", "✍️ awaiting review"),
             ("queued", "📤 approved/queued"), ("sent", "🚀 sent"),
             ("replied", "💬 replied")]
    lines = ["*📈 Email pipeline*"]
    for stage, label in order:
        if counts.get(stage):
            lines.append(f"  {label}: {counts[stage]}")
    call_list = counts.get("call_list", 0)
    if call_list:
        lines.append(f"\n📞 routed to call list: {call_list}")
    due_calls = ls.scheduled_calls(due_only=True, limit=100)
    upcoming_calls = ls.scheduled_calls(due_only=False, limit=100)
    lines.append(f"\n📞 post-email calls due: {len(due_calls)}")
    lines.append(f"📅 post-email calls upcoming: {len(upcoming_calls)}")
    awaiting = counts.get("personalized", 0)
    if awaiting:
        lines.append(f"\n🔔 {awaiting} ready to approve → tap /review")
    next_action = _pipeline_next_best_action(counts, len(due_calls))
    if next_action:
        lines.append(f"\n➡️ Next best action: {next_action}")
    if len(lines) == 1:
        lines.append("  (empty — the 8 AM run will fill it)")
    send(token, chat_id, "\n".join(lines))


def _pipeline_next_best_action(counts, due_call_count):
    """Return one short operator action based only on live counts."""
    if due_call_count:
        return "handle due calls first; replies and post-email calls are closest to booked meetings."
    if counts.get("personalized", 0):
        return "review personalized drafts with /review so approved sends can move."
    if counts.get("verified", 0) or counts.get("guessed", 0) or counts.get("enriched", 0):
        return "advance verified/enriched leads into personalized drafts before sourcing more."
    if counts.get("scraped", 0) or counts.get("analyzed", 0):
        return "run the next pipeline step to turn scraped/analyzed leads into send-ready drafts."
    if counts.get("pulled", 0) or counts.get("new", 0):
        return "scrape and enrich existing leads before adding more raw volume."
    if counts.get("call_list", 0):
        return "work the call list; email path is not active for those leads."
    return "source a small targeted batch, then enrich before increasing volume."


def cmd_sending(token, chat_id, who="operator"):
    """🚦 Is cold email sending live? + the Instantly send gate state."""
    _note_session("sending_status", "sending", who)
    enabled = os.environ.get("PIPELINE_SENDING_ENABLED", "").strip() == "1"
    fallback_campaign = (
        "set" if os.environ.get("INSTANTLY_CAMPAIGN_ID", "").strip() else "NOT set"
    )
    sequence_campaigns = sorted(
        key.removeprefix("INSTANTLY_CAMPAIGN_ID_").lower()
        for key, value in os.environ.items()
        if key.startswith("INSTANTLY_CAMPAIGN_ID_") and value.strip()
    )
    routed_campaigns = ", ".join(sequence_campaigns) if sequence_campaigns else "none"
    state = "🟢 ON — approved leads will send" if enabled else "🔴 OFF — approved leads HOLD (safe during warmup)"
    msg = (f"*🚦 Sending status*\n{state}\n"
           f"Fallback campaign ID: {fallback_campaign}\n"
           f"Configured sequence IDs: {routed_campaigns}\n"
           f"{instantly_summary()}\n\n"
           f"_To go live after warmup: set PIPELINE_SENDING_ENABLED=1 on the server "
           f"and start the Instantly campaign._")
    send(token, chat_id, msg)


def cmd_protected_send_refusal(token, chat_id, who="operator"):
    """Refuse natural-language requests that would alter live send controls."""
    _note_session("protected_send_refusal", "sending", who)
    send(token, chat_id, _format_action_result(
        "🛑 Live send control",
        "Recognized a request to change sending or campaign live state.",
        blocked="I do not turn on sending, approve leads, activate campaigns, or send email from freeform chat.",
        next_step="Use /sending and /readiness to inspect state. Live send changes must be explicit server/operator actions.",
    ))


def cmd_readiness(token, chat_id, who="operator"):
    """Validate launch configuration from Telegram without exposing secrets."""
    _note_session("readiness", "sending", who)
    enabled = os.environ.get("PIPELINE_SENDING_ENABLED", "").strip() == "1"
    cap = os.environ.get("PIPELINE_DAILY_SEND_CAP", "30").strip()
    conn = _ledger_conn()
    counts = L.count_by_stage(conn) if conn is not None else {}
    lines = [
        "*🧪 Launch readiness*",
        f"Sending: {'ON' if enabled else 'OFF'}",
        f"Daily cap: {cap}",
        f"Queued approvals: {counts.get('queued', 0)}",
        f"Awaiting review: {counts.get('personalized', 0)}",
        "",
        "*Instantly sequence routes*",
    ]
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    campaigns = [
        ("restaurant_default", "INSTANTLY_CAMPAIGN_ID_RESTAURANT_DEFAULT"),
        ("pharmacy_default", "INSTANTLY_CAMPAIGN_ID_PHARMACY_DEFAULT"),
        ("dealership_default", "INSTANTLY_CAMPAIGN_ID_DEALERSHIP_DEFAULT"),
    ]
    for sequence, env_key in campaigns:
        campaign_id = os.environ.get(env_key, "").strip()
        if not campaign_id:
            lines.append(f"❌ {sequence}: missing ID")
            continue
        if not db or not key:
            lines.append(f"⚠️ {sequence}: configured, API validation unavailable")
            continue
        try:
            leads = db.fetch_leads(key, [campaign_id])
            lines.append(f"✅ {sequence}: valid, {len(leads)} lead(s) in campaign")
        except Exception as exc:
            detail = str(exc)
            label = "campaign not found" if "Campaign not found" in detail else "API validation failed"
            lines.append(f"❌ {sequence}: {label}")
    if enabled:
        lines.append("\n⚠️ Sending is enabled. Use `/sending` to confirm live stats.")
    else:
        lines.append("\nSending remains safely off until all three routes validate.")
    send(token, chat_id, "\n".join(lines))


def cmd_brief(token, chat_id, mode="general", who="operator"):
    """Return a compact session brief that asks one workflow question."""
    _note_session("brief", mode, who)
    if session_brief is None:
        send(token, chat_id, _format_action_result(
            "🧭 Session brief",
            "Tried to load the session brief module.",
            blocked="session_brief is not importable in this runtime.",
            next_step="Check workspace/scripts/session_brief.py on the server.",
        ))
        return
    try:
        send(token, chat_id, session_brief.build_brief(mode), markdown=False)
    except Exception as exc:
        send(token, chat_id, f"⚠️ Could not build session brief: {exc}")


def cmd_session(token, chat_id, text, who="operator"):
    """
    Set or inspect the current workflow session.

    Examples:
      /session review
      /session pipeline get more review cards ready
      I'm doing calls for the next hour
    """
    if session_state is None:
        send(token, chat_id, "⚠️ Session state is not available in this runtime.")
        return
    raw = (text or "").strip()
    raw = re.sub(r"^/?session\s*", "", raw, flags=re.I).strip()
    raw = re.sub(r"^(i'?m|i am|we are|let'?s|lets)\s+", "", raw, flags=re.I).strip()
    mode = "general"
    for candidate in ("review", "calls", "pipeline", "cleanup", "research", "sending"):
        if re.search(rf"\b{candidate}\b", raw, flags=re.I):
            mode = candidate
            break
    objective = raw if raw and raw.lower() != mode else ""
    state = _set_session(mode, objective, who) or {}
    send(
        token,
        chat_id,
        f"Session set: {state.get('mode', mode)}"
        + (f"\nObjective: {state.get('objective')}" if state.get("objective") else "")
        + "\n\n"
        + (session_brief.build_brief(state.get("mode", mode)) if session_brief else "Use /brief for the next workflow prompt."),
        markdown=False,
    )


def _safe_opener(row):
    """Only show an opener when C6 recorded literal supporting evidence."""
    trigger = (row["trigger"] or "").strip() if row["trigger"] else ""
    verified = row["trigger_verified"] if "trigger_verified" in row.keys() else None
    return trigger if trigger and verified == 1 else ""


def _standard_draft(row):
    """Render the exact approved first-touch body for Telegram QA."""
    first = (row["owner_name"] or "").strip().split(" ", 1)[0] if row["owner_name"] else ""
    greeting = first or "there"
    company = row["company"] or "[company]"
    sequence = (row["sequence_key"] or "").strip() if row["sequence_key"] else ""
    if sequence in APPROVED_SEQUENCES:
        body = first_touch_body(
            sequence,
            first_name=greeting,
            city_state=row["city_state"] if "city_state" in row.keys() else "",
            trigger=row["trigger"] if "trigger" in row.keys() else "",
            email_angle=row["email_angle"] if "email_angle" in row.keys() else "",
            template_cta=row["template_cta"] if "template_cta" in row.keys() else "",
        )
    else:
        body = (
            f"Hi {greeting},\n"
            "\n"
            "I'm with Green PayTech. We help businesses review payment processing costs "
            "and workflow with a clearer picture of the current setup.\n\n"
            f"Would a quick payment workflow review be useful for {company}?\n\n"
            "Gabriella\nGreen PayTech"
        )
    warning = "\n✅ Approved 2026-06-01 DOCX template body only; no generated opener."
    return (
        f"*Template:* {row['template_key'] or 'Standard1'}\n"
        f"*Sequence:* {sequence or 'general_standard'}\n"
        f"*Subject:* Gabriella / {company} Connect\n"
        f"---\n{body}\n---{warning}"
    )


def cmd_drafts(token, chat_id, who="operator"):
    """Show five exact standard-template drafts from the approval/send queue."""
    _note_session("drafts", "review", who)
    conn = _ledger_conn()
    if conn is None:
        send(token, chat_id, "⚠️ Pipeline DB not available."); return
    rows = conn.execute(
        """
        SELECT * FROM leads
        WHERE route='email' AND stage IN ('personalized', 'queued')
        ORDER BY CASE WHEN stage='personalized' THEN 0 ELSE 1 END,
                 COALESCE(propensity,0) DESC, id ASC
        LIMIT 5
        """
    ).fetchall()
    conn.close()
    if not rows:
        send(token, chat_id, "No standard-template drafts are ready yet.")
        return
    send(token, chat_id, f"*📝 {len(rows)} standard-template draft preview(s):*")
    for row in rows:
        # Sequence keys contain underscores, which Telegram's legacy Markdown
        # parser rejects. Draft previews are QA content, so send them literally.
        send(token, chat_id, f"Lead {row['id']} — {row['company']}\n" + _standard_draft(row),
             markdown=False)


def cmd_review(token, chat_id, who="operator"):
    """Send one inline-keyboard approval card per 'personalized' lead (cap 10)."""
    _note_session("review", "review", who)
    conn = _ledger_conn()
    if conn is None:
        send(token, chat_id, "⚠️ Pipeline DB not available — ledger not connected.")
        return

    try:
        rows = conn.execute(
            "SELECT * FROM leads WHERE stage='personalized' "
            "ORDER BY COALESCE(propensity,0) DESC LIMIT 10"
        ).fetchall()
    except Exception as e:
        send(token, chat_id, f"⚠️ Could not read pipeline leads: {e}")
        return

    if not rows:
        conn.close()
        totals, err = _run_orchestrator_once(limit=10)
        if err:
            send(token, chat_id, _format_action_result(
                "✅ Review queue",
                "Checked for personalized leads, then tried to move the pipeline once.",
                blocked=err,
                next_step="Fix the deterministic pipeline loop, then retry /review.",
            ))
            return
        conn = _ledger_conn()
        if conn is None:
            send(token, chat_id, "⚠️ Pipeline DB not available after pipeline run.")
            return
        rows = conn.execute(
            "SELECT * FROM leads WHERE stage='personalized' "
            "ORDER BY COALESCE(propensity,0) DESC LIMIT 10"
        ).fetchall()
        if not rows:
            counts = L.count_by_stage(conn)
            changed = ", ".join(f"{k}: {v}" for k, v in (totals or {}).items()) or "no staged rows moved"
            send(token, chat_id, _format_action_result(
                "✅ Review queue",
                "No personalized leads were ready, so I ran the no-send pipeline once.",
                changed=changed,
                blocked="Still no leads at `personalized`.",
                next_step=_pipeline_next_best_action(counts, len(ls.scheduled_calls(due_only=True, limit=100))),
            ))
            conn.close()
            return

    send(token, chat_id,
         f"*✅ {len(rows)} lead(s) ready for approval.* "
         f"Tap Send / Call / Skip on each, or Approve all:\n")

    for row in rows:
        lead_id = row["id"]
        card_text = ls.card(row)
        if qualification is not None:
            try:
                reviews = L.reviews_for(conn, lead_id) if L is not None else []
                card_text += "\n\n" + qualification.lead_evidence_card(row, reviews=reviews)
            except Exception as exc:
                card_text += f"\n\nEvidence card unavailable: {exc}"

        inline_kb = {
            "inline_keyboard": [
                [
                    {"text": "✅ Send",   "callback_data": f"appr:send:{lead_id}"},
                    {"text": "📞 Call",  "callback_data": f"appr:call:{lead_id}"},
                    {"text": "⏭️ Skip", "callback_data": f"appr:skip:{lead_id}"},
                ],
                [
                    {"text": "✅ Approve all remaining", "callback_data": "appr:allsend"},
                ],
            ]
        }
        params = dict(
            chat_id=chat_id,
            text=card_text[:4000],
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=inline_kb,
        )
        resp = tg(token, "sendMessage", **params)
        if not resp.get("ok"):
            params.pop("parse_mode", None)
            tg(token, "sendMessage", **params)
    conn.close()


def _handle_approval(token, chat_id, callback_query_id, data, message_id, who="operator"):
    """
    Parse callback_data and apply the correct DB transition.

    Scheme:
        appr:send:<id>   → advance lead to 'queued' (approved for Instantly)
        appr:call:<id>   → divert lead to call_list (manual_review reason)
        appr:skip:<id>   → set stage='skipped', route='skip'
        appr:allsend     → advance ALL current 'personalized' leads to 'queued'

    Returns a (confirm_text, ok) tuple — confirm_text replaces the card message.
    """
    conn = _ledger_conn()
    if conn is None:
        return "⚠️ DB unavailable", False

    now_iso = datetime.now(timezone.utc).isoformat()
    parts = data.split(":")

    try:
        action = parts[1] if len(parts) >= 2 else ""

        if action == "allsend":
            rows = conn.execute(
                """
                SELECT id FROM leads
                WHERE stage='personalized'
                  AND (trigger IS NULL OR trigger='' OR trigger_verified=1)
                """
            ).fetchall()
            count = 0
            for r in rows:
                L.advance(conn, r["id"], "queued", approved_at=now_iso)
                L.log_event(conn, "outreach", r["id"], "telegram_approved",
                            {"approved_by": who})
                count += 1
            return f"✅ Approved all — {count} lead(s) queued for sending.", True

        lead_id = int(parts[2]) if len(parts) >= 3 else None
        if lead_id is None:
            return "⚠️ Malformed callback (no lead id).", False

        if action == "send":
            row = conn.execute(
                "SELECT trigger, trigger_verified FROM leads WHERE id=?",
                (lead_id,),
            ).fetchone()
            if row and row["trigger"] and row["trigger_verified"] != 1:
                return "⚠️ Opener lacks recorded evidence. Refresh or clear it before approval.", False
            L.advance(conn, lead_id, "queued", approved_at=now_iso)
            L.log_event(conn, "outreach", lead_id, "telegram_approved",
                        {"approved_by": who})
            return "✅ Queued to send.", True

        if action == "call":
            L.divert_to_call_list(conn, lead_id, "manual_review")
            return "📞 Moved to call list.", True

        if action == "skip":
            L.set_fields(conn, lead_id, stage="skipped", route="skip")
            return "⏭️ Skipped.", True

        return f"⚠️ Unknown action '{action}'.", False

    except Exception as e:
        return f"⚠️ Error: {e}", False


def cmd_freeform(token, chat_id, text):
    total, by = ls.stats()
    facts = f"Lead pipeline: {total} total ({by}).\n{instantly_summary()}"
    reply = deepseek([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "OUTREACH CONTEXT FILES:\n" + load_context_pack()},
        {"role": "system", "content": "OPERATOR-SAFE SHARED CONTEXT:\n" + load_operator_shared_context()},
        {"role": "system", "content": "KNOWLEDGE:\n" + load_knowledge()},
        {"role": "system", "content": "CONTEXT (live facts):\n" + facts},
        {"role": "user", "content": text},
    ]) or "DeepSeek not configured."
    send(token, chat_id, reply)


# ── Router ───────────────────────────────────────────────────────────────────

LOG_TRIGGERS = ("called ", "/log", "spoke ", "talked ", "left a vm", "voicemail")
ACTION_PATTERNS = [
    ("onboarding", re.compile(r"\b(onboarding|onboard|walkthrough|how do i use outreach|new operator)\b", re.I)),
    ("session", re.compile(r"\b(session|i'?m doing|i am doing|we are doing|focus on|work on)\b", re.I)),
    ("brief", re.compile(r"\b(brief|session|what should i do|what more can you do|collaborate|workflow)\b", re.I)),
    ("protected_send", re.compile(r"\b(turn|switch|set|enable|start|activate)\b.*\b(sending|send gate|email sending|live sends?|campaigns?)\b", re.I)),
    ("supply_autopilot", re.compile(r"\b(increase|grow|build|fill|expand|scale|collect|get|source|scrape|pull)\b.*\b(email supply|lead supply|more leads|leads constantly|top of funnel|top-of-funnel|supply)\b", re.I)),
    ("supply_autopilot", re.compile(r"\b(scrape|source|pull|collect)\b.*\b(leads?|businesses|restaurants?|pharmacies|dealerships?)\b", re.I)),
    ("enrichment_quality", re.compile(r"\b(why|diagnose|report|explain|audit)\b.*\b(enrichment|email find|email quality|decision maker|quality low|low quality)\b", re.I)),
    ("enrichment_quality", re.compile(r"\b(enrichment|email find-rate|find rate|decision-maker|decision maker)\b.*\b(low|bad|weak|poor|quality|report|diagnosis)\b", re.I)),
    ("pipeline_bottleneck", re.compile(r"\b(why|where|what)\b.*\b(no|zero|0|empty|missing)\b.*\b(queue|review cards?|personalized|personalised|drafts?)\b", re.I)),
    ("pipeline_bottleneck", re.compile(r"\b(no|zero|0|empty)\b.*\b(queue|review cards?|personalized|personalised|drafts?)\b", re.I)),
    ("fill_review_queue", re.compile(r"\b(personali[sz]e|draft|prepare|get|fill|make|create|generate|queue up)\b.*\b(review|reviews|drafts?|personalized|personalised|enriched|verified|leads?|cards?)\b", re.I)),
    ("fill_review_queue", re.compile(r"\b(review|draft|personalized|personalised)\b.*\b(queue|ready|cards?|leads?)\b", re.I)),
    ("review", re.compile(r"\b(review|approve|approvals?|approval cards?)\b", re.I)),
    ("send_gmail", re.compile(r"^/?(sendgmail|gmail send|send (?:lead \d+|\d+)?\s*(?:through|via|with)?\s*gmail|send queued.*gmail)\b", re.I)),
    ("run_pipeline", re.compile(r"\b(run|continue|move|advance|process)\b.*\b(pipeline|orchestrator|waterfall|stages?)\b", re.I)),
    ("research", re.compile(r"^(research|scrape|enrich|diligence|run diligence on)\s+(\d+|lead\s+\d+|[a-z0-9][a-z0-9 .&'_-]{1,60})$", re.I)),
    ("drafts", re.compile(r"\b(drafts?|templates?|show me .*draft)\b", re.I)),
    ("context", re.compile(r"\b(shared context|context summary|operator context)\b", re.I)),
    ("sending", re.compile(r"\b(sending|send status|readiness|launch)\b", re.I)),
    ("calls", re.compile(r"\b(calls?|call queue|who .*call)\b", re.I)),
    ("pipeline", re.compile(r"\b(pipeline|funnel|what.*stuck|stuck)\b", re.I)),
]

CONFIRM_RE = re.compile(r"^(yes|yep|yeah|y|ok|okay|do it|run it|continue|more|again|go ahead|proceed|approved?)\.?$", re.I)
CANCEL_RE = re.compile(r"^(no|nope|cancel|stop|hold off|don't|dont)\.?$", re.I)


def _is_research_command(t):
    return bool(re.match(
        r"^(research|scrape|enrich|diligence|run diligence on)\s+(\d+|lead\s+\d+|[a-z0-9][a-z0-9 .&'_-]{1,60})$",
        t,
        flags=re.I,
    ))


def detect_action(text):
    """Map natural language to a small allowlist of deterministic actions."""
    raw = text.strip()
    t = re.sub(r"[^\x00-\x7f]", "", raw).lower().strip().lstrip("/").strip()
    if not t:
        return "freeform", raw
    if CONFIRM_RE.match(t):
        return "confirm_pending", raw
    if CANCEL_RE.match(t):
        return "cancel_pending", raw
    for action, pattern in ACTION_PATTERNS:
        if pattern.search(t):
            return action, raw
    if t.startswith(("show ", "about ", "lookup ", "find ")):
        return "show", raw
    return "freeform", raw


def build_structured_action(text):
    """Return the deterministic action Outreach should execute for natural text."""
    action, payload = detect_action(text)
    base = {
        "action": action,
        "limit": None,
        "vertical": "",
        "region": "",
        "reason": payload[:300],
        "requires_confirmation": False,
        "allowed_mutation": "none",
        "utterance": payload[:300],
    }
    if action == "supply_autopilot":
        base.update({
            "limit": _extract_supply_limit(payload, default=60),
            "allowed_mutation": "no_send_pipeline",
        })
        return base
    if action == "enrichment_quality":
        base.update({"allowed_mutation": "read_only"})
        return base
    if action == "pipeline_bottleneck":
        base.update({"allowed_mutation": "no_send_pipeline"})
        return base
    if action == "protected_send":
        base.update({"requires_confirmation": True, "allowed_mutation": "external_send_or_gate"})
        return base
    if action == "fill_review_queue":
        base.update({
            "limit": _extract_limit(payload, default=_target_review_limit()),
            "allowed_mutation": "no_send_pipeline",
        })
        return base
    if action == "run_pipeline":
        base.update({"limit": 25, "allowed_mutation": "no_send_pipeline"})
        return base
    if action in {"review", "calls", "pipeline", "drafts", "sending", "context", "onboarding"}:
        base.update({"allowed_mutation": "read_only"})
        return base
    return base


def _describe_pending_action(action):
    """Human-readable summary for the one pending action slot."""
    if not action:
        return ""
    name = action.get("action")
    if name == "fill_review_queue":
        return f"fill the review-ready queue up to {action.get('limit', 20)} personalized lead(s), without queuing or sending"
    if name == "supply_autopilot":
        return f"run no-send sourcing with limit {action.get('limit', 60)} until lead reserve improves"
    if name == "enrichment_quality":
        return "diagnose email enrichment quality from live pipeline data"
    if name == "pipeline_bottleneck":
        return "explain the current pipeline bottleneck and run the safe no-send next step"
    if name == "run_pipeline":
        return "run the no-send pipeline once"
    return name or "the pending action"


def _execute_structured_action(token, chat_id, action, who):
    """Execute one allowlisted structured action."""
    name = (action or {}).get("action")
    if name == "supply_autopilot":
        cmd_supply_autopilot(token, chat_id, limit=action.get("limit") or 60, who=who); return True
    if name == "enrichment_quality":
        cmd_enrichment_quality(token, chat_id, who=who); return True
    if name == "pipeline_bottleneck":
        cmd_pipeline_bottleneck(token, chat_id, who=who); return True
    if name == "protected_send":
        cmd_protected_send_refusal(token, chat_id, who=who); return True
    if name == "fill_review_queue":
        cmd_fill_review_queue(token, chat_id, limit=action.get("limit"), who=who); return True
    if name == "run_pipeline":
        cmd_run_pipeline(token, chat_id, limit=int(action.get("limit") or 25), who=who); return True
    if name == "review":
        cmd_review(token, chat_id, who=who); return True
    if name == "calls":
        _note_session("calls", "calls", who)
        cmd_uncalled(token, chat_id); return True
    if name == "pipeline":
        cmd_pipeline(token, chat_id, who=who); return True
    if name == "drafts":
        cmd_drafts(token, chat_id, who=who); return True
    if name == "sending":
        cmd_sending(token, chat_id, who=who); return True
    if name == "context":
        cmd_context(token, chat_id, who=who); return True
    if name == "onboarding":
        cmd_onboarding(token, chat_id, who=who); return True
    return False


def route(token, chat_id, text, who):
    # strip emojis (button labels include them) for command matching
    t = re.sub(r"[^\x00-\x7f]", "", text).lower().strip().lstrip("/").strip()
    detected_action, _detected_payload = detect_action(text)

    if t.startswith(("help", "menu", "start")):
        send(token, chat_id, HELP_TEXT, keyboard=True); return
    if detected_action == "cancel_pending":
        pending = _pop_pending_action()
        if pending:
            send(token, chat_id, f"Canceled pending action: {_describe_pending_action(pending)}.")
        else:
            send(token, chat_id, "No pending action to cancel.")
        return
    if detected_action == "confirm_pending":
        pending = _pop_pending_action()
        if pending and _execute_structured_action(token, chat_id, pending, who):
            return
        state = _current_session_state()
        last_action = (state.get("last_action") or "").strip()
        if last_action == "supply_autopilot":
            cmd_supply_autopilot(token, chat_id, limit=60, passes=2, who=who); return
        if last_action == "enrichment_quality":
            cmd_enrichment_quality(token, chat_id, who=who); return
        if last_action == "pipeline_bottleneck":
            cmd_pipeline_bottleneck(token, chat_id, who=who); return
        if last_action == "fill_review_queue":
            cmd_fill_review_queue(token, chat_id, limit=_target_review_limit(), who=who); return
        mode = session_state.normalize_mode(state.get("mode")) if session_state is not None else "general"
        if mode in ("pipeline", "review"):
            cmd_fill_review_queue(token, chat_id, limit=_target_review_limit(), who=who); return
        if mode == "calls":
            _note_session("calls", "calls", who)
            cmd_uncalled(token, chat_id); return
        send(token, chat_id, "I don't have a pending action. Say the goal directly, like `get 20 review cards ready`.")
        return
    if t in {"why", "why?"}:
        state = _current_session_state()
        last_action = (state.get("last_action") or "").strip()
        if last_action in {"supply_autopilot", "pipeline_bottleneck", "fill_review_queue"}:
            cmd_enrichment_quality(token, chat_id, who=who); return
    if t.startswith(("onboarding", "onboard")):
        cmd_onboarding(token, chat_id, who=who); return
    if text.lower().lstrip("/").startswith(LOG_TRIGGERS) or t.startswith("log "):
        cmd_log_call(token, chat_id, text, who); return
    if t.startswith("leads") or "new lead" in t or "today's lead" in t:
        cmd_leads(token, chat_id); return
    if t.startswith(("calls", "uncalled")) or "who" in t and "call" in t or "call queue" in t:
        _note_session("calls", "calls", who)
        cmd_uncalled(token, chat_id); return
    if t.startswith(("pipeline", "funnel")):
        cmd_pipeline(token, chat_id, who=who); return
    if t.startswith(("context", "shared context")):
        cmd_context(token, chat_id, who=who); return
    if t.startswith(("drafts", "draft ", "templates")) or "show me 5" in t and "draft" in t:
        cmd_drafts(token, chat_id, who=who); return
    if t.startswith(("sending", "send status")) or t == "send":
        cmd_sending(token, chat_id, who=who); return
    if t.startswith(("readiness", "ready", "launch")):
        cmd_readiness(token, chat_id, who=who); return
    if t.startswith("session") or t.startswith(("i am doing", "im doing", "i'm doing", "focus on", "work on")):
        cmd_session(token, chat_id, text, who=who); return
    if t.startswith(("brief", "session")):
        mode = "general"
        for candidate in ("review", "calls", "pipeline", "cleanup"):
            if candidate in t:
                mode = candidate
                break
        cmd_brief(token, chat_id, mode, who=who); return
    if t.startswith("status"):
        cmd_status(token, chat_id); return
    if t.startswith(("runpipeline", "run pipeline", "continue pipeline", "advance pipeline")):
        cmd_run_pipeline(token, chat_id, who=who); return
    if t.startswith(("personalize", "personalise", "fill review", "prepare drafts", "get drafts", "get review")):
        cmd_fill_review_queue(token, chat_id, limit=_extract_limit(text, default=_target_review_limit()), who=who); return
    if t.startswith(("sendgmail", "gmail send")):
        m = re.search(r"\b(\d+)\b", t)
        cmd_send_queued(token, chat_id, "gmail", int(m.group(1)) if m else None, who=who); return
    if _is_research_command(t):
        cmd_research(token, chat_id, text, who=who); return
    if t.startswith("review") or "approve" in t and "lead" in t:
        cmd_review(token, chat_id, who=who); return
    if t.startswith(("show ", "about ", "lookup ", "find ")):
        q = re.sub(r"^(show|about|lookup|find|tell me about)\s+", "", text, flags=re.I).strip()
        cmd_show(token, chat_id, q); return
    action, payload = detected_action, _detected_payload
    if action == "protected_send":
        cmd_protected_send_refusal(token, chat_id, who=who); return
    if action == "supply_autopilot":
        cmd_supply_autopilot(token, chat_id, limit=_extract_supply_limit(payload, default=60), who=who); return
    if action == "enrichment_quality":
        cmd_enrichment_quality(token, chat_id, who=who); return
    if action == "pipeline_bottleneck":
        cmd_pipeline_bottleneck(token, chat_id, who=who); return
    if action == "session":
        cmd_session(token, chat_id, payload, who=who); return
    if action == "fill_review_queue":
        cmd_fill_review_queue(token, chat_id, limit=_extract_limit(payload, default=_target_review_limit()), who=who); return
    if action == "review":
        cmd_review(token, chat_id, who=who); return
    if action == "brief":
        cmd_brief(token, chat_id, who=who); return
    if action == "send_gmail":
        m = re.search(r"\b(?:lead|id)?\s*(\d+)\b", payload, flags=re.I)
        cmd_send_queued(token, chat_id, "gmail", int(m.group(1)) if m else None, who=who); return
    if action == "run_pipeline":
        cmd_run_pipeline(token, chat_id, who=who); return
    if action == "research":
        cmd_research(token, chat_id, payload, who=who); return
    if action == "drafts":
        cmd_drafts(token, chat_id, who=who); return
    if action == "context":
        cmd_context(token, chat_id, who=who); return
    if action == "sending":
        cmd_sending(token, chat_id, who=who); return
    if action == "calls":
        _note_session("calls", "calls", who)
        cmd_uncalled(token, chat_id); return
    if action == "pipeline":
        cmd_pipeline(token, chat_id, who=who); return
    if action == "onboarding":
        cmd_onboarding(token, chat_id, who=who); return
    if action == "show":
        q = re.sub(r"^(show|about|lookup|find|tell me about)\s+", "", payload, flags=re.I).strip()
        cmd_show(token, chat_id, q); return
    cmd_freeform(token, chat_id, text)


# ── Main loop ────────────────────────────────────────────────────────────────

class _Bot:
    username = ""


bot = _Bot()


def main():
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()
    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 2

    me = tg(token, "getMe")
    bot.username = (me.get("result") or {}).get("username", "")

    # Register the "/" command menu (the blue Menu button) so both operators
    # see the commands without memorizing them.
    tg(token, "setMyCommands", commands=[
        {"command": "calls",    "description": "Due follow-ups + call queue"},
        {"command": "leads",    "description": "Fresh leads (tap # to dial)"},
        {"command": "onboarding", "description": "Walk a new operator through Outreach"},
        {"command": "drafts",   "description": "Show 5 standard-template drafts"},
        {"command": "review",   "description": "Approve / call / skip personalized leads"},
        {"command": "pipeline", "description": "Email funnel — counts by stage"},
        {"command": "sending",  "description": "Is cold email live? + Instantly stats"},
        {"command": "brief",    "description": "Session brief + next workflow question"},
        {"command": "context",  "description": "Operator-safe shared context"},
        {"command": "session",  "description": "Set workflow mode/objective"},
        {"command": "readiness","description": "Validate launch routes + cap"},
        {"command": "research", "description": "Run pipeline on an existing lead"},
        {"command": "runpipeline", "description": "Advance the deterministic pipeline once"},
        {"command": "sendgmail", "description": "Send queued approvals through Gmail"},
        {"command": "status",   "description": "Pipeline + email stats"},
        {"command": "help",     "description": "Show all commands + buttons"},
    ])
    # Push the persistent button keyboard to the team chat once at startup.
    send(token, chat_id, "⚡ Outreach online. Buttons are pinned below 👇", keyboard=True)
    print(f"[outreach] online as @{bot.username} (chat_id={chat_id})", flush=True)

    offset = 0
    while True:
        resp = tg(token, "getUpdates", offset=offset, timeout=POLL_TIMEOUT)
        for update in resp.get("result", []):
            offset = update["update_id"] + 1

            # ── Inline-keyboard callback (approval card taps) ─────────────
            cb = update.get("callback_query")
            if cb:
                try:
                    cb_id      = cb.get("id", "")
                    cb_data    = (cb.get("data") or "").strip()
                    cb_msg     = cb.get("message") or {}
                    _record_private_operator_chat_fields(
                        cb_msg.get("chat") or {},
                        cb.get("from") or {},
                    )
                    cb_chat_id = str((cb_msg.get("chat") or {}).get("id", chat_id))
                    cb_msg_id  = cb_msg.get("message_id")
                    cb_who     = (cb.get("from") or {}).get("first_name", "operator")
                    print(f"[outreach] {cb_who} callback → {cb_data}", flush=True)

                    if cb_data.startswith("appr:"):
                        confirm, ok = _handle_approval(
                            token, cb_chat_id, cb_id, cb_data, cb_msg_id, cb_who)
                        # Acknowledge the callback (removes the spinner in Telegram).
                        tg(token, "answerCallbackQuery",
                           callback_query_id=cb_id,
                           text=confirm[:200])
                        # Edit the original card message to show the decision.
                        if cb_msg_id:
                            tg(token, "editMessageText",
                               chat_id=cb_chat_id,
                               message_id=cb_msg_id,
                               text=confirm,
                               parse_mode="Markdown")
                    else:
                        # Unknown callback — acknowledge silently so Telegram stops spinning.
                        tg(token, "answerCallbackQuery", callback_query_id=cb_id)
                except Exception as e:
                    print(f"[outreach] callback error: {e}", flush=True)
                    try:
                        tg(token, "answerCallbackQuery",
                           callback_query_id=(update.get("callback_query") or {}).get("id", ""),
                           text="⚠️ error processing action")
                    except Exception:
                        pass
                continue  # done with this update — do NOT fall through to message handler

            # ── Regular message ───────────────────────────────────────────
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            chat = msg.get("chat", {})
            if str(chat.get("id", "")) != str(chat_id) and chat.get("type") != "private":
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            _record_private_operator_chat(msg)

            # Button presses arrive as "📞 /calls" — strip emoji before checking
            _stripped  = re.sub(r"[^\x00-\x7f]", "", text).strip()
            is_cmd     = _stripped.startswith("/") or text.startswith("/")
            is_mention = f"@{bot.username}" in text
            is_dm      = chat.get("type") == "private"
            if not (is_cmd or is_mention or is_dm):
                continue

            who  = (msg.get("from", {}) or {}).get("first_name", "operator")
            text = text.replace(f"@{bot.username}", "").strip()
            print(f"[outreach] {who} → {text[:80]}", flush=True)
            try:
                route(token, str(chat.get("id")), text, who)
            except Exception as e:
                print(f"[outreach] handle error: {e}", flush=True)
                send(token, str(chat.get("id")), f"⚠️ error: {e}")
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
