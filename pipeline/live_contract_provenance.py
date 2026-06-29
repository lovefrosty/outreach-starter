#!/usr/bin/env python3
"""Verify read-only source snapshots against a captured live-send contract.

The verifier operates only on explicitly supplied local files. It never connects
to the VPS, reads environment secrets, mutates live state, or authorizes action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "outreach.live-contract-provenance-report.v1"


class LiveContractProvenanceError(ValueError):
    """Raised when contract or snapshot inputs are invalid."""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LiveContractProvenanceError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LiveContractProvenanceError(f"invalid JSON in {path}: {exc}") from exc


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_snapshot_args(values):
    snapshots = {}
    for item in values:
        if "=" not in item:
            raise LiveContractProvenanceError(
                f"snapshot must be SOURCE_KEY=LOCAL_PATH, got: {item}"
            )
        source_key, path = item.split("=", 1)
        source_key = source_key.strip()
        path = path.strip()
        if not source_key or not path:
            raise LiveContractProvenanceError(f"invalid snapshot mapping: {item}")
        if source_key in snapshots:
            raise LiveContractProvenanceError(f"duplicate snapshot key: {source_key}")
        snapshots[source_key] = path
    return snapshots


def verify(contract_path, snapshots):
    contract_file = Path(contract_path).expanduser().resolve()
    contract = _read_json(contract_file)
    if contract.get("schema_version") != "outreach.live-send-contract.v1":
        raise LiveContractProvenanceError("unsupported live send contract schema")
    sources = contract.get("sources") or {}
    if not sources:
        raise LiveContractProvenanceError("live send contract has no sources")
    if set(snapshots) != set(sources):
        missing = sorted(set(sources) - set(snapshots))
        unknown = sorted(set(snapshots) - set(sources))
        raise LiveContractProvenanceError(
            f"snapshot set mismatch: missing={missing}, unknown={unknown}"
        )

    results = []
    for source_key in sorted(sources):
        expected = sources[source_key]
        snapshot = Path(snapshots[source_key]).expanduser().resolve()
        if not snapshot.is_file():
            raise LiveContractProvenanceError(f"snapshot file not found: {snapshot}")
        actual_sha256 = _sha256(snapshot)
        expected_sha256 = expected.get("sha256")
        results.append(
            {
                "source_key": source_key,
                "live_path": expected.get("path"),
                "snapshot_path": str(snapshot),
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
                "passed": actual_sha256 == expected_sha256,
            }
        )

    passed = all(item["passed"] for item in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "contract": {
            "path": str(contract_file),
            "sha256": _sha256(contract_file),
            "captured_at": contract.get("captured_at"),
        },
        "passed": passed,
        "action_eligibility": "unchanged_review_only" if passed else "blocked_source_drift",
        "results": results,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True)
    parser.add_argument(
        "--snapshot",
        action="append",
        default=[],
        help="SOURCE_KEY=LOCAL_PATH; repeat once for every contract source",
    )
    parser.add_argument("--output")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        report = verify(args.contract, parse_snapshot_args(args.snapshot))
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            output = Path(args.output).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["passed"] else 1
    except LiveContractProvenanceError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
