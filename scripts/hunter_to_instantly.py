#!/usr/bin/env python3
"""
hunter_to_instantly.py — Hunter enrichment → quality gates → Instantly upload.

Replaces apollo_to_instantly.py. Finds owner emails via Hunter's domain-search
API, runs 8 quality gates, optionally writes a personalised Trigger line via
DeepSeek (using real review-count / rating signals from the DB — not generic
category filler), then either exports a CSV or pushes leads directly to an
Instantly campaign via the V2 API.

PIPELINE (per lead):
  1. pull leads.db  (status='new', no email, has website)
  2. pre-Hunter gate  (no domain / personal webmail / suppression)
  3. Hunter domain search  → highest-confidence personal email
  4. generic-inbox gate   (info@, sales@, etc.)
  5. dedup against seen-uploaded ledger (protects 1,000-contact cap)
  6. SMTP verify via email_verifier.py
  7. [--personalize]  DeepSeek writes 1-sentence Trigger from real signals
  8. write email back to leads.db (status → 'enriched')
  9. export CSV  OR  --push → Instantly V2 leads API
 10. [--notify]  Telegram summary

FLAGS:
  --dry-run          print what would happen, write nothing
  --limit N          max Hunter API calls this run  (default 20)
  --vertical V       only process this vertical
  --out FILE         CSV output path  (default ready_for_instantly.csv)
  --push             push leads to Instantly campaign via API (needs INSTANTLY_CAMPAIGN_ID)
  --commit           mark passing emails in seen-uploaded ledger after upload
  --notify           push Telegram summary when done
  --personalize      call DeepSeek to write a custom Trigger line per lead

ENV:
  HUNTER_API_KEY          required
  INSTANTLY_API_KEY       required for --push / --notify stats
  INSTANTLY_CAMPAIGN_ID   required for --push
  TELEGRAM_BOT_TOKEN      required for --notify
  TELEGRAM_CHAT_ID        required for --notify
  DEEPSEEK_API_KEY        required for --personalize
  EMAIL_VERIFIER_PATH     defaults to ~/.outreach/scripts/email_verifier.py
  LEAD_DB                 defaults to ~/.outreach/state/leads.db

Stdlib only.
"""

import os
import re
import sys
import csv
import json
import time
import argparse
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lead_store as ls
from outreach_templates import build_personalization_prompt
try:
    import daily_brief as db
except Exception:
    db = None

# ── Constants ─────────────────────────────────────────────────────────────────

HUNTER_DOMAIN_SEARCH = "https://api.hunter.io/v2/domain-search"
HUNTER_EMAIL_FINDER  = "https://api.hunter.io/v2/email-finder"
INSTANTLY_LEADS_URL  = "https://api.instantly.ai/api/v2/leads"
INSTANTLY_LEADS_ADD_URL = "https://api.instantly.ai/api/v2/leads/add"
DEEPSEEK_URL         = "https://api.deepseek.com/chat/completions"

SUPPRESS_FILE    = Path.home() / ".outreach" / "state" / "suppression.txt"
SEEN_UPLOADED    = Path.home() / ".outreach" / "state" / "seen-uploaded.jsonl"
HUNTER_MISSES    = Path.home() / ".outreach" / "state" / "hunter-misses.txt"
DEFAULT_VERIFIER = Path.home() / ".outreach" / "scripts" / "email_verifier.py"

GENERIC_PREFIXES = ("info", "contact", "sales", "hello", "support", "admin",
                    "office", "mail", "team", "noreply", "no-reply", "help",
                    "service", "billing", "accounts", "enquiries", "enquiry",
                    "reception", "front", "general", "webmaster", "postmaster")

OWNER_TITLES = {"owner", "founder", "co-founder", "president", "principal",
                "ceo", "proprietor", "general manager", "managing partner",
                "director", "partner", "managing director"}

# Instantly custom-variable column names (map to {{VarName}} in templates)
INSTANTLY_CUSTOM_VARS = [
    "Phone", "Website", "CityState", "Vertical", "LeadId",
    "Trigger", "KeySignal", "PainHyp", "Angle", "CallHook", "CallPriority",
]

CSV_COLS = ["Email", "FirstName", "LastName", "CompanyName", "Title",
            "Phone", "Website", "LinkedIn", "CityState", "Vertical",
            "LeadId", "HunterId", "SourceUrl", "Trigger", "KeySignal",
            "PainHyp", "Angle", "CallHook", "CallPriority",
            "SenderName", "Calendly", "ImageURL"]

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(url, params, timeout=30):
    from urllib.parse import urlencode
    full = f"{url}?{urlencode(params)}"
    req = urllib.request.Request(full)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}")


def _post_json(url, headers, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")


# ── Suppression / dedup ───────────────────────────────────────────────────────

def load_suppression():
    emails, domains = set(), set()
    if SUPPRESS_FILE.exists():
        for line in SUPPRESS_FILE.read_text().splitlines():
            line = line.strip().lower()
            if not line or line.startswith("#"):
                continue
            if line.startswith("@"):
                domains.add(line[1:])
            else:
                emails.add(line)
    return emails, domains


def load_hunter_misses():
    """Domains Hunter already returned no-result for. Skip until --retry-misses."""
    if not HUNTER_MISSES.exists():
        return set()
    return {l.strip().lower() for l in HUNTER_MISSES.read_text().splitlines() if l.strip()}


def record_hunter_miss(domain):
    HUNTER_MISSES.parent.mkdir(parents=True, exist_ok=True)
    with HUNTER_MISSES.open("a") as f:
        f.write(domain.lower() + "\n")


def load_seen_uploaded():
    seen = set()
    if SEEN_UPLOADED.exists():
        for line in SEEN_UPLOADED.read_text().splitlines():
            try:
                rec = json.loads(line)
                if rec.get("email"):
                    seen.add(rec["email"].lower())
            except Exception:
                pass
    return seen


def commit_uploaded(emails):
    SEEN_UPLOADED.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with SEEN_UPLOADED.open("a") as f:
        for e in emails:
            f.write(json.dumps({"email": e.lower(), "uploaded_at": now}) + "\n")


# ── Domain / email helpers ────────────────────────────────────────────────────

def extract_domain(website):
    if not website:
        return ""
    m = re.search(r"https?://(?:www\.)?([^/?#]+)", website.strip(), re.I)
    return m.group(1).lower() if m else ""


def is_generic(email):
    local = email.split("@")[0].lower()
    return local.startswith(GENERIC_PREFIXES)


def pre_hunter_gate(row, sup_emails, sup_domains):
    """Returns (pass:bool, reason:str). Never spends a Hunter credit on a fail."""
    domain = extract_domain(row["website"])
    if not domain:
        return False, "no_domain"
    if any(domain.endswith(d) for d in
           ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
            # Social platforms — not a real business inbox
            "instagram.com", "facebook.com", "twitter.com", "linkedin.com",
            # POS / booking / delivery platforms — not the business's own email
            "toasttab.com", "opentable.com", "yelp.com", "grubhub.com",
            "doordash.com", "squareup.com", "clover.com",
            # Link aggregators / social bios — not real inboxes
            "linktr.ee", "linktree.com", "bio.site", "beacons.ai")):
        return False, "personal_webmail"
    if domain in sup_domains:
        return False, "suppressed_domain"
    if row["email"] and row["email"].lower() in sup_emails:
        return False, "suppressed_email"
    return True, "ok"


# ── Hunter enrichment ─────────────────────────────────────────────────────────

def hunter_find_email(api_key, domain):
    """
    Domain search → pick the highest-confidence personal email that looks like
    an owner/founder. Falls back to highest-confidence personal email of any
    title. Returns dict with email, first_name, last_name, title, hunter_id
    or None if nothing useful found.
    """
    try:
        resp = _get(HUNTER_DOMAIN_SEARCH, {
            "domain": domain,
            "limit": 10,
            "type": "personal",   # exclude generic inboxes at the source
            "api_key": api_key,
        })
    except RuntimeError as e:
        return {"_error": str(e)}

    emails = (resp.get("data") or {}).get("emails") or []
    if not emails:
        return None

    # Sort by owner-title match first, then confidence descending
    def rank(e):
        title = (e.get("position") or "").lower()
        is_owner = any(t in title for t in OWNER_TITLES)
        return (1 if is_owner else 0, e.get("confidence", 0))

    emails.sort(key=rank, reverse=True)
    best = emails[0]
    email = (best.get("value") or "").strip().lower()
    if not email:
        return None

    return {
        "email":      email,
        "first_name": best.get("first_name") or "",
        "last_name":  best.get("last_name")  or "",
        "title":      best.get("position")   or "",
        "hunter_id":  str(best.get("id", "") or ""),
        "confidence": best.get("confidence", 0),
    }


# ── SMTP verification ─────────────────────────────────────────────────────────

def verify_email(email, verifier_path):
    vp = Path(verifier_path)
    if not vp.exists():
        return "verifier_missing", -1
    try:
        r = subprocess.run([sys.executable, str(vp), email],
                           capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        return "timeout", -1
    codes = {0: "verified", 10: "generic", 11: "masked", 20: "no_mx",
             21: "mx_timeout", 30: "no_mailbox", 31: "smtp_error", 32: "greylisted"}
    return codes.get(r.returncode, f"exit_{r.returncode}"), r.returncode


# ── DeepSeek personalisation ──────────────────────────────────────────────────

def personalise_lead(company, vertical, city_state, review_count, rating,
                     pain_theme="", processor="", filing_date=""):
    """
    Return one template-routed Trigger sentence or '' on any failure.

    The runtime prompt comes from templates/outreach_library.json through
    outreach_templates.py. Keep vertical knowledge in that registry and its
    linked markdown playbooks instead of adding prompt branches here.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return ""
    try:
        system_prompt, user_msg, _route = build_personalization_prompt(
            company=company,
            vertical=vertical,
            city_state=city_state,
            review_count=review_count,
            rating=rating,
            pain_theme=pain_theme,
            processor=processor,
            filing_date=filing_date,
        )
        body = {
            "model": "deepseek-chat",
            "max_tokens": 60,
            "temperature": 0.7,
            "top_p": 0.9,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
        }
        resp = _post_json(DEEPSEEK_URL, {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, body, timeout=15)
        raw = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not raw:
            return ""
        # Truncate to first full sentence — split on ". " or ".\n" not on
        # decimal points like "4.2 stars" which would produce "A 4"
        import re as _re
        m = _re.split(r'(?<=\w\w)\.\s', raw)  # split only after word chars
        sentence = m[0].strip() if m else raw.strip()
        # fallback: if still looks truncated (under 8 words), return blank
        if len(sentence.split()) < 8 or len(sentence.split()) > 40:
            print(f"[personalize] WARN: bad sentence for {company}: {sentence[:40]!r}", file=sys.stderr)
            return ""
        return sentence + ("." if not sentence.endswith(".") else "")
    except Exception as e:
        print(f"[personalize] WARN: {company} → {type(e).__name__}: {e}", file=sys.stderr)
        return ""


# ── Instantly API push ────────────────────────────────────────────────────────

def push_to_instantly(api_key, campaign_id, leads_batch):
    """
    POST leads directly into an Instantly campaign via V2 API.
    leads_batch: list of dicts with email + custom vars.
    Returns number successfully pushed.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    def _lead_payload(lead):
        standard = {
            "email", "first_name", "last_name", "company_name",
            "personalization", "phone", "website", "job_title",
        }
        out = {k: v for k, v in lead.items() if k in standard and v not in (None, "")}
        if "phone" not in out and lead.get("Phone"):
            out["phone"] = lead["Phone"]
        if "website" not in out and lead.get("Website"):
            out["website"] = lead["Website"]
        custom = {
            k: v for k, v in lead.items()
            if k not in standard and k not in {"Phone", "Website"} and v not in (None, "")
        }
        if custom:
            out["custom_variables"] = custom
        return out

    pushed = 0
    # Instantly accepts up to 1,000 leads per request; batch by 100 to be safe
    chunk_size = 100
    for i in range(0, len(leads_batch), chunk_size):
        chunk = [_lead_payload(lead) for lead in leads_batch[i:i + chunk_size]]
        body = {
            "campaign_id": campaign_id,
            "skip_if_in_workspace": True,   # dedup at workspace level
            "skip_if_in_campaign": True,
            "leads": chunk,
        }
        try:
            resp = _post_json(INSTANTLY_LEADS_ADD_URL, headers, body)
            # V2 returns {"status": "success", "total_new_leads": N}
            pushed += resp.get("total_new_leads", len(chunk))
        except RuntimeError as e:
            print(f"[instantly] ERROR pushing batch: {e}", file=sys.stderr)
    return pushed


def build_instantly_lead(row, owner):
    """Build the dict Instantly's V2 API expects from a DB row + Hunter result."""
    return {
        "email":        owner["email"],
        "first_name":   owner["first_name"],
        "last_name":    owner["last_name"],
        "company_name": row["company"],
        "personalization": owner.get("trigger", ""),   # maps to {{personalization}}
        "phone":        row["phone"] or "",
        "website":      row["website"] or "",
        # Custom variables surfaced as {{VarName}} in templates
        "Phone":        row["phone"] or "",
        "Website":      row["website"] or "",
        "CityState":    row["city_state"] or "",
        "Vertical":     row["vertical"] or "",
        "LeadId":       row["lead_ref"] or "",
        "Trigger":      owner.get("trigger", ""),
        "CallPriority": str(row["call_priority"] or ""),
    }


# ── CSV export ────────────────────────────────────────────────────────────────

def build_csv_row(row, owner):
    out = {c: "" for c in CSV_COLS}
    out.update({
        "Email":       owner["email"],
        "FirstName":   owner["first_name"],
        "LastName":    owner["last_name"],
        "CompanyName": row["company"],
        "Title":       owner["title"],
        "Phone":       row["phone"] or "",
        "Website":     row["website"] or "",
        "CityState":   row["city_state"] or "",
        "Vertical":    row["vertical"] or "",
        "LeadId":      row["lead_ref"] or "",
        "HunterId":    owner["hunter_id"],
        "SourceUrl":   row["website"] or "",
        "Trigger":     owner.get("trigger", ""),
        "CallPriority": str(row["call_priority"] or ""),
    })
    return out


# ── Telegram notify ───────────────────────────────────────────────────────────

def post_telegram(token, chat_id, text):
    if db:
        try:
            db.post_telegram(token, chat_id, text)
            return
        except Exception:
            pass
    # Fallback if daily_brief not available
    body = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[telegram] warn: {e}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Hunter → quality gates → Instantly pipeline")
    ap.add_argument("--dry-run",     action="store_true", help="print, no writes")
    ap.add_argument("--limit",       type=int, default=20, help="max Hunter API calls")
    ap.add_argument("--vertical",    help="only process this vertical")
    ap.add_argument("--out",         default="ready_for_instantly.csv")
    ap.add_argument("--push",        action="store_true",
                    help="push leads to Instantly campaign via API (needs INSTANTLY_CAMPAIGN_ID)")
    ap.add_argument("--commit",      action="store_true",
                    help="mark passing emails in seen-uploaded ledger")
    ap.add_argument("--notify",      action="store_true", help="push Telegram summary")
    ap.add_argument("--personalize", action="store_true",
                    help="call DeepSeek to write a custom Trigger per lead")
    ap.add_argument("--retry-misses", action="store_true",
                    help="ignore the hunter-misses cache and retry all leads")
    ap.add_argument("--min-priority", type=int, default=0,
                    help="only spend a Hunter search on leads with CallPriority >= N "
                         "(conserves searches; established/high-review leads have "
                         "findable emails AND convert better). Try 40.")
    args = ap.parse_args()

    hunter_key   = os.environ.get("HUNTER_API_KEY",         "").strip()
    inst_key     = os.environ.get("INSTANTLY_API_KEY",       "").strip()
    campaign_id  = os.environ.get("INSTANTLY_CAMPAIGN_ID",  "").strip()
    tg_token     = os.environ.get("TELEGRAM_BOT_TOKEN",      "").strip()
    tg_chat      = os.environ.get("TELEGRAM_CHAT_ID",        "").strip()
    verifier     = os.environ.get("EMAIL_VERIFIER_PATH",     str(DEFAULT_VERIFIER))

    if not hunter_key:
        print("ERROR: HUNTER_API_KEY not set", file=sys.stderr); return 2
    if args.push and not campaign_id:
        print("ERROR: --push requires INSTANTLY_CAMPAIGN_ID", file=sys.stderr); return 2

    sup_emails, sup_domains = load_suppression()
    seen_uploaded  = load_seen_uploaded()
    hunter_misses  = set() if args.retry_misses else load_hunter_misses()
    cap_used       = len(seen_uploaded)
    cap_remaining  = max(0, 1000 - cap_used)

    if cap_remaining == 0:
        msg = "⚠️ Instantly Growth cap FULL (1,000/1,000). Remove exhausted leads before uploading more."
        print(msg)
        if args.notify and tg_token and tg_chat:
            post_telegram(tg_token, tg_chat, msg)
        return 1

    # Pull candidates
    conn = ls.connect()
    q    = "SELECT * FROM leads WHERE status='new' AND (email IS NULL OR email='') AND website != ''"
    prms = []
    if args.vertical:
        q += " AND vertical=?"; prms.append(args.vertical)
    q += " ORDER BY COALESCE(call_priority,0) DESC, created_at ASC"
    candidates = conn.execute(q, prms).fetchall()
    conn.close()

    print(f"[hunter] {len(candidates)} candidates · cap {cap_used}/1000 used · "
          f"limit {args.limit} calls · {'DRY RUN ' if args.dry_run else ''}"
          f"{'personalize ON' if args.personalize else 'personalize OFF'}")

    stats = {"candidates": len(candidates), "pre_gate": 0, "no_email": 0,
             "generic": 0, "verify_fail": 0, "dupe": 0, "passed": 0, "personalized": 0}

    ready_rows   = []    # for CSV
    instant_leads= []    # for API push
    committed    = []    # emails to mark in ledger

    hunter_calls = 0
    for row in candidates:
        if hunter_calls >= args.limit:
            print(f"[hunter] limit reached ({args.limit}). Run again for more.")
            break
        if len(ready_rows) >= cap_remaining:
            print(f"[hunter] Instantly cap nearly full. Stopping at {cap_used + len(ready_rows)}/1000.")
            break

        # 1. Pre-Hunter gate
        ok, reason = pre_hunter_gate(row, sup_emails, sup_domains)
        if not ok:
            stats["pre_gate"] += 1
            print(f"  SKIP pre-gate [{reason}]: {row['company']}")
            continue

        domain = extract_domain(row["website"])

        # Skip domains Hunter already returned no-result for
        if domain and domain in hunter_misses:
            stats["no_email"] += 1
            continue

        # Conserve searches: skip low-fit leads. They rarely have findable
        # emails and they're call-only targets anyway (use /calls for them).
        lead_prio = (row["call_priority"] if "call_priority" in row.keys() else 0) or 0
        if lead_prio < args.min_priority:
            stats.setdefault("below_priority", 0)
            stats["below_priority"] += 1
            continue

        # 2. Hunter
        if args.dry_run:
            owner = {"email": f"owner@{domain}", "first_name": "Demo",
                     "last_name": "Owner", "title": "Owner",
                     "hunter_id": "dry", "confidence": 99}
        else:
            owner = hunter_find_email(hunter_key, domain)
            hunter_calls += 1
            time.sleep(0.5)

        if not owner or owner.get("_error"):
            stats["no_email"] += 1
            err = (owner or {}).get("_error", "no result")
            print(f"  SKIP hunter-error: {row['company']} ({err})")
            if not args.dry_run and domain:
                record_hunter_miss(domain)
                hunter_misses.add(domain)
            continue

        email = owner["email"]
        if not email:
            stats["no_email"] += 1
            print(f"  SKIP no-email: {row['company']}")
            if not args.dry_run and domain:
                record_hunter_miss(domain)
                hunter_misses.add(domain)
            continue

        # 3. Confidence floor — low-confidence guesses bounce more than they convert
        if owner.get("confidence", 100) < 60:
            stats["verify_fail"] += 1
            print(f"  SKIP low-conf [{owner['confidence']}%]: {row['company']} → {email}")
            if not args.dry_run and domain:
                record_hunter_miss(domain)
                hunter_misses.add(domain)
            continue

        # 4. Generic gate
        if is_generic(email):
            stats["generic"] += 1
            print(f"  SKIP generic: {row['company']} → {email}")
            continue

        # 4. Dedup
        if email in seen_uploaded:
            stats["dupe"] += 1
            print(f"  SKIP dupe: {row['company']} → {email}")
            continue

        # 5. SMTP verify
        if args.dry_run:
            vstatus = "verified"
        else:
            vstatus, vcode = verify_email(email, verifier)

        if vstatus != "verified":
            stats["verify_fail"] += 1
            print(f"  SKIP verify-{vstatus}: {row['company']} → {email}")
            continue

        # 6. Personalize (optional)
        trigger = ""
        if args.personalize and not args.dry_run:
            trigger = personalise_lead(
                company     = row["company"],
                vertical    = row["vertical"] or "",
                city_state  = row["city_state"] or "",
                review_count= row["review_count"] if "review_count" in row.keys() else None,
                rating      = row["rating"]       if "rating"       in row.keys() else None,
            )
            if trigger:
                stats["personalized"] += 1
                print(f"  ✏️  Trigger: {trigger[:80]}")
            time.sleep(1.5)  # gentle DeepSeek pacing
        owner["trigger"] = trigger

        # ✅ PASSED every gate
        if not args.dry_run:
            conn = ls.connect()
            conn.execute(
                "UPDATE leads SET email=?, contact_name=?, status='enriched' WHERE id=?",
                (email,
                 " ".join(x for x in [owner["first_name"], owner["last_name"]] if x).strip()
                 or row["contact_name"],
                 row["id"]))
            conn.commit(); conn.close()

        ready_rows.append(build_csv_row(row, owner))
        instant_leads.append(build_instantly_lead(row, owner))
        committed.append(email)
        stats["passed"] += 1
        print(f"  ✅ PASS [{owner['confidence']}% conf]: {row['company']} → {email}")

    # ── Output ────────────────────────────────────────────────────────────────
    pushed = 0
    if not args.dry_run and ready_rows:
        if args.push:
            if not inst_key:
                print("ERROR: --push requires INSTANTLY_API_KEY", file=sys.stderr)
            else:
                pushed = push_to_instantly(inst_key, campaign_id, instant_leads)
                print(f"\n[ok] {pushed} leads pushed to Instantly campaign {campaign_id}")
        else:
            with open(args.out, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_COLS)
                w.writeheader(); w.writerows(ready_rows)
            print(f"\n[ok] {len(ready_rows)} leads → {args.out}")
            print(f"[ok] Upload CSV to Instantly, then re-run with --commit to lock the cap.")

    if args.commit and committed and not args.dry_run:
        commit_uploaded(committed)
        print(f"[ok] {len(committed)} emails committed to uploaded ledger.")

    new_cap = cap_used + (len(committed) if args.commit else 0)
    print(
        f"\n{'[DRY RUN] ' if args.dry_run else ''}"
        f"Hunter pipeline: {stats['candidates']} candidates → {stats['passed']} passed\n"
        f"  pre-gate: {stats['pre_gate']} · no-email: {stats['no_email']} · "
        f"generic: {stats['generic']} · verify-fail: {stats['verify_fail']} · "
        f"dupe: {stats['dupe']}\n"
        f"  personalized: {stats['personalized']}/{stats['passed']}\n"
        f"  Instantly cap: {new_cap}/1000 used"
    )

    if args.notify and tg_token and tg_chat and ready_rows:
        action = f"pushed to campaign" if args.push else f"ready in `{args.out}`"
        msg = (
            f"✅ *Hunter pipeline done*\n"
            f"{stats['passed']} leads verified → {action}\n"
            f"Personalized: {stats['personalized']}/{stats['passed']}\n"
            f"Instantly cap: {new_cap}/1000"
            + (f"\nUpload the CSV, then run with `--commit`" if not args.push else "")
        )
        post_telegram(tg_token, tg_chat, msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
