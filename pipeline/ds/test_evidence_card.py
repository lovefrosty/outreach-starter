#!/usr/bin/env python3
"""
test_evidence_card.py - focused checks for evidence-graded scraper signals.

What this program tests
-----------------------
- C2 public signals are graded before they become sales language.
- Unsafe private workflow claims stay in `do_not_say`, not safe email claims.
- Qualification can render a compact evidence card for Outreach review.

Run from repo root:
`python3 workspace/pipeline/ds/test_evidence_card.py`
"""

from __future__ import annotations

import json

import evidence
import qualification


def main():
    """Run evidence-card assertions and print a compact proof."""
    row = {
        "company": "Demo Restaurant",
        "processor": "Toast",
        "tech_signals": json.dumps([
            "Toast",
            "restaurant_vertical_stack",
            "third_party_ordering",
            "public_manual_payment_hint",
            "payment_link",
        ]),
        "review_count": 175,
        "locations": "1",
        "email": "owner@example.com",
        "phone": "+12015550123",
    }
    packet = evidence.scrape_evidence(row)
    card = qualification.lead_evidence_card(row)
    print("observed_count=" + str(len(packet["observed"])))
    print("safe_email_claims=" + " | ".join(packet["safe_email_claims"]))
    print("needs_research=" + " | ".join(packet["needs_research"][:2]))
    print("---CARD---")
    print(card)

    assert any("Toast" in item for item in packet["observed"])
    assert any("manual internal workflows" in item for item in packet["do_not_say"])
    assert not any("manual internal workflows" in item for item in packet["safe_email_claims"])
    assert "Safe email claims:" in card
    assert "Research/call questions:" in card
    print("evidence_card_test=PASS")


if __name__ == "__main__":
    main()
