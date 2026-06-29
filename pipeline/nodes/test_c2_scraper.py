#!/usr/bin/env python3

import http.client
import sys
import unittest
from pathlib import Path
from unittest import mock

NODES = Path(__file__).resolve().parent
PIPELINE = NODES.parent
sys.path.insert(0, str(PIPELINE))
sys.path.insert(0, str(NODES))

import c2_scraper  # noqa: E402


class C2MalformedUrlTests(unittest.TestCase):
    def test_discovery_skips_urls_with_whitespace(self):
        html = '<a href="/menu buttonskessels 5-27-files/styles.css">bad</a>'
        urls = c2_scraper._discover_same_domain_links(
            html, "https://example.com", "example.com"
        )
        self.assertEqual(urls, [])

    def test_fetch_treats_invalid_url_as_a_normal_miss(self):
        with mock.patch.object(
            c2_scraper, "urlopen", side_effect=http.client.InvalidURL("bad URL")
        ):
            self.assertIsNone(c2_scraper._fetch_page("https://example.com/bad path"))

    def test_site_signals_capture_high_intent_public_workflow_hints(self):
        html = """
        <html>
          <body>
            <a href="/pricing">Pricing</a>
            <a href="/faq">FAQ</a>
            <form action="/contact"><input type="submit" value="Send"></form>
            <div>Book online and schedule service today</div>
            <div>Apply now for financing</div>
            <div>Customer portal login</div>
            <div>Text us to pay your invoice</div>
          </body>
        </html>
        """
        signals = c2_scraper._site_signals(html, {"same_as": []})
        self.assertIn("appointments_or_booking", signals)
        self.assertIn("contact_form", signals)
        self.assertIn("pricing_or_plans", signals)
        self.assertIn("financing", signals)
        self.assertIn("customer_portal_or_account", signals)
        self.assertIn("sms_or_text_channel", signals)
        self.assertIn("faq_or_help_center", signals)


if __name__ == "__main__":
    unittest.main()
