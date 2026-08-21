#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zoll_official_auction_watch as module


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def detail_page(
    listing_id: int,
    *,
    title: str,
    end: str = "Sa., 22.08.2026 - 18:15 Uhr",
    amount: str = "13.800,00 EUR",
    bids: int = 0,
    facts: dict[str, str] | None = None,
    description: str = "",
) -> str:
    facts = facts or {}
    fact_html = "".join(f"<dt>{key}:</dt><dd>{value}</dd>" for key, value in facts.items())
    product = json.dumps({
        "@context": "https://schema.org",
        "@type": "Product",
        "name": title,
        "sku": str(listing_id),
        "description": description,
    }, ensure_ascii=False)
    return f"""
    <html><script type="application/ld+json">{product}</script><body>
    <span id="bilder_auktionen_id">{listing_id}</span>
    <h4 id="ueberschrift_auktion">{title}</h4>
    <dd id="auktions_ende">{end}</dd>
    <span id="hoechstgebot">{amount}</span>
    <span id="anz_gebote_gesamt"><span id="anz_gebote_zahl">{bids}</span> Gebote</span>
    <dl>{fact_html}</dl></body></html>
    """


class ZollOfficialAuctionWatchTests(unittest.TestCase):
    def test_listing_parser_gets_unique_products_total_and_official_next(self):
        page = """
        <h1>Auktionssuche: 12 Treffer</h1>
        <a href="/auktion/produkt/1_VW_ID7/100">one</a>
        <a href="/auktion/produkt/1_VW_ID7/100">duplicate</a>
        <a href="/auktion/produkt/1_Ford_Transit/101">two</a>
        <a class="page" href="/auktion/auktionsuebersicht.php?pagination=2" rel="next">next</a>
        """
        links, total, next_url = module.parse_listing_page(page)
        self.assertEqual(total, 12)
        self.assertEqual(links, [
            "/auktion/produkt/1_Ford_Transit/101",
            "/auktion/produkt/1_VW_ID7/100",
        ])
        self.assertIn("pagination=2", next_url)

    def test_positive_bid_is_current_bid_and_preserves_cents(self):
        spec = module.CategorySpec(216, "Gebrauchtwagen", "Gebrauchtwagen", True)
        page = detail_page(
            973959,
            title="1 Audi A4 Avant advanced",
            amount="26.007,50 EUR",
            bids=1,
            facts={
                "Marke (Hersteller)": "Audi",
                "Modell": "A4 Avant",
                "Fahrzeugart": "Kombi",
                "Erstzulassung": "12.10.2023",
                "Kilometerstand": "59.077",
                "Kraftstoffart": "Diesel",
            },
            description="Fahrzeugart: PKW; Erstzulassung: 12.10.2023; Dieselmotor.",
        )
        row, reason = module.parse_product_page(
            page, "/auktion/produkt/1_Audi_A4/973959", category=spec, now=NOW
        )
        self.assertEqual(reason, "")
        self.assertEqual(row["price_amount"], 26007.5)
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["bid_visibility"], "live_current_bid")
        self.assertEqual(row["bid_count"], 1)
        self.assertEqual(row["mileage_km"], 59077)
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertIn("Diesel", row["eligibility_reason"])

    def test_zero_bids_is_base_price_never_current_bid(self):
        spec = module.CategorySpec(215, "Jahreswagen", "Jahreswagen", True)
        page = detail_page(
            100,
            title="VW ID.7 Pro",
            amount="32.000,00 EUR",
            bids=0,
            facts={
                "Marke (Hersteller)": "Volkswagen",
                "Modell": "ID.7 Pro",
                "Fahrzeugart": "Limousine",
                "Erstzulassung": "01.07.2025",
                "Kraftstoffart": "Elektro",
            },
            description="Fahrbereit; Erstzulassung: 01.07.2025; Elektro.",
        )
        row, _ = module.parse_product_page(
            page, "/auktion/produkt/VW_ID7/100", category=spec, now=NOW
        )
        self.assertEqual(row["price_kind"], "base_price")
        self.assertEqual(row["price_label"], "Startpreis (0 Gebote)")
        self.assertNotEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["eligibility_status"], "conditional")

    def test_old_damaged_car_is_retained_and_classified_not_eligible(self):
        spec = module.CategorySpec(
            1102, "Unfall_Bastlerfahrzeuge_PKW", "Unfall- & Bastlerfahrzeuge (PKW)", True
        )
        page = detail_page(
            101,
            title="Mercedes Vito mit Getriebeschaden",
            bids=4,
            facts={
                "Marke (Hersteller)": "Mercedes-Benz",
                "Modell": "Vito",
                "Erstzulassung": "10.02.2017",
                "Kraftstoffart": "Diesel",
            },
            description="Nicht fahrbereit, Getriebeschaden, Erstzulassung: 10.02.2017.",
        )
        row, reason = module.parse_product_page(
            page, "/auktion/produkt/Mercedes_Vito/101", category=spec, now=NOW
        )
        self.assertEqual(reason, "")
        self.assertIsNotNone(row, "broad watch must retain old/damaged cars")
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertEqual(row["official_category_id"], 1102)

    def test_candidate_leaf_keeps_van_and_rejects_equipment_and_heavy_fire_engine(self):
        facts = {"Fahrzeugart": "LKW geschlossener Kasten", "Erstzulassung": "02.02.2024"}
        self.assertEqual(module.classify_vehicle(
            category_id=1124,
            title="Peugeot Boxer Kastenwagen",
            facts=facts,
            description="Fahrzeugidentnummer vorhanden, Kilometerstand 12.000 km",
        ), "van_or_light_commercial")
        self.assertIsNone(module.classify_vehicle(
            category_id=1124,
            title="Defekter Geräteträger Kärcher MIC 70",
            facts={},
            description="Arbeitsmaschine mit Betriebsstunden",
        ))
        self.assertIsNone(module.classify_vehicle(
            category_id=1124,
            title="Geräteträger Nilfisk City Ranger 2250",
            facts={},
            description="Fahrzeugidentnummer vorhanden; selbstfahrende Arbeitsmaschine",
        ))
        self.assertIsNone(module.classify_vehicle(
            category_id=1124,
            title="Unimog U400 mit Ausleger und Schlegelmäher",
            facts={},
            description="Fahrzeugart Zugmaschine, zulässige Gesamtmasse 11990 kg",
        ))
        self.assertIsNone(module.classify_vehicle(
            category_id=1124,
            title="Schmitz Mini Kipper MK1700",
            facts={"Ansprechpartner": "Herr van Haaren"},
            description="Kommunale Arbeitsmaschine mit Kipppritsche",
        ))
        self.assertIsNone(module.classify_vehicle(
            category_id=1166,
            title="Daimler-Benz 815F Löschfahrzeug LF 8/6",
            facts={"Fahrzeugart": "LKW"},
            description="Schweres Feuerwehr-Löschfahrzeug",
        ))

    def test_electric_equipment_does_not_turn_a_diesel_into_a_hybrid(self):
        evidence = module._fuel_evidence(
            {},
            "Citroen Jumper HDI",
            "Kraftstoffart: Diesel Getriebeart: manuell, elektrische Fensterheber",
        )
        self.assertEqual(module.normalize_fuel(evidence), "diesel")

    def test_build_watch_keeps_only_future_vehicle_rows_and_emits_schema(self):
        spec = module.CategorySpec(216, "Gebrauchtwagen", "Gebrauchtwagen", True)
        listing = """
        <h1>Auktionssuche: 2 Treffer</h1>
        <a href="/auktion/produkt/Active/201">active</a>
        <a href="/auktion/produkt/Ended/202">ended</a>
        """
        pages = {
            spec.url: listing,
            module.ORIGIN + "/auktion/produkt/Active/201": detail_page(
                201,
                title="Opel Astra",
                bids=3,
                facts={"Erstzulassung": "01.09.2024", "Kraftstoffart": "Benzin"},
                description="Fahrzeugart: PKW; Erstzulassung: 01.09.2024; Benzin.",
            ),
            module.ORIGIN + "/auktion/produkt/Ended/202": detail_page(
                202,
                title="Old VW Golf",
                end="Do., 20.08.2026 - 08:00 Uhr",
                facts={"Erstzulassung": "01.09.2010", "Kraftstoffart": "Diesel"},
            ),
        }

        class Session:
            def get(self, url, headers, timeout):
                return FakeResponse(pages[url])

        payload = module.build_watch(
            Session(), now=NOW, timeout=5, workers=1, category_specs=(spec,)
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["lane"], "official_auction_watch")
        self.assertEqual(payload["discovered_product_urls"], 2)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["current_bid_rows"], 1)
        self.assertEqual(payload["rows"][0]["id"], "zoll-auktion:201")
        self.assertEqual(payload["source_reports"][module.SOURCE_KEY]["ended_during_refresh"], 1)

    def test_atomic_json_writer_replaces_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            path.write_text('{"old":true}', encoding="utf-8")
            module.atomic_write_json(path, {"schema_version": 1, "rows": []})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
