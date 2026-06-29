#!/usr/bin/env python3
"""
features.py — deterministic feature extraction for the propensity score.

Pure functions over a lead row + its reviews. No LLM, no DB. The C3 analyzer
calls these, then writes the result to the `features` table via the ledger.

Pain keywords are the v1 stand-in for the v2 TF-IDF/k-means pain themes; they
give a transparent `pain_density` in [0,1] now, and the same reviews text is
stored for the real clustering model later.
"""

import re

# Pain signal families (reused/extended from the reference build).
PAIN_KEYWORDS = {
    "checkout_friction":   ["line", "wait", "slow", "checkout", "register",
                             "card reader", "terminal", "machine down", "system down"],
    "fee_or_price":        ["expensive", "overpriced", "fees", "surcharge",
                             "hidden fee", "charge", "pricey", "cash only", "card minimum"],
    "delivery_app_pain":   ["doordash", "uber eats", "grubhub", "delivery fee", "delivery app"],
    "refund_or_dispute":   ["refund", "charged twice", "double charged", "wrong charge",
                             "dispute", "declined", "card declined"],
    "operations_pressure": ["understaffed", "chaotic", "mistake", "order wrong",
                             "long wait", "disorganized"],
}


def pain_density(review_texts):
    """[0,1] — how payment/ops-pain-heavy the reviews are. Transparent + cheap."""
    if not review_texts:
        return 0.0
    blob = " ".join(t.lower() for t in review_texts if t)
    if not blob.strip():
        return 0.0
    families_hit = 0
    total_hits = 0
    for _, kws in PAIN_KEYWORDS.items():
        fam_hit = False
        for kw in kws:
            if kw in blob:
                total_hits += 1
                fam_hit = True
        if fam_hit:
            families_hit += 1
    # weight breadth (distinct families) over raw repetition
    score = 0.18 * families_hit + 0.04 * total_hits
    return round(min(1.0, score), 3)


def pain_family_scores(review_texts):
    """Return transparent per-family keyword hit counts."""
    blob = " ".join(t.lower() for t in review_texts if t)
    scores = {}
    for family, kws in PAIN_KEYWORDS.items():
        scores[family] = sum(1 for kw in kws if kw in blob)
    return scores


def dominant_pain_theme(review_texts, tech_signals=None):
    """Pick one deterministic theme for template routing."""
    scores = pain_family_scores(review_texts)
    best = max(scores.items(), key=lambda kv: kv[1]) if scores else ("", 0)
    if best[1] > 0:
        return best[0]
    signals = set()
    for sig in tech_signals or []:
        signals.add(str(sig).lower())
    if "delivery" in signals:
        return "delivery_app_pain"
    if {"online_ordering", "reservations", "multi_location"} & signals:
        return "operations_pressure"
    if {"ecommerce", "gift_cards"} & signals:
        return "checkout_friction"
    return ""


def review_velocity(reviews):
    """Reviews are not timestamped in v1 sourcing → 0.0 placeholder.
    (Wired now so the column exists; populated when a source gives dates.)"""
    return 0.0


def tech_score(processor, tech_signals=None):
    """Score website complexity signals without pretending they prove payment pain."""
    score = 0.6 if (processor or "").strip() else 0.0
    signals = set(str(s).lower() for s in (tech_signals or []))
    for sig in ("online_ordering", "delivery", "third_party_ordering",
                "reservations", "gift_cards", "separate_loyalty_or_gift_card",
                "ecommerce", "multi_location", "catering_or_events",
                "table_or_qr_pay", "payment_link", "pharmacy_compliance_payments",
                "dealership_service_payments"):
        if sig in signals:
            score += 0.08
    return round(min(1.0, score), 3)


def extract(lead_row, reviews):
    """Return the features dict for the `features` table."""
    import json
    texts = [r["text"] for r in reviews] if reviews else []
    raw_signals = lead_row["tech_signals"] if "tech_signals" in lead_row.keys() else ""
    try:
        tech_signals = json.loads(raw_signals) if raw_signals else []
    except (TypeError, json.JSONDecodeError):
        tech_signals = []
    return {
        "review_count":   lead_row["review_count"] if "review_count" in lead_row.keys() else None,
        "rating":         lead_row["rating"] if "rating" in lead_row.keys() else None,
        "pain_density":   pain_density(texts),
        "review_velocity": review_velocity(reviews),
        "tech_score":     tech_score(
            lead_row["processor"] if "processor" in lead_row.keys() else None,
            tech_signals,
        ),
        "age_days":       None,   # populated when a filing/age source is active
    }
