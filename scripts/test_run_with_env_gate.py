#!/usr/bin/env python3
"""Tests for the Outreach env wrapper's send-gate consolidation."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "run_with_env.sh"


class RunWithEnvGateTests(unittest.TestCase):
    def test_duplicate_gate_definitions_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            secrets = Path(temp)
            (secrets / "send_gate.env").write_text(
                "SEND_PROVIDER=instantly\nPIPELINE_SENDING_ENABLED=1\nPIPELINE_DAILY_SEND_CAP=15\n",
                encoding="utf-8",
            )
            (secrets / "gmail.env").write_text("PIPELINE_SENDING_ENABLED=1\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(SCRIPT), "python3", "-c", "print('should-not-run')"],
                env={**os.environ, "OUTREACH_SECRETS_DIR": str(secrets)},
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 64)
        self.assertIn("send gate variables must only live", result.stderr)
        self.assertNotIn("should-not-run", result.stdout)

    def test_canonical_gate_file_sources_last(self):
        with tempfile.TemporaryDirectory() as temp:
            secrets = Path(temp)
            (secrets / "instantly.env").write_text("INSTANTLY_API_KEY=test\n", encoding="utf-8")
            (secrets / "send_gate.env").write_text(
                "SEND_PROVIDER=instantly\nPIPELINE_SENDING_ENABLED=1\nPIPELINE_DAILY_SEND_CAP=15\n",
                encoding="utf-8",
            )
            code = (
                "import os; "
                "print(os.environ['SEND_PROVIDER']); "
                "print(os.environ['PIPELINE_SENDING_ENABLED']); "
                "print(os.environ['PIPELINE_DAILY_SEND_CAP']); "
                "print(os.environ['OUTREACH_SEND_GATE_FILE'])"
            )
            result = subprocess.run(
                ["bash", str(SCRIPT), "python3", "-c", code],
                env={**os.environ, "OUTREACH_SECRETS_DIR": str(secrets)},
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[:3], ["instantly", "1", "15"])
        self.assertTrue(lines[3].endswith("send_gate.env"))


if __name__ == "__main__":
    unittest.main()
