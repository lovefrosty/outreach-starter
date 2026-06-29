#!/usr/bin/env python3
"""Tests for the 80-word problem-focused first-touch copy standard."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "pipeline" / "nodes"))

import approved_email_templates as templates  # noqa: E402
import orchestrator  # noqa: E402
import c7_sender  # noqa: E402


class FakeRow(dict):
    """Dict with sqlite Row-like key access for sender preview tests."""

    def __getitem__(self, key):
        return self.get(key)


class ApprovedTemplatePolicyTests(unittest.TestCase):
    def test_live_sequences_fit_80_word_copy_policy(self):
        for sequence in sorted(templates.APPROVED_SEQUENCES):
            with self.subTest(sequence=sequence):
                body = templates.first_touch_body(
                    sequence,
                    first_name="Sam",
                    city_state="Newark, NJ",
                )
                self.assertTrue(templates.validate_body(body))
                self.assertLessEqual(templates._body_word_count(body), 80)
                self.assertEqual(body.count("?"), 1)
                self.assertNotIn("•", body)
                self.assertNotIn("I've been speaking with", body)
                self.assertNotIn("10,000+ merchants", body)

    def test_vertical_copy_uses_expected_problem_and_cta_language(self):
        restaurant = templates.first_touch_body("restaurant_default", city_state="Newark, NJ")
        self.assertIn("POS, payments, rewards, and reporting", restaurant)
        self.assertIn("show how much you could save", restaurant)
        self.assertIn("Union brings ordering, payments, and guest data", restaurant)
        self.assertIn("free savings quote and short demo?", restaurant)

        pharmacy = templates.first_touch_body("pharmacy_default", city_state="Newark, NJ")
        self.assertIn("checkout, inventory, patient data", pharmacy)
        self.assertIn("show how much you could save", pharmacy)
        self.assertIn("ExampleProduct brings payments, inventory, signatures", pharmacy)
        self.assertIn("free savings quote and workflow demo?", pharmacy)

        dealership = templates.first_touch_body("dealership_default", city_state="Newark, NJ")
        self.assertIn("ROs, invoices, and payments", dealership)
        self.assertIn("show how much you could save", dealership)
        self.assertIn("deposits into one dealership workflow", dealership)
        self.assertIn("free savings quote and demo?", dealership)
        self.assertNotIn("service-lane", dealership.lower())

    def test_free_quote_is_default_offer_without_unsupported_savings_claims(self):
        for sequence in sorted(templates.APPROVED_SEQUENCES):
            body = templates.first_touch_body(sequence)
            self.assertIn("free savings quote", body.lower())
            self.assertNotIn("hidden fees", body.lower())
            self.assertNotIn("you are overpaying", body.lower())
        self.assertIn(
            "lower your expected rate",
            orchestrator.FIRST_TOUCH_ROUTE_DEFAULTS["general_standard"]["template_cta"],
        )

    def test_route_defaults_are_sentence_shaped(self):
        for vertical, sequence in (
            ("restaurant", "restaurant_default"),
            ("pharmacy", "pharmacy_default"),
            ("dealership", "dealership_default"),
        ):
            with self.subTest(vertical=vertical):
                route = orchestrator._sequence_for_vertical(vertical)
                self.assertEqual(route["sequence_key"], sequence)
                self.assertTrue(route["email_angle"].endswith("."))
                self.assertTrue(route["template_cta"].endswith("?"))

    def test_c7_preview_uses_row_trigger_angle_and_cta(self):
        row = FakeRow(
            company="Demo Dealer",
            owner_name="Alex Owner",
            sequence_key="dealership_default",
            city_state="Trenton, NJ",
            trigger="Demo Dealer lists a service department on its website.",
            email_angle="Green PayTech can review one recent statement to show how much you could save and show how ExampleProduct ties approvals, payments, and deposits into one dealership workflow.",
            template_cta="Open to a free savings quote and demo?",
        )
        _subject, body = c7_sender._build_email(row)
        self.assertIn("Demo Dealer lists a service department", body)
        self.assertIn("one dealership workflow", body)
        self.assertIn("free savings quote and demo?", body)
        self.assertNotIn("service-lane", body.lower())
        self.assertLessEqual(templates._body_word_count(body), 80)
        self.assertEqual(body.count("?"), 1)

    def test_sender_default_cap_is_30(self):
        self.assertEqual(c7_sender._DEFAULT_DAILY_CAP, 30)


if __name__ == "__main__":
    unittest.main()
