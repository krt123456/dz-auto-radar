#!/usr/bin/env python3
from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import audit_best_selection as selection_audit
import publish_radar_dashboard as publisher


V7_ALGORITHM = "schengen-observed-peer-value-v7-live-verified"
COMPACT_FIELDS = frozenset(
    {
        "id", "m", "t", "p", "q1", "mp", "sv", "sp", "dp", "pn",
        "ps", "pc", "y", "km", "f", "c", "s", "u", "ls", "v",
    }
)
LONG_FIELDS = frozenset(
    {
        "id", "model", "title", "price", "peer_lower_quartile_eur",
        "peer_median_eur", "savings_vs_lower_quartile_eur",
        "conservative_discount_bps", "median_discount_bps", "peer_count",
        "peer_source_count", "peer_country_count", "peer_dispersion", "year",
        "mileage", "fuel", "country", "source", "url", "seller",
        "last_seen_at",
    }
)
FORBIDDEN_LONG_ECONOMICS_FIELDS = frozenset(
    {
        "profit", "effective_profit", "roi", "effective_roi", "landed_cost",
        "resale_dzd", "customs_dzd", "algerian_price",
    }
)


def compact_offer(number: int, *, verified: int = 1) -> dict[str, Any]:
    price = 10_000 + number
    lower_quartile = 14_000
    median = 15_000
    savings = lower_quartile - price
    conservative_bps = round(10_000 * savings / lower_quartile)
    median_bps = round(10_000 * (median - price) / median)
    source = "Source A" if number % 3 else "Source B"
    return {
        "id": f"{source.casefold().replace(' ', '-')}:offer-{number}",
        "m": "clio5_tce90",
        "t": f"Renault Clio TCe 90 {number}",
        "p": price,
        "q1": lower_quartile,
        "mp": median,
        "sv": savings,
        "sp": round(conservative_bps / 100, 2),
        "dp": round(median_bps / 100, 2),
        "pn": 40,
        "ps": 4,
        "pc": 3,
        "y": 2025,
        "km": 10_000 + number,
        "f": "petrol",
        "c": "DE" if number % 2 == 0 else "FR",
        "s": source,
        "u": f"https://example.test/listing/{number}",
        "ls": "2026-08-11T00:00:00+00:00",
        "v": verified,
    }


def long_offer(compact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": compact["id"],
        "model": compact["m"],
        "title": compact["t"],
        "price": compact["p"],
        "peer_lower_quartile_eur": compact["q1"],
        "peer_median_eur": compact["mp"],
        "savings_vs_lower_quartile_eur": compact["sv"],
        "conservative_discount_bps": round(compact["sp"] * 100),
        "median_discount_bps": round(compact["dp"] * 100),
        "peer_count": compact["pn"],
        "peer_source_count": compact["ps"],
        "peer_country_count": compact["pc"],
        "peer_dispersion": 0.04,
        "year": compact["y"],
        "mileage": compact["km"],
        "fuel": compact["f"],
        "country": compact["c"],
        "source": compact["s"],
        "url": compact["u"],
        "seller": "dealer",
        "last_seen_at": compact["ls"],
    }


def provisional_offer_digest(offers: list[dict[str, Any]]) -> str:
    provisional = [{**offer, "v": 0} for offer in offers]
    digest = hashlib.sha256()
    for offer in provisional:
        digest.update(
            json.dumps(
                offer,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


class PipelineTest(unittest.TestCase):
    def write_v7_fixture(self, temp: Path) -> dict[str, Any]:
        root = temp / "car_deal_finder"
        board_dir = root / "mobile_site_local"
        site = temp / "site"
        board_dir.mkdir(parents=True)
        site.mkdir()
        pin = temp / "pin"
        pin.write_text("correct-horse-radar-secret\n", encoding="utf-8")
        index = temp / "index.html"
        index.write_text("<!doctype html><title>radar</title>", encoding="utf-8")

        offers = [compact_offer(number) for number in range(30)]
        offers[-1]["v"] = 0
        ranked_offers = [long_offer(offer) for offer in offers]
        snapshot_digest = hashlib.sha256(b"stable-v7-snapshot").hexdigest()
        offer_fields_digest = provisional_offer_digest(offers)
        timestamp = "2026-08-11T00:00:00+00:00"
        blocked_sources: list[str] = []
        shared = {
            "schema_version": 2,
            "algorithm": V7_ALGORITHM,
            "generated_at": timestamp,
            "data_generated_at_utc": timestamp,
            "board_built_at_utc": "2026-08-11T00:01:00Z",
            "universe_unique_offers": 100,
            "observation_cutoff_utc": "2026-08-08T00:00:00+00:00",
            "max_observation_age_hours": 72,
            "snapshot_eligible_sha256": snapshot_digest,
            "offer_fields_sha256": offer_fields_digest,
            "source_policy_sha256": None,
            "quarantine_manifest_sha256": publisher.optional_sha256_file(
                Path("/data/car_deal_sonar_export/current/quarantined_sources.json")
            ),
            "blocked_source_keys_sha256": publisher.canonical_json_sha256(
                blocked_sources
            ),
            "blocked_source_key_count": len(blocked_sources),
            "policy_blocked_sources": blocked_sources,
            "scanned_recent_rows": 30,
            "eligible_observed_rows": 30,
            "ranked_candidate_rows": 30,
            "saved_top_rows": 30,
            "ranking_complete": True,
            "outside_saved_better_than_cutoff": 0,
            "anomalous_low_prices_excluded": 0,
            "unsupported_economics_published": 0,
            "connected_country_count": 2,
            "connected_source_count": 2,
            "displayed_country_count": 2,
            "displayed_source_count": 2,
            "live_verified_offer_count": 29,
        }
        board = {
            **shared,
            "updated_utc": timestamp,
            "count": len(offers),
            "scope": "schengen_observed_peer_market",
            "offers": offers,
        }
        ranked = {
            **shared,
            "total_all": 100,
            "qualified": len(ranked_offers),
            "shown": len(ranked_offers),
            "offers": ranked_offers,
        }
        board_path = board_dir / "board.json"
        ranked_path = root / "top_offers.json"
        board_path.write_text(json.dumps(board), encoding="utf-8")
        ranked_path.write_text(json.dumps(ranked), encoding="utf-8")
        (root / "top400_validation.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "input_updated_at": timestamp,
                    "input_algorithm": V7_ALGORITHM,
                    "input_snapshot_sha256": snapshot_digest,
                    "input_offer_fields_sha256": offer_fields_digest,
                    "generated_at": "2026-08-11T00:02:00Z",
                    "checked": len(offers),
                    "counts": {"verified": 29, "dead": 0, "unknown": 1},
                    "results": [
                        {
                            "url": offer["u"],
                            "status": "verified" if number < 29 else "unknown",
                        }
                        for number, offer in enumerate(offers)
                    ],
                }
            ),
            encoding="utf-8",
        )
        database = sqlite3.connect(root / "universe_offers.sqlite")
        database.execute("CREATE TABLE offers (id INTEGER PRIMARY KEY, last_seen_at TEXT)")
        database.executemany(
            "INSERT INTO offers(id, last_seen_at) VALUES (?, ?)",
            [(number, timestamp) for number in range(1, 101)],
        )
        database.commit()
        database.close()
        manifest = temp / "manifest.json"
        audit = temp / "audit.json"
        args = SimpleNamespace(
            root=root,
            site=site,
            pin=pin,
            index=index,
            board=board_path,
            database=root / "universe_offers.sqlite",
            ranked_meta=ranked_path,
            audit_manifest=manifest,
            selection_audit=audit,
            top_n=10,
            per_country_min=1,
            per_source_min=1,
        )
        return {
            "root": root,
            "site": site,
            "pin": pin,
            "index": index,
            "board_path": board_path,
            "ranked_path": ranked_path,
            "manifest": manifest,
            "audit": audit,
            "board": board,
            "ranked": ranked,
            "args": args,
        }

    def test_schengen_lake_filters_scope_and_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / "world.csv"
            output = temp / "schengen.csv"
            report = temp / "report.json"
            fields = [
                "listing_id", "source", "source_url", "country", "price_eur",
                "first_registration_date", "title",
            ]
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    [
                        {"listing_id": "de-1", "source": "A", "source_url": "https://a.test/1", "country": "DE", "price_eur": "10000", "first_registration_date": "2025", "title": "ok"},
                        {"listing_id": "de-2", "source": "A", "source_url": "https://a.test/2\" class=\"card\">ignored", "country": "DE", "price_eur": "11000", "first_registration_date": "2025", "title": "repair"},
                        {"listing_id": "fr-1", "source": "B", "source_url": "http://b.test/1", "country": "FR", "price_eur": "9000", "first_registration_date": "2024", "title": "bad url"},
                        {"listing_id": "gb-1", "source": "C", "source_url": "https://c.test/1", "country": "GB", "price_eur": "8000", "first_registration_date": "2023", "title": "outside"},
                    ]
                )
            subprocess.run(
                [sys.executable, str(HERE / "build_schengen_lake.py"), "--input", str(source), "--output", str(output), "--report", str(report)],
                check=True, capture_output=True, text=True,
            )
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["listing_id"] for row in rows], ["de-1", "de-2"])
            self.assertEqual(rows[1]["source_url"], "https://a.test/2")
            details = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(details["world_rows"], 4)
            self.assertEqual(details["accepted_rows"], 2)
            self.assertEqual(details["repaired_source_urls"], 1)

    def test_full_universe_selection_and_encrypted_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.write_v7_fixture(Path(directory))
            first_compact = fixture["board"]["offers"][0]
            first_ranked = fixture["ranked"]["offers"][0]
            self.assertEqual(set(first_compact), COMPACT_FIELDS)
            self.assertEqual(set(first_ranked), LONG_FIELDS)
            self.assertTrue(FORBIDDEN_LONG_ECONOMICS_FIELDS.isdisjoint(first_ranked))
            self.assertEqual(first_compact["q1"], first_ranked["peer_lower_quartile_eur"])
            self.assertEqual(first_compact["sv"], first_ranked["savings_vs_lower_quartile_eur"])
            self.assertEqual(first_compact["ps"], first_ranked["peer_source_count"])

            prepare = subprocess.run(
                [
                    sys.executable, str(HERE / "publish_radar_dashboard.py"),
                    "--root", str(fixture["root"]), "--site", str(fixture["site"]),
                    "--pin", str(fixture["pin"]), "--index", str(fixture["index"]),
                    "--audit-manifest", str(fixture["manifest"]),
                    "--top-n", "10", "--per-country-min", "1", "--per-source-min", "1",
                    "--prepare-only",
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertIn('"universe_unique_offers": 100', prepare.stdout)
            result = subprocess.run(
                [
                    sys.executable, str(HERE / "audit_best_selection.py"),
                    "--root", str(fixture["root"]), "--site", str(fixture["site"]),
                    "--pin", str(fixture["pin"]), "--output", str(fixture["audit"]),
                    "--top-n", "10", "--per-country-min", "1", "--per-source-min", "1",
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("BEST_SELECTION_AUDIT_PASS", result.stdout)
            report = json.loads(fixture["audit"].read_text(encoding="utf-8"))
            self.assertEqual(report["algorithm"], V7_ALGORITHM)
            self.assertEqual(report["universe_unique_offers"], 100)
            self.assertEqual(report["qualified_universe_offers"], 29)
            self.assertEqual(report["published_offer_count"], 10)
            self.assertEqual(report["verified_live_count"], 10)
            self.assertTrue(report["same_generation_verified_only"])
            self.assertEqual(report["confirmed_dead_or_lease_like_published"], 0)
            self.assertEqual(report["unsupported_economics_published"], 0)

    def test_publisher_and_auditor_reject_wrong_board_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.write_v7_fixture(Path(directory))
            payload, _ = publisher.build_payload(fixture["args"])
            for field, invalid in (
                ("schema_version", 1),
                ("algorithm", "schengen-strict-global-economics-v6-live-verified"),
            ):
                with self.subTest(component="publisher", field=field):
                    board = copy.deepcopy(fixture["board"])
                    board[field] = invalid
                    fixture["board_path"].write_text(json.dumps(board), encoding="utf-8")
                    with self.assertRaises(RuntimeError):
                        publisher.build_payload(fixture["args"])
                with self.subTest(component="auditor", field=field):
                    with self.assertRaises(AssertionError):
                        selection_audit.audit_payload(
                            root=fixture["root"], payload=payload, top_n=10,
                            per_country_min=1, per_source_min=1,
                        )
            fixture["board_path"].write_text(
                json.dumps(fixture["board"]), encoding="utf-8"
            )

    def test_publisher_and_auditor_reject_forbidden_long_economics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.write_v7_fixture(Path(directory))
            payload, _ = publisher.build_payload(fixture["args"])
            tainted = copy.deepcopy(fixture["ranked"])
            tainted["offers"][0].update(
                {field: 1 for field in FORBIDDEN_LONG_ECONOMICS_FIELDS}
            )
            fixture["ranked_path"].write_text(json.dumps(tainted), encoding="utf-8")
            with self.subTest(component="publisher"):
                with self.assertRaises(RuntimeError):
                    publisher.build_payload(fixture["args"])
            with self.subTest(component="auditor"):
                with self.assertRaises(AssertionError):
                    selection_audit.audit_payload(
                        root=fixture["root"], payload=payload, top_n=10,
                        per_country_min=1, per_source_min=1,
                    )


class RuntimeWiringTest(unittest.TestCase):
    def test_refresh_and_installer_use_only_observed_value_pipeline(self) -> None:
        refresh = (HERE / "radar_refresh.sh").read_text(encoding="utf-8")
        installer = (HERE / "install_radar_runtime.sh").read_text(encoding="utf-8")

        self.assertIn("build_observed_value_board.py", refresh)
        builder_calls = [
            match.start()
            for match in re.finditer(r'python3\s+"\$RANKER"', refresh)
        ]
        self.assertEqual(len(builder_calls), 3)
        preflight = re.search(
            r'python3\s+"\$RANKER"\s+--capability-check', refresh
        )
        self.assertIsNotNone(preflight)
        validation = refresh.index("validate_top400.py")
        sealer_calls = [
            match.start()
            for match in re.finditer(r'python3\s+"\$VALIDATION_SEALER"', refresh)
        ]
        self.assertEqual(len(sealer_calls), 2)
        sealer = sealer_calls[1]
        self.assertLess(builder_calls[0], builder_calls[1])
        self.assertLess(builder_calls[1], validation)
        self.assertLess(validation, sealer)
        self.assertLess(sealer, builder_calls[2])

        for legacy in (
            "run_million_planet_cycle.sh",
            "precompute_top400.py",
            "export_schengen_board.py",
        ):
            self.assertNotIn(legacy, refresh)

        self.assertRegex(
            installer,
            r'install -m 0755 "\$SOURCE/build_observed_value_board\.py" '
            r'/opt/sonardeals-radar/build_observed_value_board\.py',
        )
        self.assertRegex(
            installer,
            r'install -m 0755 "\$SOURCE/seal_validation_report\.py" '
            r'/opt/sonardeals-radar/seal_validation_report\.py',
        )


if __name__ == "__main__":
    unittest.main()
