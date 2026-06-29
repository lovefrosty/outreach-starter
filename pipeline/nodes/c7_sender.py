#!/usr/bin/env python3
"""
c7_sender.py — C7 Sender node.

Reads leads at stage 'queued' (human-approved — the approval gate advanced
personalized → queued externally) and sends them through the configured
provider. Instantly remains the default provider; Gmail is a side rail for
short manual sending periods.

Pre-send gates (in order):
  1. Suppression       — reuse load_suppression from hunter_to_instantly.py
  2. Already-uploaded  — reuse load_seen_uploaded / SEEN_UPLOADED ledger
  3. Daily cap         — env PIPELINE_DAILY_SEND_CAP (default 30); count
                         'sent' outcomes today; if at cap, leave at 'queued',
                         return False (sends tomorrow on next pass)

On success:
  - ledger.advance(conn, id, "sent")
  - commit_uploaded([email])
  - ledger.record_outcome(conn, id, "sent")
  - ledger.log_cost(conn, "sent", provider, 1, 0.0)

On API failure:
  - Leave at 'queued', log error, return False (retry next pass)

Missing env keys (INSTANTLY_API_KEY / INSTANTLY_CAMPAIGN_ID):
  - Print a clear message, return False — sending is dormant until warmup.
  - Never crash.

Custom variables sent to Instantly ({{VarName}} in templates):
  Phone, Website, CityState, Vertical, LeadId, Trigger, CallPriority,
  PainTier, PainTheme, TemplateKey, TemplateRoute, SequenceKey,
  EmailAngle, TemplateCTA

ENV:
  LEAD_DB                     defaults to lead_store default
  SEND_PROVIDER               instantly (default) or gmail
  INSTANTLY_API_KEY           required for live sends
  INSTANTLY_CAMPAIGN_ID       default campaign fallback for live sends
  INSTANTLY_CAMPAIGN_ID_<SEQUENCE_KEY>
                              optional sequence-specific campaign override
  PIPELINE_DAILY_SEND_CAP     max sends per calendar day (default 30)
  PIPELINE_POST_EMAIL_CALL_DAYS
                              delay before cold-call follow-up (default 3)
  GMAIL_ACCESS_TOKEN          optional short-lived Gmail OAuth token
  GMAIL_CLIENT_ID             Gmail OAuth refresh flow
  GMAIL_CLIENT_SECRET         Gmail OAuth refresh flow
  GMAIL_REFRESH_TOKEN         Gmail OAuth refresh flow
  GMAIL_FROM                  optional From header; Gmail may override
  GMAIL_REPLY_TO              optional Reply-To header

Usage:
  python3 pipeline/nodes/c7_sender.py              # process batch
  python3 pipeline/nodes/c7_sender.py --dry-run    # print payload, no API call
  python3 pipeline/nodes/c7_sender.py --limit N
  python3 pipeline/nodes/c7_sender.py --lead-id 529 --lead-id 530
"""

import os
import re
import sys
import argparse
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ── Path bootstrap (orchestrator-importable AND standalone-runnable) ──────────
_PIPE = Path(__file__).resolve().parents[1]          # workspace/pipeline/
sys.path.insert(0, str(_PIPE))                       # import ledger
sys.path.insert(0, str(_PIPE.parent / "scripts"))    # reuse hunter_to_instantly

import ledger as L
from approved_email_templates import APPROVED_SEQUENCES, first_touch_body
from hunter_to_instantly import (
    load_suppression,
    load_seen_uploaded,
    commit_uploaded,
    push_to_instantly,
    SEEN_UPLOADED,
)
try:
    from providers import gmail_sender
except Exception:
    gmail_sender = None

# ── Default cap ───────────────────────────────────────────────────────────────
_DEFAULT_DAILY_CAP = 30
VALID_SEND_PROVIDERS = {"instantly", "gmail"}


def _send_provider():
    provider = os.environ.get("SEND_PROVIDER", "instantly").strip().lower()
    return provider if provider in VALID_SEND_PROVIDERS else "instantly"


# ── Campaign routing ─────────────────────────────────────────────────────────

def _campaign_id_for(row):
    """
    Pick the Instantly campaign for this swarm template sequence.

    A sequence-specific env var wins, for example:
      INSTANTLY_CAMPAIGN_ID_RESTAURANT_DEFAULT

    INSTANTLY_CAMPAIGN_ID remains the backwards-compatible fallback while the
    compiled campaigns are being created in Instantly.
    """
    sequence_key = (row["sequence_key"] or "").strip() if row["sequence_key"] else ""
    suffix = re.sub(r"[^A-Z0-9]+", "_", sequence_key.upper()).strip("_")
    specific = os.environ.get(f"INSTANTLY_CAMPAIGN_ID_{suffix}", "").strip() if suffix else ""
    return specific or os.environ.get("INSTANTLY_CAMPAIGN_ID", "").strip()


def _specific_campaign_id_for(row):
    """Sequence-specific campaign only (no fallback).

    Returns "" when no INSTANTLY_CAMPAIGN_ID_<SEQUENCE_KEY> is configured. The
    bare INSTANTLY_CAMPAIGN_ID fallback is NOT used for live sends because the
    active API key has no access to it (HTTP 403), which silently strands the
    lead at the queued stage.
    """
    sequence_key = (row["sequence_key"] or "").strip() if row["sequence_key"] else ""
    suffix = re.sub(r"[^A-Z0-9]+", "_", sequence_key.upper()).strip("_")
    return os.environ.get(f"INSTANTLY_CAMPAIGN_ID_{suffix}", "").strip() if suffix else ""


# ── Daily send count ──────────────────────────────────────────────────────────

def _sent_today(conn):
    """Count 'sent' outcomes recorded today (UTC calendar day)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) n FROM outcomes WHERE event='sent' AND ts LIKE ?",
        (today + "%",)
    ).fetchone()
    return row["n"] if row else 0


# ── Build the Instantly lead dict for this row ────────────────────────────────

def _build_lead(row):
    """
    Construct the Instantly V2 lead dict from a pipeline row.
    Extends build_instantly_lead (from hunter_to_instantly) with the additional
    pipeline-stage custom vars: Trigger (from leads.trigger), PainTier, PainTheme,
    TemplateKey, TemplateRoute, SequenceKey, EmailAngle, TemplateCTA.

    Custom variables (all map to {{VarName}} in Instantly templates):
      Phone, Website, CityState, Vertical, LeadId,
      Trigger, CallPriority, PainTier, PainTheme,
      TemplateKey, TemplateRoute, SequenceKey, EmailAngle, TemplateCTA
    """
    email = (row["email"] or "").strip().lower()
    # Derive first/last name: prefer owner_name if available
    first_name, last_name = "", ""
    owner = (row["owner_name"] or "").strip() if row["owner_name"] else ""
    if owner:
        parts = owner.split(None, 1)
        first_name = parts[0] if parts else ""
        last_name  = parts[1] if len(parts) > 1 else ""
    if not first_name:
        first_name = "there"

    trigger = (row["trigger"] or "").strip() if row["trigger"] else ""

    return {
        "email":        email,
        "first_name":   first_name,
        "last_name":    last_name,
        "company_name": row["company"] or "",
        "personalization": trigger,        # maps to {{personalization}} built-in
        # ── Custom variables ─────────────────────────────────────────────────
        "Phone":        row["phone"] or "",
        "Website":      row["website"] or "",
        "CityState":    row["city_state"] or "",
        "Vertical":     row["vertical"] or "",
        "LeadId":       row["lead_ref"] or str(row["id"]),
        "Trigger":      trigger,
        "CallPriority": str(row["call_priority"] or "") if row["call_priority"] else "",
        "PainTier":     row["pain_tier"] or "",
        "PainTheme":    row["pain_theme"] or "",
        "TemplateKey":  row["template_key"] or "",
        "TemplateRoute": row["template_route"] or "",
        "SequenceKey":  row["sequence_key"] or "",
        "EmailAngle":   row["email_angle"] or "",
        "TemplateCTA":  row["template_cta"] or "",
    }


def _build_email(row):
    """Render the approved first-touch email for direct Gmail sends."""
    company = row["company"] or "[company]"
    first = (row["owner_name"] or "").strip().split(" ", 1)[0] if row["owner_name"] else ""
    greeting = first or "there"
    sequence = (row["sequence_key"] or "").strip() if row["sequence_key"] else ""
    subject = f"Gabriella / {company} Connect"
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
            f"Hi {greeting},\n\n"
            "I'm with Green PayTech. We help businesses review payment processing costs "
            "and workflow with a clearer picture of the current setup.\n\n"
            f"Would a quick payment workflow review be useful for {company}?\n\n"
            "Gabriella\nGreen PayTech"
        )
    return subject, body


def _gate_file():
    return os.environ.get("OUTREACH_SEND_GATE_FILE", "").strip() or "unknown"


def _sending_enabled():
    return os.environ.get("PIPELINE_SENDING_ENABLED", "").strip() == "1"


def _mark_sent(conn, row, provider, claim_id, message_id="", thread_id=""):
    lead_id = row["id"]
    email = (row["email"] or "").strip().lower()
    sent_at = datetime.now(timezone.utc)
    call_days = int(os.environ.get("PIPELINE_POST_EMAIL_CALL_DAYS", "3"))
    fields = {
        "sent_at": sent_at.isoformat(),
        "call_due_at": (sent_at + timedelta(days=call_days)).isoformat(),
        "call_reason": "post_email_followup",
        "send_provider": provider,
        "send_error": "",
    }
    if provider == "gmail":
        fields["gmail_message_id"] = message_id
        fields["gmail_thread_id"] = thread_id
    if not L.mark_send_claim_sent(conn, lead_id, claim_id, fields):
        print(f"[c7] CRITICAL lead {lead_id}: send succeeded but CAS sent mark failed "
              f"for claim {claim_id}; leaving row for manual review.",
              file=sys.stderr)
        return False
    L.record_outcome(conn, lead_id, "sent", provider)
    L.log_cost(conn, "sent", provider, 1, 0.0)
    try:
        commit_uploaded([email])
    except Exception as exc:
        # The external send already succeeded and SQLite is the source of truth.
        # Keep C7 successful; the uploaded ledger is only a duplicate guard.
        print(f"[c7] WARN lead {lead_id}: uploaded-ledger write failed: {exc}",
              file=sys.stderr)
    return True


# ── Core process function ─────────────────────────────────────────────────────

def process(conn, row, dry_run=False):
    """
    Process a single 'queued' (human-approved) lead.

    Returns True  on success (row advanced to 'sent').
    Returns False when at daily cap (row stays 'queued' — sends tomorrow),
                   on suppression/dedup skip (row stays 'queued'),
                   on API failure (row stays 'queued' — retry next pass),
                   on missing env keys,
                   on unexpected error.
    Never raises.
    """
    lead_id = row["id"]
    company = row["company"] or f"lead-{lead_id}"
    provider = _send_provider()
    claim_id = ""

    try:
        # HARD SAFETY GATE: nothing is ever pushed to Instantly until the
        # operator explicitly enables sending (post-warmup). Approved leads
        # simply wait at 'queued'. Flip with: PIPELINE_SENDING_ENABLED=1
        if not dry_run and not _sending_enabled():
            print(f"[c7] HOLD: sending disabled (PIPELINE_SENDING_ENABLED!=1) — "
                  f"{company} stays queued until you turn sending on.", flush=True)
            return False

        inst_key    = os.environ.get("INSTANTLY_API_KEY",      "").strip()
        campaign_id = _campaign_id_for(row)
        daily_cap   = int(os.environ.get("PIPELINE_DAILY_SEND_CAP",
                                          str(_DEFAULT_DAILY_CAP)))

        email = (row["email"] or "").strip().lower()
        if not email:
            print(f"[c7] SKIP lead {lead_id} ({company}): no email on row",
                  file=sys.stderr)
            return False
        trigger = (row["trigger"] or "").strip() if row["trigger"] else ""
        trigger_verified = row["trigger_verified"] if "trigger_verified" in row.keys() else None
        if trigger and trigger_verified != 1:
            print(f"[c7] HOLD lead {lead_id} ({company}): opener has no recorded "
                  f"evidence; refresh C6 or clear Trigger before sending.")
            return False

        # ── Gate 1: suppression ───────────────────────────────────────────────
        sup_emails, sup_domains = load_suppression()
        email_domain = email.split("@")[-1] if "@" in email else ""
        if email.lower() in sup_emails or email_domain in sup_domains:
            print(f"[c7] SKIP lead {lead_id} ({company}): suppressed email {email}")
            return False

        # ── Gate 2: already-uploaded ──────────────────────────────────────────
        seen_uploaded = load_seen_uploaded()
        if email.lower() in seen_uploaded:
            print(f"[c7] SKIP lead {lead_id} ({company}): already uploaded {email}")
            return False

        # ── Gate 3: daily cap ─────────────────────────────────────────────────
        if not dry_run:
            sent_count = _sent_today(conn)
            if sent_count >= daily_cap:
                print(f"[c7] CAP lead {lead_id} ({company}): "
                      f"daily cap {daily_cap} reached ({sent_count} sent today); "
                      f"will retry tomorrow")
                return False

        # ── Build provider payload ────────────────────────────────────────────
        lead_dict = _build_lead(row)

        if dry_run:
            print(f"  [dry] lead {lead_id} ({company})  provider={provider}  email={email}")
            if provider == "gmail":
                subject, body = _build_email(row)
                print(f"    Gmail subject: {subject!r}")
                print(f"    Gmail body:\n{body}")
            else:
                print(f"    Instantly payload:")
                for k, v in lead_dict.items():
                    print(f"      {k}: {v!r}")
            print(f"    (no API call in dry-run mode)")
            return True

        specific_campaign = ""
        if provider == "gmail":
            if gmail_sender is None:
                L.set_fields(conn, lead_id, send_error="gmail_provider_unavailable")
                print(f"[c7] DORMANT lead {lead_id} ({company}): Gmail provider unavailable.")
                return False
        else:
            # ── Env-key guard ─────────────────────────────────────────────────
            if not inst_key:
                print(f"[c7] DORMANT: INSTANTLY_API_KEY not set — "
                      f"sending paused until warmup completes. lead {lead_id} stays queued.")
                return False
            if not campaign_id:
                print(f"[c7] DORMANT: INSTANTLY_CAMPAIGN_ID not set — "
                      f"sending paused. lead {lead_id} stays queued.")
                return False

            # Use ONLY a sequence-specific campaign (bare fallback 403s on the
            # active key and would silently strand the lead).
            specific_campaign = _specific_campaign_id_for(row)
            if not specific_campaign:
                seq = (row["sequence_key"] or "").strip() if row["sequence_key"] else ""
                suffix = re.sub(r"[^A-Z0-9]+", "_", seq.upper()).strip("_")
                L.set_fields(conn, lead_id,
                             send_error=f"no_campaign_for_sequence:{seq or 'unknown'}")
                print(f"[c7] SKIP lead {lead_id} ({company}): no Instantly campaign for "
                      f"sequence_key={seq!r} (set INSTANTLY_CAMPAIGN_ID_{suffix}); "
                      f"not sending.", file=sys.stderr)
                return False
            campaign_id = specific_campaign

        claim_id = uuid.uuid4().hex
        if not L.claim_send(
            conn,
            lead_id,
            provider,
            claim_id,
            datetime.now(timezone.utc).isoformat(),
        ):
            print(f"[c7] CLAIMED lead {lead_id} ({company}): no longer queued; "
                  f"another sender owns or already processed it.")
            return False

        if provider == "gmail":
            subject, body = _build_email(row)
            try:
                result = gmail_sender.send_email(
                    email,
                    subject,
                    body,
                    from_email=os.environ.get("GMAIL_FROM", "").strip(),
                    reply_to=os.environ.get("GMAIL_REPLY_TO", "").strip(),
                )
            except Exception as exc:
                L.release_send_claim(conn, lead_id, claim_id, f"gmail_send_failed: {exc}")
                print(f"[c7] RETRY lead {lead_id} ({company}): Gmail send failed — {exc}",
                      file=sys.stderr)
                return False
            if not _mark_sent(
                conn,
                row,
                "gmail",
                claim_id,
                message_id=result.get("message_id", ""),
                thread_id=result.get("thread_id", ""),
            ):
                return False
        else:

            # ── Push to Instantly ─────────────────────────────────────────────
            pushed = push_to_instantly(inst_key, campaign_id, [lead_dict])
            if pushed == 0:
                L.release_send_claim(conn, lead_id, claim_id, "instantly_push_returned_0")
                # push_to_instantly prints the error; we leave at 'queued' for retry
                print(f"[c7] RETRY lead {lead_id} ({company}): push returned 0 — "
                      f"leaving at 'queued'", file=sys.stderr)
                return False
            if not _mark_sent(conn, row, "instantly", claim_id):
                return False

        print(f"  [c7] lead {lead_id} ({company}) → sent via {provider}  email={email}")
        return True

    except Exception as exc:
        if claim_id:
            try:
                L.release_send_claim(
                    conn,
                    lead_id,
                    claim_id,
                    f"send_exception:{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        print(f"[c7] ERROR lead {lead_id} ({company}): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return False


# ── Batch runner ──────────────────────────────────────────────────────────────

def _read_exact_ids(conn, lead_ids):
    """Return queued rows for exact lead IDs, preserving CLI order."""
    rows = []
    for lead_id in lead_ids:
        row = conn.execute(
            "SELECT * FROM leads WHERE id=? AND stage='queued'",
            (lead_id,),
        ).fetchone()
        if not row:
            print(f"[c7_sender] HOLD lead {lead_id}: not found at stage='queued'",
                  file=sys.stderr)
            continue
        rows.append(row)
    return rows


def run_batch(dry_run=False, limit=50, lead_ids=None):
    conn = L.connect()
    if not dry_run:
        requeued = L.requeue_stale_send_claims(conn)
        if requeued:
            print(f"[c7_sender] requeued stale send claims: {requeued}")
    rows = _read_exact_ids(conn, lead_ids or []) if lead_ids else L.read_by_stage(conn, "queued", limit=limit)
    provider = _send_provider()
    has_inst_key = bool(os.environ.get("INSTANTLY_API_KEY", "").strip())
    has_campaign = any(_specific_campaign_id_for(row) for row in rows)
    daily_cap    = int(os.environ.get("PIPELINE_DAILY_SEND_CAP",
                                       str(_DEFAULT_DAILY_CAP)))
    sent_today   = _sent_today(conn) if not dry_run else 0

    print(f"[c7_sender] {len(rows)} leads at 'queued'  "
          f"dry_run={dry_run}  "
          f"sending_enabled={_sending_enabled()}  "
          f"provider={provider}  "
          f"inst_key={'YES' if has_inst_key else 'NO'}  "
          f"campaign={'YES' if has_campaign else 'NO'}  "
          f"daily_cap={daily_cap}  sent_today={sent_today}  "
          f"gate_file={_gate_file()}")

    ok_count = skip_count = err_count = 0
    for row in rows:
        result = process(conn, row, dry_run=dry_run)
        if result:
            ok_count += 1
        else:
            # Could be cap, skip, or error — all leave at 'queued'
            skip_count += 1

    conn.close()
    print(f"[c7_sender] done: sent={ok_count} skipped/deferred={skip_count}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="C7 Sender — human-approved leads → Instantly V2 push"
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="print Instantly payload; no API call, no writes")
    ap.add_argument("--limit", type=int, default=50,
                    help="max leads to process per run (default 50)")
    ap.add_argument("--lead-id", action="append", type=int, default=[],
                    help="process exact queued lead ID; repeat for multiple")
    args = ap.parse_args()

    if args.dry_run:
        import tempfile

        db_path = os.environ.get("LEAD_DB", "")
        if not db_path:
            tmp = tempfile.mktemp(suffix=".db")
            os.environ["LEAD_DB"] = tmp

        conn_dry = L.connect()
        print(
            f"[c7_sender --dry-run] provider={_send_provider()}  "
            f"daily_cap={os.environ.get('PIPELINE_DAILY_SEND_CAP', str(_DEFAULT_DAILY_CAP))}  "
            f"sending_enabled={_sending_enabled()}  "
            f"gate_file={_gate_file()}"
        )

        rows = _read_exact_ids(conn_dry, args.lead_id) if args.lead_id else L.read_by_stage(conn_dry, "queued", limit=args.limit)
        if rows:
            print(f"[c7_sender --dry-run] {len(rows)} real queued leads found:")
            for r in rows:
                process(conn_dry, r, dry_run=True)
        else:
            _seed_dry_run(conn_dry)
            rows = L.read_by_stage(conn_dry, "queued", limit=5)
            print(f"[c7_sender --dry-run] {len(rows)} seeded leads:")
            for r in rows:
                process(conn_dry, r, dry_run=True)

        conn_dry.close()
    else:
        run_batch(dry_run=False, limit=args.limit, lead_ids=args.lead_id)


def _seed_dry_run(conn):
    """Insert a minimal synthetic lead at stage='queued' for dry-run demos."""
    try:
        conn.execute("""
            INSERT OR IGNORE INTO leads
              (company, phone, website, email, city_state, vertical,
               review_count, rating, pain_tier, pain_theme, stage, route,
               trigger, template_key, template_route, sequence_key,
               email_angle, template_cta, trigger_verified,
               owner_name, call_priority, status, created_at)
            VALUES
              ('Example Pharmacy', '201-555-0100', 'https://examplepharmacy.com',
               'owner@examplepharmacy.com', 'Hoboken, NJ', 'pharmacy',
               180, 4.3, 'HOT', NULL, 'queued', 'email',
               'Example Pharmacy lists prescription services and immunizations for local patients.',
               'PharmacyExampleProductA', 'pharmacy:cold_fit:default',
               'pharmacy_default',
               'Green PayTech can review one recent statement to show how much you could save and show how ExampleProduct brings payments, inventory, signatures, and history into one pharmacy workflow.',
               'Open to a free savings quote and workflow demo?',
               1, 'John Padraic', 75, 'new', datetime('now'))
        """)
        conn.execute("""
            INSERT OR IGNORE INTO leads
              (company, phone, website, email, city_state, vertical,
               review_count, rating, pain_tier, pain_theme, stage, route,
               trigger, template_key, template_route, sequence_key,
               email_angle, template_cta, owner_name, call_priority, status, created_at)
            VALUES
              ('Example Grill', '201-555-0200', 'https://examplegrill.com',
               'mgr@examplegrill.com', 'Hoboken, NJ', 'restaurant',
               95, 4.1, 'WARM', 'fee_or_price', 'queued', 'email',
               '',
               'Restaurant2', 'restaurant:cold_fit:fee_or_price',
               'restaurant_default',
               'Green PayTech can review one recent statement to show how much you could save and show how Union brings ordering, payments, and guest data into one restaurant system.',
               'Open to a free savings quote and short demo?',
               '', 50, 'new', datetime('now'))
        """)
        conn.commit()
    except Exception as e:
        print(f"[c7 seed] warn: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
