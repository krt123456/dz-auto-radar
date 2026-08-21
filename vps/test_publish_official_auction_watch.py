from __future__ import annotations

import copy
import datetime as dt
import unittest

import build_auction_board as builder
import publish_radar_dashboard as publisher


UTC = dt.timezone.utc


class OfficialAuctionWatchPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
        self.row = {
            "id": "pvp-giustizia:fixture-1",
            "source": "pvp-giustizia",
            "source_key": "pvp-giustizia",
            "registry_key": "pvp-giustizia",
            "registry_priority": 7,
            "url": (
                "https://pvp.giustizia.it/pvp/it/"
                "detail_annuncio.page?idAnnuncio=fixture-1"
            ),
            "title": "Official PVP fixture",
            "model": "Fixture",
            "country": "IT",
            "year": 2024,
            "mileage": 10_000,
            "fuel": "petrol",
            "seller": "",
            "price_eur": 7_500.0,
            "price_kind": "minimum_bid",
            "price_currency": "EUR",
            "price_amount": 7_500.0,
            "price_label": "Offerta minima",
            "bid_visibility": "base_or_minimum_only",
            "registration_date": "2024-05-02",
            "canonical_end_utc": (self.now + dt.timedelta(days=2)).isoformat(),
            "sale_end_utc": (self.now + dt.timedelta(days=2)).isoformat(),
            "ends_soon": False,
            "first_seen_at": None,
            "last_seen_at": self.now.isoformat(),
            "eligibility_status": "review_required",
            "eligibility_reason": "The per-lot bidder and export terms require review.",
            "access_sale_note": "Official PVP scheduled sale.",
            "evidence": "Official PVP API fixture.",
            "ouedkniss_reference": {
                "average_dzd": 4_500_000,
                "sample_count": 3,
                "observed_at_utc": self.now.isoformat(),
                "source": "Ouedkniss",
            },
        }

    def watch(self) -> dict:
        return builder.build_monitored_watch(
            [self.row], generated_at=self.now.isoformat(), rejected_counts={}
        )

    def test_builder_watch_passes_publisher_contract(self) -> None:
        watch = self.watch()
        publisher.validate_official_auction_watch(watch, now=self.now)
        self.assertEqual(watch["row_count"], 1)

    def test_non_official_domain_is_rejected(self) -> None:
        watch = self.watch()
        watch["rows"][0]["url"] = "https://example.invalid/lot/1"
        with self.assertRaisesRegex(RuntimeError, "registry/domain"):
            publisher.validate_official_auction_watch(watch, now=self.now)

    def test_priced_semantic_without_amount_is_rejected(self) -> None:
        watch = self.watch()
        watch["rows"][0]["price_eur"] = None
        with self.assertRaisesRegex(RuntimeError, "missing its labelled price"):
            publisher.validate_official_auction_watch(watch, now=self.now)

    def test_stale_watch_is_rejected(self) -> None:
        watch = self.watch()
        watch["generated_at_utc"] = (
            self.now - dt.timedelta(hours=9)
        ).isoformat()
        with self.assertRaisesRegex(RuntimeError, "stale"):
            publisher.validate_official_auction_watch(watch, now=self.now)

    def test_registry_drift_is_rejected(self) -> None:
        watch = copy.deepcopy(self.watch())
        watch["registry_digest"] += "drift"
        with self.assertRaisesRegex(RuntimeError, "registry digest"):
            publisher.validate_official_auction_watch(watch, now=self.now)

    def test_invalid_ouedkniss_reference_is_rejected(self) -> None:
        watch = self.watch()
        watch["rows"][0]["ouedkniss_reference"]["sample_count"] = 1
        with self.assertRaisesRegex(RuntimeError, "Ouedkniss reference"):
            publisher.validate_official_auction_watch(watch, now=self.now)


if __name__ == "__main__":
    unittest.main()
