#!/usr/bin/env python3
"""Audit source rows and compile explicit evidence envelopes for Campaign Genome.

This module is offline only. It does not call source APIs, read the live CRM,
write a database, or promote a campaign. Legacy lead-shaped rows are audited
and blocked when their provenance cannot satisfy the research envelope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
DEFAULT_POLICY = REPO_ROOT / "workspace/config/campaign_research_contract.json"
ENVELOPE_SCHEMA_VERSION = "outreach.research-envelope.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ResearchContractError(ValueError):
    """Raised when an evidence envelope cannot be safely compiled."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResearchContractError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ResearchContractError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _valid_url(value):
    parsed = urlparse(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _valid_timestamp(value):
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return "T" in str(value)


def load_policy(path=DEFAULT_POLICY):
    policy = _read_json(path)
    if policy.get("schema_version") != "outreach.campaign-research-contract.v1":
        raise ResearchContractError("unsupported campaign research contract schema")
    return policy


def audit_legacy_row(row, policy=None):
    """Report why a normalized live-shaped row is not claim-ready.

    The audit deliberately does not parse provenance out of reviews or other
    free text. It returns field names and blocker IDs, never customer values.
    """
    policy = policy or load_policy()
    blockers = []
    source = str(row.get("source") or "").strip()
    if source:
        blockers.append("adapter_key_is_not_source_provenance")
    else:
        blockers.append("source_adapter_missing")
    for field, blocker in (
        ("source_url", "source_url_missing"),
        ("external_id", "stable_external_id_missing"),
        ("captured_at", "capture_timestamp_missing"),
        ("source_artifact_sha256", "source_artifact_fingerprint_missing"),
    ):
        if not row.get(field):
            blockers.append(blocker)
    if not str(row.get("website") or "").strip():
        blockers.append("campaign_website_missing")
    if not isinstance(row.get("field_sources"), dict) or not row.get("field_sources"):
        blockers.append("field_level_provenance_missing")
    reviews = row.get("reviews") or []
    if any("source=" in str(item) for item in reviews):
        blockers.append("free_text_provenance_not_accepted")
    observed_fields = sorted(
        field for field in ("processor", "tech_signals") if row.get(field)
    )
    return {
        "schema_version": "outreach.legacy-research-audit.v1",
        "eligible_for_campaign_research": False,
        "eligible_action": policy["legacy_policy"]["eligible_action"],
        "source_adapter_present": bool(source),
        "observed_fields_present": observed_fields,
        "blockers": sorted(set(blockers)),
        "values_reproduced": False,
        "remediation": (
            "Reacquire evidence into an explicit research envelope; do not infer URLs, "
            "external IDs, capture times, or field lineage from this row."
        ),
    }


def _evidence_urls(envelope, required_fields):
    evidence = envelope.get("evidence")
    if not isinstance(evidence, dict):
        raise ResearchContractError("research envelope requires `evidence` mappings")
    source = envelope["source"]
    entity = envelope["entity"]
    allowed_urls = {source["source_url"], entity["website"]}
    field_urls = {}
    for field in required_fields:
        references = evidence.get(field)
        if not isinstance(references, list) or not references:
            raise ResearchContractError(f"research envelope requires evidence for `{field}`")
        urls = []
        for reference in references:
            if not isinstance(reference, dict):
                raise ResearchContractError(f"evidence for `{field}` must be structured")
            url = str(reference.get("url") or "").strip()
            locator = str(reference.get("locator") or "").strip()
            content_sha256 = str(reference.get("content_sha256") or "").strip()
            if not _valid_url(url) or url not in allowed_urls:
                raise ResearchContractError(f"evidence for `{field}` has an unapproved URL")
            if not locator:
                raise ResearchContractError(f"evidence for `{field}` requires a locator")
            if not SHA256_RE.fullmatch(content_sha256):
                raise ResearchContractError(f"evidence for `{field}` requires content SHA-256")
            urls.append(url)
        field_urls[field] = list(dict.fromkeys(urls))
    return field_urls


def compile_envelope(envelope, policy=None):
    """Compile one explicit evidence envelope into a campaign research record."""
    policy = policy or load_policy()
    if envelope.get("schema_version") != policy["envelope_schema_version"]:
        raise ResearchContractError("unsupported research envelope schema")
    unknown_top_level = set(envelope) - {"schema_version", "source", "entity", "observed", "evidence"}
    if unknown_top_level:
        raise ResearchContractError(
            "research envelope contains unsupported top-level fields: "
            + ", ".join(sorted(unknown_top_level))
        )
    source = envelope.get("source")
    entity = envelope.get("entity")
    observed = envelope.get("observed")
    if not all(isinstance(item, dict) for item in (source, entity, observed)):
        raise ResearchContractError("research envelope requires source, entity, and observed objects")
    for field in policy["required_source_fields"]:
        if not str(source.get(field) or "").strip():
            raise ResearchContractError(f"research envelope source requires `{field}`")
    if not _valid_url(source["source_url"]):
        raise ResearchContractError("research envelope source_url is invalid")
    if not _valid_timestamp(source["captured_at"]):
        raise ResearchContractError("research envelope captured_at is invalid")
    if not SHA256_RE.fullmatch(str(source["source_artifact_sha256"])):
        raise ResearchContractError("research envelope source artifact SHA-256 is invalid")
    for field in policy["required_entity_fields"]:
        if not str(entity.get(field) or "").strip():
            raise ResearchContractError(f"research envelope entity requires `{field}`")
    if not _valid_url(entity["website"]):
        raise ResearchContractError("research envelope website is invalid")
    observed_keys = set(observed)
    prohibited = observed_keys & set(policy["prohibited_observed_fields"])
    if prohibited:
        raise ResearchContractError(
            "research envelope contains prohibited inferred/private fields: "
            + ", ".join(sorted(prohibited))
        )
    unsupported = observed_keys - set(policy["allowed_observed_fields"])
    if unsupported:
        raise ResearchContractError(
            "research envelope contains unsupported observed fields: "
            + ", ".join(sorted(unsupported))
        )
    processor = str(observed.get("processor") or "").strip()
    tech_signals = observed.get("tech_signals") or []
    if not isinstance(tech_signals, list):
        raise ResearchContractError("research envelope tech_signals must be a list")
    tech_signals = [str(item).strip() for item in tech_signals if str(item).strip()]
    required_evidence = ["entity.company", "entity.website"]
    if processor:
        required_evidence.append("observed.processor")
    required_evidence.extend(f"observed.tech_signals.{signal}" for signal in tech_signals)
    field_urls = _evidence_urls(envelope, required_evidence)
    source_urls = list(
        dict.fromkeys(
            [source["source_url"], entity["website"]]
            + [url for urls in field_urls.values() for url in urls]
        )
    )
    campaign_field_sources = {
        "tech_signals": {
            signal: field_urls[f"observed.tech_signals.{signal}"]
            for signal in tech_signals
        }
    }
    if processor:
        campaign_field_sources["processor"] = field_urls["observed.processor"]
    return {
        "company": str(entity["company"]).strip(),
        "website": str(entity["website"]).strip(),
        "source_urls": source_urls,
        "processor": processor,
        "tech_signals": tech_signals,
        "field_sources": campaign_field_sources,
        "research_envelope": {
            "adapter": str(source["adapter"]).strip(),
            "external_id_sha256": hashlib.sha256(
                str(source["external_id"]).encode("utf-8")
            ).hexdigest(),
            "captured_at": source["captured_at"],
            "source_artifact_sha256": source["source_artifact_sha256"],
            "values_minimized": True,
        },
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit-legacy", help="audit a legacy normalized source row")
    audit.add_argument("--input", required=True)
    audit.add_argument("--output", required=True)
    compile_parser = sub.add_parser("compile", help="compile an explicit evidence envelope")
    compile_parser.add_argument("--input", required=True)
    compile_parser.add_argument("--output", required=True)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        policy = load_policy(args.policy)
        payload = _read_json(args.input)
        result = (
            audit_legacy_row(payload, policy)
            if args.command == "audit-legacy"
            else compile_envelope(payload, policy)
        )
        _write_json(args.output, result)
        print(json.dumps({"output": str(Path(args.output).resolve()), "result": result}, indent=2))
        return 0
    except ResearchContractError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
