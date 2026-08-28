#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import auctionmaster_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def lot(listing_id: int, *, auction_id: int = 900, bid: int = 5300) -> dict[str, object]:
    return {
        "id": listing_id,
        "volgNummer": str(listing_id),
        "naam": f"Passenger car, Toyota Prius Hybrid, 2021, 12 345 km ({listing_id})",
        "sluitingsDatumISO": "2026-08-30T12:30:00Z",
        "hoogsteBod": bid,
        "openingsBod": 1000,
        "aantalBiedingen": 5,
        "categorie": {
            "id": 10,
            "naam": "Passenger cars",
            "parentTrackingKey": "Cars and other transport",
        },
        "veiling": {
            "id": auction_id,
            "land": "NL",
            "naam": "Car Auction",
            "openingsDatumISO": "2026-08-27T12:00:00Z",
        },
    }


def response(
    page: int,
    total: int,
    items: list[dict[str, object]],
    *,
    reported_total: int | None = None,
    reported_pages: int | None = None,
) -> dict[str, object]:
    return {
        "_declared_total": total,
        "totalElements": total if reported_total is None else reported_total,
        "totalPages": max(1, (total + 99) // 100) if reported_pages is None else reported_pages,
        "number": page,
        "content": items,
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
        self.page_calls = 0

    def get(self, url: str, *, params: dict[str, object] | None = None, **_: object) -> Response:
        snapshot_index = min(self.page_calls // 2, len(self.snapshots) - 1)
        if url == watch.CATEGORY_URL:
            total = int(self.snapshots[snapshot_index][1]["_declared_total"])
            return Response({
                "categorieen": [{
                    "categorieId": 10,
                    "open": total,
                    "categorieDetails": {
                        "id": 10,
                        "naam": "Passenger cars",
                        "parent_id": "1",
                    },
                }],
            })
        if url != watch.LIST_URL:
            raise AssertionError(url)
        if params is None:
            raise AssertionError("listing request has no parameters")
        if params != {"page": params["page"], "size": 100, "status": "open", "categorieIds": "10"}:
            raise AssertionError(params)
        page_number = int(params["page"])
        self.page_calls += 1
        return Response(self.snapshots[snapshot_index][page_number])


class AuctionmasterWatchTest(unittest.TestCase):
    def test_lot_normalization_preserves_current_bid_and_vehicle_fields(self) -> None:
        row = watch.row_from_lot(lot(10), observed_at=NOW.isoformat(), now=NOW)
        self.assertEqual(row["id"], "auctionmaster:900:10")
        self.assertEqual(row["url"], "https://auctionmaster.com/en/veilingen/900/kavels/10")
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["mileage"], 12345)
        self.assertEqual(row["fuel"], "hybrid")
        self.assertEqual(row["category"], "car")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 5300)
        self.assertEqual(row["price_eur"], 5300)
        self.assertEqual(row["canonical_end_utc"], "2026-08-30T12:30:00+00:00")

    def test_two_complete_passes_emit_every_declared_lot(self) -> None:
        lots = [lot(index) for index in range(1, 102)]
        snapshot = {
            1: response(1, 101, lots[:100]),
            2: response(2, 101, lots[100:]),
        }
        payload = watch.build_watch(session=Session([snapshot]), now=NOW, timeout=5)
        self.assertEqual(payload["row_count"], 101)
        self.assertEqual(payload["rows"][0]["id"], "auctionmaster:900:1")
        self.assertEqual(payload["rows"][-1]["id"], "auctionmaster:900:101")
        report = payload["source_reports"]["auctionmaster"]
        self.assertEqual(report["declared"], 101)
        self.assertEqual(report["pages"], 2)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_incomplete_page_fails_closed(self) -> None:
        broken = {1: response(1, 2, [lot(1)])}
        with self.assertRaisesRegex(watch.AuctionmasterWatchError, "incomplete"):
            watch.build_watch(session=Session([broken]), now=NOW, timeout=5)

    def test_changed_second_pass_fails_closed(self) -> None:
        lots = [lot(index) for index in range(1, 102)]
        first = {1: response(1, 101, lots[:100]), 2: response(2, 101, lots[100:])}
        changed_lots = [lot(index) for index in range(1, 101)] + [lot(202)]
        changed = {
            1: response(1, 101, changed_lots[:100]),
            2: response(2, 101, changed_lots[100:]),
        }
        with self.assertRaisesRegex(watch.AuctionmasterWatchError, "final reconciliation"):
            watch.build_watch(session=Session([first, changed]), now=NOW, timeout=5)

    def test_inconsistent_listing_metadata_does_not_override_category_total(self) -> None:
        lots = [lot(index) for index in range(1, 102)]
        snapshot = {
            1: response(1, 101, lots[:100], reported_total=200, reported_pages=2),
            2: response(2, 101, lots[100:], reported_total=201, reported_pages=3),
        }
        payload = watch.build_watch(session=Session([snapshot]), now=NOW, timeout=5)
        report = payload["source_reports"]["auctionmaster"]
        self.assertEqual(payload["row_count"], 101)
        self.assertEqual(report["declared"], 101)
        self.assertEqual(report["page_metadata_mismatch_pages"], [1, 2])
        self.assertTrue(report["page_metadata_is_not_authoritative"])

    def test_non_passenger_category_is_rejected_at_source(self) -> None:
        commercial = lot(10)
        commercial["categorie"] = {
            "id": 11,
            "naam": "Company cars",
            "parentTrackingKey": "Cars and other transport",
        }
        with self.assertRaisesRegex(watch.AuctionmasterWatchError, "passenger-car"):
            watch.row_from_lot(commercial, observed_at=NOW.isoformat(), now=NOW)


if __name__ == "__main__":
    unittest.main()
