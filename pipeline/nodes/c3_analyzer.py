#!/usr/bin/env python3
"""
C3 pain-analyzer — stage 'scraped' -> 'analyzed'.

Deterministic v1 (no LLM): read the lead's reviews, compute pain density +
features, score switch-propensity, set pain_tier, advance. The propensity tier
is what gates Hunter spend in C4 (only HOT/WARM) and ranks the call queue.

Contract: process(conn, row) -> bool. See skills/nodes/C3-pain-analyzer.md.

Reuses ds/features.py + ds/propensity.py so phases 2/3 (k-means, supervised)
drop in behind the same interface.
"""

import sys
from pathlib import Path

_PIPE = Path(__file__).resolve().parents[1]          # pipeline/
sys.path.insert(0, str(_PIPE))                        # import ledger
import ledger as L                                    # noqa: E402
from ds import features, propensity                   # noqa: E402


def process(conn, row):
    lead_id = row["id"]
    reviews = L.reviews_for(conn, lead_id)
    review_texts = [r["text"] for r in reviews] if reviews else []
    try:
        import json
        tech_signals = json.loads(row["tech_signals"] or "[]") if "tech_signals" in row.keys() else []
    except (TypeError, ValueError):
        tech_signals = []

    feats = features.extract(row, reviews)
    L.upsert_features(conn, lead_id, **feats)
    pain_theme = features.dominant_pain_theme(review_texts, tech_signals)

    p = propensity.score(
        review_count=row["review_count"] if "review_count" in row.keys() else None,
        rating=row["rating"] if "rating" in row.keys() else None,
        pain_density=feats.get("pain_density", 0.0),
        processor=row["processor"] if "processor" in row.keys() else None,
        switch_window_score=row["switch_window_score"] if "switch_window_score" in row.keys() else 0.0,
    )
    t = propensity.tier(p)
    L.advance(conn, lead_id, "analyzed", propensity=p, pain_tier=t, pain_theme=pain_theme or None)
    return True


def _demo():
    """Seed a scraped lead + reviews in a temp DB and run process()."""
    conn = L.connect()
    conn.execute(
        "INSERT INTO leads (company, website, vertical, rating, review_count, processor, stage, created_at) "
        "VALUES ('Demo Pharmacy','https://demo.com','pharmacy',4.2,240,'Square','scraped',?)",
        (L._now(),))
    conn.commit()
    lid = conn.execute("SELECT id FROM leads ORDER BY id DESC LIMIT 1").fetchone()["id"]
    L.add_review(conn, lid, "the line is always slow and they charge a card minimum fee", 3)
    L.add_review(conn, lid, "great staff but checkout is painful", 4)
    row = conn.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    process(conn, row)
    out = conn.execute("SELECT stage, propensity, pain_tier FROM leads WHERE id=?", (lid,)).fetchone()
    print("C3 demo ->", dict(out))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="run a demo on a temp lead")
    ap.parse_args()
    _demo()
