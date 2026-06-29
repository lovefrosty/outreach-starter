#!/usr/bin/env python3
"""Approved first-touch email bodies for Acme outbound.

The active first-touch standard is problem-focused, offer-led, and capped at
80 words for the message body plus CTA, excluding greeting and signoff.
"""

import re
from hashlib import sha256

SOURCE_DOCUMENT = "workspace/reports/acme-outbound-scale-roadmap-2026-06-28.md"
TEMPLATE_VERSION = "2026-06-28-savings-80w-v3"
APPROVED_SEQUENCES = frozenset({
    "restaurant_default",
    "pharmacy_default",
    "dealership_default",
    "general_standard",
})
ROBOTIC_PHRASES = (
    "i've been speaking with",
    "i have been speaking with",
    "many pharmacies",
    "several high-volume",
    "we view our",
    "10,000+ merchants",
    "as partners not customers",
    "substantially less",
)
BULLET_CHARS = ("•", "-", "*")


class CopyPolicyError(ValueError):
    """Raised when approved outbound copy violates the live copy policy."""


def _name(value):
    return (value or "").strip() or "there"


def _city(value):
    return (value or "").strip() or "your area"


def _clean_sentence(value):
    return " ".join((value or "").strip().split())


def _usable_sentence(value, default, require_question=False):
    value = _clean_sentence(value)
    if not value:
        return default
    if require_question:
        return value if value.endswith("?") and len(value.split()) >= 5 else default
    return value if value.endswith((".", "?")) and len(value.split()) >= 6 else default


def _body_word_count(text):
    """Count the message body + CTA, excluding greeting and signoff."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    body_lines = [
        line for line in lines
        if not line.lower().startswith("hi ")
        and line.lower() not in {"gabriella", "green paytech"}
    ]
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", " ".join(body_lines))
    return len(words)


def validate_body(text, max_words=80):
    """Validate the approved first-touch copy policy."""
    count = _body_word_count(text)
    if count > max_words:
        raise CopyPolicyError(f"body exceeds {max_words} words: {count}")
    if text.count("?") != 1:
        raise CopyPolicyError("body must contain exactly one CTA question")
    lowered = text.lower()
    for phrase in ROBOTIC_PHRASES:
        if phrase in lowered:
            raise CopyPolicyError(f"robotic or bloated phrase not allowed: {phrase}")
    for line in text.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(char) for char in BULLET_CHARS):
            raise CopyPolicyError("bullet lists are not allowed")
    return True


def _verified_callout(trigger, fallback):
    trigger = _clean_sentence(trigger)
    trigger = trigger.replace("?", ".")
    return trigger if trigger else fallback


def _compose(first_name, callout, problem, outcome, cta):
    body = (
        f"Hi {first_name},\n\n"
        f"{callout} {problem} {outcome} {cta}\n\n"
        "Gabriella\n"
        "Green PayTech"
    )
    validate_body(body)
    return body


def first_touch_body(
    sequence,
    first_name="{{first_name}}",
    city_state="{{CityState}}",
    trigger="",
    email_angle="",
    template_cta="",
):
    """Return approved problem-focused copy for one live vertical sequence."""
    first_name = _name(first_name)
    city_state = _city(city_state)
    if sequence == "dealership_default":
        return _compose(
            first_name,
            _verified_callout(trigger, f"Your dealership in {city_state} has advisors, customers, ROs, invoices, and payments moving through the same day."),
            "When approvals and collections sit in separate tools, closeouts slow down and reconciliation gets messy.",
            _usable_sentence(email_angle, "Green PayTech can review one recent statement to show how much you could save and show how ExampleProduct ties approvals, payments, and deposits into one dealership workflow."),
            _usable_sentence(template_cta, "Open to a free savings quote and demo?", require_question=True),
        )
    if sequence == "restaurant_default":
        return _compose(
            first_name,
            _verified_callout(trigger, f"Your team is serving guests in {city_state}, where every payment handoff affects speed and margin."),
            "When POS, payments, rewards, and reporting sit apart, fees stay harder to benchmark and staff loses time.",
            _usable_sentence(email_angle, "Green PayTech can review one recent statement to show how much you could save and show how Union brings ordering, payments, and guest data into one restaurant system."),
            _usable_sentence(template_cta, "Open to a free savings quote and short demo?", require_question=True),
        )
    if sequence == "pharmacy_default":
        return _compose(
            first_name,
            _verified_callout(trigger, f"Your pharmacy in {city_state} has checkout, inventory, patient data, and payments competing for attention at the counter."),
            "When those systems stay separate, fees are harder to compare and reporting takes longer.",
            _usable_sentence(email_angle, "Green PayTech can review one recent statement to show how much you could save and show how ExampleProduct brings payments, inventory, signatures, and history into one pharmacy workflow."),
            _usable_sentence(template_cta, "Open to a free savings quote and workflow demo?", require_question=True),
        )
    if sequence == "general_standard":
        return _compose(
            first_name,
            _verified_callout(trigger, f"Your business in {city_state} has payments, statements, and reporting that determine what you keep each month."),
            "When fees are hard to compare, it is easy to miss avoidable costs or workflow drag.",
            _usable_sentence(email_angle, "Green PayTech can review one recent statement to show how much you could save."),
            _usable_sentence(template_cta, "Open to a free savings quote to see whether we can lower your expected rate?", require_question=True),
        )
    raise KeyError(f"no approved first-touch template for sequence: {sequence}")


def template_checksum(sequence):
    """Stable checksum for future live-copy drift checks."""
    return sha256(first_touch_body(sequence).encode()).hexdigest()
