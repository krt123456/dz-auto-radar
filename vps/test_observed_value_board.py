#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build_observed_value_board as observed


COMPACT_FIELDS = frozenset(
    {
        "id", "m", "t", "p", "q1", "mp", "sv", "sp", "dp", "pn",
        "ps", "pc", "y", "km", "f", "c", "s", "u", "ls", "v",
    }
)
FORBIDDEN_LONG_ECONOMICS_FIELDS = frozenset(
    {
        "profit", "effective_profit", "roi", "effective_roi", "landed_cost",
        "resale_dzd", "customs_dzd", "algerian_price",
    }
)


class ObservedValueBoardTest(unittest.TestCase):
    def test_polish_olx_aliases_share_one_family(self) -> None:
        self.assertEqual(observed.source_family("olx.pl"), "pl_listing_mirrors")
        self.assertEqual(
            observed.source_family("OLX Poland Cars"), "pl_listing_mirrors"
        )

    def test_validation_pool_is_larger_than_publication_target(self) -> None:
        self.assertEqual(observed.DEFAULT_TOP_N, 60_000)
        self.assertEqual(observed.MAX_TOP_N, 100_000)
        self.assertGreater(observed.DEFAULT_TOP_N, 10_000)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "universe.sqlite"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE offers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              source_listing_id TEXT NOT NULL,
              source_url TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              make_model TEXT NOT NULL DEFAULT '',
              variant TEXT NOT NULL DEFAULT '',
              country TEXT NOT NULL DEFAULT '',
              price_eur INTEGER NOT NULL DEFAULT 0,
              raw_price TEXT NOT NULL DEFAULT '',
              currency TEXT NOT NULL DEFAULT '',
              year INTEGER NOT NULL DEFAULT 0,
              mileage_km INTEGER NOT NULL DEFAULT 0,
              fuel TEXT NOT NULL DEFAULT '',
              seller_type TEXT NOT NULL DEFAULT '',
              location TEXT NOT NULL DEFAULT '',
              fetched_at TEXT NOT NULL,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              raw_json TEXT NOT NULL DEFAULT '',
              UNIQUE(source, source_listing_id)
            );
            CREATE INDEX idx_offers_last_seen ON offers(last_seen_at);
            """
        )
        observed_at = datetime.now(UTC).replace(microsecond=0).isoformat()

        def insert(
            source: str,
            number: int,
            price: int,
            country: str,
            *,
            title: str = "Renault Clio TCe 90",
            model: str = "clio5_tce90",
            fuel: str = "petrol",
            year: int = 2025,
            mileage: int = 20_000,
            auction: str = "",
        ) -> None:
            listing_id = f"{source.lower().replace(' ', '-')}-{number}"
            connection.execute(
                """
                INSERT INTO offers(
                  source, source_listing_id, source_url, title, make_model,
                  country, price_eur, year, mileage_km, fuel, seller_type,
                  fetched_at, first_seen_at, last_seen_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dealer', ?, ?, ?, ?)
                """,
                (
                    source, listing_id, f"https://{source.lower().replace(' ', '')}.test/{listing_id}",
                    title, model, country, price, year, mileage, fuel,
                    observed_at, observed_at, observed_at,
                    json.dumps({"listing_id": listing_id, "auction_end_at": auction}),
                ),
            )

        # Source A candidates are benchmarked only against B/C/D.
        insert("Source A", 1, 8_000, "DE")
        insert("Source A", 2, 5_000, "DE")  # below 60% of median: anomaly only
        for number in range(10):
            insert("Source B", number, 11_800 + number * 20, "DE")
            insert("Source C", number, 12_100 + number * 20, "FR")
            insert("Source D", number, 12_400 + number * 20, "NL")
        insert("Source B", 100, 7_500, "DE", title="Renault Clio Cesja")
        insert("Source C", 100, 7_500, "FR", fuel="diesel", title="Renault Clio dCi diesel")
        insert("Blocked", 1, 7_500, "NL")
        connection.commit()
        connection.close()

        self.policy = self.root / "policy.json"
        self.policy.write_text(
            json.dumps({"schema_version": 1, "sources": {"Blocked": {"mode": "blocked"}}}),
            encoding="utf-8",
        )
        self.quarantine = self.root / "quarantine.json"
        self.quarantine.write_text("{}\n", encoding="utf-8")
        self.validation = self.root / "validation.json"

    def arguments(self) -> argparse.Namespace:
        return argparse.Namespace(
            database=self.database,
            ranked_output=self.root / "top.json",
            board_output=self.root / "board.json",
            validation_report=self.validation,
            source_policy=self.policy,
            quarantine_manifest=self.quarantine,
            top_n=20,
            max_observation_age_hours=72,
            min_peer_count=8,
            min_peer_sources=2,
            min_peer_countries=2,
            source_sample_cap=25,
            max_dispersion=0.35,
            min_median_ratio=0.60,
        )

    def insert_offer(
        self,
        *,
        source: str,
        source_listing_id: str,
        native_listing_id: str,
        url: str,
        title: str,
        price: int,
        country: str,
        sale_term_code: str = "",
    ) -> None:
        observed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            INSERT INTO offers(
              source, source_listing_id, source_url, title, make_model,
              country, price_eur, year, mileage_km, fuel, seller_type,
              fetched_at, first_seen_at, last_seen_at, raw_json
            ) VALUES (?, ?, ?, ?, 'clio5_tce90', ?, ?, 2025, 20000,
                      'petrol', 'dealer', ?, ?, ?, ?)
            """,
            (
                source, source_listing_id, url, title, country, price,
                observed_at, observed_at, observed_at,
                json.dumps(
                    {
                        "listing_id": native_listing_id,
                        "auction_end_at": "",
                        "sale_term_code": sale_term_code,
                    }
                ),
            ),
        )
        connection.commit()
        connection.close()

    def validation_payload(
        self, board: dict[str, object], verified_url: str
    ) -> dict[str, object]:
        offers = board["offers"]
        assert isinstance(offers, list)
        results = [
            {
                "board_rank": index,
                "url": offer["u"],
                "status": "verified" if offer["u"] == verified_url else "unknown",
            }
            for index, offer in enumerate(offers, start=1)
        ]
        frontier_rank = next(
            result["board_rank"] for result in results
            if result["status"] == "verified"
        )
        return {
            "schema_version": 1,
            "input_updated_at": board["data_generated_at_utc"],
            "input_algorithm": observed.ALGORITHM,
            "input_snapshot_sha256": board["snapshot_eligible_sha256"],
            "input_offer_fields_sha256": board["offer_fields_sha256"],
            "generated_at": datetime.now(UTC).isoformat(),
            "checked": len(results),
            "ranked_pool_count": len(results),
            "verified_target": 1,
            "direct_attempted_count": len(results),
            "browser_target_count": 0,
            "browser_attempted_count": 0,
            "browser_target_ranks": [],
            "browser_attempted_ranks": [],
            "selection_frontier_rank": frontier_rank,
            "browser_frontier_target_count": 0,
            "browser_frontier_attempted_count": 0,
            "browser_frontier_complete": True,
            "target_reached": True,
            "pool_exhausted": False,
            "ranked_candidate_count": board["ranked_candidate_rows"],
            "ranked_universe_exhausted": (
                board["ranked_candidate_rows"] <= len(results)
            ),
            "full_input_coverage": True,
            "counts": {
                "verified": 1,
                "dead": 0,
                "unknown": len(results) - 1,
            },
            "results": results,
        }

    def test_validation_payload_accepts_target_reached_partial_browser_attempts(self) -> None:
        _, board = observed.build(self.arguments())
        offers = board["offers"]
        assert isinstance(offers, list)
        validation = self.validation_payload(board, offers[0]["u"])
        validation["browser_target_count"] = 2
        validation["browser_attempted_count"] = 1
        validation["browser_target_ranks"] = [1, 2]
        validation["browser_attempted_ranks"] = [1]
        validation["browser_frontier_target_count"] = 1
        validation["browser_frontier_attempted_count"] = 1
        validation["results"][0]["direct_reason"] = "http_200_listing_identity_unproven"
        validation["results"][1]["status"] = "unknown"
        self.validation.write_text(json.dumps(validation), encoding="utf-8")

        verification, summary = observed.load_validation(
            self.validation,
            expected_timestamp=board["data_generated_at_utc"],
            expected_algorithm=board["algorithm"],
            expected_snapshot_sha256=board["snapshot_eligible_sha256"],
            expected_offer_fields_sha256=board["offer_fields_sha256"],
            expected_urls=[offer["u"] for offer in offers],
            expected_ranked_candidate_rows=board["ranked_candidate_rows"],
        )
        self.assertEqual(verification[offers[0]["u"]], 1)
        self.assertTrue(summary["target_reached"])
        self.assertEqual(summary["browser_attempted_count"], 1)
        self.assertEqual(summary["browser_target_count"], 2)

    def test_terminal_paruvendu_protection_redirect_is_not_browser_evidence(self) -> None:
        result = {
            "status": "unknown",
            "reason": "protection_redirect",
            "url": "https://www.paruvendu.fr/a/voiture-occasion/example",
            "final_url": (
                "https://www.paruvendu.fr/communfo/antiaspiration/default/"
                "getCaptcha"
            ),
        }
        self.assertFalse(observed.browser_evidence_expected(result))
        self.assertTrue(
            observed.browser_evidence_expected(
                {**result, "url": "https://cars.example/listing/1"}
            )
        )
        self.assertTrue(
            observed.browser_evidence_expected(
                {**result, "direct_reason": "protection_redirect"}
            )
        )

    def test_source_excluded_peer_math_and_no_invented_economics(self) -> None:
        ranked, board = observed.build(self.arguments())
        candidate = next(
            offer for offer in ranked["offers"]
            if offer["url"].endswith("/source-a-1")
        )
        self.assertFalse(
            any(offer["url"].endswith("/source-a-2") for offer in ranked["offers"])
        )
        self.assertGreater(candidate["peer_lower_quartile_eur"], candidate["price"])
        self.assertEqual(
            candidate["savings_vs_lower_quartile_eur"],
            candidate["peer_lower_quartile_eur"] - candidate["price"],
        )
        self.assertGreaterEqual(candidate["peer_source_count"], 2)
        self.assertGreaterEqual(candidate["peer_country_count"], 2)
        forbidden = {
            "profit", "roi", "landed_cost", "resale_dzd", "customs_dzd",
            "algerian_price", "effective_profit",
        }
        self.assertTrue(forbidden.isdisjoint(candidate))
        self.assertEqual(ranked["unsupported_economics_published"], 0)
        self.assertIn("non_vehicle_price", ranked["rejected_counts"])
        self.assertIn("unsupported_fuel", ranked["rejected_counts"])
        self.assertIn("blocked_source", ranked["rejected_counts"])
        compact = next(
            offer for offer in board["offers"] if offer["id"] == candidate["id"]
        )
        self.assertEqual(set(compact), COMPACT_FIELDS)
        self.assertTrue({"pr", "roi", "rd", "ld", "cd", "ci", "cb"}.isdisjoint(compact))
        self.assertEqual(compact["v"], 0)
        self.assertEqual(board["schema_version"], 2)
        self.assertEqual(board["algorithm"], observed.ALGORITHM)
        self.assertEqual(ranked["schema_version"], 2)
        self.assertEqual(ranked["algorithm"], observed.ALGORITHM)
        self.assertTrue(FORBIDDEN_LONG_ECONOMICS_FIELDS.isdisjoint(candidate))
        self.assertEqual(compact["q1"], candidate["peer_lower_quartile_eur"])
        self.assertEqual(compact["mp"], candidate["peer_median_eur"])
        self.assertEqual(compact["sv"], candidate["savings_vs_lower_quartile_eur"])
        self.assertEqual(compact["pn"], candidate["peer_count"])
        self.assertEqual(compact["ps"], candidate["peer_source_count"])
        self.assertEqual(compact["pc"], candidate["peer_country_count"])

    def test_validation_is_bound_to_generation_snapshot_and_offer_fields(self) -> None:
        ranked, board = observed.build(self.arguments())
        target = board["offers"][0]
        payload = self.validation_payload(board, target["u"])
        self.validation.write_text(json.dumps(payload), encoding="utf-8")
        _, verified_board = observed.build(self.arguments())
        verified = next(offer for offer in verified_board["offers"] if offer["id"] == target["id"])
        self.assertEqual(verified["v"], 1)

        for field, value in (
            ("input_updated_at", "stale-generation"),
            ("input_snapshot_sha256", "0" * 64),
            ("input_offer_fields_sha256", "f" * 64),
        ):
            with self.subTest(field=field):
                mismatched = {**payload, field: value}
                self.validation.write_text(json.dumps(mismatched), encoding="utf-8")
                _, stale_board = observed.build(self.arguments())
                stale = next(
                    offer for offer in stale_board["offers"]
                    if offer["id"] == target["id"]
                )
                self.assertEqual(stale["v"], 0)

    def test_cross_source_native_id_collision_retains_both_namespaced_offers(self) -> None:
        self.insert_offer(
            source="Collision Source A",
            source_listing_id="database-row-a",
            native_listing_id="native-shared-42",
            url="https://collision-a.test/native-shared-42",
            title="Renault Clio collision A",
            price=8_100,
            country="DE",
        )
        self.insert_offer(
            source="Collision Source B",
            source_listing_id="database-row-b",
            native_listing_id="native-shared-42",
            url="https://collision-b.test/native-shared-42",
            title="Renault Clio collision B",
            price=8_200,
            country="FR",
        )

        ranked, board = observed.build(self.arguments())
        retained = [
            offer for offer in ranked["offers"]
            if offer["source"].startswith("Collision Source")
        ]
        self.assertEqual(len(retained), 2)
        self.assertEqual(len({offer["id"] for offer in retained}), 2)
        for offer in retained:
            self.assertRegex(offer["id"], r"^[0-9a-f]{64}$")
        compact_ids = {
            offer["id"] for offer in board["offers"]
            if offer["s"].startswith("Collision Source")
        }
        self.assertEqual(compact_ids, {offer["id"] for offer in retained})

    def test_olx_legacy_and_incremental_identities_cannot_be_duplicate_peers(self) -> None:
        self.insert_offer(
            source="OLX Poland Cars",
            source_listing_id="legacy-database-row",
            native_listing_id="olxpl_tiguan_15_tsi_1084550358",
            url="https://www.olx.pl/d/oferta/legacy-CID5-IDabc.html",
            title="Volkswagen Tiguan petrol",
            price=20_000,
            country="PL",
        )
        self.insert_offer(
            source="olx.pl",
            source_listing_id="olxpl_1084550358",
            native_listing_id="olxpl_1084550358",
            url="https://www.olx.pl/d/oferta/incremental-CID5-IDdef.html",
            title="Volkswagen Tiguan petrol",
            price=20_000,
            country="PL",
        )
        connection = sqlite3.connect(self.database)
        try:
            evidence = observed.ScanEvidence()
            query, parameters = observed.candidate_query(
                "2000-01-01T00:00:00+00:00", 2026
            )
            rows = list(
                observed.eligible_rows(
                    connection,
                    query=query,
                    parameters=parameters,
                    blocked_source_keys=frozenset(),
                    evidence=evidence,
                )
            )
        finally:
            connection.close()
        olx_rows = [row for row in rows if row["source"] == "olx.pl"]
        self.assertEqual(len(olx_rows), 1)
        self.assertEqual(olx_rows[0]["source_family"], "pl_listing_mirrors")
        self.assertEqual(evidence.rejected["identity_duplicate"], 1)

    def test_auction_sale_term_code_is_rejected(self) -> None:
        self.insert_offer(
            source="Auction Source",
            source_listing_id="auction-database-row",
            native_listing_id="auction-native-row",
            url="https://auction-source.test/auction-native-row",
            title="Renault Clio auction listing",
            price=8_300,
            country="DE",
            sale_term_code="auction",
        )

        ranked, board = observed.build(self.arguments())
        self.assertFalse(
            any(offer["source"] == "Auction Source" for offer in ranked["offers"])
        )
        self.assertFalse(any(offer["s"] == "Auction Source" for offer in board["offers"]))
        self.assertGreaterEqual(ranked["rejected_counts"].get("auction_bid", 0), 1)

    def test_rank_order_is_deterministic(self) -> None:
        first, _ = observed.build(self.arguments())
        second, _ = observed.build(self.arguments())
        self.assertEqual(
            [offer["id"] for offer in first["offers"]],
            [offer["id"] for offer in second["offers"]],
        )
        self.assertEqual(first["snapshot_eligible_sha256"], second["snapshot_eligible_sha256"])

    def test_required_source_policy_fails_closed_when_missing(self) -> None:
        self.policy.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "required source policy"):
            observed.build(self.arguments())


if __name__ == "__main__":
    unittest.main()
