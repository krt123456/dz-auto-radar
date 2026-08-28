from __future__ import annotations

import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path

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

    def test_cross_border_exleasingcar_asset_country_is_accepted(self) -> None:
        row = copy.deepcopy(self.row)
        row.update(
            {
                "id": "exleasingcar:fixture-1",
                "source": "exleasingcar",
                "source_key": "exleasingcar",
                "registry_key": "exleasingcar",
                "registry_priority": 22,
                "url": "https://www.exleasingcar.com/en/auto-details/fixture-1",
                "country": "BE",
            }
        )
        watch = builder.build_monitored_watch(
            [row], generated_at=self.now.isoformat(), rejected_counts={}
        )
        publisher.validate_official_auction_watch(watch, now=self.now)

    def test_cross_border_rbauction_asset_country_is_accepted(self) -> None:
        row = copy.deepcopy(self.row)
        row.update(
            {
                "id": "rbauction-eu:fixture-1",
                "source": "rbauction-eu",
                "source_key": "rbauction-eu",
                "registry_key": "rbauction-eu",
                "registry_priority": 22,
                "url": "https://www.rbauction.com/pdp/fixture/fixture-1",
                "country": "ES",
            }
        )
        watch = builder.build_monitored_watch(
            [row], generated_at=self.now.isoformat(), rejected_counts={}
        )
        publisher.validate_official_auction_watch(watch, now=self.now)

    def test_cross_border_autorola_asset_country_is_accepted(self) -> None:
        row = copy.deepcopy(self.row)
        row.update(
            {
                "id": "autorola-eu:111:fixture-1",
                "source": "autorola-eu",
                "source_key": "autorola-eu",
                "registry_key": "autorola-eu",
                "registry_priority": 22,
                "url": "https://www.autorola.eu/dealer/bid?eid=fixture-1&aid=111",
                "country": "BE",
                "adapter_authorized": True,
            }
        )
        watch = builder.build_monitored_watch(
            [row], generated_at=self.now.isoformat(), rejected_counts={}
        )
        publisher.validate_official_auction_watch(watch, now=self.now)

    def test_priced_semantic_without_amount_is_rejected(self) -> None:
        watch = self.watch()
        watch["rows"][0]["price_eur"] = None
        watch["rows"][0]["price_amount"] = None
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

    def test_sharded_public_watch_reconstructs_all_rows(self) -> None:
        second = copy.deepcopy(self.row)
        second["id"] = "pvp-giustizia:fixture-2"
        second["url"] = (
            "https://pvp.giustizia.it/pvp/it/"
            "detail_annuncio.page?idAnnuncio=fixture-2"
        )
        watch = builder.build_monitored_watch(
            [self.row, second], generated_at=self.now.isoformat(), rejected_counts={}
        )
        root_bytes, parts, root = publisher.build_published_official_auction_watch(
            watch, part_rows=1, now=self.now
        )
        self.assertEqual(root["schema_version"], 2)
        self.assertEqual(len(parts), 2)
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary)
            (site / "official_auction_watch.json").write_bytes(root_bytes)
            for relative_path, content in parts:
                path = site / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            reconstructed, published_root = publisher.load_published_official_auction_watch(
                site, now=self.now
            )
        self.assertEqual(reconstructed["row_count"], 2)
        self.assertEqual([row["id"] for row in reconstructed["rows"]], [
            "pvp-giustizia:fixture-1", "pvp-giustizia:fixture-2",
        ])
        self.assertEqual(published_root["parts"], root["parts"])

    def test_sharded_public_watch_rejects_tampered_part(self) -> None:
        root_bytes, parts, _ = publisher.build_published_official_auction_watch(
            self.watch(), part_rows=1, now=self.now
        )
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary)
            (site / "official_auction_watch.json").write_bytes(root_bytes)
            relative_path, content = parts[0]
            path = site / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content + b" ")
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                publisher.load_published_official_auction_watch(site, now=self.now)


if __name__ == "__main__":
    unittest.main()
