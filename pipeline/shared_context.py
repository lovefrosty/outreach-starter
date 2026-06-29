#!/usr/bin/env python3
"""Shared context store for Outreach operator and research lanes.

The store is a small JSON document, not chat history. Writers can update only
their allowed section; every update appends compact audit metadata.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "outreach.shared-context.v1"
DEFAULT_CONTEXT_PATH = Path.home() / ".outreach" / "state" / "shared_context" / "context.json"

SECTION_DEFAULTS = {
    "operator_state": {
        "telegram_safe_status": "",
        "pending_approvals": [],
        "delivery_summaries": [],
        "live_action_blockers": [],
        "updated_at": "",
    },
    "research_signals": [],
    "handoff_summary": {
        "summary": "",
        "safe_items": [],
        "updated_at": "",
    },
    "audit": {
        "last_writer": "",
        "last_section": "",
        "last_source_path": "",
        "last_captured_at": "",
        "last_evidence_refs": [],
        "updates": [],
    },
}

WRITER_ALLOWED_SECTIONS = {
    "outreach": {"operator_state"},
    "telegram": {"operator_state"},
    "social_intent": {"research_signals", "handoff_summary"},
    "codex_automation": {"research_signals", "handoff_summary"},
    "system": {"operator_state", "research_signals", "handoff_summary"},
}


class SharedContextError(ValueError):
    """Raised when the shared context document is unavailable or invalid."""


class SharedContextPermissionError(PermissionError):
    """Raised when a writer tries to update a section it does not own."""


def context_path(path=None):
    """Return the configured shared-context path."""
    return Path(path or os.environ.get("OUTREACH_SHARED_CONTEXT") or DEFAULT_CONTEXT_PATH)


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def blank_context(captured_at=None):
    """Return a fresh valid context document."""
    captured = captured_at or now_utc()
    doc = {"schema_version": SCHEMA_VERSION}
    doc.update(json.loads(json.dumps(SECTION_DEFAULTS)))
    doc["audit"]["last_captured_at"] = captured
    return doc


def validate_context(doc):
    """Validate the top-level shape and fail closed on unknown schemas."""
    if not isinstance(doc, dict):
        raise SharedContextError("shared context must be a JSON object")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SharedContextError("unsupported shared context schema")
    for section in SECTION_DEFAULTS:
        if section not in doc:
            raise SharedContextError(f"shared context missing `{section}`")
    if not isinstance(doc["operator_state"], dict):
        raise SharedContextError("operator_state must be an object")
    if not isinstance(doc["research_signals"], list):
        raise SharedContextError("research_signals must be a list")
    if not isinstance(doc["handoff_summary"], dict):
        raise SharedContextError("handoff_summary must be an object")
    if not isinstance(doc["audit"], dict):
        raise SharedContextError("audit must be an object")
    if not isinstance(doc["audit"].get("updates", []), list):
        raise SharedContextError("audit.updates must be a list")
    return doc


def load_context(path=None, bootstrap_missing=False):
    """Load and validate context. Missing files fail closed unless bootstrapped."""
    p = context_path(path)
    if not p.exists():
        if not bootstrap_missing:
            raise SharedContextError(f"shared context not found: {p}")
        return blank_context()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SharedContextError(f"invalid shared context JSON: {exc}") from exc
    return validate_context(doc)


def atomic_write(path, doc):
    """Atomically write one validated context document."""
    p = context_path(path)
    validate_context(doc)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
    return p


def _check_write_allowed(writer, section):
    allowed = WRITER_ALLOWED_SECTIONS.get(writer, set())
    if section not in allowed:
        raise SharedContextPermissionError(f"{writer} cannot write `{section}`")


def write_section(path, section, value, writer, source_path="", evidence_refs=None, captured_at=None):
    """Update one owned section and append audit metadata."""
    if section not in SECTION_DEFAULTS or section == "audit":
        raise SharedContextError(f"unsupported writable section: {section}")
    _check_write_allowed(writer, section)
    captured = captured_at or now_utc()
    doc = load_context(path, bootstrap_missing=True)
    doc[section] = value
    if isinstance(doc[section], dict) and "updated_at" in doc[section]:
        doc[section]["updated_at"] = captured
    event = {
        "writer": writer,
        "section": section,
        "source_path": str(source_path or ""),
        "captured_at": captured,
        "evidence_refs": list(evidence_refs or []),
    }
    audit = doc["audit"]
    audit["last_writer"] = writer
    audit["last_section"] = section
    audit["last_source_path"] = event["source_path"]
    audit["last_captured_at"] = captured
    audit["last_evidence_refs"] = event["evidence_refs"]
    audit.setdefault("updates", []).append(event)
    return atomic_write(path, doc)


def append_research_signals(path, signals, writer="social_intent", source_path="", captured_at=None):
    """Append research-only signal envelopes to the shared context."""
    _check_write_allowed(writer, "research_signals")
    doc = load_context(path, bootstrap_missing=True)
    existing = list(doc.get("research_signals") or [])
    incoming = list(signals or [])
    captured = captured_at or now_utc()
    for signal in incoming:
        if isinstance(signal, dict) and not signal.get("captured_at"):
            signal["captured_at"] = captured
    evidence_refs = [
        ref
        for signal in incoming
        if isinstance(signal, dict)
        for ref in signal.get("evidence_refs", [])
    ]
    return write_section(
        path,
        "research_signals",
        existing + incoming,
        writer=writer,
        source_path=source_path,
        evidence_refs=evidence_refs,
        captured_at=captured,
    )


def operator_safe_snapshot(path=None):
    """Return only Telegram-safe shared-context fields."""
    doc = load_context(path, bootstrap_missing=False)
    operator = doc["operator_state"]
    handoff = doc["handoff_summary"]
    return {
        "telegram_safe_status": operator.get("telegram_safe_status", ""),
        "pending_approvals": operator.get("pending_approvals", []),
        "delivery_summaries": operator.get("delivery_summaries", []),
        "live_action_blockers": operator.get("live_action_blockers", []),
        "handoff_summary": handoff.get("summary", ""),
        "safe_items": handoff.get("safe_items", []),
        "updated_at": operator.get("updated_at") or handoff.get("updated_at") or "",
        "audit": {
            "last_writer": doc["audit"].get("last_writer", ""),
            "last_captured_at": doc["audit"].get("last_captured_at", ""),
        },
    }


def format_operator_safe_snapshot(snapshot):
    """Render the safe snapshot for Telegram/freeform model context."""
    lines = ["Shared context: operator-safe view"]
    status = snapshot.get("telegram_safe_status") or "no Telegram-safe status recorded"
    lines.append(f"Status: {status}")
    for label, key in (
        ("Pending approvals", "pending_approvals"),
        ("Delivery summaries", "delivery_summaries"),
        ("Live-action blockers", "live_action_blockers"),
        ("Safe research handoff", "safe_items"),
    ):
        items = snapshot.get(key) or []
        if items:
            lines.append(f"{label}:")
            for item in items[:5]:
                lines.append(f"- {item}")
    if snapshot.get("handoff_summary"):
        lines.append(f"Handoff summary: {snapshot['handoff_summary']}")
    audit = snapshot.get("audit") or {}
    if audit.get("last_captured_at"):
        lines.append(f"Last shared update: {audit['last_captured_at']} by {audit.get('last_writer') or 'unknown'}")
    return "\n".join(lines)
