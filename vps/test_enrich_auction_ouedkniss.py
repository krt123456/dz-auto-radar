#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

import enrich_auction_ouedkniss as module


class OuedknissReferenceTests(unittest.TestCase):
    def test_price_units(self) -> None:
        self.assertEqual(module.price_dzd({"price": 5_580_000}), 5_580_000)
        self.assertEqual(module.price_dzd({"pricePreview": 558}), 5_580_000)

    def test_search_identity_uses_model_key_alias(self) -> None:
        value = module.search_identity({"model": "vw_multivan_1_4_tsi", "title": "1 Multivan"})
        self.assertIsNotNone(value)
        self.assertEqual(value[0], "Volkswagen multivan")

    def test_observe_requires_two_and_removes_large_outlier(self) -> None:
        rows = [
            {"id": "1", "title": "Renault Clio 2024", "slug": "a", "price": 5_000_000},
            {"id": "2", "title": "Renault Clio 2024", "slug": "b", "price": 5_200_000},
            {"id": "3", "title": "Renault Clio 2024", "slug": "c", "price": 5_100_000},
            {"id": "4", "title": "Renault Clio 2024", "slug": "d", "price": 40_000_000},
        ]
        with mock.patch.object(module, "query_page", return_value=rows):
            result = module.observe(mock.Mock(), "Renault Clio", ("renault", "clio"), 2024,
                                    pages=1, timeout=1, sleep_seconds=0)
        self.assertIsNotNone(result)
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["average_dzd"], 5_100_000)

    def test_enrich_reuses_fresh_cache(self) -> None:
        observed = dt.datetime.now(dt.timezone.utc).isoformat()
        reference = {"average_dzd": 5_100_000, "median_dzd": 5_100_000,
                     "sample_count": 3, "min_dzd": 5_000_000, "max_dzd": 5_200_000,
                     "model_query": "Renault Clio", "model_year": 2024,
                     "observed_at_utc": observed, "source": "Ouedkniss", "evidence_urls": []}
        cache = {"entries": {"renault clio|2024": {"cached_at_utc": observed,
                                                    "reference": reference}}}
        lane = {"rows": [{"title": "Renault Clio", "model": "renault_clio", "year": 2024}]}
        with mock.patch.object(module, "observe") as observe:
            enriched, _ = module.enrich(lane, cache, ttl_hours=6, pages=1, timeout=1,
                                        sleep_seconds=0, max_queries=5)
        observe.assert_not_called()
        self.assertEqual(enriched["rows"][0]["ouedkniss_reference"]["average_dzd"], 5_100_000)


if __name__ == "__main__":
    unittest.main()
