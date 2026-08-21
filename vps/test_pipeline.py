#!/usr/bin/env python3
from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import audit_best_selection as selection_audit
import build_observed_value_board as observed
import listing_availability as lifecycle
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

    def test_autoscout_collection_urls_are_rejected_at_release_gates(self) -> None:
        search = compact_offer(1)
        search["u"] = "https://www.autoscout24.com/lst/toyota/corolla?atype=C"
        self.assertFalse(publisher.eligible_offer(search))
        self.assertFalse(selection_audit.eligible(search))

        detail = compact_offer(1)
        detail["u"] = (
            "https://www.autoscout24.it/annunci/toyota-corolla-hybrid-"
            "503f6455-b5a5-48af-bcfa-8a08c1dd87c7"
        )
        self.assertTrue(publisher.eligible_offer(detail))
        self.assertTrue(selection_audit.eligible(detail))

    def test_french_damaged_inflections_are_rejected_without_broad_matches(self) -> None:
        damaged_titles = (
            "Peugeot 208 endommagé",
            "Peugeot 208 endommagée",
            "Peugeot 208 endommagés",
            "Peugeot 208 endommagées",
            "Peugeot 208 endommage",
            "Peugeot 208 endommagee",
            "Peugeot 208 endommages",
            "Peugeot 208 endommagees",
        )
        risk_matchers = (
            (observed.RISK_PATTERN, observed.normalized_text),
            (selection_audit.RISK_PATTERN, selection_audit.normalized_semantic_text),
            (publisher.RISK_PATTERN, publisher.normalized_semantic_text),
        )
        for title in damaged_titles:
            with self.subTest(title=title):
                for pattern, normalizer in risk_matchers:
                    self.assertIsNotNone(pattern.search(normalizer(title)))
                offer = compact_offer(1)
                offer["t"] = title
                self.assertFalse(publisher.eligible_offer(offer))
                self.assertFalse(selection_audit.eligible(offer))

        related_but_not_damaged = (
            "Peugeot 208 avec protection anti-endommagement",
            "Peugeot 208 à ne pas endommager",
            "Peugeot 208 historique des dommages disponible",
        )
        for title in related_but_not_damaged:
            with self.subTest(negative_control=title):
                for pattern, normalizer in risk_matchers:
                    self.assertIsNone(pattern.search(normalizer(title)))
                offer = compact_offer(1)
                offer["t"] = title
                self.assertTrue(publisher.eligible_offer(offer))
                self.assertTrue(selection_audit.eligible(offer))

    def test_publisher_accepts_git_worktree_metadata_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            (site / ".git").write_text(
                "gitdir: /tmp/example/.git/worktrees/publication\n",
                encoding="utf-8",
            )
            completed = SimpleNamespace(returncode=0, stdout="true\n")
            with mock.patch.object(publisher, "run_git", return_value=completed):
                self.assertTrue(publisher.is_git_checkout(site))

            (site / ".git").unlink()
            self.assertFalse(publisher.is_git_checkout(site))
    def write_v7_fixture(self, temp: Path) -> dict[str, Any]:
        root = temp / "car_deal_finder"
        board_dir = root / "mobile_site_local"
        site = temp / "site"
        board_dir.mkdir(parents=True)
        site.mkdir()
        source_policy = root / "schengen_source_policy.json"
        source_policy.write_text(
            json.dumps({"schema_version": 1, "sources": {}}),
            encoding="utf-8",
        )
        pin = temp / "pin"
        pin.write_text("correct-horse-radar-secret\n", encoding="utf-8")
        index = temp / "index.html"
        index.write_text("<!doctype html><title>radar</title>", encoding="utf-8")

        offers = [compact_offer(number) for number in range(30)]
        offers[-1]["v"] = 0
        ranked_offers = [long_offer(offer) for offer in offers]
        snapshot_digest = hashlib.sha256(b"stable-v7-snapshot").hexdigest()
        offer_fields_digest = provisional_offer_digest(offers)
        timestamp = (
            datetime.now(UTC).replace(microsecond=0).isoformat()
        )
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
            "source_policy_sha256": publisher.sha256_file(source_policy),
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
            "validation": {
                "schema_version": 1,
                "input_updated_at": timestamp,
                "input_algorithm": V7_ALGORITHM,
                "input_snapshot_sha256": snapshot_digest,
                "input_offer_fields_sha256": offer_fields_digest,
                "generated_at": (
                    datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                ),
                "checked": len(offers),
                "counts": {"verified": 29, "dead": 0, "unknown": 1},
            },
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
            payload, _ = publisher.build_payload(fixture["args"])
            self.assertEqual(
                payload["displayed_country_count"],
                len({offer["c"] for offer in payload["offers"]}),
            )
            self.assertEqual(
                payload["displayed_source_count"],
                len({offer["s"] for offer in payload["offers"]}),
            )

    def test_publisher_rejects_stale_missing_or_future_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.write_v7_fixture(Path(directory))
            freshness = publisher.PUBLICATION_VALIDATION_MAX_AGE_HOURS

            def with_generated_at(generated_at):
                board = copy.deepcopy(fixture["board"])
                board["validation"] = {
                    **board["validation"],
                    "generated_at": generated_at,
                }
                fixture["board_path"].write_text(json.dumps(board), encoding="utf-8")
                return board

            for label, generated_at in (
                ("missing", None),
                (
                    "stale",
                    (
                        datetime.now(UTC)
                        - timedelta(hours=freshness, seconds=1)
                    )
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
                (
                    "future",
                    (
                        datetime.now(UTC) + timedelta(hours=1)
                    )
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
            ):
                with self.subTest(component="publisher", case=label):
                    with_generated_at(generated_at)
                    with self.assertRaises(RuntimeError):
                        publisher.build_payload(fixture["args"])
            just_under = (
                datetime.now(UTC) - timedelta(hours=freshness) + timedelta(seconds=1)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            with_generated_at(just_under)
            payload, _ = publisher.build_payload(fixture["args"])
            self.assertEqual(payload["published_offer_count"], 10)

    def test_publication_freshness_boundaries_fail_closed(self) -> None:
        now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)

        def validation(age: timedelta) -> dict[str, str]:
            generated = now - age
            return {
                "generated_at": generated.isoformat().replace("+00:00", "Z")
            }

        publisher.require_publishable_validation(
            validation(publisher.PUBLICATION_VALIDATION_MAX_AGE - timedelta(microseconds=1)),
            now=now,
        )
        for age in (
            publisher.PUBLICATION_VALIDATION_MAX_AGE,
            publisher.PUBLICATION_VALIDATION_MAX_AGE + timedelta(microseconds=1),
        ):
            with self.subTest(age=age):
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    publisher.require_publishable_validation(validation(age), now=now)
        with self.assertRaisesRegex(RuntimeError, "future"):
            publisher.require_publishable_validation(
                validation(timedelta(microseconds=-1)), now=now,
            )

    def test_board_data_freshness_boundaries_match_dashboard(self) -> None:
        now = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)

        def timestamp(age: timedelta) -> str:
            return (now - age).isoformat().replace("+00:00", "Z")

        for age in (
            publisher.PUBLICATION_DATA_MAX_AGE - timedelta(seconds=1),
            -publisher.PUBLICATION_DATA_FUTURE_SKEW_ALLOWANCE,
        ):
            with self.subTest(accepted_age=age):
                publisher.require_publishable_data_timestamp(
                    timestamp(age), now=now,
                )
        with self.assertRaisesRegex(RuntimeError, "stale"):
            publisher.require_publishable_data_timestamp(
                timestamp(publisher.PUBLICATION_DATA_MAX_AGE), now=now,
            )
        with self.assertRaisesRegex(RuntimeError, "future"):
            publisher.require_publishable_data_timestamp(
                timestamp(
                    -publisher.PUBLICATION_DATA_FUTURE_SKEW_ALLOWANCE
                    - timedelta(seconds=1)
                ),
                now=now,
            )

        dashboard = (HERE.parent / "index.html").read_text(encoding="utf-8")
        max_age = re.search(
            r"const DATA_MAX_AGE_MS=(\d+)\*60\*60\*1000;", dashboard,
        )
        freshness_check = re.search(
            r"dataAge>=-(\d+)\*60\*1000&&dataAge<DATA_MAX_AGE_MS", dashboard,
        )
        self.assertIsNotNone(max_age)
        self.assertIsNotNone(freshness_check)
        assert max_age is not None and freshness_check is not None
        self.assertEqual(
            int(max_age.group(1)), publisher.PUBLICATION_DATA_MAX_AGE_HOURS,
        )
        self.assertEqual(
            timedelta(minutes=int(freshness_check.group(1))),
            publisher.PUBLICATION_DATA_FUTURE_SKEW_ALLOWANCE,
        )

    def test_push_only_rechecks_board_data_freshness_before_git_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.write_v7_fixture(Path(directory))
            board = copy.deepcopy(fixture["board"])
            stale_data_timestamp = (
                datetime.now(UTC) - publisher.PUBLICATION_DATA_MAX_AGE
            ).isoformat().replace("+00:00", "Z")
            for key in ("generated_at", "data_generated_at_utc", "updated_utc"):
                board[key] = stale_data_timestamp
            board["validation"]["input_updated_at"] = stale_data_timestamp
            fixture["board_path"].write_text(json.dumps(board), encoding="utf-8")
            fixture["manifest"].write_text(
                json.dumps(
                    {
                        "source_board_sha256": publisher.sha256_file(
                            fixture["board_path"]
                        )
                    }
                ),
                encoding="utf-8",
            )
            fixture["audit"].write_text(
                json.dumps({"result": "BEST_SELECTION_AUDIT_PASS"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "stale"):
                publisher.enforce_publication_audit(fixture["args"])

    def test_push_only_rechecks_validation_freshness_before_git_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.write_v7_fixture(Path(directory))
            board = copy.deepcopy(fixture["board"])
            board["validation"]["generated_at"] = (
                datetime.now(UTC) - publisher.PUBLICATION_VALIDATION_MAX_AGE
            ).isoformat().replace("+00:00", "Z")
            fixture["board_path"].write_text(json.dumps(board), encoding="utf-8")
            fixture["manifest"].write_text(
                json.dumps(
                    {
                        "source_board_sha256": publisher.sha256_file(
                            fixture["board_path"]
                        )
                    }
                ),
                encoding="utf-8",
            )
            fixture["audit"].write_text(
                json.dumps({"result": "BEST_SELECTION_AUDIT_PASS"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "validation evidence is stale"):
                publisher.enforce_publication_audit(fixture["args"])

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

    def test_publisher_and_auditor_fail_closed_without_source_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.write_v7_fixture(Path(directory))
            payload, _ = publisher.build_payload(fixture["args"])
            (fixture["root"] / "schengen_source_policy.json").unlink()

            with self.subTest(component="publisher"):
                with self.assertRaises(RuntimeError):
                    publisher.build_payload(fixture["args"])
            with self.subTest(component="auditor"):
                with self.assertRaises(AssertionError):
                    selection_audit.audit_payload(
                        root=fixture["root"], payload=payload, top_n=10,
                        per_country_min=1, per_source_min=1,
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
    def test_refresh_resumes_validation_without_rewriting_checkpoint_inputs(self) -> None:
        refresh = HERE / "radar_refresh.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "state" / "runtime"
            board = root / "car" / "mobile_site_local" / "board.json"
            ranked = root / "car" / "top_offers.json"
            bin_dir = root / "bin"
            calls = root / "calls.log"
            runtime.mkdir(parents=True)
            board.parent.mkdir(parents=True)
            bin_dir.mkdir()
            board.write_text('{"offers":[{"u":"https://example.test/1"}]}', encoding="utf-8")
            ranked.write_text('{"offers":[{"u":"https://example.test/1"}]}', encoding="utf-8")
            (runtime / "top400_validation.checkpoint.json").write_text(
                '{"checkpoint":"fixture"}', encoding="utf-8"
            )
            python_stub = bin_dir / "python3"
            python_stub.write_text(
                '#!/bin/sh\nprintf "python3 %s\\n" "$*" >> "$RADAR_TEST_CALLS"\n',
                encoding="utf-8",
            )
            xvfb_stub = bin_dir / "xvfb-run"
            xvfb_stub.write_text(
                '#!/bin/sh\nprintf "xvfb-run %s\\n" "$*" >> "$RADAR_TEST_CALLS"\nexit 42\n',
                encoding="utf-8",
            )
            python_stub.chmod(0o755)
            xvfb_stub.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "RADAR_TEST_CALLS": str(calls),
                "RADAR_CAR_ROOT": str(root / "car"),
                "RADAR_STATE_DIR": str(root / "state"),
                "RADAR_REFRESH_LOCK_FILE": str(root / "refresh.lock"),
                "RADAR_RANKER": str(root / "ranker.py"),
                "RADAR_VALIDATION_SEALER": str(root / "sealer.py"),
            }
            result = subprocess.run(
                ["bash", str(refresh), "smart", "scheduled-resume-test"],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            invoked = calls.read_text(encoding="utf-8")
            self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
            self.assertIn("RADAR_VALIDATION_RESUME", result.stdout)
            self.assertIn("--capability-check", invoked)
            self.assertIn("xvfb-run -a python3", invoked)
            self.assertNotIn("--database", invoked)
            self.assertNotIn("run_parallel_smart_harvest", invoked)
            self.assertNotIn("capture_alces_fx", invoked)

    def test_real_dashboard_accepts_python_half_even_basis_points(self) -> None:
        html = (HERE.parent / "index.html").read_text(encoding="utf-8")
        match = re.search(
            r'<script>\s*"use strict";(?P<contract>.*?)const CONTROL_API=',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        payload = {
            "schema_version": 2,
            "algorithm": V7_ALGORITHM,
            "unsupported_economics_published": 0,
            "offers": [
                {
                    "id": "half-even-offer",
                    "m": "clio5_tce90",
                    "t": "Renault Clio TCe 90",
                    "p": 13_996,
                    "q1": 16_000,
                    "mp": 18_000,
                    "sv": 2_004,
                    "sp": 12.52,
                    "dp": 22.24,
                    "pn": 30,
                    "ps": 4,
                    "pc": 3,
                    "y": 2025,
                    "km": 20_000,
                    "f": "petrol",
                    "c": "DE",
                    "s": "Source A",
                    "u": "https://example.test/listing/half-even",
                    "ls": "2026-08-11T00:00:00+00:00",
                    "v": 1,
                }
            ],
        }
        contract = '"use strict";\n' + match.group("contract")
        exercise = f"""
const payload={json.dumps(payload, separators=(',', ':'))};
validatePayload(payload);
payload.offers[0].sp=12.53;
let rejected=false;
try{{validatePayload(payload);}}catch(error){{rejected=error&&error.code==="unsupported_contract";}}
if(!rejected)process.exit(9);
process.stdout.write("DASHBOARD_CONTRACT_PASS");
"""
        result = subprocess.run(
            ["node", "-"],
            input=contract + exercise,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "DASHBOARD_CONTRACT_PASS")

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
        self.assertEqual(refresh.count('--top-n "$RANKED_POOL_LIMIT"'), 2)
        self.assertIn('${RADAR_RANKED_POOL_LIMIT:-60000}', refresh)
        self.assertIn('${RADAR_VERIFIED_TARGET:-10000}', refresh)
        self.assertIn('${RADAR_BROWSER_VERIFY_LIMIT:-$RANKED_POOL_LIMIT}', refresh)
        self.assertIn('${RADAR_BROWSER_SESSION_SIZE:-1000}', refresh)
        self.assertIn('--verified-target "$VERIFIED_TARGET"', refresh)
        self.assertIn('${RADAR_VALIDATION_CHECKPOINT_BATCH_SIZE:-1000}', refresh)
        self.assertIn('${RADAR_VALIDATION_CHECKPOINT_INTERVAL_SEC:-120}', refresh)
        self.assertIn('${RADAR_VALIDATION_CHECKPOINT_MAX_AGE_SEC:-21600}', refresh)
        self.assertIn('--prepare-only --top-n "$VERIFIED_TARGET"', refresh)
        self.assertIn('--output "$AUDIT" --top-n "$VERIFIED_TARGET"', refresh)
        self.assertIn("capture_alces_fx.py", refresh)
        self.assertIn("RADAR_FX_CAPTURE_SKIPPED", refresh)
        self.assertIn("sectigo-public-server-authentication-ca-dv-r36.pem", refresh)
        self.assertIn(
            'AUCTION_DATABASE="${RADAR_AUCTION_DATABASE:-$STATE/auction_offers.sqlite}"',
            refresh,
        )
        auction_phase = refresh[refresh.index('PHASE="auction_fetch"'):]
        self.assertNotIn('--db "$ROOT/universe_offers.sqlite"', auction_phase)
        self.assertIn('--database "$AUCTION_DATABASE"', auction_phase)

        hourly = (HERE / "auction_refresh.sh").read_text(encoding="utf-8")
        self.assertIn(
            'AUCTION_DATABASE="${RADAR_AUCTION_DATABASE:-$STATE/auction_offers.sqlite}"',
            hourly,
        )
        self.assertNotIn('--db "$ROOT/universe_offers.sqlite"', hourly)
        self.assertIn('--database "$AUCTION_DATABASE"', hourly)

        dashboard = (HERE.parent / "index.html").read_text(encoding="utf-8")
        freshness = re.search(
            r"LIVE_VERIFICATION_MAX_AGE_MS=(\d+)\*60\*60\*1000", dashboard
        )
        self.assertIsNotNone(freshness)
        self.assertEqual(
            int(freshness.group(1)),
            publisher.PUBLICATION_VALIDATION_MAX_AGE_HOURS,
        )
        self.assertIn(
            "validationAge<LIVE_VERIFICATION_MAX_AGE_MS", dashboard
        )

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
