#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import justiz_auktion_fetcher as module


DETAIL = """
<html><h2 class="auktionstitel">VW Golf VIII</h2>
<dl><dt>Startgebot:</dt><dd>10.000,00 €</dd><dt>Aktuelles Gebot:</dt><dd>12.500,00 €</dd>
<dt>Versteigerungsende:</dt><dd>04.09.2030 13:44:30</dd></dl>
<div id="beschreibung"><p>Marke / Typ: VW Golf<br>Erstzulassung: 21.08.2024<br>
Hubraum (cm³): 1498<br>Kilometerstand: 21.756<br>Antriebsart/Kraftstoff: Benzin<br>
Getriebeart: Automatik<br>Fahrbereit: Ja<br>Papiere vorhanden: Ja</p></div></html>
"""


class JustizTests(unittest.TestCase):
    def test_money(self) -> None:
        self.assertEqual(module.money("12.500,00 €"), 12500)

    def test_detail(self) -> None:
        row = module.parse_detail(
            DETAIL,
            "https://www.justiz-auktion.de/VW-Golf-211755",
            now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["listing_id"], "211755")
        self.assertEqual(row["price_eur"], "12500.00")
        self.assertEqual(row["mileage_km"], "21756")
        self.assertEqual(row["auction_end_at"], "2030-09-04T11:44:30Z")
        self.assertEqual(row["sale_term_code"], "auction-current-bid")

    def test_missing_vehicle_documents_fails_closed(self) -> None:
        markup = DETAIL.replace("<br>Papiere vorhanden: Ja", "")
        self.assertIsNone(
            module.parse_detail(
                markup,
                "https://www.justiz-auktion.de/VW-Golf-211755",
                now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            )
        )

    def test_conflicting_old_baujahr_fails_closed(self) -> None:
        markup = DETAIL.replace("Getriebeart", "Baujahr: 2012. Getriebeart")
        self.assertIsNone(
            module.parse_detail(
                markup,
                "https://www.justiz-auktion.de/VW-Golf-211755",
                now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            )
        )


if __name__ == "__main__":
    unittest.main()
