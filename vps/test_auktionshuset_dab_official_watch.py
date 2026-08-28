#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import auktionshuset_dab_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
CATEGORY_ID = "JGrml207QN"


def category_markup() -> str:
    return f'''<form><label><input name="categories[]" value="{CATEGORY_ID}"> Køretøjer</label></form>'''


def card(lot_id: str, *, title: str = "Toyota Prius Hybrid 2021", bid: str = "15.600,00") -> str:
    return f'''<li class="lot-item item-bid has_counter" id="{lot_id}" data-ends="2026-09-03 14:00:00">
      <img src="https://images/{lot_id}.jpg" alt="{title}">
      <a class="flex flex-col" href="/auktioner/vehicle/lots/1/toyota-prius-{lot_id}"><h3>{title}</h3></a>
      <p class="bid-amount open-text">{bid}</p><p class="bid-amount-total">20.000,00</p>
    </li>'''


def lot_page(rows: list[str], total: int) -> str:
    return f'''{category_markup()}<div class="filter"><div class="result">{total} lots</div></div>
      <ul class="lot-list">{''.join(rows)}</ul>'''


class Response:
    def __init__(self, markup: str) -> None:
        self.text = markup

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


class Session:
    def __init__(self, snapshots: list[dict[int, str]]) -> None:
        self.snapshots = snapshots
        self.pass_index = -1

    def get(self, url: str, *, params: dict[str, object], **_: object) -> Response:
        if url != watch.LOT_URL:
            raise AssertionError(url)
        if params.get("categories[0]") is None:
            self.pass_index += 1
            return Response(category_markup())
        if params.get("categories[0]") != CATEGORY_ID:
            raise AssertionError(params)
        snapshot = self.snapshots[min(self.pass_index, len(self.snapshots) - 1)]
        return Response(snapshot[int(params["page"])])


class AuktionshusetDabWatchTest(unittest.TestCase):
    def test_vehicle_category_is_read_from_public_filter(self) -> None:
        self.assertEqual(watch.vehicle_category_id(category_markup()), CATEGORY_ID)

    def test_normalization_preserves_bid_and_vehicle_fields(self) -> None:
        from bs4 import BeautifulSoup
        row = watch.card_to_watch(
            BeautifulSoup(card("abc"), "html.parser").select_one("li"), observed_at=NOW.isoformat(), now=NOW
        )
        self.assertEqual(row["id"], "auktionshuset-dab:abc")
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["fuel"], "hybrid")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 15600)
        self.assertEqual(row["price_currency"], "DKK")
        self.assertEqual(row["canonical_end_utc"], "2026-09-03T12:00:00+00:00")

    def test_two_complete_passes_emit_every_declared_vehicle_lot(self) -> None:
        rows = [card(str(number)) for number in range(1, 50)]
        pages = {1: lot_page(rows[:48], 49), 2: lot_page(rows[48:], 49)}
        payload = watch.build_watch(session=Session([pages, pages]), now=NOW, timeout=5)
        self.assertEqual(payload["row_count"], 49)
        report = payload["source_reports"]["auktionshuset-dab"]
        self.assertEqual(report["declared"], 49)
        self.assertEqual(report["pages"], 2)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_counter_gap_fails_closed(self) -> None:
        rows = [card(str(number)) for number in range(1, 49)]
        pages = {1: lot_page(rows, 49), 2: lot_page([], 49)}
        with self.assertRaisesRegex(watch.AuktionshusetDabWatchError, "expected 1"):
            watch.build_watch(session=Session([pages]), now=NOW, timeout=5)

    def test_changed_second_pass_fails_closed(self) -> None:
        first = {1: lot_page([card("one"), card("two")], 2)}
        second = {1: lot_page([card("one"), card("three")], 2)}
        with self.assertRaisesRegex(watch.AuktionshusetDabWatchError, "final reconciliation"):
            watch.build_watch(session=Session([first, second]), now=NOW, timeout=5)


if __name__ == "__main__":
    unittest.main()
