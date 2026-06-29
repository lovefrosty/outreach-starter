#!/usr/bin/env python3

import copy
import json
import tempfile
import unittest
from pathlib import Path

import outbound_event_ledger
import webhook_gateway


ROOT = Path(__file__).resolve().parents[2]
FIXTURES_PATH = ROOT / "workspace/campaigns/examples/webhook-requests.json"


class WebhookGatewayTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
        self.policy = webhook_gateway.load_policy()
        self.secret = self.payload["synthetic_secret"]

    def signed_request(self, request, body=None, secret=None, signed_at=None):
        profile = next(
            item for item in self.policy["profiles"]
            if item["profile_id"] == request["profile_id"]
        )
        raw_body = webhook_gateway._canonical_body(body or request["body"])
        timestamp = signed_at or request["signature_timestamp"]
        auth = profile["authentication"]
        headers = {
            auth["timestamp_header"]: timestamp,
            auth["signature_header"]: webhook_gateway.sign_request(
                secret or self.secret,
                timestamp,
                raw_body,
                auth["signature_prefix"],
            ),
        }
        return headers, raw_body

    def test_normalizes_two_profiles_and_quarantines_unsupported_event(self):
        report = webhook_gateway.normalize_fixtures(self.payload, self.policy)
        self.assertEqual(report["summary"], {
            "submitted": 3,
            "accepted": 2,
            "quarantined": 1,
            "duplicates": 0,
        })
        self.assertEqual(
            {event["provider"] for event in report["canonical_batch"]["events"]},
            {"instantly", "smtp_relay"},
        )
        self.assertEqual(report["quarantine"][0]["reason"], "unsupported_provider_event_type")

    def test_canonical_events_pass_ledger_validation(self):
        report = webhook_gateway.normalize_fixtures(self.payload, self.policy)
        ledger_policy = outbound_event_ledger.load_policy()
        for event in report["canonical_batch"]["events"]:
            validated = outbound_event_ledger.validate_event(event, ledger_policy)
            self.assertEqual(validated["event_id"], event["event_id"])

    def test_raw_reply_and_diagnostic_content_are_not_emitted(self):
        report = webhook_gateway.normalize_fixtures(self.payload, self.policy)
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("This raw reply must never appear", serialized)
        self.assertNotIn("private provider diagnostic", serialized)
        self.assertFalse(report["safety"]["raw_body_emitted"])

    def test_reply_records_webhook_receipt_and_secure_reference(self):
        report = webhook_gateway.normalize_fixtures(self.payload, self.policy)
        reply = next(
            event for event in report["canonical_batch"]["events"]
            if event["event_type"] == "reply_received"
        )
        self.assertEqual(reply["attributes"]["webhook_received_at"], "2026-06-13T15:00:10Z")
        self.assertTrue(reply["payload_ref"].startswith("secure://"))
        self.assertEqual(len(reply["payload_sha256"]), 64)

    def test_tampered_body_fails_signature(self):
        request = self.payload["requests"][0]
        headers, raw_body = self.signed_request(request)
        tampered = raw_body.replace("restaurant-a", "restaurant-b")
        with self.assertRaisesRegex(webhook_gateway.WebhookGatewayError, "signature mismatch"):
            webhook_gateway.normalize_request(
                request["profile_id"], headers, tampered, self.secret,
                request["received_at"], self.policy,
            )

    def test_stale_signature_fails_replay_window(self):
        request = self.payload["requests"][0]
        headers, raw_body = self.signed_request(request, signed_at="2026-06-13T14:00:00Z")
        with self.assertRaisesRegex(webhook_gateway.WebhookGatewayError, "outside replay window"):
            webhook_gateway.normalize_request(
                request["profile_id"], headers, raw_body, self.secret,
                request["received_at"], self.policy,
            )

    def test_missing_attribution_is_quarantined(self):
        request = copy.deepcopy(self.payload["requests"][0])
        del request["body"]["metadata"]["lead_id"]
        headers, raw_body = self.signed_request(request)
        result = webhook_gateway.normalize_request(
            request["profile_id"], headers, raw_body, self.secret,
            request["received_at"], self.policy,
        )
        self.assertEqual(result["reason"], "missing_attribution")
        self.assertEqual(result["missing_fields"], ["lead_id"])

    def test_insecure_reply_reference_is_quarantined(self):
        request = copy.deepcopy(self.payload["requests"][0])
        request["body"]["metadata"]["payload_ref"] = "local://raw-reply"
        headers, raw_body = self.signed_request(request)
        result = webhook_gateway.normalize_request(
            request["profile_id"], headers, raw_body, self.secret,
            request["received_at"], self.policy,
        )
        self.assertEqual(result["reason"], "reply_payload_ref_not_secure")

    def test_unknown_profile_is_rejected(self):
        request = self.payload["requests"][0]
        headers, raw_body = self.signed_request(request)
        with self.assertRaisesRegex(webhook_gateway.WebhookGatewayError, "unknown webhook profile"):
            webhook_gateway.normalize_request(
                "missing_profile", headers, raw_body, self.secret,
                request["received_at"], self.policy,
            )

    def test_identical_provider_event_is_deduplicated(self):
        payload = copy.deepcopy(self.payload)
        duplicate = copy.deepcopy(payload["requests"][0])
        duplicate["fixture_id"] = "instantly-reply-duplicate"
        payload["requests"].append(duplicate)
        report = webhook_gateway.normalize_fixtures(payload, self.policy)
        self.assertEqual(report["summary"]["duplicates"], 1)
        self.assertEqual(report["summary"]["accepted"], 2)

    def test_conflicting_provider_event_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        conflicting = copy.deepcopy(payload["requests"][0])
        conflicting["fixture_id"] = "instantly-reply-conflict"
        conflicting["body"]["metadata"]["lead_id"] = "different-lead"
        payload["requests"].append(conflicting)
        with self.assertRaisesRegex(
            webhook_gateway.WebhookGatewayError,
            "conflicting duplicate provider event",
        ):
            webhook_gateway.normalize_fixtures(payload, self.policy)

    def test_unsafe_policy_is_rejected_and_no_external_action_is_enabled(self):
        unsafe = copy.deepcopy(self.policy)
        unsafe["safety"]["workflow_invocation_allowed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(json.dumps(unsafe), encoding="utf-8")
            with self.assertRaisesRegex(webhook_gateway.WebhookGatewayError, "external action"):
                webhook_gateway.load_policy(path)
        report = webhook_gateway.normalize_fixtures(self.payload, self.policy)
        for key in (
            "http_listener_enabled",
            "network_calls_allowed",
            "ledger_writes_allowed",
            "workflow_invocation_allowed",
            "email_send_allowed",
            "crm_mutation_allowed",
        ):
            self.assertFalse(report["safety"][key])


if __name__ == "__main__":
    unittest.main()
