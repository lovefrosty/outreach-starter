#!/usr/bin/env python3
"""Tests for attribution-safe, cross-channel campaign learning."""

from __future__ import annotations

import copy
import unittest

import campaign_learning


def sidecar(business_id="b1"):
    return {
        "schema_version": campaign_learning.SIDECAR_SCHEMA_VERSION,
        "business_id": business_id,
        "campaign_id": "campaign-001",
        "style": {
            "style_id": "operational-xray",
            "version": "1.0.0",
            "sha256": "a" * 64,
        },
        "email_mapping": {
            "status": "proposed",
            "email_template_key": "Restaurant2",
            "email_sequence": "restaurant_default",
            "promotable": True,
            "live_metadata_written": False,
        },
        "channel_variants": [
            {
                "channel": "instagram-reels",
                "variant_id": "reel-001",
                "status": "draft",
            }
        ],
    }


def events(items):
    return {"schema_version": campaign_learning.EVENT_SCHEMA_VERSION, "events": items}


class CampaignLearningTests(unittest.TestCase):
    def test_duplicate_reply_events_do_not_exceed_one_distinct_reply(self):
        payload = events(
            [
                {"event_id": "e1", "business_id": "b1", "channel": "email", "asset_variant_id": "Restaurant2", "event": "exposure"},
                {"event_id": "e2", "business_id": "b1", "channel": "email", "asset_variant_id": "Restaurant2", "event": "reply"},
                {"event_id": "e3", "business_id": "b1", "channel": "email", "asset_variant_id": "Restaurant2", "event": "reply"},
            ]
        )

        report = campaign_learning.evaluate({"b1": sidecar()}, payload)
        metric = report["metrics"][0]
        self.assertEqual(metric["exposed_businesses"], 1)
        self.assertEqual(metric["reply_businesses"], 1)
        self.assertEqual(metric["reply_rate"], 1.0)
        self.assertEqual(metric["event_count"], 3)

    def test_identical_event_id_is_deduplicated_but_conflict_is_rejected(self):
        exposure = {"event_id": "same", "business_id": "b1", "channel": "email", "asset_variant_id": "Restaurant2", "event": "exposure"}
        report = campaign_learning.evaluate({"b1": sidecar()}, events([exposure, copy.deepcopy(exposure)]))
        self.assertEqual(report["input"]["unique_events"], 1)

        conflict = dict(exposure)
        conflict["event"] = "reply"
        with self.assertRaisesRegex(campaign_learning.CampaignLearningError, "conflicting duplicate"):
            campaign_learning.evaluate({"b1": sidecar()}, events([exposure, conflict]))

    def test_metrics_are_grouped_by_style_and_channel_with_explicit_value(self):
        payload = events(
            [
                {"event_id": "v1", "business_id": "b1", "channel": "instagram-reels", "asset_variant_id": "reel-001", "event": "exposure"},
                {"event_id": "v2", "business_id": "b1", "channel": "instagram-reels", "asset_variant_id": "reel-001", "event": "reply"},
                {"event_id": "v3", "business_id": "b1", "channel": "instagram-reels", "asset_variant_id": "reel-001", "event": "reply_intent", "value": "booking"},
                {"event_id": "v4", "business_id": "b1", "channel": "instagram-reels", "asset_variant_id": "reel-001", "event": "booked"},
                {"event_id": "v5", "business_id": "b1", "channel": "instagram-reels", "asset_variant_id": "reel-001", "event": "verified_value", "value": 2500},
            ]
        )

        metric = campaign_learning.evaluate({"b1": sidecar()}, payload)["metrics"][0]
        self.assertEqual(metric["style_id"], "operational-xray")
        self.assertEqual(metric["channel"], "instagram-reels")
        self.assertEqual(metric["booking_rate"], 1.0)
        self.assertEqual(metric["verified_value_usd"], 2500.0)
        self.assertEqual(metric["value_per_exposure_usd"], 2500.0)
        self.assertEqual(metric["reply_intents"], {"booking": 1})

    def test_unknown_variant_is_rejected_instead_of_inferred(self):
        payload = events(
            [
                {"event_id": "bad", "business_id": "b1", "channel": "instagram-reels", "asset_variant_id": "unknown", "event": "exposure"}
            ]
        )
        with self.assertRaisesRegex(campaign_learning.CampaignLearningError, "asset variant is not declared"):
            campaign_learning.evaluate({"b1": sidecar()}, payload)

    def test_zero_denominator_is_n_a_and_small_samples_are_flagged(self):
        payload = events(
            [
                {"event_id": "reply-only", "business_id": "b1", "channel": "email", "asset_variant_id": "Restaurant2", "event": "reply"}
            ]
        )
        report = campaign_learning.evaluate({"b1": sidecar()}, payload)
        metric = report["metrics"][0]
        self.assertIsNone(metric["reply_rate"])
        self.assertIsNone(metric["booking_rate"])
        self.assertEqual(metric["sample_status"], "too_early")
        self.assertIn("n/a", campaign_learning.render_markdown(report))


if __name__ == "__main__":
    unittest.main()
