#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import pvp_official_auction_watch as module


UTC = dt.timezone.utc


class FakeResponse:
    def __init__(self, *, text: str = "", payload=None, status: int = 200):
        self.text = text
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def pvp_item(
    listing_id: int,
    sale_at: str,
    description: str,
    *,
    base=10_000,
    minimum=7_500,
    lot="1",
):
    return {
        "id": listing_id,
        "dataOraVendita": sale_at,
        "descLotto": description,
        "prezzoBaseAsta": base,
        "offertaMinima": minimum,
        "numeroLotto": lot,
        "tribunale": "Tribunale di ROMA",
    }


class PvpOfficialWatchTests(unittest.TestCase):
    def test_missing_public_amount_is_retained_as_unknown_not_base_price(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        item = pvp_item(
            99,
            "2026-09-22T12:00",
            "Autovettura FIAT, immatricolazione 2024, alimentazione benzina.",
            base=0,
            minimum=0,
        )

        row = module.item_to_row(item, now=now)

        self.assertIsNotNone(row)
        self.assertIsNone(row["price_eur"])
        self.assertEqual(row["price_kind"], "unknown")
        self.assertEqual(row["bid_visibility"], "not_published")

    def test_vehicle_fields_and_minimum_offer_are_labelled_without_live_bid(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        item = pvp_item(
            4620200,
            "2026-09-22T12:00",
            "Autovettura, marca MG, modello ZS, data di immatricolazione "
            "03.06.2024, alimentazione benzina, km rilevati in sede di perizia "
            "57.892, carta di circolazione in originale, condizioni buone e marciante.",
            base=13_800,
            minimum=13_800,
        )

        row = module.item_to_row(item, now=now)

        self.assertIsNotNone(row)
        self.assertEqual(row["id"], "pvp-giustizia:4620200")
        self.assertEqual(row["model"], "MG ZS")
        self.assertEqual(row["registration_date"], "2024-06-03")
        self.assertEqual(row["year"], 2024)
        self.assertEqual(row["fuel"], "petrol")
        self.assertEqual(row["mileage_km"], 57_892)
        self.assertEqual(row["price_amount"], 13_800)
        self.assertEqual(row["price_kind"], "minimum_offer")
        self.assertEqual(row["price_label"], "Offerta minima")
        self.assertEqual(row["bid_visibility"], "base_or_minimum_only")
        self.assertEqual(row["eligibility_status"], "conditional")
        self.assertEqual(row["sale_end_at"], "2026-09-22T10:00:00+00:00")
        self.assertNotEqual(row["price_kind"], "current_bid")

    def test_old_diesel_is_retained_but_marked_not_eligible(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        item = pvp_item(
            4620356,
            "2026-12-03T12:00",
            "Mercedes-Benz Classe E immatricolata in data 31/05/2004, "
            "alimentata a gasolio, km 491.201.",
            base=3_000,
            minimum=2_250,
        )

        row = module.item_to_row(item, now=now)

        self.assertIsNotNone(row, "broad watch must retain an ineligible active lot")
        self.assertEqual(row["fuel"], "diesel")
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertIn("Diesel", row["eligibility_reason"])
        self.assertEqual(row["mileage_km"], 491_201)

    def test_dynamic_discovery_and_all_page_fetch(self):
        pages = {
            0: {
                "content": [{"id": 1}, {"id": 2}],
                "totalPages": 2,
                "totalElements": 3,
            },
            1: {
                "content": [{"id": 3}],
                "totalPages": 2,
                "totalElements": 3,
            },
        }

        class Session:
            def __init__(self):
                self.posts = []

            def get(self, url, headers, timeout):
                if url == module.PVP_HOME_URL:
                    return FakeResponse(text='<div data-path="/bo-a1b2-986a1b71/bo-ms"></div>')
                return FakeResponse(payload={
                    "msUrl": {
                        "ricerca": "ric-123-986a1b71/ric-ms",
                        "vendite": "ve-456-986a1b71/ve-ms",
                    }
                })

            def post(self, url, params, json, headers, timeout):
                self.posts.append((url, params, json))
                return FakeResponse(payload={"body": pages[params["page"]]})

        session = Session()
        services = module.discover_services(session, timeout=5)
        items, total = module.fetch_catalogue(
            session, services["search_url"], timeout=5, page_size=2
        )

        self.assertEqual(services["search_url"],
                         "https://pvp.giustizia.it/ric-123-986a1b71/ric-ms/ricerca/vendite")
        self.assertEqual(total, 3)
        self.assertEqual([item["id"] for item in items], [1, 2, 3])
        self.assertEqual([call[1]["page"] for call in session.posts], [0, 1])
        self.assertTrue(all(call[2] == module.SEARCH_BODY for call in session.posts))

    def test_build_watch_filters_only_expired_rows_and_emits_shared_schema(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        active = pvp_item(
            10,
            "2026-08-22T10:00",
            "FIAT, anno immatricolazione 2024, alimentazione benzina, km 10000",
            minimum=0,
            base=7_000,
        )
        expired = pvp_item(
            11,
            "2026-08-20T10:00",
            "Audi anno 2020 alimentazione diesel",
        )

        class Session:
            def get(self, url, headers, timeout):
                if url == module.PVP_HOME_URL:
                    return FakeResponse(text="/bo-abc-986a1b71/bo-ms")
                return FakeResponse(payload={
                    "msUrl": {
                        "ricerca": "ric-abc-986a1b71/ric-ms",
                        "vendite": "ve-abc-986a1b71/ve-ms",
                    }
                })

            def post(self, url, params, json, headers, timeout):
                return FakeResponse(payload={"body": {
                    "content": [active, expired],
                    "totalPages": 1,
                    "totalElements": 2,
                }})

        payload = module.build_watch(Session(), now=now, timeout=5)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["lane"], "official_auction_watch")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["id"], "pvp-giustizia:10")
        self.assertEqual(payload["rows"][0]["price_kind"], "base_price")
        self.assertEqual(payload["rows"][0]["eligibility_status"], "unknown")
        self.assertEqual(payload["source_reports"][module.SOURCE_KEY]["catalogue_total"], 2)


if __name__ == "__main__":
    unittest.main()
