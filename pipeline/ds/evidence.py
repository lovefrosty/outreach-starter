#!/usr/bin/env python3
"""
evidence.py - grade scraper facts before Outreach turns them into sales claims.

What this program does
----------------------
The scraper can observe public facts, but it cannot prove private business
problems. This module separates four evidence levels:

- `observed`: directly visible in public HTML or JSON-LD.
- `inferred`: a reasonable internal scoring hint, not email-ready by itself.
- `needs_research`: useful question for a research agent, call, or statement review.
- `do_not_say`: unsafe claims Outreach should not put into outreach copy.

Main functions
--------------
- `decode_tech_signals(row)`: read C2 `tech_signals` from a row or dict.
- `scrape_evidence(row)`: return graded evidence from processor/signals.
- `format_scrape_evidence(evidence)`: compact text block for review cards.

Program entrypoint
------------------
Run `python3 workspace/pipeline/ds/evidence.py` for a built-in demo.
"""

from __future__ import annotations

import json


OBSERVED_SIGNAL_LABELS = {
    "online_ordering": "online ordering language/link visible",
    "third_party_ordering": "third-party ordering link visible",
    "delivery": "delivery marketplace or delivery language visible",
    "reservations": "reservation platform/language visible",
    "gift_cards": "gift-card language/link visible",
    "separate_loyalty_or_gift_card": "separate loyalty/gift-card provider hinted",
    "ecommerce": "cart/checkout/ecommerce language visible",
    "multi_location": "multi-location language visible",
    "catering_or_events": "catering/events language visible",
    "hiring_or_careers": "hiring/careers language visible",
    "appointments_or_booking": "booking/appointment language visible",
    "contact_form": "contact form or get-in-touch flow visible",
    "pricing_or_plans": "pricing, plans, or membership language visible",
    "financing": "financing/apply-now language visible",
    "customer_portal_or_account": "portal/account-access language visible",
    "sms_or_text_channel": "text/SMS contact or payment language visible",
    "faq_or_help_center": "FAQ/help-center language visible",
    "table_or_qr_pay": "table-pay, QR-pay, or handheld language visible",
    "payment_link": "generic payment link or pay-online language visible",
    "public_manual_payment_hint": "public call-to-pay/pay-by-phone/PDF invoice hint visible",
    "pharmacy_compliance_payments": "FSA/HSA/SIGIS/IIAS or pharmacy payment language visible",
    "pharmacy_stack": "pharmacy software/payment stack visible",
    "dealership_service_payments": "service-lane, RO, text-to-pay, or dealership payment language visible",
    "dealer_dms_or_payments": "dealer DMS/payment solution visible",
    "restaurant_vertical_stack": "restaurant-specific POS/order/pay stack visible",
    "public_docs": "same-domain public document link visible",
}

PROCESSOR_HINTS = {
    "square", "stripe", "paypal", "paypal zettle", "clover", "toast",
    "lightspeed", "spoton", "touchbistro", "primerx", "bestrx", "liberty",
    "pioneerrx", "cdk", "dealertrack", "tekion", "kimoby", "dealerpay",
    "revel", "upserve", "aloha", "rx30", "qs/1", "computer-rx",
}

RESEARCH_QUESTIONS_BY_SIGNAL = {
    "third_party_ordering": "Is ordering/payment fragmented across outside tools?",
    "separate_loyalty_or_gift_card": "Are loyalty or gift cards separate from payments/POS?",
    "public_manual_payment_hint": "Does this public payment hint reflect manual reconciliation or just a normal payment option?",
    "payment_link": "Is the payment link tied into POS/accounting, or is it a separate workaround?",
    "table_or_qr_pay": "Is table/QR pay fully adopted, or only mentioned on the site?",
    "pharmacy_compliance_payments": "Are FSA/HSA/SIGIS/IIAS and pharmacy payments already handled well?",
    "dealership_service_payments": "Does the service-lane payment flow write back cleanly to the DMS?",
    "restaurant_vertical_stack": "Is the restaurant-specific stack fully adopted or only partially used?",
    "pharmacy_stack": "Is the pharmacy stack locked in, or is payment/support/rate still open?",
    "dealer_dms_or_payments": "Is the DMS payment workflow fully adopted, or is payment still separate?",
    "appointments_or_booking": "Does booking tie cleanly into payment, reminders, and customer records?",
    "contact_form": "Is the contact form part of sales intake, support, or general operations only?",
    "pricing_or_plans": "Are pricing, memberships, or subscriptions part of the checkout flow?",
    "financing": "Is financing integrated into checkout or handled in a separate workflow?",
    "customer_portal_or_account": "Does the portal/account flow also handle invoices, statements, or payments?",
    "sms_or_text_channel": "Is texting tied into support only, or also into payments, service, and reminders?",
}

DO_NOT_SAY = [
    "Do not say the business has manual internal workflows from public HTML alone.",
    "Do not say they are unhappy with their processor unless a review, reply, or call says that.",
    "Do not say Green PayTech can replace a vertical stack feature unless capability is confirmed.",
    "Do not say they are overpaying before a statement review or rate evidence.",
]


def _row_value(row, key, default=""):
    """Read dict or sqlite Row values without raising on missing columns."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = row.get(key, default) if isinstance(row, dict) else default
    return default if value is None else value


def decode_tech_signals(row):
    """Decode C2 `tech_signals` while tolerating old rows and bad JSON."""
    raw = _row_value(row, "tech_signals")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def scrape_evidence(row):
    """
    Build an evidence packet from public scrape facts.

    The packet is safe for Outreach cards and filtering. Only `safe_email_claims`
    should be considered email-copy material; `research_questions` are prompts
    for a research agent, call, or statement review.
    """
    signals = decode_tech_signals(row)
    signal_lowers = [signal.lower() for signal in signals]
    processor = str(_row_value(row, "processor")).strip()
    observed = []
    inferred = []
    research_questions = []
    safe_email_claims = []

    if processor:
        observed.append(f"Processor/POS fingerprint detected: {processor}")
        safe_email_claims.append(f"your current {processor} setup")

    for raw, lower in zip(signals, signal_lowers):
        if lower.startswith("social_url:"):
            observed.append(f"Social profile link observed: {raw.split(':', 2)[1]}")
            continue
        if lower.startswith("public_doc_url:"):
            observed.append("Public document link observed on website")
            continue
        if lower in PROCESSOR_HINTS and not processor:
            observed.append(f"Processor/POS hint detected: {raw}")
            continue
        label = OBSERVED_SIGNAL_LABELS.get(lower)
        if label:
            observed.append(label)
            question = RESEARCH_QUESTIONS_BY_SIGNAL.get(lower)
            if question:
                research_questions.append(question)

    if "public_manual_payment_hint" in signal_lowers:
        inferred.append("Possible payment-workflow friction, but only as a research hypothesis.")
    if {"third_party_ordering", "delivery", "separate_loyalty_or_gift_card"} & set(signal_lowers):
        inferred.append("Possible fragmented customer/order/payment workflow.")
    if {"restaurant_vertical_stack", "pharmacy_stack", "dealer_dms_or_payments"} & set(signal_lowers):
        inferred.append("Vertical stack may already solve workflow; use relationship/rate wrapper first.")

    if "table_or_qr_pay" in signal_lowers:
        safe_email_claims.append("table/QR or handheld payment options visible on your site")
    if "payment_link" in signal_lowers:
        safe_email_claims.append("a public pay-online/payment-link path")
    if "appointments_or_booking" in signal_lowers:
        safe_email_claims.append("online booking or appointment language visible on your site")
    if "pricing_or_plans" in signal_lowers:
        safe_email_claims.append("public pricing, plan, or membership language visible on your site")
    if "financing" in signal_lowers:
        safe_email_claims.append("public financing/apply-now language visible on your site")
    if "sms_or_text_channel" in signal_lowers:
        safe_email_claims.append("text/SMS contact or payment language visible on your site")
    if "pharmacy_compliance_payments" in signal_lowers:
        safe_email_claims.append("pharmacy payment/compliance language visible on your site")
    if "dealership_service_payments" in signal_lowers:
        safe_email_claims.append("service-lane or text-to-pay language visible on your site")

    if not observed:
        research_questions.append("No processor/POS signal found; should a research pass inspect pages manually?")

    return {
        "observed": sorted(set(observed)),
        "inferred": sorted(set(inferred)),
        "needs_research": sorted(set(research_questions)),
        "safe_email_claims": sorted(set(safe_email_claims)),
        "do_not_say": DO_NOT_SAY,
    }


def format_scrape_evidence(evidence):
    """Render the evidence packet as a compact review-card block."""
    sections = [
        ("Observed", evidence.get("observed") or ["none"]),
        ("Inferred", evidence.get("inferred") or ["none"]),
        ("Safe to say", evidence.get("safe_email_claims") or ["none"]),
        ("Needs research", evidence.get("needs_research") or ["none"]),
        ("Do not say", evidence.get("do_not_say") or ["none"]),
    ]
    lines = []
    for title, items in sections:
        lines.append(f"{title}:")
        for item in items[:4]:
            lines.append(f"- {item}")
    return "\n".join(lines)


if __name__ == "__main__":
    demo = {
        "processor": "Toast",
        "tech_signals": json.dumps([
            "Toast", "restaurant_vertical_stack", "third_party_ordering",
            "public_manual_payment_hint", "social_url:instagram:https://example.com",
        ]),
    }
    packet = scrape_evidence(demo)
    print(json.dumps(packet, indent=2))
    print("---")
    print(format_scrape_evidence(packet))
