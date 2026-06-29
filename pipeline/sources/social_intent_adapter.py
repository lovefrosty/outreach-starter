#!/usr/bin/env python3
"""Public social/web intent adapter.

This adapter is research-only. It normalizes public observations into evidence
envelopes and never writes leads, queues outreach, or mutates the live CRM.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


SCHEMA_VERSION = "outreach.social-intent-signal.v1"
PUBLIC_SOCIAL_HOSTS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "tiktok.com",
)
INTENT_PATTERNS = {
    "hiring": re.compile(r"\b(hiring|join our team|now hiring|careers?)\b", re.I),
    "new_location": re.compile(r"\b(grand opening|new location|coming soon|now open)\b", re.I),
    "payment_or_checkout": re.compile(r"\b(pay online|payment link|checkout|invoice|tap to pay|qr pay)\b", re.I),
    "operations_change": re.compile(r"\b(new system|upgrade|renovation|remodel|expanded hours|online ordering)\b", re.I),
}


class SocialIntentError(ValueError):
    """Raised when a public signal cannot be normalized safely."""


def _now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_public_url(value):
    parsed = urlparse(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _host(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def _source_kind(url):
    host = _host(url)
    if any(host == domain or host.endswith("." + domain) for domain in PUBLIC_SOCIAL_HOSTS):
        return "public_social"
    return "public_web"


def _sha256(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _detected_intents(text):
    return [name for name, pattern in INTENT_PATTERNS.items() if pattern.search(text or "")]


def normalize_signal(raw):
    """Normalize one public observation into a research-only evidence packet."""
    source_url = str(raw.get("source_url") or "").strip()
    if not _valid_public_url(source_url):
        raise SocialIntentError("social intent signal requires a public http(s) source_url")
    observed_text = re.sub(r"\s+", " ", str(raw.get("observed_text") or "")).strip()
    if not observed_text:
        raise SocialIntentError("social intent signal requires observed_text")
    captured_at = str(raw.get("captured_at") or _now_utc())
    company = str(raw.get("company") or "").strip()
    intents = _detected_intents(observed_text)
    content_sha256 = raw.get("content_sha256") or _sha256(observed_text)
    locator = str(raw.get("locator") or raw.get("source_url") or "").strip()
    evidence_ref = {
        "url": source_url,
        "locator": locator,
        "content_sha256": content_sha256,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "company": company,
        "source": {
            "kind": _source_kind(source_url),
            "source_url": source_url,
            "captured_at": captured_at,
            "source_artifact_sha256": content_sha256,
        },
        "observed": {
            "public_text_excerpt": observed_text[:500],
            "detected_intents": intents,
        },
        "inferred": [
            f"Possible {intent.replace('_', ' ')} signal; research-only until verified."
            for intent in intents
        ],
        "needs_research": [
            "Verify whether this public signal is current, relevant, and tied to a real outreach reason."
        ] if intents else [
            "No supported intent pattern detected; keep as context only."
        ],
        "do_not_do": [
            "Do not queue, send, approve, or mutate live outreach from this signal.",
            "Do not state private business problems from public social/web text alone.",
        ],
        "evidence_refs": [evidence_ref],
        "captured_at": captured_at,
    }


class SocialIntentAdapter:
    """Pure adapter: accepts fixture/public observations and returns envelopes."""

    name = "social_intent"
    gets = ("public_social_signals", "public_web_signals", "research_intent")

    def __init__(self, config=None):
        self.config = config or {}

    def fetch(self, params):
        rows = params.get("fixture_rows")
        if rows is None:
            rows = params.get("signals")
        if rows is None:
            raise SocialIntentError(
                "SocialIntentAdapter is fixture/input driven for now; pass fixture_rows or signals."
            )
        return [normalize_signal(row) for row in rows]
