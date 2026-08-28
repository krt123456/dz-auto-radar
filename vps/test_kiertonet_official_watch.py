#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import kiertonet_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
CATEGORY_MARKUP = '''<auctions-list :fixed_params="{&quot;kategoria&quot;:&quot;8,12,31&quot;}"></auctions-list>'''


def auction(
    auction_id: int,
    *,
    category: int = 8,
    is_buy_now: int = 0,
    bid: int | None = 900,
) -> dict[str, object]:
    return {
        "id": auction_id,
        "url": f"toyota-prius-{auction_id}",
        "title": "Toyota Prius Hybrid 2021",
        "full_title": "Toyota Prius Hybrid 2021",
        "ends_at": "2026-09-03T12:00:00.000000Z",
        "hasEnded": False,
        "fullUrl": f"https://kiertonet.fi/huutokaupat/toyota-prius-{auction_id}",
        "starting_price": 500,
        "is_buy_now": is_buy_now,
        "category_id": category,
        "city": "Helsinki",
        "seller_name": "Helsinki City",
        "highest_bid": bid,
        "medium_image_url": f"https://images/{auction_id}.jpg",
        "is_sold_to_highest_bidder": 1,
        "is_bankruptcy_estate_auction": 0,
    }


def page(rows: list[dict[str, object]], total: int, current_page: int) -> dict[str, object]:
    return {
        "data": rows,
        "meta": {
            "total": total,
            "per_page": 30,
            "current_page": current_page,
            "last_page": max(1, (total + 29) // 30),
        },
    }


class TextResponse:
    def __init__(self, value: str) -> None:
        self.text = value

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


class JsonResponse:
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
        self.pass_index = -1

    def get(self, url: str, *, params: dict[str, object] | None = None, **_: object) -> object:
        if url == watch.VEHICLES_URL:
            self.pass_index += 1
            return TextResponse(CATEGORY_MARKUP)
        if url != watch.FILTER_URL or params is None:
            raise AssertionError(url)
        return JsonResponse(self.snapshots[min(self.pass_index, len(self.snapshots) - 1)][int(params["page"])])


class KiertonetWatchTest(unittest.TestCase):
    def test_vehicle_category_scope_is_read_from_the_public_page(self) -> None:
        self.assertEqual(watch.vehicle_category_csv(CATEGORY_MARKUP), "8,12,31")

    def test_normalization_preserves_public_bid_and_vehicle_fields(self) -> None:
        row = watch.row_to_watch(
            auction(10), category_ids=frozenset({"8", "12", "31"}), observed_at=NOW.isoformat(), now=NOW
        )
        self.assertEqual(row["id"], "kiertonet:10")
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["fuel"], "hybrid")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 900)
        self.assertEqual(row["price_currency"], "EUR")
        self.assertEqual(row["canonical_end_utc"], "2026-09-03T12:00:00+00:00")

    def test_two_complete_passes_emit_every_declared_vehicle_auction(self) -> None:
        rows = [auction(number) for number in range(1, 32)]
        pages = {1: page(rows[:30], 31, 1), 2: page(rows[30:], 31, 2)}
        payload = watch.build_watch(session=Session([pages, pages]), now=NOW, timeout=5)
        self.assertEqual(payload["row_count"], 31)
        report = payload["source_reports"]["kiertonet"]
        self.assertEqual(report["declared"], 31)
        self.assertEqual(report["pages"], 2)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_buy_now_rows_are_not_emitted_as_auctions(self) -> None:
        rows = [auction(1), auction(2, is_buy_now=1)]
        pages = {1: page(rows, 2, 1)}
        payload = watch.build_watch(session=Session([pages, pages]), now=NOW, timeout=5)
        self.assertEqual([row["id"] for row in payload["rows"]], ["kiertonet:1"])
        self.assertEqual(payload["source_reports"]["kiertonet"]["fixed_price_rows_excluded"], 1)

    def test_counter_gap_fails_closed(self) -> None:
        pages = {1: page([auction(number) for number in range(1, 31)], 31, 1), 2: page([], 31, 2)}
        with self.assertRaisesRegex(watch.KiertonetWatchError, "expected 1"):
            watch.build_watch(session=Session([pages]), now=NOW, timeout=5)

    def test_changed_second_pass_fails_closed(self) -> None:
        first = {1: page([auction(1), auction(2)], 2, 1)}
        second = {1: page([auction(1), auction(3)], 2, 1)}
        with self.assertRaisesRegex(watch.KiertonetWatchError, "final reconciliation"):
            watch.build_watch(session=Session([first, second]), now=NOW, timeout=5)


if __name__ == "__main__":
    unittest.main()
