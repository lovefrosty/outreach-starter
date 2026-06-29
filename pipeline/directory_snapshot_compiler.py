#!/usr/bin/env python3
"""Compile downloaded public-directory snapshots into safe source records.

The compiler is source-agnostic and non-fetching. It preserves snapshot and row
fingerprints, stable public IDs, and source URLs. Rows without websites remain
research seeds; rows with websites can emit Campaign Genome research envelopes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parents[1]
MANIFEST_SCHEMA = "outreach.directory-sources.v1"
INPUT_SCHEMA = "outreach.directory-snapshot-batch.v1"
OUTPUT_SCHEMA = "outreach.directory-compile-report.v1"
ENVELOPE_SCHEMA = "outreach.research-envelope.v1"
DEFAULT_MANIFEST = REPO_ROOT / "workspace/config/directory_sources.json"


class DirectoryCompilerError(ValueError):
    """Raised when a public-directory snapshot is unsafe or inconsistent."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DirectoryCompilerError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DirectoryCompilerError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _required_text(record, field, context):
    value = str(record.get(field) or "").strip()
    if not value:
        raise DirectoryCompilerError(f"{context} requires {field}")
    return value


def _timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise DirectoryCompilerError(f"{field} requires an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise DirectoryCompilerError(f"invalid timestamp for {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise DirectoryCompilerError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_url(value):
    parsed = urlparse(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _sha(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_manifest(path=DEFAULT_MANIFEST):
    manifest = _read_json(path)
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise DirectoryCompilerError("unsupported directory source manifest schema")
    safety = manifest.get("safety") or {}
    if (
        safety.get("external_fetch_allowed") is not False
        or safety.get("database_writes_allowed") is not False
        or safety.get("automatic_lead_promotion_allowed") is not False
        or safety.get("login_or_access_control_bypass_allowed") is not False
    ):
        raise DirectoryCompilerError("directory source manifest enables an external action")
    prohibited = set(manifest.get("prohibited_input_fields") or [])
    if not prohibited:
        raise DirectoryCompilerError("directory source manifest has no prohibited fields")
    sources = {}
    for source in manifest.get("sources") or []:
        source_id = _required_text(source, "source_id", "directory source")
        if source_id in sources:
            raise DirectoryCompilerError(f"duplicate directory source: {source_id}")
        source_url = _required_text(source, "source_url", f"source_id={source_id}")
        if not _valid_url(source_url):
            raise DirectoryCompilerError(f"invalid directory source URL: {source_id}")
        if source.get("acquisition_mode") != "downloaded_public_dataset":
            raise DirectoryCompilerError(f"unsupported directory acquisition mode: {source_id}")
        if source.get("format") != "json_rows":
            raise DirectoryCompilerError(f"unsupported directory format: {source_id}")
        _required_text(source, "stable_id_field", f"source_id={source_id}")
        mapping = source.get("field_mapping") or {}
        if not mapping.get("company") or not mapping.get("website"):
            raise DirectoryCompilerError(f"directory mapping requires company and website: {source_id}")
        compliance = source.get("compliance") or {}
        if compliance.get("robots_policy") != "not_applicable_official_download":
            raise DirectoryCompilerError(f"directory robots policy is not approved: {source_id}")
        if compliance.get("terms_review_status") != "manual_review_required_before_live_fetch":
            raise DirectoryCompilerError(f"directory terms review boundary is missing: {source_id}")
        if int(compliance.get("rate_limit_per_minute", 0)) <= 0:
            raise DirectoryCompilerError(f"directory rate limit must be positive: {source_id}")
        sources[source_id] = source
    if len(sources) < 2:
        raise DirectoryCompilerError("directory manifest must demonstrate at least two sources")
    return manifest


def _field_evidence(source_url, row_hash, field_name, value):
    return {
        "url": source_url,
        "locator": f"row_sha256:{row_hash}/field:{field_name}",
        "content_sha256": hashlib.sha256(str(value).encode("utf-8")).hexdigest(),
    }


def _research_envelope(source, captured_at, snapshot_hash, stable_id, row_hash, fields):
    return {
        "schema_version": ENVELOPE_SCHEMA,
        "source": {
            "adapter": source["source_id"],
            "source_url": source["source_url"],
            "external_id": stable_id,
            "captured_at": captured_at,
            "source_artifact_sha256": snapshot_hash,
        },
        "entity": {
            "company": fields["company"],
            "website": fields["website"],
        },
        "observed": {"processor": "", "tech_signals": []},
        "evidence": {
            "entity.company": [
                _field_evidence(source["source_url"], row_hash, "company", fields["company"])
            ],
            "entity.website": [
                _field_evidence(source["source_url"], row_hash, "website", fields["website"])
            ],
        },
    }


def compile_snapshots(payload, manifest):
    if payload.get("schema_version") != INPUT_SCHEMA:
        raise DirectoryCompilerError("unsupported directory snapshot batch schema")
    source_index = {item["source_id"]: item for item in manifest["sources"]}
    prohibited = set(manifest["prohibited_input_fields"])
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list):
        raise DirectoryCompilerError("directory snapshot batch requires snapshots")
    seen_snapshots = {}
    records = {}
    snapshot_reports = []
    duplicate_rows = 0
    for raw_snapshot in snapshots:
        snapshot = dict(raw_snapshot)
        snapshot_id = _required_text(snapshot, "snapshot_id", "directory snapshot")
        canonical_snapshot = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        prior_snapshot = seen_snapshots.get(snapshot_id)
        if prior_snapshot and prior_snapshot != canonical_snapshot:
            raise DirectoryCompilerError(f"conflicting duplicate snapshot_id: {snapshot_id}")
        if prior_snapshot:
            continue
        seen_snapshots[snapshot_id] = canonical_snapshot
        source_id = _required_text(snapshot, "source_id", f"snapshot_id={snapshot_id}")
        source = source_index.get(source_id)
        if not source:
            raise DirectoryCompilerError(f"snapshot references unknown directory source: {source_id}")
        captured_at = _timestamp(snapshot.get("captured_at"), f"snapshot_id={snapshot_id}.captured_at")
        rows = snapshot.get("rows")
        if not isinstance(rows, list):
            raise DirectoryCompilerError(f"snapshot rows must be a list: {snapshot_id}")
        snapshot_hash = _sha(rows)
        source_record_count = 0
        for row in rows:
            if not isinstance(row, dict):
                raise DirectoryCompilerError(f"directory row must be an object: {snapshot_id}")
            prohibited_present = prohibited & set(row)
            if prohibited_present:
                raise DirectoryCompilerError(
                    "directory row contains prohibited inferred/private fields: "
                    + ", ".join(sorted(prohibited_present))
                )
            for required in source.get("required_fields") or []:
                _required_text(row, required, f"snapshot_id={snapshot_id}")
            stable_id_field = source["stable_id_field"]
            stable_id = _required_text(row, stable_id_field, f"snapshot_id={snapshot_id}")
            mapping = source["field_mapping"]
            fields = {
                target: str(row.get(source_field) or "").strip()
                for target, source_field in mapping.items()
            }
            if not fields.get("company"):
                raise DirectoryCompilerError(f"directory row has no company: {source_id}/{stable_id}")
            website = fields.get("website", "")
            if website and not _valid_url(website):
                raise DirectoryCompilerError(f"directory row website is invalid: {source_id}/{stable_id}")
            row_hash = _sha(row)
            key = f"{source_id}:{stable_id}"
            record = {
                "record_key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
                "source_id": source_id,
                "source_url": source["source_url"],
                "external_id_sha256": hashlib.sha256(stable_id.encode("utf-8")).hexdigest(),
                "captured_at": captured_at,
                "snapshot_sha256": snapshot_hash,
                "row_sha256": row_hash,
                "fields": fields,
                "stage": "research_envelope_ready" if website else "seeded_research_needed",
                "automatic_lead_promotion_allowed": False,
                "research_envelope": (
                    _research_envelope(
                        source, captured_at, snapshot_hash, stable_id, row_hash, fields
                    )
                    if website
                    else None
                ),
            }
            canonical_record = json.dumps(record, sort_keys=True, separators=(",", ":"))
            previous = records.get(key)
            if previous and previous[0] != canonical_record:
                raise DirectoryCompilerError(f"conflicting duplicate directory external ID: {key}")
            if previous:
                duplicate_rows += 1
                continue
            records[key] = (canonical_record, record)
            source_record_count += 1
        snapshot_reports.append(
            {
                "snapshot_id": snapshot_id,
                "source_id": source_id,
                "captured_at": captured_at,
                "snapshot_sha256": snapshot_hash,
                "input_rows": len(rows),
                "new_records": source_record_count,
            }
        )

    result_records = [item[1] for item in records.values()]
    stage_counts = Counter(item["stage"] for item in result_records)
    source_counts = Counter(item["source_id"] for item in result_records)
    return {
        "schema_version": OUTPUT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "offline_shadow",
        "safety": {
            "external_fetch_allowed": False,
            "database_writes_allowed": False,
            "automatic_lead_promotion_allowed": False,
        },
        "summary": {
            "snapshots": len(snapshot_reports),
            "records": len(result_records),
            "duplicate_rows": duplicate_rows,
            "sources": dict(sorted(source_counts.items())),
            "stages": dict(sorted(stage_counts.items())),
        },
        "snapshot_reports": snapshot_reports,
        "records": result_records,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    return parser


def main():
    args = build_parser().parse_args()
    try:
        report = compile_snapshots(_read_json(args.snapshots), load_manifest(args.manifest))
        output = _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "sources": report["summary"]["sources"],
                    "records": report["summary"]["records"],
                    "external_fetch_allowed": report["safety"]["external_fetch_allowed"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except DirectoryCompilerError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
