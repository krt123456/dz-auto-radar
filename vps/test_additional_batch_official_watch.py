#!/usr/bin/env python3
"""Focused contract tests for the additional-batch auction row builder."""
from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("additional_batch_official_watch.py")
SPEC = importlib.util.spec_from_file_location("additional_batch_official_watch", SOURCE)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)


class AdditionalBatchRowContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 8, 28, 17, 30, tzinfo=dt.timezone.utc)

    def row(self, *, source: str, price_label: str | None = None) -> dict:
        kwargs = {
            "source": source,
            "listing_id": "fixture-1",
            "country": "hu" if source == "nav-hu" else "de",
            "url": "https://example.invalid/fixture-1",
            "title": "Audi A4 2024 passenger car",
            "now": self.now,
            "price_amount": 1200.0,
            "price_kind": "starting_bid",
        }
        if price_label is not None:
            kwargs["price_label"] = price_label
        return watch.make_row(**kwargs)

    def test_nav_hu_supplied_price_label_is_emitted(self) -> None:
        self.assertEqual(self.row(source="nav-hu", price_label="starting price")["price_label"], "starting price")

    def test_second_labeled_caller_uses_the_same_contract(self) -> None:
        self.assertEqual(self.row(source="vebeg", price_label="public opening amount")["price_label"], "public opening amount")

    def test_unlabeled_callers_retain_an_empty_default(self) -> None:
        self.assertEqual(self.row(source="nav-hu")["price_label"], "")


if __name__ == "__main__":
    unittest.main()
