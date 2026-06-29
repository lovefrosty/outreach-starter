#!/usr/bin/env python3
"""Shared helpers for source-first lead seed adapters.

These helpers keep directory/license/marketplace seed generation deterministic.
They do not fetch remote pages, write databases, approve leads, queue leads, or
send email.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse


STRONG_MATCH_FIELDS = (
    "website",
    "google_place_id",
    "phone",
    "address",
    "official_license_id",
    "official_entity_id",
)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_list(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return [clean(value) for value in values if clean(value)]


def source_row_hash(row):
    payload = json.dumps(
        {str(k).strip().lower(): clean(v) for k, v in dict(row).items()},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def stable_dedupe_key(*parts):
    cleaned = [clean(part).lower() for part in parts if clean(part)]
    if not cleaned:
        return ""
    return hashlib.sha256("|".join(cleaned).encode("utf-8")).hexdigest()


def valid_http_url(value):
    parsed = urlparse(clean(value))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def promotion_evidence(raw):
    evidence = []
    if valid_http_url(raw.get("website")):
        evidence.append("website")
    if clean(raw.get("google_place_id") or raw.get("place_id")):
        evidence.append("google_place_id")
    if clean(raw.get("phone")):
        evidence.append("phone")
    if clean(raw.get("address")):
        evidence.append("address")
    if clean(raw.get("license_id") or raw.get("license_number") or raw.get("official_license_id")):
        evidence.append("official_license_id")
    if clean(raw.get("entity_id") or raw.get("official_entity_id")):
        evidence.append("official_entity_id")
    return evidence


def seed_stage(raw):
    """Return the pre-C2 lane for a source row.

    Website-backed rows are C2 candidates, but adapters still mark automatic
    promotion false. Rows without websites remain research seeds until a
    resolver or human records durable match evidence.
    """
    return "pulled_candidate" if valid_http_url(raw.get("website")) else "seeded_research_needed"


def seed_confidence(raw):
    evidence = set(promotion_evidence(raw))
    if "website" in evidence and ("phone" in evidence or "address" in evidence):
        return "high"
    if "google_place_id" in evidence and ("phone" in evidence or "address" in evidence):
        return "high"
    if "official_license_id" in evidence and ("phone" in evidence or "address" in evidence):
        return "medium"
    if "phone" in evidence and "address" in evidence:
        return "medium"
    if evidence:
        return "low"
    return "research_needed"


def evidence_note(source_url, source_type, external_id, row, extra=None):
    parts = []
    if source_url:
        parts.append(f"source={source_url}")
    if source_type:
        parts.append(f"type={source_type}")
    if external_id:
        parts.append(f"external_id={external_id}")
    for key, value in (extra or {}).items():
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(clean_list(value))
        if clean(value):
            parts.append(f"{key}={clean(value)}")
    parts.append(f"source_row_hash={source_row_hash(row)}")
    return " | ".join(parts)


def seed_fields(raw, source_type, source_url, external_id, dedupe_parts):
    return {
        "source_type": source_type,
        "source_url": source_url,
        "external_id": external_id,
        "source_record_id": external_id,
        "seed_stage": seed_stage(raw),
        "seed_confidence": seed_confidence(raw),
        "dedupe_key": stable_dedupe_key(*dedupe_parts),
        "promotion_evidence": promotion_evidence(raw),
        "automatic_lead_promotion_allowed": False,
    }
