#!/usr/bin/env python3
"""Compile scraper evidence and a versioned style into a Higgsfield-ready brief.

This module never calls Higgsfield, spends credits, publishes media, or contacts
prospects. It creates a deterministic review artifact for the control plane.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
sys.path.insert(0, str(PIPELINE_DIR / "ds"))

import evidence as scrape_evidence_module  # noqa: E402


SCHEMA_VERSION = "outreach.campaign-brief.v1"
SIDECAR_SCHEMA_VERSION = "outreach.campaign-sidecar.v1"
DEFAULT_STYLES = REPO_ROOT / "workspace/config/campaign_styles.json"
DEFAULT_TEAM = REPO_ROOT / "workspace/config/marketing_team.json"
DEFAULT_EMAIL_REGISTRY = REPO_ROOT / "workspace/config/email_template_registry.json"
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
PUBLIC_EVIDENCE_FIELDS = (
    "company",
    "website",
    "source_urls",
    "processor",
    "tech_signals",
    "field_sources",
)
PROVENANCE_POLICY = {
    "accepted_input_fields": list(PUBLIC_EVIDENCE_FIELDS),
    "unknown_input_fields_used_as_evidence": False,
    "claim_source_granularity": "field",
    "source_level_claim_mapping_required": True,
}
SAFE_CLAIM_SIGNAL_MAP = {
    "table/QR or handheld payment options visible on your site": "table_or_qr_pay",
    "a public pay-online/payment-link path": "payment_link",
    "pharmacy payment/compliance language visible on your site": "pharmacy_compliance_payments",
    "service-lane or text-to-pay language visible on your site": "dealership_service_payments",
}


class CampaignStudioError(ValueError):
    """Raised when campaign input, styles, or safety rules are invalid."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_value(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CampaignStudioError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CampaignStudioError(f"Invalid JSON in {path}: {exc}") from exc


def _write_json(path, payload):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _valid_url(value):
    parsed = urlparse(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _require_text(payload, key):
    value = str(payload.get(key) or "").strip()
    if not value:
        raise CampaignStudioError(f"campaign input requires `{key}`")
    return value


def load_style(styles_path, brand_id, style_id):
    registry = _read_json(styles_path)
    if registry.get("schema_version") != "outreach.campaign-styles.v1":
        raise CampaignStudioError("unsupported campaign style registry schema")
    brand = (registry.get("brands") or {}).get(brand_id)
    if not brand:
        raise CampaignStudioError(f"unknown brand: {brand_id}")
    style = (registry.get("styles") or {}).get(style_id)
    if not style:
        raise CampaignStudioError(f"unknown campaign style: {style_id}")
    return registry, brand, style


def load_marketing_team(team_path=DEFAULT_TEAM):
    team = _read_json(team_path)
    if team.get("schema_version") != "outreach.marketing-team.v1":
        raise CampaignStudioError("unsupported marketing team schema")
    if not team.get("roles") or not team.get("deliverables"):
        raise CampaignStudioError("marketing team requires roles and deliverables")
    return team


def load_email_template_registry(registry_path=DEFAULT_EMAIL_REGISTRY):
    registry = _read_json(registry_path)
    if registry.get("schema_version") != "outreach.email-template-registry.v1":
        raise CampaignStudioError("unsupported email template registry schema")
    if not registry.get("routes") or not registry.get("approved_sequences"):
        raise CampaignStudioError("email template registry has no routes or approved sequences")
    return registry


def validate_email_mapping(template_key, sequence, registry):
    template_key = str(template_key or "").strip()
    sequence = str(sequence or "").strip()
    if bool(template_key) != bool(sequence):
        raise CampaignStudioError(
            "email_template_key and email_sequence must be supplied together"
        )
    if not template_key:
        return None
    matching_routes = [
        route for route in registry["routes"]
        if route.get("template_key") == template_key and route.get("sequence") == sequence
    ]
    if not matching_routes:
        raise CampaignStudioError(
            f"email template mapping is not emitted by the captured router: {template_key}/{sequence}"
        )
    if sequence not in set(registry["approved_sequences"]):
        raise CampaignStudioError(f"email: unsupported_sequence: {sequence}")
    return {
        "template_key": template_key,
        "sequence": sequence,
        "routes": [
            {"vertical": route["vertical"], "variant": route["variant"]}
            for route in matching_routes
        ],
    }


def normalize_record(record):
    company = str(record.get("company") or "").strip()
    website = str(record.get("website") or "").strip()
    source_urls = [str(item).strip() for item in record.get("source_urls") or []]
    if website and website not in source_urls:
        source_urls.insert(0, website)
    source_urls = list(dict.fromkeys(item for item in source_urls if item))
    if not company:
        raise CampaignStudioError("each research record requires `company`")
    if not source_urls or any(not _valid_url(item) for item in source_urls):
        raise CampaignStudioError(f"research record `{company}` requires valid HTTP source URLs")
    tech_signals = record.get("tech_signals") or []
    if not isinstance(tech_signals, list):
        raise CampaignStudioError(f"research record `{company}` requires a tech_signals list")
    tech_signals = [str(item).strip() for item in tech_signals if str(item).strip()]
    raw_field_sources = record.get("field_sources") or {}
    if not isinstance(raw_field_sources, dict):
        raise CampaignStudioError(f"research record `{company}` requires field_sources")
    processor = str(record.get("processor") or "").strip()
    required_fields = (["processor"] if processor else []) + [
        f"tech_signals.{signal}" for signal in tech_signals
    ]
    normalized_field_sources = {}
    for field in required_fields:
        if field == "processor":
            values = raw_field_sources.get("processor") or []
        else:
            signal = field.removeprefix("tech_signals.")
            values = (raw_field_sources.get("tech_signals") or {}).get(signal) or []
        if not isinstance(values, list) or not values:
            raise CampaignStudioError(
                f"research record `{company}` requires field source URLs for `{field}`"
            )
        urls = list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
        if any(not _valid_url(url) or url not in source_urls for url in urls):
            raise CampaignStudioError(
                f"research record `{company}` has invalid field source URLs for `{field}`"
            )
        normalized_field_sources[field] = urls
    normalized = {
        "company": company,
        "website": website,
        "source_urls": source_urls,
        "processor": processor,
        "tech_signals": tech_signals,
        "field_sources": normalized_field_sources,
        "_excluded_input_fields": sorted(
            str(key) for key in record if key not in PUBLIC_EVIDENCE_FIELDS
        ),
    }
    return normalized


def _packet_item_sources(record, text, section):
    processor = record["processor"]
    if processor and text in {
        f"Processor/POS fingerprint detected: {processor}",
        f"your current {processor} setup",
    }:
        return record["field_sources"]["processor"]

    for signal in record["tech_signals"]:
        lower = signal.lower()
        field = f"tech_signals.{signal}"
        if text == scrape_evidence_module.OBSERVED_SIGNAL_LABELS.get(lower):
            return record["field_sources"][field]
        if lower.startswith("social_url:") and text.startswith("Social profile link observed:"):
            return record["field_sources"][field]
        if lower.startswith("public_doc_url:") and text == "Public document link observed on website":
            return record["field_sources"][field]
        if not processor and lower in scrape_evidence_module.PROCESSOR_HINTS:
            if text == f"Processor/POS hint detected: {signal}":
                return record["field_sources"][field]
        if section == "safe_claims" and SAFE_CLAIM_SIGNAL_MAP.get(text) == lower:
            return record["field_sources"][field]
    raise CampaignStudioError(
        f"could not resolve field-level provenance for `{record['company']}` evidence: {text}"
    )


def aggregate_research(records):
    observed = []
    safe_claims = []
    hypotheses = []
    forbidden_claims = set()
    sources = []
    input_exclusions = []
    for raw_record in records:
        record = normalize_record(raw_record)
        packet = scrape_evidence_module.scrape_evidence(record)
        source_refs = [
            {"company": record["company"], "url": url}
            for url in record["source_urls"]
        ]
        sources.extend(source_refs)
        if record["_excluded_input_fields"]:
            input_exclusions.append(
                {
                    "company": record["company"],
                    "fields": record["_excluded_input_fields"],
                    "values_retained": False,
                }
            )
        for item in packet["observed"]:
            item_sources = _packet_item_sources(record, item, "observed")
            observed.append(
                {
                    "text": item,
                    "company": record["company"],
                    "sources": item_sources,
                    "evidence_class": "public_observation",
                    "provenance_granularity": "field",
                }
            )
        for item in packet["safe_email_claims"]:
            item_sources = _packet_item_sources(record, item, "safe_claims")
            safe_claims.append(
                {
                    "text": item,
                    "company": record["company"],
                    "sources": item_sources,
                    "evidence_class": "public_claim_candidate",
                    "provenance_granularity": "field",
                }
            )
        for item in packet["inferred"] + packet["needs_research"]:
            hypotheses.append(
                {
                    "text": item,
                    "company": record["company"],
                    "evidence_class": "hypothesis",
                    "usable_as_claim": False,
                }
            )
        forbidden_claims.update(packet["do_not_say"])
    research = {
        "sample_size": len(records),
        "observed": observed,
        "safe_claims": safe_claims,
        "hypotheses": hypotheses,
        "forbidden_claims": sorted(forbidden_claims),
        "sources": sources,
        "input_exclusions": input_exclusions,
        "provenance_policy": PROVENANCE_POLICY,
    }
    research["evidence_sha256"] = _sha256_value(research)
    return research


def _evidence_context(research):
    claims = [item["text"] for item in research["safe_claims"]]
    if not claims:
        return "No public claim is approved for copy. Use only brand-level visual metaphor."
    joined = "; ".join(claims[:4])
    return (
        "Public-sample context, not a claim about the whole audience: " + joined + ". "
        "Do not name or depict sampled merchants."
    )


def _build_prompt(brand, style, campaign, research, channel, mode):
    visual = "; ".join(style["visual_grammar"])
    shots = " -> ".join(style["shot_grammar"])
    required = " ".join(brand["required"])
    forbidden = " ".join(brand["forbidden"] + style.get("avoid", []))
    return (
        f"Create a {channel['duration_seconds']}-second {mode} campaign concept for "
        f"{brand['name']} in {channel['aspect_ratio']} format for {channel['id']}. "
        f"Audience: {campaign['audience']}. Objective: {campaign['objective']}. "
        f"Offer and CTA: {campaign['offer']}. Creative thesis: {style['thesis']} "
        f"Visual grammar: {visual}. Shot progression: {shots}. "
        f"Evidence boundary: {_evidence_context(research)} "
        f"Required behavior: {required} Prohibited behavior: {forbidden} "
        "Any synthetic person must be clearly fictional and must not imply a real testimonial. "
        "Return a storyboard, on-screen copy, voiceover, shot list, and generation-ready visual direction."
    )


def build_team_plan(team, campaign_id):
    workstreams = []
    for role in team["roles"]:
        workstreams.append(
            {
                "role_id": role["id"],
                "role": role["name"],
                "owns": role["owns"],
                "status": "draft",
                "campaign_id": campaign_id,
                "human_accountable": True,
            }
        )
    return {
        "operating_model": "specialist agents prepare evidence and drafts; humans approve external actions",
        "workstreams": workstreams,
        "deliverables": team["deliverables"],
    }


def build_channel_system(team, campaign, research, style_ref):
    safe_claims = [item["text"] for item in research["safe_claims"]]
    message_anchor = safe_claims[0] if safe_claims else "an evidence-led review of the current setup"
    return {
        "shared_message": {
            "campaign_id": campaign["campaign_id"],
            "style": style_ref,
            "objective": campaign["objective"],
            "offer": campaign["offer"],
            "evidence_anchor": message_anchor,
        },
        "channel_briefs": [
            {
                "id": "email-entry-point",
                "role": "conversation opener",
                "brief": (
                    "Open with one source-backed observation or a neutral operational question. "
                    "Do not compress the entire brand story into the email. Route interest to the "
                    "campaign landing page or the narrowest useful resource."
                ),
                "status": "draft_pending_human",
            },
            {
                "id": "landing-page-story",
                "role": "durable campaign explanation",
                "brief": (
                    "Explain the campaign question, evidence boundary, review process, and next step. "
                    "Carry the same style and offer as email and video."
                ),
                "status": "draft_pending_human",
            },
            {
                "id": "founder-and-social-content",
                "role": "public education and brand familiarity",
                "brief": (
                    "Turn research questions into educational posts, diagrams, and short videos. "
                    "Never identify sampled merchants without rights."
                ),
                "status": "draft_pending_human",
            },
            {
                "id": "sales-conversation-resource",
                "role": "reply and meeting support",
                "brief": "Select a resource by reply intent rather than sending the same follow-up to everyone.",
                "status": "draft_pending_human",
            },
        ],
        "reply_system": {
            "intents": team["reply_intents"],
            "resources": team["resource_library"],
            "automatic_response_allowed": False,
            "booking_is_not_completion": "A booking must retain campaign, evidence, objection, and resource context.",
        },
    }


def compile_campaign(payload, styles_path=DEFAULT_STYLES, team_path=DEFAULT_TEAM):
    campaign_id = _require_text(payload, "campaign_id")
    if not SAFE_ID_RE.match(campaign_id):
        raise CampaignStudioError("campaign_id must be a lowercase kebab-case identifier")
    brand_id = _require_text(payload, "brand_id")
    style_id = _require_text(payload, "style_id")
    campaign = {
        "audience": _require_text(payload, "audience"),
        "objective": _require_text(payload, "objective"),
        "offer": _require_text(payload, "offer"),
    }
    records = payload.get("research_records") or []
    if not isinstance(records, list) or not records:
        raise CampaignStudioError("campaign input requires at least one research record")

    registry, brand, style = load_style(styles_path, brand_id, style_id)
    team = load_marketing_team(team_path)
    research = aggregate_research(records)
    provider = registry["provider"]
    jobs = []
    modes = style.get("higgsfield_modes") or []
    if not modes:
        raise CampaignStudioError(f"style `{style_id}` has no Higgsfield modes")
    for index, channel in enumerate(style.get("channels") or []):
        duration = int(channel["duration_seconds"])
        if duration > int(provider["max_duration_seconds"]):
            raise CampaignStudioError(f"channel `{channel['id']}` exceeds provider duration limit")
        mode = modes[index % len(modes)]
        jobs.append(
            {
                "variant_id": f"{campaign_id}-{channel['id']}-{index + 1:02d}",
                "capability": provider["capability"],
                "mode": mode,
                "channel": channel["id"],
                "aspect_ratio": channel["aspect_ratio"],
                "duration_seconds": duration,
                "model_selection": "provider_auto",
                "prompt": _build_prompt(brand, style, campaign, research, channel, mode),
                "reference_assets": [],
                "status": "draft",
            }
        )

    style_ref = {
        "id": style_id,
        "version": style["version"],
        "sha256": _sha256_value(style),
    }
    campaign_context = dict(campaign)
    campaign_context["campaign_id"] = campaign_id
    brief = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "created_at": _now(),
        "status": "draft_pending_human",
        "brand": {"id": brand_id, "name": brand["name"]},
        "style": style_ref,
        "input_sha256": _sha256_value(payload),
        "campaign": campaign,
        "research": research,
        "creative_direction": {
            "name": style["name"],
            "thesis": style["thesis"],
            "voice": list(dict.fromkeys(brand["voice"] + style["voice"])),
            "visual_grammar": style["visual_grammar"],
            "shot_grammar": style["shot_grammar"],
        },
        "marketing_team": build_team_plan(team, campaign_id),
        "brand_channel_system": build_channel_system(team, campaign_context, research, style_ref),
        "render_manifest": {
            "provider": provider["id"],
            "transport": provider["transport"],
            "connector_url": provider["connector_url"],
            "ready_to_submit": False,
            "blocked_by": ["claim_review", "rights_review", "human_campaign_approval"],
            "jobs": jobs,
        },
        "approval_gates": [
            {
                "id": "claim_review",
                "question": "Does every factual statement stay inside the source-backed evidence boundary?",
                "status": "pending",
            },
            {
                "id": "rights_review",
                "question": "Are all logos, people, locations, music, and references owned or licensed?",
                "status": "pending",
            },
            {
                "id": "human_campaign_approval",
                "question": "Does a human approve this style, offer, audience, spend, and render request?",
                "status": "pending",
            },
            {
                "id": "asset_review",
                "question": "After rendering, do assets match the brief without hallucinated text or claims?",
                "status": "not_started",
            },
            {
                "id": "publish_approval",
                "question": "Does a human approve the exact final assets and channel launch?",
                "status": "not_started",
            },
        ],
        "launch_policy": {
            "render_requires_human_approval": True,
            "publish_requires_separate_human_approval": True,
            "automatic_spend_allowed": False,
            "automatic_publish_allowed": False,
        },
        "evaluation": {
            "creative": ["hook clarity", "brand distinctiveness", "evidence fidelity", "visual coherence"],
            "delivery": ["three-second hold", "completion rate", "qualified response rate", "booked review rate"],
            "learning_rule": "Promote a style only on verified downstream value, not generation quality alone.",
        },
    }
    brief["brief_sha256"] = _sha256_value(brief)
    return brief


def verify_brief(brief):
    failures = []
    fingerprinted = dict(brief)
    expected_brief_sha256 = fingerprinted.pop("brief_sha256", None)
    if expected_brief_sha256 != _sha256_value(fingerprinted):
        failures.append("campaign brief fingerprint is stale")
    if brief.get("schema_version") != SCHEMA_VERSION:
        failures.append("unsupported brief schema")
    manifest = brief.get("render_manifest") or {}
    if manifest.get("ready_to_submit") is not False:
        failures.append("unapproved brief is renderable")
    policy = brief.get("launch_policy") or {}
    if policy.get("automatic_publish_allowed") is not False:
        failures.append("automatic publishing is enabled")
    if policy.get("automatic_spend_allowed") is not False:
        failures.append("automatic spend is enabled")
    reply_system = (brief.get("brand_channel_system") or {}).get("reply_system") or {}
    if reply_system.get("automatic_response_allowed") is not False:
        failures.append("automatic reply sending is enabled")
    research = brief.get("research") or {}
    evidence_payload = dict(research)
    expected_evidence_sha256 = evidence_payload.pop("evidence_sha256", None)
    if expected_evidence_sha256 != _sha256_value(evidence_payload):
        failures.append("research evidence fingerprint is stale")
    if research.get("provenance_policy") != PROVENANCE_POLICY:
        failures.append("research provenance policy is missing or unsupported")
    source_urls = [item.get("url") for item in research.get("sources") or []]
    if not source_urls or any(not _valid_url(item) for item in source_urls):
        failures.append("research provenance is missing or invalid")
    source_url_set = set(source_urls)
    for section, evidence_class in (
        ("observed", "public_observation"),
        ("safe_claims", "public_claim_candidate"),
    ):
        for item in research.get(section) or []:
            item_sources = item.get("sources") or []
            if item.get("evidence_class") != evidence_class:
                failures.append(f"{section} item has an invalid evidence class")
            if item.get("provenance_granularity") != "field":
                failures.append(f"{section} item has unsupported provenance granularity")
            if not item_sources or any(
                not _valid_url(url) or url not in source_url_set for url in item_sources
            ):
                failures.append(f"{section} item has invalid claim provenance")
    hypotheses = [item.get("text", "") for item in research.get("hypotheses") or []]
    for item in research.get("hypotheses") or []:
        if item.get("evidence_class") != "hypothesis" or item.get("usable_as_claim") is not False:
            failures.append("hypothesis classification is invalid")
    for job in manifest.get("jobs") or []:
        prompt = job.get("prompt") or ""
        if any(item and item in prompt for item in hypotheses):
            failures.append(f"hypothesis leaked into prompt: {job.get('variant_id')}")
        if int(job.get("duration_seconds", 0)) > 15:
            failures.append(f"provider duration exceeded: {job.get('variant_id')}")
    return {"passed": not failures, "failures": failures}


def build_draft_sidecar(
    brief,
    business_id,
    manifest_path,
    email_template_key=None,
    email_sequence=None,
    registry_path=DEFAULT_EMAIL_REGISTRY,
):
    """Build draft metadata without writing to the live lead database.

    `style_id` is deliberately separate from the live funnel's email-specific
    `template_key`. A proposed mapping remains inert until a later human-approved
    promotion step outside this module.
    """
    verification = verify_brief(brief)
    if not verification["passed"]:
        raise CampaignStudioError(
            "cannot create sidecar from invalid campaign brief: "
            + "; ".join(verification["failures"])
        )
    business_id = str(business_id or "").strip()
    if not business_id:
        raise CampaignStudioError("business_id is required for a campaign sidecar")
    manifest_file = Path(manifest_path).expanduser().resolve()
    if not manifest_file.is_file():
        raise CampaignStudioError(f"campaign manifest does not exist: {manifest_file}")
    manifest_sha256 = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    registry = load_email_template_registry(registry_path)
    mapping = validate_email_mapping(email_template_key, email_sequence, registry)
    registry_sha256 = _sha256_value(registry)
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "created_at": _now(),
        "business_id": business_id,
        "campaign_id": brief["campaign_id"],
        "style": {
            "style_id": brief["style"]["id"],
            "version": brief["style"]["version"],
            "sha256": brief["style"]["sha256"],
        },
        "email_mapping": {
            "status": "proposed" if mapping else "unmapped",
            "email_template_key": mapping["template_key"] if mapping else None,
            "email_sequence": mapping["sequence"] if mapping else None,
            "promotable": bool(mapping),
            "eligible_routes": mapping["routes"] if mapping else [],
            "registry_sha256": registry_sha256,
            "live_metadata_written": False,
        },
        "manifest": {
            "path": str(manifest_file),
            "sha256": manifest_sha256,
        },
        "channel_variants": [
            {
                "variant_id": job["variant_id"],
                "channel": job["channel"],
                "mode": job["mode"],
                "status": job["status"],
            }
            for job in brief["render_manifest"]["jobs"]
        ],
        "promotion": {
            "status": "draft",
            "requires_human_approval": True,
        },
        "safety": {
            "live_db_write_allowed": False,
            "send_allowed": False,
            "publish_allowed": False,
            "render_allowed": False,
        },
    }


def verify_sidecar(sidecar, registry_path=DEFAULT_EMAIL_REGISTRY):
    failures = []
    if sidecar.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        failures.append("unsupported sidecar schema")
    style = sidecar.get("style") or {}
    email_mapping = sidecar.get("email_mapping") or {}
    if not style.get("style_id"):
        failures.append("style_id is missing")
    if email_mapping.get("status") == "unmapped" and email_mapping.get("email_template_key"):
        failures.append("unmapped sidecar contains an email template key")
    if email_mapping.get("status") == "unmapped" and email_mapping.get("email_sequence"):
        failures.append("unmapped sidecar contains an email sequence")
    if email_mapping.get("status") == "proposed" and not email_mapping.get("email_template_key"):
        failures.append("proposed email mapping has no template key")
    if email_mapping.get("status") == "proposed" and not email_mapping.get("email_sequence"):
        failures.append("proposed email mapping has no sequence")
    if email_mapping.get("live_metadata_written") is not False:
        failures.append("draft sidecar claims live metadata was written")
    try:
        registry = load_email_template_registry(registry_path)
        if email_mapping.get("registry_sha256") != _sha256_value(registry):
            failures.append("email template registry fingerprint is stale")
        mapping = validate_email_mapping(
            email_mapping.get("email_template_key"),
            email_mapping.get("email_sequence"),
            registry,
        )
        if bool(mapping) != (email_mapping.get("promotable") is True):
            failures.append("email promotability does not match captured router")
        expected_routes = mapping["routes"] if mapping else []
        if email_mapping.get("eligible_routes") != expected_routes:
            failures.append("email eligible routes do not match captured router")
    except CampaignStudioError as exc:
        failures.append(str(exc))
    promotion = sidecar.get("promotion") or {}
    if promotion.get("status") != "draft" or promotion.get("requires_human_approval") is not True:
        failures.append("sidecar bypasses draft promotion gate")
    safety = sidecar.get("safety") or {}
    for key in ("live_db_write_allowed", "send_allowed", "publish_allowed", "render_allowed"):
        if safety.get(key) is not False:
            failures.append(f"sidecar enables prohibited action: {key}")
    manifest = sidecar.get("manifest") or {}
    path = Path(manifest.get("path") or "")
    if not path.is_file():
        failures.append("sidecar manifest path is missing")
    elif hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("sha256"):
        failures.append("sidecar manifest fingerprint is stale")
    return {"passed": not failures, "failures": failures}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--styles", default=str(DEFAULT_STYLES))
    parser.add_argument("--team", default=str(DEFAULT_TEAM))
    sub = parser.add_subparsers(dest="command", required=True)
    compile_parser = sub.add_parser("compile", help="compile a draft campaign brief")
    compile_parser.add_argument("--input", required=True)
    compile_parser.add_argument("--output", required=True)
    verify_parser = sub.add_parser("verify", help="verify a compiled brief")
    verify_parser.add_argument("--brief", required=True)
    sidecar_parser = sub.add_parser("sidecar", help="create inert draft campaign metadata")
    sidecar_parser.add_argument("--brief", required=True)
    sidecar_parser.add_argument("--business-id", required=True)
    sidecar_parser.add_argument("--output", required=True)
    sidecar_parser.add_argument("--email-template-key")
    sidecar_parser.add_argument("--email-sequence")
    sidecar_parser.add_argument("--email-registry", default=str(DEFAULT_EMAIL_REGISTRY))
    verify_sidecar_parser = sub.add_parser("verify-sidecar", help="verify inert draft metadata")
    verify_sidecar_parser.add_argument("--sidecar", required=True)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.command == "compile":
            brief = compile_campaign(_read_json(args.input), args.styles, args.team)
            _write_json(args.output, brief)
            result = {"output": str(Path(args.output).resolve()), "verification": verify_brief(brief)}
        elif args.command == "verify":
            result = verify_brief(_read_json(args.brief))
        elif args.command == "sidecar":
            brief = _read_json(args.brief)
            sidecar = build_draft_sidecar(
                brief,
                args.business_id,
                args.brief,
                email_template_key=args.email_template_key,
                email_sequence=args.email_sequence,
                registry_path=args.email_registry,
            )
            _write_json(args.output, sidecar)
            result = {
                "output": str(Path(args.output).resolve()),
                "verification": verify_sidecar(sidecar),
            }
        else:
            result = verify_sidecar(_read_json(args.sidecar))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("passed", result.get("verification", {}).get("passed")) else 1
    except CampaignStudioError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
