#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import bilweb_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def index(slug: str = "septemberauktion-1-2026") -> str:
    return f'''<div class="EventPage-ongoingCard"><a href="/en/{slug}">Current auction</a></div>'''


def object_row(object_id: int, *, title: str = "Toyota Prius Hybrid — 2021", bid: str = "15 000 SEK") -> str:
    return f'''<div class="RowObject row-object" id="{object_id}">
      <a class="RowObject-image" href="/en/septemberauktion-1-2026/toyota-prius-{object_id}"><img src="https://img/{object_id}.jpg"></a>
      <div class="RowObject-content">
        <h4 class="RowObject-title"><a href="/en/septemberauktion-1-2026/toyota-prius-{object_id}">{title}</a></h4>
        <p class="RowObject-desc">12 345 km, petrol hybrid</p>
        <div class="Badge Badge--reserve">No reserve</div>
      </div>
      <div class="RowObject-info"><div class="RowObject-infoGrid">
        <div><div class="RowObject-infoLabel">Countdown</div><div class="RowObject-infoValue">02 SEP 10:00</div></div>
        <div><div class="RowObject-infoLabel">Current Bid</div><div class="RowObject-infoValue">{bid}</div></div>
      </div></div>
    </div>'''


def auction_page(total: int, rows: list[str]) -> str:
    return f'''<h1 class="ObjectListPage-title">September Auction 1 2026</h1>
    <input type="hidden" name="total_objects" id="total_objects" value="{total}">
    <div id="active_objects" data-totalobjects="{total}">{''.join(rows)}</div>'''


class Response:
    def __init__(self, markup: str) -> None:
        self.text = markup

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


class Session:
    def __init__(self, snapshots: list[tuple[str, str]]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def get(self, url: str, **_: object) -> Response:
        snapshot_index = min(self.calls // 2, len(self.snapshots) - 1)
        self.calls += 1
        root, page = self.snapshots[snapshot_index]
        if url == watch.AUCTIONS_URL:
            return Response(root)
        if url == f"{watch.ROOT_URL}/en/septemberauktion-1-2026":
            return Response(page)
        raise AssertionError(url)


class BilwebWatchTest(unittest.TestCase):
    def test_object_normalization_preserves_current_bid_and_vehicle_fields(self) -> None:
        payload = auction_page(1, [object_row(10)])
        parsed = watch.parse_auction_page(
            payload, slug="septemberauktion-1-2026", observed_at=NOW.isoformat(), now=NOW
        )
        row = parsed.rows[0]
        self.assertEqual(row["id"], "bilweb:septemberauktion-1-2026:10")
        self.assertEqual(row["url"], "https://bilwebauctions.se/en/septemberauktion-1-2026/toyota-prius-10")
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["mileage"], 12345)
        self.assertEqual(row["fuel"], "hybrid")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 15000)
        self.assertEqual(row["price_currency"], "SEK")
        self.assertTrue(row["no_reserve"])
        self.assertEqual(row["canonical_end_utc"], "2026-09-02T08:00:00+00:00")

    def test_two_complete_passes_emit_every_declared_object(self) -> None:
        page = auction_page(2, [object_row(10), object_row(11)])
        payload = watch.build_watch(session=Session([(index(), page)]), now=NOW, timeout=5)
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual([row["id"] for row in payload["rows"]], [
            "bilweb:septemberauktion-1-2026:10",
            "bilweb:septemberauktion-1-2026:11",
        ])
        report = payload["source_reports"]["bilweb"]
        self.assertEqual(report["declared"], 2)
        self.assertEqual(report["current_auctions"], 1)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_counter_gap_fails_closed(self) -> None:
        incomplete = auction_page(2, [object_row(10)])
        with self.assertRaisesRegex(watch.BilwebWatchError, "declared 2 active objects"):
            watch.build_watch(session=Session([(index(), incomplete)]), now=NOW, timeout=5)

    def test_changed_second_pass_fails_closed(self) -> None:
        first = auction_page(2, [object_row(10), object_row(11)])
        changed = auction_page(2, [object_row(10), object_row(12)])
        with self.assertRaisesRegex(watch.BilwebWatchError, "final reconciliation"):
            watch.build_watch(session=Session([(index(), first), (index(), changed)]), now=NOW, timeout=5)


if __name__ == "__main__":
    unittest.main()
