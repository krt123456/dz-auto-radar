#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import multi_official_auction_fetcher as module


class GenericOfficialAuctionTests(unittest.TestCase):
    def test_parse_complete_euro_lot(self) -> None:
        source = module.Source("finshop", "BE", "EUR", "Europe/Brussels",
                               ("https://finshop.belgium.be/shop",), ("finshop.belgium.be",))
        markup = """<html><h1>Vehicle Renault Clio</h1><p>First registration: 10/01/2024</p>
        <p>Mileage: 12 500 km</p><p>Current bid: 8.500,00 EUR</p>
        <p>Auction end: 2030-09-04 13:44</p></html>"""
        row = module.parse_detail(markup, "https://finshop.belgium.be/product/auction/57391",
                                  source, {"EUR": 1.0},
                                  now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        self.assertIsNotNone(row)
        self.assertEqual(row["price_eur"], "8500.00")
        self.assertEqual(row["mileage_km"], "12500")

    def test_missing_explicit_end_fails_closed(self) -> None:
        source = module.SOURCES[0]
        markup = "<h1>Voiture Renault Clio</h1><p>1ère mise en circulation: 01/01/2024</p><p>Mise a prix 5000 EUR</p>"
        self.assertIsNone(module.parse_detail(markup, "https://encheres-domaine.gouv.fr/lot/12345",
                                              source, {"EUR": 1.0}))

    def test_sek_conversion(self) -> None:
        self.assertEqual(module.parse_amount("110 000", "SEK", {"SEK": 11.0}), 10000)


if __name__ == "__main__":
    unittest.main()
