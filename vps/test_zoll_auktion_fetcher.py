#!/usr/bin/env python3
from __future__ import annotations

import unittest

import zoll_auktion_fetcher as module


class ZollParserTests(unittest.TestCase):
    def test_bid_formats(self) -> None:
        self.assertEqual(module.parse_bid("43.500,00 EUR"), 43500)
        self.assertEqual(module.parse_bid("1.200 EUR"), 1200)
        self.assertIsNone(module.parse_bid("auf Anfrage"))

    def test_berlin_end_is_utc(self) -> None:
        parsed = module.parse_end_time("Di., 18.08.2026 - 07:00 Uhr")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.isoformat(), "2026-08-18T05:00:00+00:00")

    def test_listing_page(self) -> None:
        markup = '''Auktionssuche: 12 Treffer
        <a href="/auktion/produkt/VW_Golf/123456">Golf</a>
        <nav aria-label="Suchergebnis Paginierung"><a rel="next" href="?pagination=2">weiter</a></nav>'''
        links, total, next_url = module.parse_listing_page(markup)
        self.assertEqual(links, ["/auktion/produkt/VW_Golf/123456"])
        self.assertEqual(total, 12)
        self.assertEqual(next_url, "?pagination=2")


if __name__ == "__main__":
    unittest.main()
