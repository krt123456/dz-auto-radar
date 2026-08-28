#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import kvdcars_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def auction(
    auction_id: int,
    *,
    state: str = "OPEN",
    auction_type: str = "BIDDING",
    closed_at: str | None = None,
    bid: int | None = 15000,
    start_bid: int | None = 5000,
) -> dict[str, object]:
    active = {
        "highestBid": {"amount": bid} if bid is not None else None,
        "bids": [{"amount": bid}] if bid is not None else [],
        "reservationPriceReached": False,
        "preliminaryCloseAt": "2026-09-03T12:00:00+00:00",
    }
    return {
        "id": str(auction_id),
        "auctionType": auction_type,
        "auctionUrl": f"https://www.kvd.se/auktioner/toyota-prius-{auction_id}",
        "state": state,
        "closedAt": closed_at,
        "currency": "SEK",
        "startBid": start_bid,
        "preliminaryPrice": 22000,
        "countdownStartAt": "2026-09-03T11:56:30+00:00",
        "isReserved": False,
        "activeAuction": active,
        "previewImage": f"https://images/{auction_id}.jpg",
        "processObject": {
            "title": "Toyota Prius Plug-in Hybrid",
            "vehicleType": "CAR",
            "objectType": "CAR",
            "properties": {
                "title": "Toyota Prius Plug-in Hybrid",
                "modelName": "Prius Plug-in",
                "modelYear": 2021,
                "odometerReading": 12345,
                "odometerUnit": "Mil",
                "fuels": [{"fuelCode": "Petrol Hybrid"}],
                "exportable": True,
            },
            "locationInfo": {"facility": {"city": "Stockholm"}},
        },
    }


def page(rows: list[dict[str, object]], total: int) -> dict[str, object]:
    return {"auctions": rows, "total": total, "hits": total}


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
        self.pages_per_snapshot = len(snapshots[0])

    def get(self, url: str, *, params: dict[str, int], **_: object) -> Response:
        self.assert_url(url)
        snapshot = min(self.calls // self.pages_per_snapshot, len(self.snapshots) - 1)
        self.calls += 1
        return Response(self.snapshots[snapshot][params["offset"]])

    @staticmethod
    def assert_url(url: str) -> None:
        if url != watch.API_URL:
            raise AssertionError(url)


class KvdCarsWatchTest(unittest.TestCase):
    def test_normalization_preserves_public_bid_and_vehicle_fields(self) -> None:
        row = watch.row_to_watch(auction(10), observed_at=NOW.isoformat(), now=NOW)
        self.assertEqual(row["id"], "kvdcars:10")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 15000)
        self.assertEqual(row["price_currency"], "SEK")
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["mileage_km"], 123450)
        self.assertEqual(row["fuel"], "hybrid")
        self.assertEqual(row["canonical_end_utc"], "2026-09-03T12:00:00+00:00")

    def test_two_complete_passes_emit_every_current_bidding_auction(self) -> None:
        rows = [auction(number) for number in range(1, 52)]
        pages = {0: page(rows[:50], 51), 50: page(rows[50:], 51)}
        payload = watch.build_watch(session=Session([pages, pages]), now=NOW, timeout=5)
        self.assertEqual(payload["row_count"], 51)
        report = payload["source_reports"]["kvdcars"]
        self.assertEqual(report["api_catalogue_total"], 51)
        self.assertEqual(report["api_bidding_total"], 51)
        self.assertEqual(report["pages"], 2)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_fixed_price_and_closed_rows_are_not_emitted_as_bidding_auctions(self) -> None:
        rows = [
            auction(1),
            auction(2, state="WINNER_CHECKOUT_PENDING", closed_at="2026-08-28T06:00:00+00:00"),
            auction(3, auction_type="BUY_NOW"),
        ]
        pages = {0: page(rows, 3)}
        payload = watch.build_watch(session=Session([pages, pages]), now=NOW, timeout=5)
        self.assertEqual([row["id"] for row in payload["rows"]], ["kvdcars:1"])
        report = payload["source_reports"]["kvdcars"]
        self.assertEqual(report["closed_or_inactive_bidding_rows"], 1)
        self.assertEqual(report["fixed_price_rows_excluded"], 1)

    def test_counter_gap_fails_closed(self) -> None:
        pages = {0: page([auction(number) for number in range(1, 51)], 51), 50: page([], 51)}
        with self.assertRaisesRegex(watch.KvdCarsWatchError, "expected 1"):
            watch.build_watch(session=Session([pages]), now=NOW, timeout=5)

    def test_changed_second_pass_fails_closed(self) -> None:
        first = {0: page([auction(1), auction(2)], 2)}
        second = {0: page([auction(1), auction(3)], 2)}
        with self.assertRaisesRegex(watch.KvdCarsWatchError, "final reconciliation"):
            watch.build_watch(session=Session([first, second]), now=NOW, timeout=5)


if __name__ == "__main__":
    unittest.main()
