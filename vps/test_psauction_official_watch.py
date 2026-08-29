#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import unittest

import psauction_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def lot_payload(
    item_id: int = 1838555,
    number: str = "1405978",
    slug: str | None = None,
    title: str = "Citroën Berlingo Van 1.6 BlueHDi 75hk 2017",
    *,
    end: str = "2026-09-01 12:00",
    leading_bid: float | None = 15000.00,
    leading: bool = False,
    has_recent_bid: bool = False,
    active: bool = True,
    cancelled: bool = False,
    auctionended: bool = False,
    currency: str = "SEK",
) -> dict:
    return {
        "id": item_id,
        "slug": slug if slug is not None else f"{title.lower().replace(' ', '-')}-{number}",
        "thumbnail": "https://d2q01ftr6ua4w.cloudfront.net/assets/images/x.jpg",
        "altText": f"{number} - {title}",
        "number": number,
        "name": title,
        "endtime": end,
        "location": "70341 Örebro",
        "site": "se",
        "active": active,
        "cancelled": cancelled,
        "aicancelled": False,
        "auctionended": auctionended,
        "currency": currency,
        "leading": leading,
        "leadingbid": leading_bid,
        "hasRecentBid": has_recent_bid,
    }


def search_payload(items: list, total: int | None = None, *, page: int = 1, has_next: bool = False) -> str:
    return json.dumps({
        "total": f"{total if total is not None else len(items)} objekt",
        "pagination": [{"label": page, "active": True, "page": page}],
        "current": page,
        "prev": page - 1,
        "next": page + 1 if has_next else 1,
        "hasprev": page > 1,
        "hasnext": has_next,
        "items": items,
    })


class FakeFetcher:
    """Two-pass fetcher over scripted same-URL response sequences."""

    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, str]:
        self.calls.append(url)
        sequence = self.responses[url]
        if not sequence:
            raise AssertionError(f"no remaining response for {url}")
        body = sequence.pop(0)
        return 200, body


def catalog(items: list, *, has_next: bool = False) -> str:
    return search_payload(items, has_next=has_next)


CAR = lot_payload()


class PsauctionWatchTest(unittest.TestCase):
    def test_parses_lot_public_facts(self) -> None:
        lot = watch.parse_lot(CAR, context="t")
        self.assertEqual(lot.item_id, 1838555)
        self.assertEqual(lot.number, "1405978")
        self.assertEqual(lot.leading_bid, 15000.0)
        self.assertEqual(
            lot.end_utc,
            dt.datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        )

    def test_two_pass_reconciles_and_excludes_ended(self) -> None:
        car = CAR
        ended = lot_payload(
            item_id=2, number="1500002", slug="old-volvo-1500002",
            title="Volvo V70 2008", end="2026-08-01 12:00",
        )
        responses = {watch.SOURCE_ORIGIN + watch.SEARCH_PATH: [catalog([car, ended])] * 2}
        payload = watch.build_watch(fetch=FakeFetcher(responses), now=NOW, snapshot_attempts=1)
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["id"], "psauction-se:1405978")
        self.assertEqual(report["source_excluded"], {"ended_or_cancelled": 1})
        self.assertTrue(report["two_pass_verified"])

    def test_unbid_car_is_starting_bid_and_bid_car_is_current_bid(self) -> None:
        unbid = lot_payload(leading=False, has_recent_bid=False)
        bid = lot_payload(item_id=9, number="1500009", slug="bmw-x5-1500009", title="BMW X5 2019", leading=True, has_recent_bid=True)
        responses = {watch.SOURCE_ORIGIN + watch.SEARCH_PATH: [catalog([unbid, bid])] * 2}
        payload = watch.build_watch(fetch=FakeFetcher(responses), now=NOW, snapshot_attempts=1)
        kinds = {row["id"]: row["price_kind"] for row in payload["rows"]}
        self.assertEqual(kinds["psauction-se:1405978"], "starting_bid")
        self.assertEqual(kinds["psauction-se:1500009"], "current_bid")

    def test_zero_bid_is_unknown_price(self) -> None:
        zero = lot_payload(leading_bid=0)
        lot = watch.parse_lot(zero, context="t")
        row = watch.normalize_lot(lot, observed_at="2026-08-29T12:00:00+00:00")
        self.assertIsNone(row["price_amount"])
        self.assertEqual(row["price_kind"], "unknown")

    def test_non_sek_bid_fails_closed(self) -> None:
        payload = lot_payload(currency="EUR")
        with self.assertRaisesRegex(watch.PsauctionWatchError, "non-SEK"):
            watch.parse_lot(payload, context="t")

    def test_slug_without_number_fails_closed(self) -> None:
        with self.assertRaisesRegex(watch.PsauctionWatchError, "slug"):
            watch.parse_lot(lot_payload(slug="totally-different"), context="t")

    def test_bad_end_time_fails_closed(self) -> None:
        with self.assertRaisesRegex(watch.PsauctionWatchError, "end time"):
            watch.parse_lot(lot_payload(end="tomorrow noon"), context="t")

    def test_cancelled_and_inactive_excluded(self) -> None:
        cancelled = lot_payload(item_id=3, number="1500003", slug="audi-a4-1500003", title="Audi A4 2015", cancelled=True)
        inactive = lot_payload(item_id=4, number="1500004", slug="vw-golf-1500004", title="VW Golf 2014", active=False)
        responses = {watch.SOURCE_ORIGIN + watch.SEARCH_PATH: [catalog([CAR, cancelled, inactive])] * 2}
        payload = watch.build_watch(fetch=FakeFetcher(responses), now=NOW, snapshot_attempts=1)
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(
            report["source_excluded"],
            {"ended_or_cancelled": 1, "not_active": 1},
        )

    def test_non_passenger_titles_excluded(self) -> None:
        for title in (
            "Volvo FL6 Lastbil 2005",
            "Scania buss 40 sits 1998",
            "Husvagn Hobbi 2010",
            "Yamaha motorcykel 2012",
        ):
            lot = watch.parse_lot(lot_payload(title=title, slug=f"x-{CAR['number']}"), context="t")
            self.assertEqual(
                watch.passenger_exclusion_reason(lot),
                "commercial_or_non_passenger_title",
                msg=title,
            )

    def test_pagination_walks_next_pages(self) -> None:
        page_one = [lot_payload(item_id=i + 1, number=f"15010{i}", slug=f"car-{i}-15010{i}", title=f"Kia Ceed {2010 + i}") for i in range(5)]
        page_two = [lot_payload(item_id=99, number="1501099", slug="car-99-1501099", title="Kia Ceed 2020")]
        base = watch.SOURCE_ORIGIN + watch.SEARCH_PATH
        responses = {
            base: [search_payload(page_one, total=6, has_next=True)] * 2,
            base + "?page=2": [search_payload(page_two, total=6, page=2)] * 2,
        }
        payload = watch.build_watch(fetch=FakeFetcher(responses), now=NOW, snapshot_attempts=1)
        self.assertEqual(payload["row_count"], 6)
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(report["declared"], 6)
        self.assertEqual(report["visited"], 6)

    def test_total_changed_between_passes_fails_closed(self) -> None:
        base = watch.SOURCE_ORIGIN + watch.SEARCH_PATH
        responses = {
            base: [
                catalog([CAR]),
                json.dumps({"total": "2 objekt", "current": 1, "hasnext": False, "items": [CAR, dict(CAR, id=7, number="1500007", slug="x-1500007")]}),
            ],
        }
        with self.assertRaisesRegex(watch.PsauctionWatchError, "total changed"):
            watch.build_watch(fetch=FakeFetcher(responses), now=NOW, snapshot_attempts=1)

    def test_lot_facts_changed_between_passes_fails_closed(self) -> None:
        base = watch.SOURCE_ORIGIN + watch.SEARCH_PATH
        responses = {
            base: [
                catalog([CAR]),
                catalog([lot_payload(title="Citroën Berlingo Van 2018 RETITLED")]),
            ],
        }
        with self.assertRaisesRegex(watch.PsauctionWatchError, "facts changed"):
            watch.build_watch(fetch=FakeFetcher(responses), now=NOW, snapshot_attempts=1)

    def test_year_and_fuel_inference(self) -> None:
        lot = watch.parse_lot(lot_payload(title="Kia E-Soul Electric 64 kWh 2021", slug="kia-e-soul-1405978"), context="t")
        row = watch.normalize_lot(lot, observed_at="2026-08-29T12:00:00+00:00")
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["fuel"], "electric")

    def test_sek_currency_kept_with_eur_null(self) -> None:
        lot = watch.parse_lot(CAR, context="t")
        row = watch.normalize_lot(lot, observed_at="2026-08-29T12:00:00+00:00")
        self.assertEqual(row["price_currency"], "SEK")
        self.assertEqual(row["price_amount"], 15000.0)
        self.assertIsNone(row["price_eur"])


if __name__ == "__main__":
    unittest.main()
