#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import huutokaupat_official_watch as watch


UTC = dt.timezone.utc


def card(
    lot_id: int,
    title: str,
    details: str,
    price: int,
    bids: int,
    *,
    ended: bool = False,
) -> str:
    status = "<p>Päättynyt</p>" if ended else ""
    return f'''<article data-test="entry-card-{lot_id}">
      <a data-test="entry-card-link-{lot_id}" href="/kohde/{lot_id}/lot-{lot_id}">
        <h3>{title}</h3></a>
      <p>{details}</p><p>{price}€</p><p>{bids} tarjousta</p>{status}
    </article>'''


def page(total: int, current: int, pages: int, cards: str) -> str:
    return f'''<p>{total} ilmoitusta, sivu {current}</p>
      <button aria-label="Sivu {current}/{pages}">Sivu</button>{cards}'''


PAGE_1 = page(
    3,
    1,
    2,
    card(11, "Mazda 5, 2024, Raisio", "1.8 l, Bensiini, 85 kW, 12000 km", 450, 5)
    + card(12, "Toyota Yaris, 2025, Turku", "Hybridi, 8000 km", 8200, 3),
)
PAGE_2 = page(
    3,
    2,
    2,
    card(13, "Ford Transit, 2023, Kuopio", "Diesel, 45000 km", 3600, 2, ended=True),
)


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses

    def get(self, url: str, **_: object) -> Response:
        return Response(self.responses[url])


class HuutokaupatWatchTest(unittest.TestCase):
    def responses(self, first: str = PAGE_1) -> dict[str, str]:
        return {
            watch.SOURCE_URL: first,
            watch.page_url(2): PAGE_2,
        }

    def test_complete_category_reconciles_and_preserves_visible_bid_fields(self) -> None:
        payload = watch.build_watch(
            session=Session(self.responses()),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
        )
        rows = {row["id"]: row for row in payload["rows"]}
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["source_reports"]["huutokaupat"]["declared"], 3)
        self.assertEqual(payload["source_reports"]["huutokaupat"]["pages"], 2)
        self.assertEqual(rows["huutokaupat:11"]["fuel"], "petrol")
        self.assertEqual(rows["huutokaupat:12"]["fuel"], "hybrid")
        self.assertEqual(rows["huutokaupat:13"]["fuel"], "diesel")
        self.assertEqual(rows["huutokaupat:11"]["price_eur"], 450)
        self.assertEqual(rows["huutokaupat:11"]["bid_count"], 5)
        self.assertEqual(rows["huutokaupat:13"]["auction_status"], "ended")
        self.assertTrue(all(row["adapter_authorized"] for row in rows.values()))

    def test_mismatched_page_count_fails_closed(self) -> None:
        invalid = PAGE_1.replace('aria-label="Sivu 1/2"', 'aria-label="Sivu 1/3"')
        with self.assertRaises(watch.HuutokaupatWatchError):
            watch.build_watch(
                session=Session(self.responses(first=invalid)),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
            )


if __name__ == "__main__":
    unittest.main()
