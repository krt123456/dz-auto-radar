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
            "hoogsteBod": 10231, "aantalBiedingen": 3, "isClosed": False,
            "zichtbaar": True, "buitenlandseBiederToegestaan": True,
            "categorie": {"id": 10}, "veiling": {"id": 9325, "type": "DRZ", "isGeopend": True},
            "kavelData": {"kavelDataType": "AUTO", "bouwjaar": "2023", "registratiedatum": "2023-11-16",
                          "naam": "Personenauto, Volkswagen, Polo, 2023", "merk": "Volkswagen", "model": "Polo",
                          "brandstof": "BENZINE", "motorinhoud": "999", "kilometerstand": 15558},
        }
        row = module._ovm_detail_to_row(detail, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        self.assertIsNotNone(row)
        self.assertEqual(row["first_registration_date"], "2023-11-16")
        self.assertEqual(row["price_eur"], "10231.00")
        self.assertEqual(row["sale_term_code"], "auction-current-bid")
        self.assertIn("/9325/kavels/25", row["source_url"])

    def test_ovm_opening_amount_without_bids_is_not_a_current_bid(self) -> None:
        detail = {
            "id": 2, "volgNummer": "2", "sluitingsDatumISO": "2030-08-26T17:36:44Z",
            "hoogsteBod": 10231, "aantalBiedingen": 0, "isClosed": False,
            "zichtbaar": True, "buitenlandseBiederToegestaan": True,
            "categorie": {"id": 10},
            "veiling": {"id": 9325, "type": "DRZ", "isGeopend": True},
            "kavelData": {"kavelDataType": "AUTO", "bouwjaar": "2024",
                          "registratiedatum": "2024-11-16",
                          "naam": "Volkswagen Polo 2024", "merk": "Volkswagen",
                          "model": "Polo", "brandstof": "BENZINE"},
        }
        self.assertIsNone(
            module._ovm_detail_to_row(
                detail, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
            )
        )

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

    def test_ovm_rejects_missing_registration_document(self) -> None:
        detail = {
            "id": 1, "volgNummer": "1", "sluitingsDatumISO": "2030-08-26T17:36:44Z", "hoogsteBod": 100,
            "isClosed": False, "zichtbaar": True, "buitenlandseBiederToegestaan": True,
            "categorie": {"id": 10}, "veiling": {"id": 2, "type": "DRZ", "isGeopend": True},
            "kavelData": {"kavelDataType": "AUTO", "bouwjaar": "2024", "naam": "Volkswagen Golf 2024",
                          "specificaties": "Eerste toelating internationaal: 18-03-2024. Het Duitse kentekenbewijs ontbreekt."},
        }
        self.assertIsNone(module._ovm_detail_to_row(detail, now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)))

    def test_domaine_public_active_vehicle_is_admitted(self) -> None:
        item = {
            "id": 304166, "auction_type": "1", "professional_only": 0,
            "start_auction_lot_at": "2026-01-01T10:00:00+00:00",
            "end_auction_lot_at": "2030-01-02T10:00:00+00:00", "last_bid": 8750,
            "name": "DS DS4", "url_key": "ds-ds4-304166",
            "short_description": {"html": "<p>Essence, 1ère mise en circulation le 15/05/2024, 12 345 km.</p>"},
        }
        row = module._domaine_item_to_row(item, now=dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc))
        self.assertIsNotNone(row)
        self.assertEqual(row["first_registration_date"], "15/05/2024")
        self.assertEqual(row["price_eur"], "8750.00")

    def test_domaine_rejects_upcoming_or_professional_only(self) -> None:
        item = {
            "id": 1, "auction_type": "1", "professional_only": 0,
            "start_auction_lot_at": "2030-01-01T10:00:00+00:00",
            "end_auction_lot_at": "2030-01-02T10:00:00+00:00", "price_auction": 5000,
            "name": "Renault Clio", "url_key": "clio-1",
            "short_description": {"html": "1ère mise en circulation 15/05/2024"},
        }
        now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.assertIsNone(module._domaine_item_to_row(item, now=now))
        item["start_auction_lot_at"] = "2025-01-01T10:00:00+00:00"; item["professional_only"] = 1
        self.assertIsNone(module._domaine_item_to_row(item, now=now))

    def test_czech_state_live_roadworthy_petrol_vehicle_is_admitted(self) -> None:
        item = {
            "Id": 70001, "Name": "Osobni automobil Skoda Octavia", "AuctionStatus": 1,
            "StartDate": "2025-01-01T09:00:00", "EndDate": "2026-09-17T08:00:00",
            "Price": "250 000,00", "NbrOfBids": 2,
            "Description": (
                "Datum prvni registrace: 15.05.2024. Palivo: benzin. "
                "Vozidlo je pojizdne a provozuschopne. Technicky prukaz je k dispozici. "
                "Stav tachometru: 12 345 km."
            ),
        }
        row = module._czech_item_to_row(
            item, rates={"CZK": 25.0}, now=dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc)
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["first_registration_date"], "2024-05-15")
        self.assertEqual(row["fuel"], "petrol")
        self.assertEqual(row["price_eur"], "10000.00")
        self.assertEqual(row["mileage_km"], "12345")

    def test_czech_state_rejects_diesel_damage_or_residency_gate(self) -> None:
        base = {
            "Id": 70002, "Name": "Osobni automobil Skoda Superb", "AuctionStatus": 1,
            "StartDate": "2025-01-01T09:00:00", "EndDate": "2026-09-17T08:00:00",
            "Price": "250 000,00",
            "Description": (
                "Datum prvni registrace: 15.05.2024. Palivo: nafta. "
                "Vozidlo je pojizdne. Technicky prukaz."
            ),
        }
        now = dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc)
        self.assertIsNone(module._czech_item_to_row(base, rates={"CZK": 25.0}, now=now))
        base["Description"] = (
            "Datum prvni registrace: 15.05.2024. Palivo: benzin. "
            "Vozidlo je nepojizdne. Technicky prukaz."
        )
        self.assertIsNone(module._czech_item_to_row(base, rates={"CZK": 25.0}, now=now))
        base["Description"] = (
            "Datum prvni registrace: 15.05.2024. Palivo: benzin. "
            "Vozidlo je pojizdne. Technicky prukaz. "
            "Ucastnit se muze pouze obcan Ceske republiky."
        )
        self.assertIsNone(module._czech_item_to_row(base, rates={"CZK": 25.0}, now=now))

    def test_czech_state_requires_exact_registration_not_model_year(self) -> None:
        item = {
            "Id": 70003, "Name": "Osobni automobil Skoda Fabia", "AuctionStatus": 1,
            "StartDate": "2026-01-01T09:00:00", "EndDate": "2026-09-17T08:00:00",
            "Price": "200 000,00", "Description": (
                "Rok vyroby 2024. Palivo: benzin. Vozidlo je pojizdne. Technicky prukaz."
            ),
        }
        self.assertIsNone(module._czech_item_to_row(
            item, rates={"CZK": 25.0}, now=dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc)
        ))

    def test_czech_state_does_not_treat_electronic_auction_as_electric_fuel(self) -> None:
        item = {
            "Id": 70004, "Name": "Osobni automobil Skoda Fabia", "AuctionStatus": 1,
            "StartDate": "2026-01-01T09:00:00", "EndDate": "2026-09-17T08:00:00",
            "Price": "200 000,00", "Description": (
                "Datum prvni registrace: 15.05.2024. Vozidlo je pojizdne. Technicky prukaz."
            ),
        }
        self.assertIsNone(module._czech_item_to_row(
            item, rates={"CZK": 25.0}, now=dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc),
            detail_text="Elektronicke aukce registrace uzivatele",
        ))

    def test_czech_state_rejects_no_price_accessory_or_major_fault(self) -> None:
        item = {
            "Id": 70005, "Name": "Osobni automobil Skoda Kamiq", "AuctionStatus": 1,
            "StartDate": "2026-01-01T09:00:00", "EndDate": "2026-09-17T08:00:00",
            "Price": "200 000,00", "NoPrice": True, "Description": (
                "Datum prvni registrace: 15.05.2024. Palivo: benzin. "
                "Vozidlo je pojizdne. Technicky prukaz."
            ),
        }
        now = dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc)
        self.assertIsNone(module._czech_item_to_row(item, rates={"CZK": 25.0}, now=now))
        item["NoPrice"] = False; item["Name"] = "Soubor pneumatik Skoda"
        self.assertIsNone(module._czech_item_to_row(item, rates={"CZK": 25.0}, now=now))
        item["Name"] = "Osobni automobil Skoda Kamiq"
        item["Description"] += " Motor nelze nastartovat."
        self.assertIsNone(module._czech_item_to_row(item, rates={"CZK": 25.0}, now=now))

    def test_czech_labelled_petrol_electric_is_hybrid(self) -> None:
        self.assertEqual(module._czech_fuel("palivo: BA 95 B + elektricka energie"),
                         "petrol/electric hybrid")
        self.assertEqual(module._czech_fuel("palivo BA 95 B"), "petrol")
        self.assertIsNone(module._czech_fuel("palivo NM"))

    def test_boe_result_ids_dedupe_and_login_gate(self) -> None:
        markup = """
        <a href='detalleSubasta.php?idSub=SUB-JA-2026-263728&ver=1'>one</a>
        <a href='detalleSubasta.php?idSub=SUB-JA-2026-263728&ver=3'>duplicate</a>
        <a href='detalleSubasta.php?idSub=SUB-AT-2026-99ABC&ver=1'>two</a>
        """
        self.assertEqual(module._boe_result_ids(markup),
                         ["SUB-AT-2026-99ABC", "SUB-JA-2026-263728"])
        self.assertTrue(module._boe_price_login_gate(
            "<div>Con puja (inicie sesi&oacute;n para consultar el importe)</div>"
        ))

    def test_boe_monitor_posts_exact_filters_and_never_emits_rows(self) -> None:
        class Response:
            def __init__(self, text): self.text = text
            def raise_for_status(self): return None
        class Session:
            def __init__(self): self.data = None
            def post(self, url, data, headers, timeout):
                self.data = tuple(data)
                return Response("<a href='detalleSubasta.php?idSub=SUB-JA-2026-263728&ver=1'>x</a>")
            def get(self, url, headers, timeout):
                return Response("Con puja (inicie sesion para consultar el importe)")
        session = Session()
        rows, report = module.harvest_boe_monitor(session, timeout=5)
        self.assertEqual(rows, [])
        self.assertEqual(report["discovered_auction_events"], 1)
        self.assertEqual(report["current_bid_visibility"], "login_required")
        self.assertEqual(session.data, module.BOE_SEARCH_DATA)


if __name__ == "__main__":
    unittest.main()
