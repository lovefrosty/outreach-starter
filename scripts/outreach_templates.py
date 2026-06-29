#!/usr/bin/env python3
"""
outreach_templates.py - deterministic runtime routing for outreach copy.

The detailed operator playbooks stay in workspace/verticals and
workspace/patterns. This module reads templates/outreach_library.json, the
compact runtime index distilled from those playbooks, and selects a route for
the DeepSeek one-sentence Trigger generator.

Adding a vertical should normally require one JSON entry and a markdown
playbook, not a new Python prompt branch.
"""

import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

_MODULE_ROOT = Path(__file__).resolve().parents[1]


def _default_library_path():
    """Prefer the live swarm workspace, then the local source tree."""
    candidates = [
        Path.home() / ".outreach" / "workspace" / "templates" / "outreach_library.json",
        _MODULE_ROOT / "templates" / "outreach_library.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[-1]


@lru_cache(maxsize=4)
def _load_cached(path_text):
    path = Path(path_text)
    data = json.loads(path.read_text())
    if "_fallback" not in (data.get("verticals") or {}):
        raise ValueError(f"template library missing verticals._fallback: {path}")
    return data


def load_library(path=None):
    """Load and validate the outreach routing registry."""
    selected = Path(
        path
        or os.environ.get("OUTREACH_TEMPLATE_LIBRARY", "")
        or _default_library_path()
    )
    return _load_cached(str(selected.resolve()))


def normalize_vertical(vertical, library=None):
    """Map free-form vertical names to a configured runtime route."""
    lib = library or load_library()
    raw = (vertical or "").strip().lower().replace("-", " ")
    raw = " ".join(raw.split())
    aliases = lib.get("vertical_aliases") or {}
    key = aliases.get(raw, raw.replace(" ", "_"))
    return key if key in (lib.get("verticals") or {}) else "_fallback"


def normalize_pain_theme(pain_theme, library=None):
    """Map legacy pain-theme labels to the D1 vocabulary."""
    lib = library or load_library()
    raw = (pain_theme or "").strip().lower()
    return (lib.get("pain_theme_aliases") or {}).get(raw, raw)


def _is_recent_filing(filing_date, as_of=None, days=90):
    """True when a public filing date is within the recent-business window."""
    if not (filing_date or "").strip():
        return False
    try:
        parsed = datetime.fromisoformat(str(filing_date).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        now = as_of or datetime.now(timezone.utc)
        age_days = (now - parsed).days
        return 0 <= age_days <= days
    except (TypeError, ValueError):
        return False


def select_route(vertical, pain_theme="", filing_date="", as_of=None):
    """
    Return a deterministic template route for one lead.

    The route includes an auditable template key, the selected angle and CTA,
    and the markdown source file that contains the detailed playbook.
    """
    lib = load_library()
    vertical_key = normalize_vertical(vertical, lib)
    theme_key = normalize_pain_theme(pain_theme, lib)
    vertical_cfg = (lib["verticals"].get(vertical_key)
                    or lib["verticals"]["_fallback"])
    theme_cfg = (lib.get("pain_themes") or {}).get(theme_key, {})
    recent_filing = _is_recent_filing(filing_date, as_of=as_of)
    hook_key = (
        "new_business"
        if recent_filing
        else theme_cfg.get("hook_family", "cold_fit")
    )
    hook_cfg = (lib.get("hook_families") or {}).get(
        hook_key, lib["hook_families"]["cold_fit"]
    )
    override = (vertical_cfg.get("pain_overrides") or {}).get(theme_key, {})

    angle = (
        override.get("angle")
        or theme_cfg.get("angle")
        or vertical_cfg.get("default_angle")
        or hook_cfg.get("angle")
        or ""
    )
    cta = (
        override.get("cta")
        or theme_cfg.get("cta")
        or vertical_cfg.get("default_cta")
        or hook_cfg.get("cta")
        or ""
    )
    theme_token = theme_key or "default"
    variant_key = (
        "new_pain" if recent_filing and theme_key
        else "new_default" if recent_filing
        else "existing_pain" if theme_key
        else "existing_default"
    )
    template_key = (vertical_cfg.get("templates") or {}).get(variant_key)
    if not template_key:
        template_key = (
            lib["verticals"]["_fallback"]["templates"].get(variant_key)
            or "Standard1"
        )

    return {
        "template_key": template_key,
        "route_key": f"{vertical_key}:{hook_key}:{theme_token}",
        "vertical_key": vertical_key,
        "vertical_label": vertical_cfg.get("label", vertical_key),
        "source": vertical_cfg.get("source", ""),
        "product": vertical_cfg.get("product", ""),
        "sequence": vertical_cfg.get("sequence", "general_standard"),
        "hook_family": hook_key,
        "hook_label": hook_cfg.get("label", hook_key),
        "pain_theme": theme_key,
        "recent_filing": recent_filing,
        "angle": angle,
        "cta": cta,
        "instructions": [
            hook_cfg.get("instruction", ""),
            theme_cfg.get("instruction", ""),
            vertical_cfg.get("instruction", ""),
        ],
    }


def build_verified_opener(company, vertical, city_state="", website="", source=""):
    """
    Return (opener, evidence, source) using literal sourced facts only.

    Review count, rating, processor fingerprints, and pain clusters remain
    useful for routing and internal prioritization, but they must not be
    converted into claims about the prospect's operations. Public listing facts
    alone do not earn a custom opener: standard-template mode intentionally
    leaves Trigger blank until a useful verified event source is added.
    """
    company = (company or "").strip()
    city_state = (city_state or "").strip()
    website = (website or "").strip()
    source = (source or "").strip() or "public business listing"
    return "", "", source


def build_personalization_prompt(
    company,
    vertical,
    city_state,
    review_count=None,
    rating=None,
    pain_theme="",
    processor="",
    filing_date="",
):
    """Build the constrained DeepSeek prompt and return (system, user, route)."""
    route = select_route(vertical, pain_theme, filing_date)
    signals = []
    if review_count:
        try:
            if int(review_count) >= 50:
                signals.append(f"{int(review_count)} Google reviews")
        except (TypeError, ValueError):
            pass
    if rating:
        try:
            signals.append(f"{float(rating):g} star Google rating")
        except (TypeError, ValueError):
            pass
    if city_state:
        signals.append(f"location: {city_state}")
    if route["recent_filing"]:
        signals.append(f"public filing date: {filing_date}")

    evidence = "; ".join(signals) if signals else "No lead-specific public listing signal supplied."
    processor_note = (
        f"Website fingerprint: {processor}. Treat this as internal context only; "
        "do not name it in the opener."
        if (processor or "").strip()
        else "No processor fingerprint supplied."
    )
    instructions = "\n".join(
        f"- {line}" for line in route["instructions"] if line
    )

    system = """\
You write one-sentence opening lines for plain-text cold emails from Green PayTech.
Use only the supplied public evidence and the approved template route.

Rules:
- Output exactly one sentence of plain text and nothing else
- Maximum 32 words
- No greeting, no hype, no exclamation point, no em dash
- Never write "I noticed", "I saw", "I came across", or "As a leading"
- Do not invent a current processor, delivery platform, operational problem, customer complaint, savings amount, or client result
- Treat the selected pain theme as internal framing, not a verified fact about the lead
- State only literal supplied facts. Do not infer transaction volume, operational maturity, or business pain from review counts, ratings, location, or vertical.
"""

    user = f"""\
Business: {company}
City/State: {city_state}
Original vertical: {vertical}
Runtime vertical: {route['vertical_label']}
Detailed playbook: {route['source']}
Swarm template: {route['template_key']}
Runtime route: {route['route_key']}
Hook family: {route['hook_label']}
Approved angle: {route['angle']}
Campaign CTA: {route['cta']}
Public evidence: {evidence}
Internal context: {processor_note}
Route-specific instructions:
{instructions or '- Keep the opener broad and evidence-backed.'}

Write one sentence.
"""
    return system, user, route
