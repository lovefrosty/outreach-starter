#!/usr/bin/env python3
"""
base.py — the SourceAdapter contract every lead source implements.

Adding a source = subclass SourceAdapter, implement `_fetch`, register it in
sources.json. Adapters NEVER touch the DB — they only return normalized rows.
That keeps each source a pure, testable function.

Source-first acquisition rule:
    directory, license, marketplace, association, locator, and aggregator
    adapters create seed rows before C2. They do not scrape official websites
    and they do not decide that a lead is send-ready. C2 remains the official
    website scraper. A seed can become a C2 `pulled` candidate only after a
    resolver records strong match evidence such as website, Google Place ID,
    phone, address, official license/entity ID, or a similarly durable public
    identifier. No-website rows stay in a seeded/research-needed lane.

Normalized lead schema (every adapter returns dicts with these keys; missing =
empty string / None):
    company, website, domain, phone, city_state, vertical,
    rating, review_count, owner_name, filing_date, processor,
    reviews (list[str]), source,
    address, source_url, source_type, external_id, source_record_id,
    seed_stage, seed_confidence, dedupe_key, promotion_evidence,
    automatic_lead_promotion_allowed, brand, group_owner,
    directory_clues, workflow_clues, decision_maker_signals, social_links

Stdlib only.
"""

import os
import re
import sys
import json
from pathlib import Path

# reuse phone normalizer from the DB layer
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import lead_store as ls  # noqa: E402

NORMALIZED_FIELDS = (
    "company", "website", "domain", "phone", "city_state", "vertical",
    "rating", "review_count", "owner_name", "filing_date", "processor",
    "reviews", "source", "address", "source_url", "source_type",
    "external_id", "source_record_id", "seed_stage", "seed_confidence",
    "dedupe_key", "promotion_evidence", "automatic_lead_promotion_allowed",
    "brand", "group_owner", "directory_clues", "workflow_clues",
    "decision_maker_signals", "social_links",
)

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "sources.json"


def extract_domain(website):
    if not website:
        return ""
    m = re.search(r"https?://(?:www\.)?([^/?#]+)", website.strip(), re.I)
    if m:
        return m.group(1).lower()
    # bare domain with no scheme
    m = re.match(r"(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", website.strip(), re.I)
    return m.group(1).lower() if m else ""


def normalize(raw, source):
    """Coerce an adapter's raw dict into the canonical normalized schema."""
    website = (raw.get("website") or "").strip()
    out = {k: "" for k in NORMALIZED_FIELDS}
    out.update({
        "company":      (raw.get("company") or "").strip(),
        "website":      website,
        "domain":       raw.get("domain") or extract_domain(website),
        "phone":        ls.norm_phone(raw.get("phone")),
        "city_state":   (raw.get("city_state") or "").strip(),
        "vertical":     (raw.get("vertical") or "").strip(),
        "rating":       raw.get("rating"),
        "review_count": raw.get("review_count"),
        "owner_name":   (raw.get("owner_name") or "").strip(),
        "filing_date":  (raw.get("filing_date") or "").strip(),
        "processor":    (raw.get("processor") or "").strip(),
        "reviews":      raw.get("reviews") or [],
        "source":       source,
        "address":      (raw.get("address") or "").strip(),
        "source_url":   (raw.get("source_url") or "").strip(),
        "source_type":  (raw.get("source_type") or "").strip(),
        "external_id":  (raw.get("external_id") or "").strip(),
        "source_record_id": (raw.get("source_record_id") or "").strip(),
        "seed_stage":   (raw.get("seed_stage") or "").strip(),
        "seed_confidence": (raw.get("seed_confidence") or "").strip(),
        "dedupe_key":   (raw.get("dedupe_key") or "").strip(),
        "promotion_evidence": raw.get("promotion_evidence") or [],
        "automatic_lead_promotion_allowed": bool(raw.get("automatic_lead_promotion_allowed")),
        "brand":        (raw.get("brand") or "").strip(),
        "group_owner":  (raw.get("group_owner") or "").strip(),
        "directory_clues": raw.get("directory_clues") or [],
        "workflow_clues": raw.get("workflow_clues") or [],
        "decision_maker_signals": raw.get("decision_maker_signals") or [],
        "social_links": raw.get("social_links") or [],
    })
    return out


class SourceAdapter:
    """Base class. Subclasses set `name` and implement `_fetch`."""
    name = "base"
    # what signals this source provides (documentation / routing)
    gets = ()

    def __init__(self, config=None):
        self.config = config or {}

    def _fetch(self, params):
        """Return a list of RAW dicts. Override in subclass."""
        raise NotImplementedError

    def fetch(self, params):
        """Public entry: fetch raw rows, return normalized rows."""
        rows = self._fetch(params) or []
        return [normalize(r, self.name) for r in rows]


# ── Registry ────────────────────────────────────────────────────────────────

def load_registry(path=REGISTRY_PATH):
    if not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text()).get("sources", {})


def active_sources(registry=None):
    reg = registry if registry is not None else load_registry()
    return [name for name, cfg in reg.items() if cfg.get("active")]


if __name__ == "__main__":
    reg = load_registry()
    print("registry:", json.dumps(reg, indent=2))
    print("active:", active_sources(reg))
