#!/usr/bin/env python3
"""Compile source-first adapter inputs into no-send seed batches.

This runner is intentionally offline/input-driven. It does not fetch remote
sources, write SQLite, approve leads, queue leads, or send email. Operators can
use it to normalize downloaded directory/profile/license rows before a resolver
decides which seeds are strong enough for C2.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = (
    PIPELINE_DIR.parent
    if (PIPELINE_DIR.parent / "config/source_first_source_map.json").exists()
    else PIPELINE_DIR.parent / "workspace"
)
INPUT_SCHEMA = "outreach.source-first-input.v1"
OUTPUT_SCHEMA = "outreach.source-first-seed-batch.v1"
DEFAULT_SOURCE_MAP = WORKSPACE_DIR / "config/source_first_source_map.json"
ADAPTERS = {
    "dealership_directory": (
        "sources.dealership_directory_adapter",
        "DealershipDirectoryAdapter",
    ),
    "restaurant_directory": (
        "sources.restaurant_directory_adapter",
        "RestaurantDirectoryAdapter",
    ),
    "pharmacy_directory": (
        "sources.pharmacy_directory_adapter",
        "PharmacyDirectoryAdapter",
    ),
}


class SourceFirstRunnerError(ValueError):
    """Raised when a source-first batch is unsafe or invalid."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceFirstRunnerError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceFirstRunnerError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _load_source_map(path=DEFAULT_SOURCE_MAP):
    source_map = _read_json(path)
    safety = source_map.get("safety") or {}
    forbidden = (
        "external_fetch_allowed",
        "database_writes_allowed",
        "automatic_lead_promotion_allowed",
        "paid_enrichment_allowed",
        "sending_or_queue_approval_allowed",
        "login_or_access_control_bypass_allowed",
    )
    if any(safety.get(key) is not False for key in forbidden):
        raise SourceFirstRunnerError("source-first map enables an external or unsafe action")
    return source_map


def _adapter(adapter_name):
    target = ADAPTERS.get(adapter_name)
    if not target:
        raise SourceFirstRunnerError(f"unsupported source-first adapter: {adapter_name}")
    module_name, class_name = target
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


def _payload_rows(payload):
    if isinstance(payload, list):
        return payload
    if payload.get("schema_version") != INPUT_SCHEMA:
        raise SourceFirstRunnerError("unsupported source-first input schema")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise SourceFirstRunnerError("source-first input requires rows")
    return rows


def compile_seed_batch(payload, adapter_name, source_map=None, limit=None):
    _load_source_map(source_map or DEFAULT_SOURCE_MAP)
    rows = _payload_rows(payload)
    params = {"fixture_rows": rows}
    if limit is not None:
        params["max"] = int(limit)
    seeds = _adapter(adapter_name).fetch(params)
    stages = Counter(seed.get("seed_stage") or "unknown" for seed in seeds)
    confidence = Counter(seed.get("seed_confidence") or "unknown" for seed in seeds)
    return {
        "schema_version": OUTPUT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "no_send_source_first",
        "adapter": adapter_name,
        "safety": {
            "external_fetch_allowed": False,
            "database_writes_allowed": False,
            "automatic_lead_promotion_allowed": False,
            "paid_enrichment_allowed": False,
            "sending_or_queue_approval_allowed": False,
        },
        "summary": {
            "input_rows": len(rows),
            "seed_rows": len(seeds),
            "seed_stages": dict(sorted(stages.items())),
            "seed_confidence": dict(sorted(confidence.items())),
        },
        "records": seeds,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, choices=sorted(ADAPTERS))
    parser.add_argument("--input", required=True, help="JSON list or source-first input batch")
    parser.add_argument("--output", required=True, help="Output JSON seed batch")
    parser.add_argument("--source-map", default=str(DEFAULT_SOURCE_MAP))
    parser.add_argument("--limit", type=int)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        report = compile_seed_batch(
            _read_json(args.input),
            args.adapter,
            source_map=args.source_map,
            limit=args.limit,
        )
        output = _write_json(args.output, report)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "adapter": report["adapter"],
                    "summary": report["summary"],
                    "external_fetch_allowed": report["safety"]["external_fetch_allowed"],
                    "database_writes_allowed": report["safety"]["database_writes_allowed"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except SourceFirstRunnerError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
