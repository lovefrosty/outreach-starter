#!/usr/bin/env python3
"""
qualification.py - sales qualification signals from scrape/enrichment data.

What this program does
----------------------
The outreach system needs to answer sales questions before spending more time
researching or emailing a lead:

- Who makes the decision?
- What system do they use now?
- Are they annoyed enough to change?
- Is volume high enough?
- Is there a timing reason?
- Can they send a statement or take a discovery call?

This module converts existing deterministic signals into a compact
qualification readout. It does not call external APIs and it does not send
messages. It is meant for filtering, review-card context, and session briefs.

Main functions
--------------
- `qualify(row, reviews=None, email_candidates=None)`: return a qualification
  dictionary for one lead row.
- `recommended_next_step(answers)`: choose statement review, discovery call,
  call-first, or research-more.
- `recommended_cta(answers)`: choose call-first vs free-statement-review ask.
- `positioning_wrapper(answers)`: keep the pitch honest when the merchant
  already has a strong vertical stack.
- `lead_evidence_card(row, reviews=None, email_candidates=None)`: compact card
  showing qualification plus what is safe to say.
- `format_summary(answers)`: readable text for Outreach cards/reports.

Program entrypoint
------------------
Run `python3 workspace/pipeline/ds/qualification.py` for a built-in demo.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

try:
    from ds import evidence, features
except Exception:
    import evidence  # type: ignore
    import features  # type: ignore


GENERIC_EMAIL_LOCAL_PARTS = {
    "info", "contact", "hello", "support", "admin", "sales", "office",
    "service", "billing", "accounts", "general", "team",
}

# Sales thresholds from operator guidance.
# Below 50 reviews is usually too small unless a stronger timing/signal exists.
MIN_REVIEW_COUNT = 50
STRONG_REVIEW_COUNT = 200

# These are useful wedges when paired with volume/contact quality. They are not
# "bad" processors by default; the pitch is rate review, support, chargebacks,
# and workflow fit.
STRONG_WEDGE_PROCESSORS = {"square", "clover", "stripe", "paypal", "paypal zettle", "heartland"}

# These stacks are harder to displace when fully adopted because they already
# address vertical workflow. They can still be worth outreach for rate/support
# review, partial adoption, contract timing, or visible dissatisfaction.
HARDER_DISPLACEMENT_PROCESSORS = {
    "toast", "lightspeed", "spoton", "touchbistro",
    "primerx", "bestrx", "liberty", "pioneerrx",
    "cdk", "dealertrack", "tekion", "kimoby", "dealerpay",
}

# Outreach should be careful with larger groups because they may already have
# negotiated terms and slower decision cycles. This is not an automatic skip,
# but it changes the next step toward research or strategic handling.
GROUP_SIGNALS = {"restaurant group", "hospitality group", "restaurant group", "lp", "llc group"}


def _row_value(row, key, default=""):
    """Read either sqlite Row or dict values safely."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = row.get(key, default) if isinstance(row, dict) else default
    return default if value is None else value


def _review_texts(reviews):
    """Extract review text from sqlite rows, dicts, or plain strings."""
    texts = []
    for review in reviews or []:
        if isinstance(review, str):
            texts.append(review)
        else:
            texts.append(_row_value(review, "text"))
    return [text for text in texts if text]


def _tech_signals(row):
    """Decode C2 tech signals while tolerating old/bad JSON."""
    raw = _row_value(row, "tech_signals")
    if isinstance(raw, list):
        return [str(item).lower() for item in raw]
    try:
        return [str(item).lower() for item in json.loads(raw or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _email_type(email):
    """Classify reachability from the current email value."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return "none"
    local = email.split("@", 1)[0]
    return "generic" if local in GENERIC_EMAIL_LOCAL_PARTS else "named_or_direct"


def decision_maker_signal(row):
    """Answer: who likely makes the decision?"""
    owner = (_row_value(row, "owner_name") or _row_value(row, "contact_name")).strip()
    if owner:
        return {
            "answer": owner,
            "confidence": "medium",
            "evidence": "owner/contact name captured during enrichment",
        }
    return {
        "answer": "unknown",
        "confidence": "low",
        "evidence": "no owner/contact name captured yet",
    }


def current_system_signal(row):
    """Answer: what system do they use now?"""
    processor = (_row_value(row, "processor") or "").strip()
    signals = _tech_signals(row)
    visible = [signal for signal in signals if not signal.startswith(("social_url:", "public_doc_url:"))]
    if processor:
        return {
            "answer": processor,
            "confidence": "medium",
            "evidence": "website processor/POS fingerprint",
        }
    if visible:
        return {
            "answer": ", ".join(visible[:3]),
            "confidence": "low",
            "evidence": "website technology/channel signals, not a verified processor",
        }
    return {
        "answer": "unknown",
        "confidence": "low",
        "evidence": "no processor or workflow signal captured yet",
    }


def processor_wedge_signal(row):
    """
    Answer: is the current processor/POS a useful sales wedge?

    Square/Toast/Clover/etc. are not "bad" targets by themselves. The wedge is
    rate review, high-touch support, chargeback help, partial adoption, contract
    timing, and matching/improving the setup without disrupting what works.
    """
    processor = (_row_value(row, "processor") or "").strip().lower()
    signals = set(_tech_signals(row))
    if processor in STRONG_WEDGE_PROCESSORS:
        return {
            "answer": "strong_wedge",
            "confidence": "medium",
            "evidence": f"{processor} detected; pitch rate match/beat, support, chargebacks, workflow fit",
        }
    if processor in HARDER_DISPLACEMENT_PROCESSORS:
        return {
            "answer": "narrow_wedge",
            "confidence": "low",
            "evidence": f"{processor} detected; use rate/support/partial-adoption angle, not generic replacement",
        }
    if {"online_ordering", "delivery", "reservations", "gift_cards", "ecommerce",
            "third_party_ordering", "separate_loyalty_or_gift_card",
            "table_or_qr_pay", "payment_link", "public_manual_payment_hint",
            "pharmacy_compliance_payments", "pharmacy_stack",
            "dealership_service_payments", "dealer_dms_or_payments",
            "restaurant_vertical_stack"} & signals:
        return {
            "answer": "workflow_wedge",
            "confidence": "low",
            "evidence": "website shows public payment-adjacent workflow hints",
        }
    if processor:
        return {
            "answer": "known_system",
            "confidence": "low",
            "evidence": f"{processor} detected, but wedge strength unknown",
        }
    return {
        "answer": "unknown",
        "confidence": "low",
        "evidence": "no processor/POS wedge captured",
    }


def ownership_fit_signal(row):
    """
    Answer: is this likely the kind of business Outreach should pursue?

    The current strategy prefers one-to-three location owner-operated businesses
    over restaurant groups/LP-owned groups because owners are reachable and may
    not already have negotiated enterprise terms.
    """
    locations = str(_row_value(row, "locations") or "").strip()
    company = (_row_value(row, "company") or "").lower()
    notes = (_row_value(row, "notes") or "").lower()
    try:
        loc_count = int(locations) if locations else 1
    except ValueError:
        loc_count = 1
    if any(signal in company or signal in notes for signal in GROUP_SIGNALS) or loc_count > 3:
        return {
            "answer": "group_or_multi_location_caution",
            "confidence": "low",
            "evidence": f"locations={loc_count}; possible group/negotiated terms",
        }
    return {
        "answer": "owner_operator_fit",
        "confidence": "medium" if loc_count <= 3 else "low",
        "evidence": f"locations={loc_count}",
    }


def change_pain_signal(row, reviews=None):
    """Answer: are they annoyed enough to change?"""
    texts = _review_texts(reviews)
    density = features.pain_density(texts)
    theme = (_row_value(row, "pain_theme") or features.dominant_pain_theme(texts, _tech_signals(row))).strip()
    if density >= 0.35 or theme:
        return {
            "answer": theme or "payment/operations pain",
            "confidence": "medium" if density >= 0.35 else "low",
            "evidence": f"review/website pain density={density}",
        }
    return {
        "answer": "not proven",
        "confidence": "low",
        "evidence": "no strong public pain signal captured",
    }


def volume_signal(row):
    """Answer: is transaction volume likely high enough?"""
    review_count = _row_value(row, "review_count", 0) or 0
    locations = str(_row_value(row, "locations") or "").strip()
    try:
        review_count = int(review_count)
    except (TypeError, ValueError):
        review_count = 0
    if review_count >= STRONG_REVIEW_COUNT or locations not in ("", "1"):
        return {
            "answer": "likely high enough",
            "confidence": "medium",
            "evidence": f"review_count={review_count}" + (f", locations={locations}" if locations else ""),
        }
    if review_count >= MIN_REVIEW_COUNT:
        return {
            "answer": "possibly high enough",
            "confidence": "low",
            "evidence": f"review_count={review_count}",
        }
    return {
        "answer": "not proven",
        "confidence": "low",
        "evidence": f"review_count={review_count or 'unknown'}",
    }


def timing_signal(row):
    """Answer: is there a timing reason to reach out now?"""
    filing_date = (_row_value(row, "filing_date") or "").strip()
    switch_score = _row_value(row, "switch_window_score", 0) or 0
    if filing_date:
        try:
            parsed = datetime.fromisoformat(filing_date.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - parsed).days
            if 0 <= age_days <= 120:
                return {
                    "answer": "recent public filing/change",
                    "confidence": "medium",
                    "evidence": f"filing_date={filing_date}",
                }
        except ValueError:
            pass
    try:
        switch_score = float(switch_score)
    except (TypeError, ValueError):
        switch_score = 0.0
    if switch_score >= 0.5:
        return {
            "answer": "possible switch window",
            "confidence": "low",
            "evidence": f"switch_window_score={switch_score}",
        }
    return {
        "answer": "not proven",
        "confidence": "low",
        "evidence": "no timing trigger captured",
    }


def statement_or_call_signal(row, answers):
    """Answer: can they send a statement or take a discovery call?"""
    email_kind = _email_type(_row_value(row, "email"))
    phone = (_row_value(row, "phone") or "").strip()
    if email_kind == "named_or_direct":
        return {
            "answer": "ask for free statement review",
            "confidence": "medium",
            "evidence": "direct/named email path exists",
        }
    if email_kind == "generic" and phone:
        return {
            "answer": "call first, then ask for statement owner",
            "confidence": "medium",
            "evidence": "generic email plus phone path",
        }
    if phone:
        return {
            "answer": "call-first discovery",
            "confidence": "low",
            "evidence": "phone path exists but no direct email",
        }
    return {
        "answer": "research more",
        "confidence": "low",
        "evidence": "no reliable contact path captured",
    }


def recommended_next_step(answers):
    """
    Choose the next sales action from qualification answers.

    This is the filtering point: high-volume + pain/system/contact earns
    statement-review or discovery; weak/no-contact leads should not consume
    email review time.
    """
    volume_answer = answers["volume"]["answer"]
    volume_ok = volume_answer in {"likely high enough", "possibly high enough"}
    too_small = volume_answer == "not proven"
    pain_ok = answers["pain"]["answer"] != "not proven"
    system_known = answers["current_system"]["answer"] != "unknown"
    wedge_ok = answers["processor_wedge"]["answer"] in {"strong_wedge", "workflow_wedge", "narrow_wedge"}
    owner_fit = answers["ownership_fit"]["answer"] == "owner_operator_fit"
    contact = answers["statement_or_call"]["answer"]
    if too_small and not pain_ok and not wedge_ok:
        return "skip_or_archive"
    if contact == "ask for free statement review" and owner_fit and (volume_ok or pain_ok or wedge_ok or system_known):
        return "statement_review"
    if contact.startswith("call") and owner_fit and (volume_ok or pain_ok or wedge_ok):
        return "call_first"
    if volume_ok and (system_known or wedge_ok):
        return "discovery_call"
    return "research_more"


def recommended_cta(answers):
    """
    Choose the outreach ask without changing approved templates.

    Operator preference:
    - Type A asks for a call first.
    - Type B asks for a free statement review.
    - Do not use the blunt "would you actually switch" line as default copy.
    """
    step = answers.get("next_step") or recommended_next_step(answers)
    if step == "statement_review":
        return {
            "variant": "B_statement_review",
            "text": "Would a free statement review be useful if it showed either savings or a cleaner payment workflow?",
        }
    if step in {"call_first", "discovery_call"}:
        return {
            "variant": "A_call_first",
            "text": "Open to a quick call to see if matching or improving your current setup would be useful?",
        }
    return {
        "variant": "research_or_hold",
        "text": "Hold for better contact, timing, or workflow evidence before outreach.",
    }


def positioning_wrapper(answers):
    """
    Choose the honest sales framing that wraps the raw processor signal.

    This is intentionally separate from `processor_wedge_signal`: the wedge says
    what Outreach found, while this wrapper says how Outreach should talk about it.
    Strong vertical stacks should not be attacked as broken. The safer pitch is:
    keep what works, review the rate/support/chargeback situation, and only
    improve workflow where Green PayTech can actually match or improve it.
    """
    wedge = answers["processor_wedge"]["answer"]
    current = str(answers["current_system"]["answer"]).lower()
    if wedge == "narrow_wedge":
        return {
            "variant": "relationship_rate_wrapper",
            "text": (
                "Do not claim their current stack is bad. Lead with a review of "
                "rate, relationship support, chargeback help, contract timing, "
                "and whether Green PayTech can match or improve the setup "
                "without making the workflow worse."
            ),
        }
    if wedge == "strong_wedge" and current in STRONG_WEDGE_PROCESSORS:
        return {
            "variant": "generalist_processor_wrapper",
            "text": (
                "Lead with match/beat rate, higher-touch relationship management, "
                "chargeback support, and a cleaner setup if their current tools "
                "are scattered or only partially adopted."
            ),
        }
    if wedge == "workflow_wedge":
        return {
            "variant": "workflow_hint_wrapper",
            "text": (
                "Treat public workflow hints as a reason to ask, not a proven "
                "pain point. Ask whether the current setup is creating extra "
                "work before making a stronger workflow claim."
            ),
        }
    return {
        "variant": "research_first_wrapper",
        "text": "Hold strong claims until Outreach has processor, workflow, timing, or contact evidence.",
    }


def qualify(row, reviews=None, email_candidates=None):
    """Return qualification answers for one lead."""
    answers = {
        "decision_maker": decision_maker_signal(row),
        "current_system": current_system_signal(row),
        "processor_wedge": processor_wedge_signal(row),
        "ownership_fit": ownership_fit_signal(row),
        "pain": change_pain_signal(row, reviews),
        "volume": volume_signal(row),
        "timing": timing_signal(row),
    }
    answers["statement_or_call"] = statement_or_call_signal(row, answers)
    answers["next_step"] = recommended_next_step(answers)
    answers["recommended_cta"] = recommended_cta(answers)
    answers["positioning_wrapper"] = positioning_wrapper(answers)
    return answers


def format_summary(answers):
    """Render qualification answers for Outreach or review reports."""
    return "\n".join([
        f"Decision maker: {answers['decision_maker']['answer']} ({answers['decision_maker']['confidence']})",
        f"Current system: {answers['current_system']['answer']} ({answers['current_system']['confidence']})",
        f"Processor wedge: {answers['processor_wedge']['answer']} ({answers['processor_wedge']['confidence']})",
        f"Ownership fit: {answers['ownership_fit']['answer']} ({answers['ownership_fit']['confidence']})",
        f"Pain/change: {answers['pain']['answer']} ({answers['pain']['confidence']})",
        f"Volume: {answers['volume']['answer']} ({answers['volume']['confidence']})",
        f"Timing: {answers['timing']['answer']} ({answers['timing']['confidence']})",
        f"Ask: {answers['statement_or_call']['answer']} ({answers['statement_or_call']['confidence']})",
        f"Next step: {answers['next_step']}",
        f"CTA: {answers['recommended_cta']['variant']} - {answers['recommended_cta']['text']}",
        f"Positioning: {answers['positioning_wrapper']['variant']} - {answers['positioning_wrapper']['text']}",
    ])


def lead_evidence_card(row, reviews=None, email_candidates=None):
    """
    Build a compact reviewer card from qualification and evidence grading.

    This is the card Outreach should show before a lead becomes email copy. It
    makes the difference explicit: observed public facts can be used safely,
    while inferred/private workflow claims must stay in research or call notes.
    """
    answers = qualify(row, reviews=reviews, email_candidates=email_candidates)
    scrape_packet = evidence.scrape_evidence(row)
    lines = [
        "Lead evidence card",
        f"Company: {_row_value(row, 'company') or '(unknown)'}",
        f"Current processor/POS: {answers['current_system']['answer']} ({answers['current_system']['confidence']})",
        f"Ownership fit: {answers['ownership_fit']['answer']} ({answers['ownership_fit']['confidence']})",
        f"Review threshold: {answers['volume']['answer']} ({answers['volume']['evidence']})",
        f"Public workflow hints: {', '.join(scrape_packet['observed'][:4]) or 'none'}",
        f"Safe email claims: {', '.join(scrape_packet['safe_email_claims'][:3]) or 'none'}",
        f"Research/call questions: {', '.join(scrape_packet['needs_research'][:3]) or 'none'}",
        f"Next step: {answers['next_step']}",
        f"CTA: {answers['recommended_cta']['variant']}",
        f"Positioning: {answers['positioning_wrapper']['variant']}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    demo = {
        "company": "Demo Pharmacy",
        "vertical": "pharmacy",
        "owner_name": "Jane Owner",
        "email": "jane@example.com",
        "phone": "+12015550100",
        "processor": "Square",
        "review_count": 220,
        "rating": 4.2,
    }
    demo_reviews = [
        "Checkout is always slow and the card reader was down again.",
        "Helpful staff but the line gets long at the register.",
    ]
    print(json.dumps(qualify(demo, demo_reviews), indent=2))
    print("---")
    print(format_summary(qualify(demo, demo_reviews)))
    print("---")
    print(lead_evidence_card(demo, demo_reviews))
