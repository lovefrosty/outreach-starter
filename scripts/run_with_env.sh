#!/usr/bin/env bash
set -euo pipefail

SECRETS_DIR="${OUTREACH_SECRETS_DIR:-$HOME/.outreach/secrets}"
GATE_FILE="${OUTREACH_SEND_GATE_FILE:-$SECRETS_DIR/send_gate.env}"
GATE_RE='^(SEND_PROVIDER|PIPELINE_SENDING_ENABLED|PIPELINE_DAILY_SEND_CAP)='

shopt -s nullglob
duplicate_gate_files=()
for f in "$SECRETS_DIR"/*.env; do
  if [ "$f" = "$GATE_FILE" ]; then
    continue
  fi
  if grep -Eq "$GATE_RE" "$f"; then
    duplicate_gate_files+=("$f")
  fi
done

if [ "${#duplicate_gate_files[@]}" -gt 0 ]; then
  echo "[run_with_env] ERROR: send gate variables must only live in $GATE_FILE" >&2
  printf '[run_with_env] duplicate gate definition: %s\n' "${duplicate_gate_files[@]}" >&2
  exit 64
fi

if [ ! -f "$GATE_FILE" ]; then
  echo "[run_with_env] ERROR: missing send gate file: $GATE_FILE" >&2
  exit 65
fi

set -a
for f in "$SECRETS_DIR"/*.env; do
  if [ "$f" != "$GATE_FILE" ]; then
    . "$f"
  fi
done
. "$GATE_FILE"
set +a
export OUTREACH_SEND_GATE_FILE="$GATE_FILE"
export PYTHONPATH="$HOME/.outreach/scripts:${PYTHONPATH:-}"
exec "$@"
