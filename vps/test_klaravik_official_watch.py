#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

import klaravik_official_watch as watch


UTC = dt.timezone.utc
SOURCE = watch.SourceSpec("klaravik-se", "Klaravik Sweden", "SE", "www.klaravik.se", "SEK")
NOW = dt.datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def item(
    item_id: int,
    name: str,
    category1: str,
    category2: str,
    *,
    bid: int = 1000,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "make": "Sea-Doo" if "Sea-Doo" in name else "",
        "model": "RXT260" if "Sea-Doo" in name else "",
        "url": f"https://www.klaravik.se/auktion/produkt/{item_id}-lot/",
        "endDate": "2026-08-29T12:00:00+02:00",
        "ended": False,
        "currentBid": bid,
        "startingPrice": 500,
        "amountOfBids": 2,
        "nextBidStep": 100,
        "reservationPriceReached": True,
        "categoryNameLevel1": category1,
        "categoryNameLevel2": category2,
        "categoryNameLevel3": None,
        "municipalityName": "Aarhus",
        "countyName": "Region Midtjylland",
    }


class Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class SnapshotSession:
    """Serves one complete page set for each pass beginning at page one."""

    def __init__(self, snapshots: list[dict[int, list[dict[str, Any]]]]) -> None:
        self.snapshots = snapshots
        self.pass_index = -1

    def get(self, _: str, *, params: dict[str, Any], **__: Any) -> Response:
        page = int(params["page"])
        if page == 1:
            self.pass_index += 1
        snapshot = self.snapshots[min(self.pass_index, len(self.snapshots) - 1)]
        total = sum(len(entries) for entries in snapshot.values())
        pages = len(snapshot)
        return Response({
            "data": {
                "pagination": {
                    "totalPages": pages,
                    "totalCount": total,
                    "pageSize": watch.PAGE_SIZE,
                    "pageItemCount": len(snapshot[page]),
                },
                "items": snapshot[page],
            }
        })


class KlaravikWatchTest(unittest.TestCase):
    def test_two_complete_passes_preserve_vehicle_and_general_lot_categories(self) -> None:
        snapshot = {
            1: [
                item(1, "Personbil Volvo V60 Bensin, 2022", "Fordon", "Personbilar", bid=150000),
                item(2, "Sea-Doo RXT260 & GTX260 Vandscootere", "Køretøjer", "Vandscootere", bid=5300),
            ],
            2: [
                item(3, "Gaming PC with console bundle", "Elektronik", "Gaming", bid=4200),
            ],
        }
        original_page_size = watch.PAGE_SIZE
        try:
            watch.PAGE_SIZE = 2
            payload = watch.build_watch(
                session=SnapshotSession([snapshot, snapshot]),
                source_specs=(SOURCE,),
                now=NOW,
                timeout=10,
            )
        finally:
            watch.PAGE_SIZE = original_page_size
        rows = {row["id"]: row for row in payload["rows"]}
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(rows["klaravik:se:1"]["category"], "car")
        self.assertEqual(rows["klaravik:se:1"]["fuel"], "petrol")
        self.assertEqual(rows["klaravik:se:2"]["category"], "jetski")
        self.assertEqual(rows["klaravik:se:3"]["category"], "gaming")
        self.assertEqual(rows["klaravik:se:2"]["country"], "SE")
        self.assertEqual(payload["source_reports"]["klaravik-se"]["declared"], 3)
        self.assertTrue(payload["source_reports"]["klaravik-se"]["full_catalogue_rechecked"])

    def test_changed_second_pass_fails_closed(self) -> None:
        first = {
            1: [item(1, "Personbil Volvo Bensin, 2022", "Fordon", "Personbilar"), item(2, "Laptop Dell", "Elektronik", "Datorer")],
            2: [item(3, "Sea-Doo Jetski", "Køretøjer", "Vandscootere")],
        }
        changed = {
            1: [item(1, "Personbil Volvo Bensin, 2022", "Fordon", "Personbilar"), item(2, "Laptop Dell", "Elektronik", "Datorer")],
            2: [item(4, "Gaming console", "Elektronik", "Gaming")],
        }
        original_page_size = watch.PAGE_SIZE
        try:
            watch.PAGE_SIZE = 2
            with self.assertRaises(watch.KlaravikWatchError):
                watch.build_watch(
                    session=SnapshotSession([first, changed]),
                    source_specs=(SOURCE,),
                    now=NOW,
                    timeout=10,
                )
        finally:
            watch.PAGE_SIZE = original_page_size

    def test_cross_domain_lot_url_is_rejected(self) -> None:
        with self.assertRaises(watch.KlaravikWatchError):
            watch.source_url(SOURCE, "https://example.test/auction/1")


if __name__ == "__main__":
    unittest.main()
