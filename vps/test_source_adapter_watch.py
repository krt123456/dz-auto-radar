#!/usr/bin/env python3
"""Tests for the generated-connector bridge used by auction_refresh."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build_auction_board as board  # noqa: E402
import run_source_adapter_watch as bridge  # noqa: E402


class SourceAdapterWatchTests(unittest.TestCase):
    def test_configured_blocked_source_is_merged_only_with_adapter_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / "configs"
            feed_root = root / "feeds"
            work_dir = root / "work"
            config_dir.mkdir()
            feed_root.mkdir()
            feed_path = feed_root / "auto1.json"
            feed_path.write_text(json.dumps({"items": [{
                "id": "lot-1",
                "url": "https://www.auto1.com/offer/lot-1",
                "title": "Test petrol vehicle",
                "country": "DE",
                "category": "car",
                "year": 2025,
                "mileage": 12000,
                "fuel": "petrol",
                "price": 12345,
                "currency": "EUR",
                "price_kind": "current_bid",
                "end": "2099-01-01T12:00:00Z",
            }]}), encoding="utf-8")
            config = {
                "schema_version": 1,
                "canonical_identity": "auto1.com",
                "source_key": "auto1",
                "execution": {"mode": "file", "feed_file": str(feed_path)},
                "feed": {
                    "url": "https://www.auto1.com/authorized-feed.json",
                    "format": "json",
                    "pagination": {"type": "single", "items_path": "items"},
                },
                "mapping": {
                    "id": "id", "url": "url", "title": "title", "country": "country",
                    "category": "category", "year": "year", "mileage": "mileage",
                    "fuel": "fuel", "price_amount": "price", "price_currency": "currency",
                    "price_kind": "price_kind", "end_at_utc": "end",
                },
            }
            (config_dir / "auto1.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            profiles = bridge.source_profiles(HERE / "source_launchers_118")
            watch = bridge.build_watch(
                profiles=profiles,
                config_dir=config_dir,
                feed_root=feed_root,
                work_dir=work_dir,
                timeout_seconds=30,
                workers=2,
                execute=True,
            )
            self.assertEqual(watch["row_count"], 1)
            self.assertEqual(len(watch["source_reports"]), 118)
            self.assertEqual(watch["source_reports"]["auto1"]["status"], "ok")
            self.assertTrue(watch["rows"][0]["adapter_authorized"])

            input_path = root / "adapter-watch.json"
            bridge.atomic_write_json(input_path, watch)
            rows, rejected = board.monitored_rows(
                [], [input_path], generated_at=watch["generated_at_utc"]
            )
            self.assertEqual(rejected, {})
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["adapter_authorized"])

            unmarked = copy.deepcopy(watch)
            unmarked["rows"][0].pop("adapter_authorized")
            bridge.atomic_write_json(input_path, unmarked)
            rows, rejected = board.monitored_rows(
                [], [input_path], generated_at=watch["generated_at_utc"]
            )
            self.assertEqual(rows, [])
            self.assertEqual(rejected, {"source_not_publishable": 1})


if __name__ == "__main__":
    unittest.main()
