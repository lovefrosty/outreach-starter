#!/usr/bin/env python3

import copy
import json
import unittest
from pathlib import Path

import campaign_research_contract
import maps_job_controller


ROOT = Path(__file__).resolve().parents[2]
EVENTS_PATH = ROOT / "workspace/campaigns/examples/maps-job-events.json"


class MapsJobControllerTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        self.policy = maps_job_controller.load_policy()

    def replay(self, payload=None):
        return maps_job_controller.replay(payload or self.payload, self.policy)

    def test_timeout_is_retry_pending_not_empty_territory(self):
        payload = copy.deepcopy(self.payload)
        payload["events"] = payload["events"][:1]
        state = self.replay(payload)
        self.assertEqual(state["state"], "retry_pending")
        self.assertFalse(state["territory_exhausted"])
        self.assertEqual(state["empty_result_interpretation"], "not_proven_empty")
        self.assertEqual(state["resume"]["cursor"], "apify-dataset-offset-0")
        self.assertEqual(state["resume"]["next_action"]["attempt"], 2)

    def test_primary_retry_exhaustion_requests_fallback_without_calling_it(self):
        payload = copy.deepcopy(self.payload)
        payload["events"] = payload["events"][:2]
        state = self.replay(payload)
        self.assertEqual(state["state"], "fallback_pending")
        self.assertEqual(state["resume"]["active_provider_id"], "places_direct")
        self.assertEqual(
            state["resume"]["next_action"]["type"],
            "fallback_provider_attempt_required",
        )
        self.assertFalse(state["resume"]["next_action"]["automatic_execution_allowed"])

    def test_fallback_success_completes_with_durable_place_provenance(self):
        state = self.replay()
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["record_count"], 2)
        self.assertFalse(state["territory_exhausted"])
        envelope = state["research_envelopes"][0]
        self.assertEqual(envelope["source"]["adapter"], "places_direct")
        self.assertTrue(envelope["source"]["source_url"].startswith("https://www.google.com/maps/"))
        self.assertEqual(len(envelope["source"]["source_artifact_sha256"]), 64)
        compiled = campaign_research_contract.compile_envelope(envelope)
        self.assertTrue(compiled["research_envelope"]["values_minimized"])
        self.assertEqual(len(compiled["research_envelope"]["external_id_sha256"]), 64)

    def test_primary_empty_success_does_not_prove_territory_exhaustion(self):
        payload = copy.deepcopy(self.payload)
        event = payload["events"][2]
        event["event_id"] = "primary-empty"
        event["provider_id"] = "apify_maps"
        event["attempt"] = 1
        event["items"] = []
        payload["events"] = [event]
        state = self.replay(payload)
        self.assertEqual(state["state"], "fallback_pending")
        self.assertFalse(state["territory_exhausted"])

    def test_only_primary_and_fallback_empty_success_proves_exhaustion(self):
        payload = copy.deepcopy(self.payload)
        primary = copy.deepcopy(payload["events"][2])
        primary.update(
            {
                "event_id": "primary-empty",
                "provider_id": "apify_maps",
                "attempt": 1,
                "items": [],
            }
        )
        fallback = copy.deepcopy(payload["events"][2])
        fallback.update(
            {
                "event_id": "fallback-empty",
                "provider_id": "places_direct",
                "attempt": 1,
                "started_at": "2026-06-13T13:06:00Z",
                "finished_at": "2026-06-13T13:06:20Z",
                "items": [],
            }
        )
        payload["events"] = [primary, fallback]
        state = self.replay(payload)
        self.assertEqual(state["state"], "territory_exhausted")
        self.assertTrue(state["territory_exhausted"])
        self.assertEqual(
            state["empty_result_interpretation"],
            "proven_empty_after_primary_and_fallback_success",
        )

    def test_partial_success_requires_and_preserves_resume_cursor(self):
        payload = copy.deepcopy(self.payload)
        event = copy.deepcopy(payload["events"][2])
        event.update(
            {
                "event_id": "partial-primary",
                "provider_id": "apify_maps",
                "attempt": 1,
                "complete": False,
                "next_cursor": "dataset-offset-2",
            }
        )
        payload["events"] = [event]
        state = self.replay(payload)
        self.assertEqual(state["state"], "resume_pending")
        self.assertEqual(state["record_count"], 2)
        self.assertEqual(state["resume"]["cursor"], "dataset-offset-2")

    def test_budget_overrun_holds_before_retry_or_fallback(self):
        payload = copy.deepcopy(self.payload)
        payload["job"]["budget_usd"] = 0.04
        payload["events"] = payload["events"][:1]
        state = self.replay(payload)
        self.assertEqual(state["state"], "budget_hold")
        self.assertEqual(
            state["resume"]["next_action"]["type"],
            "human_budget_review_required",
        )

    def test_exact_duplicate_event_is_idempotent(self):
        payload = copy.deepcopy(self.payload)
        payload["events"].append(copy.deepcopy(payload["events"][-1]))
        state = self.replay(payload)
        self.assertEqual(len(state["event_log"]), 3)
        self.assertEqual(state["record_count"], 2)

    def test_conflicting_duplicate_event_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        duplicate = copy.deepcopy(payload["events"][0])
        duplicate["cost_usd"] = 0.06
        payload["events"].append(duplicate)
        with self.assertRaisesRegex(
            maps_job_controller.MapsJobError,
            "conflicting duplicate Maps event",
        ):
            self.replay(payload)

    def test_timeout_before_configured_deadline_is_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["events"] = [copy.deepcopy(payload["events"][0])]
        payload["events"][0]["finished_at"] = "2026-06-13T13:00:30Z"
        with self.assertRaisesRegex(
            maps_job_controller.MapsJobError,
            "timeout reported before configured deadline",
        ):
            self.replay(payload)

    def test_controller_cannot_call_or_write(self):
        state = self.replay()
        self.assertFalse(state["safety"]["external_api_calls_allowed"])
        self.assertFalse(state["safety"]["database_writes_allowed"])
        self.assertFalse(state["safety"]["automatic_retry_calls_allowed"])
        self.assertFalse(state["safety"]["automatic_fallback_calls_allowed"])
        self.assertFalse(state["safety"]["cost_commitment_allowed"])


if __name__ == "__main__":
    unittest.main()
