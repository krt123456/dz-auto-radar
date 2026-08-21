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

    def test_ovm_drz_vehicle_is_admitted(self) -> None:
        detail = {
            "id": 1890891, "volgNummer": "25", "sluitingsDatumISO": "2030-08-26T17:36:44Z",
            "hoogsteBod": 10231, "isClosed": False, "zichtbaar": True, "buitenlandseBiederToegestaan": True,
            "categorie": {"id": 10}, "veiling": {"id": 9325, "type": "DRZ", "isGeopend": True},
            "kavelData": {"kavelDataType": "AUTO", "bouwjaar": "2023", "registratiedatum": "2023-11-16",
                          "naam": "Personenauto, Volkswagen, Polo, 2023", "merk": "Volkswagen", "model": "Polo",
                          "brandstof": "BENZINE", "motorinhoud": "999", "kilometerstand": 15558},
        }
        row = module._ovm_detail_to_row(detail, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        self.assertIsNotNone(row)
        self.assertEqual(row["first_registration_date"], "2023-11-16")
        self.assertEqual(row["price_eur"], "10231.00")
        self.assertIn("/9325/kavels/25", row["source_url"])

    def test_ovm_rejects_non_drz_or_foreign_blocked(self) -> None:
        base = {
            "id": 1, "volgNummer": "1", "sluitingsDatumISO": "2030-08-26T17:36:44Z", "hoogsteBod": 100,
            "isClosed": False, "zichtbaar": True, "buitenlandseBiederToegestaan": True,
            "categorie": {"id": 10}, "veiling": {"id": 2, "type": "THEMAVEILING", "isGeopend": True},
            "kavelData": {"kavelDataType": "AUTO", "bouwjaar": "2024", "naam": "Volkswagen Polo 2024"},
        }
        self.assertIsNone(module._ovm_detail_to_row(base, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)))
        base["veiling"]["type"] = "DRZ"; base["buitenlandseBiederToegestaan"] = False
        self.assertIsNone(module._ovm_detail_to_row(base, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)))

    def test_ovm_rejects_2023_without_exact_registration(self) -> None:
        detail = {
            "id": 1, "volgNummer": "1", "sluitingsDatumISO": "2030-08-26T17:36:44Z", "hoogsteBod": 100,
            "isClosed": False, "zichtbaar": True, "buitenlandseBiederToegestaan": True,
            "categorie": {"id": 10}, "veiling": {"id": 2, "type": "DRZ", "isGeopend": True},
            "kavelData": {"kavelDataType": "AUTO", "bouwjaar": "2023", "naam": "Kia Sportage 2023"},
        }
        self.assertIsNone(module._ovm_detail_to_row(detail, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)))


if __name__ == "__main__":
    unittest.main()
