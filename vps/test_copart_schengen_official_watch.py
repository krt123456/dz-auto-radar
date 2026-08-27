#!/usr/bin/env python3
import datetime as dt
import unittest
from unittest import mock

import copart_schengen_official_watch as watch


UTC = dt.timezone.utc


class FakeResponse:
    def __init__(self, total, content):
        self._payload = {
            "returnCode": 1,
            "data": {"results": {"totalElements": total, "content": content}},
        }

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class CopartWatchTests(unittest.TestCase):
    def test_finland_queries_all_car_vehicle_facets(self):
        source = watch.SOURCE_BY_KEY["copart-fi"]
        self.assertIn("vehicle_type_code:K", source.vehicle_queries)

    def test_inventory_shrink_during_pagination_fails_closed(self):
        source = watch.SOURCE_BY_KEY["copart-de"]
        items = [{"lotNumberStr": str(100000 + index)} for index in range(100)]

        class Session:
            def post(self, url, json, headers, timeout):
                if json["page"] == 0:
                    return FakeResponse(101, items)
                return FakeResponse(100, [])

        with mock.patch.object(watch, "configured_session", return_value=Session()):
            with self.assertRaisesRegex(RuntimeError, "totalElements decreased"):
                watch.crawl_source(
                    source, timeout=5, page_size=100, max_pages=120,
                )

    def test_hybrid_fuel_semantics_are_specific(self):
        self.assertEqual(watch.normalize_fuel("Hybrid Gasoline"), "petrol/electric hybrid")
        self.assertEqual(watch.normalize_fuel("Hybrid Diesel"), "diesel/electric hybrid")
        self.assertEqual(watch.normalize_fuel("Hybrid"), "hybrid")

    def test_native_currency_price_is_retained_without_fake_euro_value(self):
        source = watch.SOURCE_BY_KEY["copart-fi"]
        row = watch.item_to_row(
            source,
            {
                "lotNumberStr": "12345678",
                "mkn": "Toyota",
                "lm": "Corolla",
                "ld": "Toyota Corolla",
                "ft": "Hybrid Gasoline",
                "cuc": "SEK",
                "lcy": 2024,
                "dynamicLotDetails": {"currentBid": 12500},
            },
            now=dt.datetime(2026, 8, 23, tzinfo=UTC),
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 12500)
        self.assertEqual(row["price_currency"], "SEK")
        self.assertIsNone(row["price_eur"])

    def test_current_bid_no_reserve_and_session_countdown_are_explicit(self):
        source = watch.SOURCE_BY_KEY["copart-es"]
        event = dt.datetime(2026, 9, 3, 9, 30, tzinfo=UTC)
        row = watch.item_to_row(
            source,
            {
                "lotNumberStr": "12345679",
                "mkn": "Toyota",
                "lm": "Yaris",
                "ld": "2024 Toyota Yaris",
                "ft": "Petrol",
                "cuc": "EUR",
                "lcy": 2024,
                "adu": int(event.timestamp() * 1000),
                "dynamicLotDetails": {
                    "currentBid": 5300,
                    "saleStatus": "PURE_SALE",
                    "sellerReserveMet": True,
                },
            },
            now=dt.datetime(2026, 8, 27, tzinfo=UTC),
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 5300)
        self.assertEqual(row["sale_event_utc"], "2026-09-03T09:30:00+00:00")
        self.assertTrue(row["no_reserve"])
        self.assertEqual(row["sale_terms"], "No Reserve")
        self.assertTrue(row["reserve_met"])


if __name__ == "__main__":
    unittest.main()
