#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlparse
import unittest

import caraukce_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def card(
    listing_id: int,
    *,
    title: str = "Toyota Prius",
    year: int = 2021,
    mileage: str = "12 345",
    fuel: str = "Benzín",
    price_title: str = "Vyvolávací cena",
    price: str = "250 000 Kč",
    end: str = "30.08.2026 12:30",
) -> str:
    return f'''<div class="vehicle-card h-100">
      <a href="/item/{listing_id}" class="vehicle-card__image"><img src="/image/{listing_id}.jpg"></a>
      <div class="vehicle-card__body">
        <h3 class="vehicle-card__title"><a href="/item/{listing_id}">{title}</a></h3>
        <ul class="vehicle-card__params">
          <li>{year}</li><li>{mileage} km</li><li>{fuel}</li><li>Automatická</li>
        </ul>
        <div class="vehicle-card__price"><span class="vehicle-card__price-title">{price_title}</span><strong>{price}</strong></div>
      </div>
      <div class="vehicle-card__footer"><div class="vehicle-card__auction">Konec: <strong>{end}</strong></div></div>
    </div>'''


def page(total: int, cards: list[str], pages: tuple[int, ...]) -> str:
    links = "".join(f'<a href="/vozidla?strana={number}">{number}</a>' for number in pages)
    return (
        f'<h2>Všechna vozidla <span class="vehicle-results__count">{total}</span></h2>'
        f'{links}<div class="row vehicle-results">{"".join(cards)}</div>'
    )


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
        self.calls = 0

    def get(self, url: str, **_: object) -> Response:
        page_number = int(parse_qs(urlparse(url).query)["strana"][0])
        snapshot_index = min(self.calls // 2, len(self.snapshots) - 1)
        self.calls += 1
        return Response(self.snapshots[snapshot_index][page_number])


class CarAukceWatchTest(unittest.TestCase):
    def test_parse_card_preserves_native_price_and_vehicle_fields(self) -> None:
        parsed = watch.parse_page(
            page(2, [card(10), card(11, fuel="Nafta", price_title="Orientační cena")], (1,)),
            page=1,
            observed_at=NOW.isoformat(),
            now=NOW,
            fx_rate=20.0,
        )
        self.assertEqual(parsed.announced_total, 2)
        self.assertEqual(len(parsed.rows), 2)
        first, second = parsed.rows
        self.assertEqual(first["id"], "caraukce:10")
        self.assertEqual(first["url"], "https://www.caraukce.cz/item/10")
        self.assertEqual(first["year"], 2021)
        self.assertEqual(first["mileage"], 12345)
        self.assertEqual(first["fuel"], "petrol")
        self.assertEqual(first["price_amount"], 12500)
        self.assertEqual(first["price_currency"], "EUR")
        self.assertEqual(first["price_eur"], 12500)
        self.assertEqual(first["price_kind"], "starting_bid")
        self.assertEqual(second["fuel"], "diesel")
        self.assertEqual(second["price_kind"], "guide_price")
        self.assertEqual(first["canonical_end_utc"], "2026-08-30T10:30:00+00:00")

    def test_two_complete_passes_emit_every_announced_listing(self) -> None:
        snapshot = {
            1: page(3, [card(10), card(11)], (1, 2)),
            2: page(3, [card(12)], (1, 2)),
        }
        payload = watch.build_watch(session=Session([snapshot]), now=NOW, timeout=5, fx_rates={"CZK": (20.0, "explicit")})
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual([row["id"] for row in payload["rows"]], [
            "caraukce:10", "caraukce:11", "caraukce:12",
        ])
        report = payload["source_reports"]["caraukce"]
        self.assertEqual(report["declared"], 3)
        self.assertEqual(report["pages"], 2)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_declared_count_gap_fails_closed(self) -> None:
        incomplete = {
            1: page(4, [card(10), card(11)], (1, 2)),
            2: page(4, [card(12)], (1, 2)),
        }
        with self.assertRaisesRegex(watch.CarAukceWatchError, "announced vehicle total"):
            watch.build_watch(session=Session([incomplete]), now=NOW, timeout=5, fx_rates={"CZK": (20.0, "explicit")})

    def test_changed_second_pass_fails_closed(self) -> None:
        first = {
            1: page(3, [card(10), card(11)], (1, 2)),
            2: page(3, [card(12)], (1, 2)),
        }
        changed = {
            1: page(3, [card(10), card(11)], (1, 2)),
            2: page(3, [card(13)], (1, 2)),
        }
        with self.assertRaisesRegex(watch.CarAukceWatchError, "final reconciliation"):
            watch.build_watch(session=Session([first, changed]), now=NOW, timeout=5, fx_rates={"CZK": (20.0, "explicit")})


if __name__ == "__main__":
    unittest.main()
