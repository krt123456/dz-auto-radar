#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import aurena_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
END_MS = int(dt.datetime(2026, 8, 30, 12, 30, tzinfo=UTC).timestamp() * 1000)


def item(lot_id: int, *, auction_id: int = 400, bid: int = 5300) -> dict[str, object]:
    return {
        "lid": lot_id,
        "aid": auction_id,
        "cat": 5,
        "et": END_MS,
        "hib": {"id": lot_id + 900, "val": bid},
        "sp": 2000,
        "bc": 4,
        "im": [f"https://images.example/{lot_id}.jpg"],
        "ld": {
            "ti": {"de_DE": f"Toyota Prius Hybrid {lot_id}"},
            "de": {"de_DE": "Baujahr 2021, 12 345 km, Benzin-Hybrid"},
        },
    }


def response(offset: int, element_count: int, items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "filter": {
            "auctions": [],
            "provinces": [],
            "brands": [],
            "categories": [{"path": [{"id": 5, "lots": element_count}], "subcategories": []}],
        },
        "limit": 96,
        "offset": offset,
        "elementCount": element_count,
        "items": items,
    }


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload

    def close(self) -> None:
        return None


class Session:
    def __init__(self, snapshots: list[dict[int, dict[str, object]]]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def post(self, url: str, *, json: dict[str, object], **_: object) -> Response:
        if url != watch.PACKAGE_URL:
            raise AssertionError(url)
        self.assert_request(json)
        offset = int(json["offset"])
        snapshot_index = min(self.calls // 2, len(self.snapshots) - 1)
        self.calls += 1
        return Response(self.snapshots[snapshot_index][offset])

    @staticmethod
    def assert_request(value: dict[str, object]) -> None:
        assert value["limit"] == 96
        assert value["filter"] == {
            "auctions": [], "provinces": [], "brands": [], "categories": [[5]], "bidCount": None,
        }


class AurenaWatchTest(unittest.TestCase):
    def test_lot_normalization_preserves_bid_and_vehicle_fields(self) -> None:
        row = watch.item_to_row(item(10), observed_at=NOW.isoformat(), now=NOW)
        self.assertEqual(row["id"], "aurena:400:10")
        self.assertEqual(row["url"], "https://www.aurena.at/auktion/400/lot/10")
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["mileage"], 12345)
        self.assertEqual(row["fuel"], "hybrid")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 5300)
        self.assertEqual(row["price_eur"], 5300)
        self.assertEqual(row["canonical_end_utc"], "2026-08-30T12:30:00+00:00")

    def test_vehicle_category_descendants_are_preserved(self) -> None:
        category_ids = watch.vehicle_category_ids({
            "categories": [{
                "path": [{"id": 5, "lots": 2}],
                "subcategories": [{"id": 36, "lots": 2}],
            }],
        })
        self.assertEqual(category_ids, frozenset({5, 36}))
        vehicle = item(10)
        vehicle["cat"] = 36
        row = watch.item_to_row(
            vehicle, observed_at=NOW.isoformat(), now=NOW, category_ids=category_ids
        )
        self.assertEqual(row["category_raw"], "Fahrzeuge")

    def test_two_complete_passes_emit_every_declared_vehicle_lot(self) -> None:
        lots = [item(index) for index in range(1, 98)]
        snapshot = {
            0: response(0, 97, lots[:96]),
            96: response(96, 97, lots[96:]),
        }
        payload = watch.build_watch(session=Session([snapshot]), now=NOW, timeout=5)
        self.assertEqual(payload["row_count"], 97)
        self.assertEqual(payload["rows"][0]["id"], "aurena:400:1")
        self.assertEqual(payload["rows"][-1]["id"], "aurena:400:97")
        report = payload["source_reports"]["aurena"]
        self.assertEqual(report["declared"], 97)
        self.assertEqual(report["pages"], 2)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_incomplete_page_fails_closed(self) -> None:
        broken = {0: response(0, 2, [item(1)])}
        with self.assertRaisesRegex(watch.AurenaWatchError, "page is incomplete"):
            watch.build_watch(session=Session([broken]), now=NOW, timeout=5)

    def test_changed_second_pass_fails_closed(self) -> None:
        lots = [item(index) for index in range(1, 98)]
        first = {0: response(0, 97, lots[:96]), 96: response(96, 97, lots[96:])}
        changed_lots = [item(index) for index in range(1, 97)] + [item(200)]
        changed = {0: response(0, 97, changed_lots[:96]), 96: response(96, 97, changed_lots[96:])}
        with self.assertRaisesRegex(watch.AurenaWatchError, "final reconciliation"):
            watch.build_watch(session=Session([first, changed]), now=NOW, timeout=5)


if __name__ == "__main__":
    unittest.main()
