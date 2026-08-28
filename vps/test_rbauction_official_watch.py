#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import unittest

import build_auction_board as board
import rbauction_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def epoch(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def record(
    item_number: int,
    *,
    country: str = "DEU",
    buying_format: str = "Live Auction",
    title: str = "2024 Toyota Prius Hybrid Automobile",
    start_price: int | None = 5000,
) -> dict[str, object]:
    end = dt.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    return {
        "itemNumber": str(item_number),
        "assetDescription": title,
        "modelLocalized": "Prius Hybrid",
        "rawModelName": "Prius",
        "locationCountry": country,
        "buyingFormat": buying_format,
        "listingStatus": "Open",
        "biddingEndTime": epoch(end),
        "eventEndDateTime": epoch(end + dt.timedelta(hours=2)),
        "priceCurrency": "EUR",
        "startPrice": start_price,
        "usageKilometers": 12345,
        "manufactureYear": 2024,
        "features": "Petrol Hybrid Engine",
        "eventAdvertisedName": "Germany Unreserved Auction",
        "locationCity": "Berlin",
        "saleEventID": "sale-1",
        "assetTypeLocalized": "Automobile",
    }


def page(total: int, records: list[dict[str, object]]) -> str:
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "results": {
                        "totalAmount": total,
                        "returnedAmount": len(records),
                        "records": records,
                    }
                }
            }
        }
    }
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


class Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


class Session:
    def __init__(self, snapshots: list[dict[int, str]]) -> None:
        self.snapshots = snapshots
        self.calls = 0
        self.pages_per_snapshot = len(snapshots[0])

    def get(self, url: str, *, params: dict[str, int], **_: object) -> Response:
        if url != watch.CATALOGUE_URL:
            raise AssertionError(url)
        offset = params.get("from")
        if not isinstance(offset, int):
            raise AssertionError(params)
        snapshot = min(
            self.calls // self.pages_per_snapshot,
            len(self.snapshots) - 1,
        )
        self.calls += 1
        return Response(self.snapshots[snapshot][offset])


class RitchieBrosWatchTest(unittest.TestCase):
    def test_two_passes_keep_only_schengen_explicit_auction_cards(self) -> None:
        records = [
            record(1),
            record(2, country="ESP", buying_format="Make Offer"),
            record(3, country="USA"),
            record(4, country="FRA", buying_format="Online Auction"),
        ]
        snapshot = {0: page(4, records)}
        payload = watch.build_watch(session=Session([snapshot, snapshot]), now=NOW, timeout=5)
        self.assertEqual([row["id"] for row in payload["rows"]], ["rbauction-eu:1", "rbauction-eu:4"])
        first = payload["rows"][0]
        self.assertEqual(first["country"], "DE")
        self.assertEqual(first["fuel"], "petrol/electric hybrid")
        self.assertEqual(first["price_kind"], "starting_bid")
        self.assertEqual(first["canonical_end_utc"], "2026-09-03T12:00:00+00:00")
        report = payload["source_reports"]["rbauction-eu"]
        self.assertEqual(report["catalogue_total"], 4)
        self.assertEqual(report["schengen_auction_rows"], 2)
        self.assertEqual(report["rejected_counts"], {
            "non_auction_format": 1,
            "non_schengen_asset": 1,
        })

    def test_cross_border_rbauction_card_reaches_the_broad_watch(self) -> None:
        snapshot = {0: page(1, [record(1, country="DEU")])}
        payload = watch.build_watch(session=Session([snapshot, snapshot]), now=NOW, timeout=5)
        normalized, reason = board._normalize_monitored_row(
            payload["rows"][0], generated_at=NOW
        )
        self.assertEqual(reason, "")
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["country"], "DE")
        self.assertEqual(normalized["source"], "rbauction-eu")

    def test_pagination_enumerates_every_card_and_rechecks_the_full_set(self) -> None:
        records = [record(number) for number in range(1, 62)]
        snapshot = {
            0: page(61, records[:60]),
            60: page(61, records[60:]),
        }
        payload = watch.build_watch(session=Session([snapshot, snapshot]), now=NOW, timeout=5)
        self.assertEqual(payload["row_count"], 61)
        report = payload["source_reports"]["rbauction-eu"]
        self.assertEqual(report["listing_pages"], 2)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_counter_gap_fails_closed(self) -> None:
        snapshot = {0: page(2, [record(1)])}
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "expected 2"):
            watch.build_watch(session=Session([snapshot]), now=NOW, timeout=5)

    def test_changed_second_pass_fails_closed(self) -> None:
        first = {0: page(1, [record(1)])}
        second = {0: page(1, [record(2)])}
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "final reconciliation"):
            watch.build_watch(session=Session([first, second]), now=NOW, timeout=5)


if __name__ == "__main__":
    unittest.main()
