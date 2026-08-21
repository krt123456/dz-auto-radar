#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import fr_cz_de_official_watch as module


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class OfficialBroadWatchTests(unittest.TestCase):
    def test_domaine_upcoming_old_diesel_is_retained_as_base_price(self):
        item = {
            "auction": 467,
            "auction_type": "1",
            "end_auction_lot_at": "2026-08-27T08:00:00+00:00",
            "id": 97563,
            "last_bid": None,
            "lot_number": 86,
            "lot_status": 13,
            "name": "Renault KANGOO 1.5 dci",
            "price_auction": 2000,
            "professional_only": 0,
            "short_description": {
                "html": "<p>RENAULT KANGOO, Gazole, 1ère mise en circulation le "
                        "16/04/2015, 165 924 km.</p>"
            },
            "start_auction_lot_at": "2026-08-24T10:00:00+00:00",
            "url_key": "renault-kangoo-97563",
        }

        row = module.domaine_item_to_row(item, now=NOW)

        self.assertIsNotNone(row)
        self.assertEqual(row["id"], "encheres-du-domaine:97563")
        self.assertEqual(row["registration_date"], "2015-04-16")
        self.assertEqual(row["fuel"], "diesel")
        self.assertEqual(row["price_kind"], "base_price")
        self.assertEqual(row["price_amount"], 2000)
        self.assertEqual(row["eligibility_status"], "not_eligible")

    def test_domaine_public_current_bid_is_distinct_from_base(self):
        item = {
            "auction_type": "1",
            "end_auction_lot_at": "2026-08-22T08:00:00Z",
            "id": 100,
            "last_bid": 8900,
            "lot_status": "14",
            "name": "Peugeot 208",
            "price_auction": 5000,
            "professional_only": False,
            "short_description": {"html": "Essence, première mise en circulation 03/06/2024, 12000 km"},
            "start_auction_lot_at": "2026-08-20T08:00:00Z",
            "url_key": "peugeot-208",
        }
        row = module.domaine_item_to_row(item, now=NOW)
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 8900)
        self.assertEqual(row["eligibility_status"], "conditional")
        item["professional_only"] = 1
        row = module.domaine_item_to_row(item, now=NOW)
        self.assertIsNotNone(row, "broad watch retains professional-only official lots")
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertIn("professionals", row["eligibility_reason"])

    def test_czech_current_vehicle_keeps_diesel_and_rejects_accessory(self):
        item = {
            "Id": 61592,
            "Name": "Osobní automobil PEUGEOT 206",
            "Description": (
                "Osobní automobil Peugeot 206. Uvedení do provozu: 22.03.2001. "
                "Palivo: NM. Stav tachometru: 180 500 km. Automobil není provozuschopný."
            ),
            "Price": "13 500,00",
            "NoPrice": False,
            "AuctionStatus": 1,
            "StartDate": "2026-08-20T08:00:00",
            "EndDate": "2026-09-17T08:00:00",
            "NbrOfBids": 3,
            "DistrictName": "Hodonín",
        }
        row = module.czech_item_to_row(item, now=NOW, rates={"CZK": 25.0})
        self.assertIsNotNone(row)
        self.assertEqual(row["registration_date"], "2001-03-22")
        self.assertEqual(row["fuel"], "diesel")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_eur"], 540.0)
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertEqual(row["sale_end_at"], "2026-09-17T08:00:00+00:00")

        item["Id"] = 61384
        item["Name"] = "Disky na automobil Fabia"
        self.assertIsNone(module.czech_item_to_row(item, now=NOW, rates={"CZK": 25.0}))

    def test_czech_no_bid_amount_is_labelled_base_price(self):
        item = {
            "Id": 9,
            "Name": "Osobní automobil ŠKODA FABIA",
            "Description": "Datum první registrace: 3.3.2024; palivo: benzin; automobil je pojízdný.",
            "Price": "250 000,00",
            "NoPrice": False,
            "AuctionStatus": 1,
            "StartDate": "2026-08-20T10:00:00",
            "EndDate": "2026-09-07T07:00:00",
            "NbrOfBids": 0,
        }
        row = module.czech_item_to_row(item, now=NOW, rates={"CZK": 25.0})
        self.assertEqual(row["price_kind"], "base_price")
        self.assertEqual(row["price_amount"], 250000)
        self.assertEqual(row["eligibility_status"], "conditional")

    def test_justiz_category_and_detail_keep_only_real_cars(self):
        markup = """
        <h4>Suchergebnisse (2 Treffer)</h4><ul class="auktionen">
        <li id="rlaid211755"><h5><a href="Audi-A5-211755">Audi A5</a></h5>
        <p>Restzeit (04.09.2026 13:44 Uhr)</p><p>Startgebot: 1.000,00 €</p>
        <p class="gebot">2.050,00 €</p><div>Deutschland</div></li>
        <li id="rlaid211474"><h5><a href="Fussmatten-211474">3 Fussmatten</a></h5>
        <p>(02.09.2026 20:16 Uhr)</p><p>Startgebot: 20,00 €</p>
        <p class="gebot">0,00 €</p><div>Österreich</div></li></ul>
        """
        summaries, total = module.parse_justiz_summaries(markup)
        self.assertEqual(total, 2)
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0].current_price, 2050)
        self.assertEqual(summaries[1].country, "AT")

        detail = """
        <h1>Audi A5</h1><section>Marke / Typ: Audi A5
        Erstzulassung: 21.08.2012 Leistung (kW/PS): 130 kW
        Hubraum (cm³): 1968 Kilometerstand: 216.756
        Antriebsart/Kraftstoff: Diesel Getriebeart: Automatik
        Fahrbereit: Nein Schlüssel vorhanden: Ja Papiere vorhanden: Ja</section>
        """
        row = module.justiz_detail_to_row(summaries[0], detail, now=NOW)
        self.assertIsNotNone(row)
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["mileage_km"], 216756)
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertIsNone(module.justiz_detail_to_row(summaries[1], detail, now=NOW))

    def test_ovm_open_drz_row_preserves_current_bid_and_status(self):
        detail = {
            "id": 1890867,
            "volgNummer": "1",
            "veiling": {"id": 9325, "type": "DRZ", "isGeopend": True},
            "categorie": {"id": 10},
            "kavelData": {
                "kavelDataType": "AUTO",
                "naam": "Personenauto, Porsche 911 Turbo S Cabrio",
                "merk": "Porsche",
                "productType": "911 Turbo S Cabrio",
                "bouwjaar": "2020",
                "specificaties": (
                    "Eerste toelating internationaal: 28-03-2020; "
                    "Brandstof: Benzine; Afgelezen tellerstand: 051.017; Start: ja"
                ),
            },
            "sluitingsDatumISO": "2026-08-26T17:30:44Z",
            "isClosed": False,
            "zichtbaar": True,
            "hoogsteBod": 106031,
            "openingsBod": 10,
            "aantalBiedingen": 45,
            "buitenlandseBiederToegestaan": True,
        }
        row = module.ovm_detail_to_row(detail, now=NOW)
        self.assertIsNotNone(row)
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 106031)
        self.assertEqual(row["mileage_km"], 51017)
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertIn("older", row["eligibility_reason"])

        detail["buitenlandseBiederToegestaan"] = False
        row = module.ovm_detail_to_row(detail, now=NOW)
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertIn("foreign bidder", row["eligibility_reason"])

    def test_build_watch_emits_shared_lane_and_counts(self):
        original = module.HARVESTERS["justiz-auktion"]
        sample = {
            "id": "justiz-auktion:1",
            "canonical_end_utc": "2026-09-01T10:00:00+00:00",
            "source": "justiz-auktion",
            "eligibility_status": "unknown",
            "price_kind": "base_price",
        }
        try:
            module.HARVESTERS["justiz-auktion"] = lambda session, now, timeout: ([sample], {"live": 1})
            payload = module.build_watch(
                object(), now=NOW, timeout=1, sources=["justiz-auktion"], rates={"EUR": 1.0}
            )
        finally:
            module.HARVESTERS["justiz-auktion"] = original
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["lane"], "official_auction_watch")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["eligibility_counts"]["unknown"], 1)
        self.assertEqual(payload["price_kinds"]["base_price"], 1)


if __name__ == "__main__":
    unittest.main()
