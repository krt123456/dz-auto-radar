#!/usr/bin/env python3
from __future__ import annotations

import json
import csv
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent


class PipelineTest(unittest.TestCase):
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
            self.assertEqual([row["listing_id"] for row in rows], ["de-1"])
            details = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(details["world_rows"], 3)
            self.assertEqual(details["accepted_rows"], 1)

    def test_full_universe_selection_and_encrypted_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            root = temp / "car_deal_finder"
            board_dir = root / "mobile_site_local"
            site = temp / "site"
            board_dir.mkdir(parents=True)
            site.mkdir()
            pin = temp / "pin"
            pin.write_text("correct-horse-radar-secret\n", encoding="utf-8")
            index = temp / "index.html"
            index.write_text("<!doctype html><title>radar</title>", encoding="utf-8")

            offers = []
            for number in range(30):
                offers.append(
                    {
                        "id": f"offer-{number}", "m": "clio", "t": f"Car {number}",
                        "p": 10_000 + number, "pr": 8_000 - number,
                        "ep": 7_000 - number, "roi": 50, "er": 45,
                        "y": 2025, "km": 10_000 + number, "f": "petrol",
                        "c": "DE" if number % 2 == 0 else "FR",
                        "s": "Source A" if number % 3 else "Source B",
                        "u": f"https://example.test/listing/{number}",
                        "cr": 90, "v": 1, "a": 0,
                    }
                )
            offers.extend(
                [
                    {**offers[0], "id": "lease", "u": "https://example.test/lease", "t": "cesja najmu"},
                    {**offers[1], "id": "dead", "u": "https://example.test/dead", "v": -1},
                    {**offers[2], "id": "duplicate-url"},
                ]
            )
            (board_dir / "board.json").write_text(
                json.dumps(
                    {
                        "updated_utc": "2026-07-18T00:00:00Z",
                        "connected_country_count": 29,
                        "connected_source_count": 90,
                        "offers": offers,
                    }
                ),
                encoding="utf-8",
            )
            (root / "top_offers.json").write_text(
                json.dumps({"total_all": 1_500_000}), encoding="utf-8"
            )
            database = sqlite3.connect(root / "universe_offers.sqlite")
            database.execute("CREATE TABLE offers (id INTEGER PRIMARY KEY, last_seen_at TEXT)")
            database.executemany(
                "INSERT INTO offers(id, last_seen_at) VALUES (?, ?)",
                [(number, "2026-07-18T00:00:00Z") for number in range(1, 101)],
            )
            database.commit()
            database.close()
            manifest = temp / "manifest.json"
            audit = temp / "audit.json"

            prepare = subprocess.run(
                [
                    sys.executable, str(HERE / "publish_radar_dashboard.py"),
                    "--root", str(root), "--site", str(site), "--pin", str(pin),
                    "--index", str(index), "--audit-manifest", str(manifest),
                    "--top-n", "10", "--per-country-min", "1", "--per-source-min", "1",
                    "--prepare-only",
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertIn('"universe_unique_offers": 100', prepare.stdout)
            result = subprocess.run(
                [
                    sys.executable, str(HERE / "audit_best_selection.py"),
                    "--root", str(root), "--site", str(site), "--pin", str(pin),
                    "--output", str(audit), "--top-n", "10",
                    "--per-country-min", "1", "--per-source-min", "1",
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("BEST_SELECTION_AUDIT_PASS", result.stdout)
            report = json.loads(audit.read_text(encoding="utf-8"))
            self.assertEqual(report["universe_unique_offers"], 100)
            self.assertEqual(report["qualified_universe_offers"], 30)
            self.assertEqual(report["published_offer_count"], 10)
            self.assertEqual(report["confirmed_dead_or_lease_like_published"], 0)


if __name__ == "__main__":
    unittest.main()
