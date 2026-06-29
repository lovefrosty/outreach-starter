#!/usr/bin/env python3
"""
propensity.py — v1 transparent switch-propensity score.

Deterministic, documented, no black box. Phase 2 swaps the body for TF-IDF +
k-means pain themes; phase 3 for a supervised model trained on the `outcomes`
table — both behind this same `score()` / `tier()` interface so nothing
downstream changes.

NORTH STAR = volume of qualified, reachable leads. So the score leans on the
CHEAP, attainable signals: transaction volume (reviews), the processor WHO
qualifier (on a default flat-rate processor = overpaying), and review pain.
Timing (switch_window) is a light bonus, never a gate.
"""

# Default flat-rate / un-negotiated processors → the strongest cheap qualifier.
FLAT_RATE = {"square", "stripe", "paypal", "toast", "clover", "shopify payments"}
MID = {"lightspeed", "heartland", "aloha", "braintree", "authorize.net"}

# Weights (sum = 1.0). Tunable; documented on purpose.
W_VOLUME   = 0.30   # review_count (proxy for transaction volume = real $ pain)
W_RATING   = 0.15   # stability/legitimacy
W_PAIN     = 0.20   # review pain density
W_PROC     = 0.25   # processor WHO qualifier (flat-rate = overpaying)
W_SWITCH   = 0.10   # timing bonus (ownership/processor change)


def processor_factor(processor):
    p = (processor or "").strip().lower()
    if not p:
        return 0.5            # unknown — neutral; still worth pursuing
    if p in FLAT_RATE:
        return 1.0            # overpaying on default rates → best target
    if p in MID:
        return 0.6
    return 0.4                # something custom/negotiated → less upside


def _volume_factor(review_count):
    rc = review_count or 0
    return min(1.0, rc / 300.0)   # 300+ reviews saturates the volume signal


def _rating_factor(rating):
    if not rating:
        return 0.5
    if rating >= 4.0:
        return 1.0
    if rating >= 3.5:
        return 0.7
    if rating >= 3.0:
        return 0.4
    return 0.2                # struggling/closing — deprioritize


def score(review_count=None, rating=None, pain_density=0.0,
          processor=None, switch_window_score=0.0):
    p = (W_VOLUME * _volume_factor(review_count)
         + W_RATING * _rating_factor(rating)
         + W_PAIN   * (pain_density or 0.0)
         + W_PROC   * processor_factor(processor)
         + W_SWITCH * (switch_window_score or 0.0))
    return round(min(1.0, p), 3)


# Tier cutoffs calibrated to THIS formula + a volume-first posture (pass enough
# leads; only HOT/WARM spend a Hunter credit in C4). Tune as G0 data comes in.
def tier(p):
    if p >= 0.65:
        return "HOT"
    if p >= 0.50:
        return "WARM"
    if p >= 0.35:
        return "COOL"
    return "COLD"


if __name__ == "__main__":
    # quick sanity: a solid independent on Square should land HOT/WARM
    demos = [
        dict(review_count=240, rating=4.2, pain_density=0.3, processor="Square"),
        dict(review_count=20,  rating=4.0, pain_density=0.0, processor=None),
        dict(review_count=600, rating=3.4, pain_density=0.5, processor="Stripe"),
    ]
    for d in demos:
        s = score(**d)
        print(f"{d} -> {s} ({tier(s)})")
