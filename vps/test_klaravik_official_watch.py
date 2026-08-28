#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

import klaravik_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
SOURCE = watch.SourceSpec(
    "klaravik-se", "Klaravik Sweden", "SE", "www.klaravik.se", "SEK",
    "/auktion/fordon/latta-fordon/personbilar/",
)


def card(
    item_id: int,
    title: str,
    *,
    price: str = "31.000 SEK",
    bids: int = 2,
    end: str = "2026-08-29T12:00:00+02:00",
    location: str = "Aarhus",
) -> str:
    return f'''<article class="product_card" id="product_card--{item_id}">
      <a href="/auktion/produkt/{item_id}-lot/" title="{title}">
        <span data-prod-id="{item_id}" data-auction-close="{end}"></span>
        <p class="product_card__title">{title}</p>
        <p class="product_card__info-text">{location}</p>
        <p class="product_card__current-bid">{price}</p>
        <p id="antbids_{item_id}">{bids}</p>
      </a>
    </article>'''


def page(total: int, cards: str) -> str:
    return f"<script>window.objectsInList = {total};</script>{cards}"


PAGE_1 = page(
    3,
    card(1, "Personbil Volvo V60 Bensin, 2024")
    + card(2, "Sea-Doo RXT260 Vandscooter"),
)
PAGE_2 = page(3, card(3, "Varebil Ford Transit, 2023"))


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses

    def get(self, url: str, **_: Any) -> Response:
        return Response(self.responses[url])


class SequencedSession:
    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = {url: list(bodies) for url, bodies in responses.items()}

    def get(self, url: str, **_: Any) -> Response:
        bodies = self.responses[url]
        if not bodies:
            raise AssertionError(f"unexpected extra request for {url}")
        return Response(bodies.pop(0))


class KlaravikWatchTest(unittest.TestCase):
    def responses(self, first: str = PAGE_1) -> dict[str, str]:
        return {
            watch.page_url(SOURCE, 1): first,
            watch.page_url(SOURCE, 2): PAGE_2,
        }

    def test_dedicated_category_emits_only_passenger_cars(self) -> None:
        original_page_size = watch.PAGE_SIZE
        try:
            watch.PAGE_SIZE = 2
            payload = watch.build_watch(
                session=Session(self.responses()), source_specs=(SOURCE,), now=NOW, timeout=10,
            )
        finally:
            watch.PAGE_SIZE = original_page_size
        self.assertEqual(payload["row_count"], 1)
        row = payload["rows"][0]
        report = payload["source_reports"]["klaravik-se"]
        self.assertEqual(row["id"], "klaravik:se:1")
        self.assertEqual(row["category"], "car")
        self.assertEqual(row["fuel"], "petrol")
        self.assertEqual(row["price_amount"], 31000)
        self.assertEqual(row["bid_count"], 2)
        self.assertEqual(report["declared"], 3)
        self.assertEqual(report["non_passenger_excluded"], 2)
        self.assertEqual(report["snapshot_attempts"], 1)
        self.assertIn("personbilar", SOURCE.category_url)

    def test_retries_if_the_category_changes_during_pagination(self) -> None:
        changed_second_page = page(4, card(3, "Varebil Ford Transit, 2023"))
        original_page_size = watch.PAGE_SIZE
        try:
            watch.PAGE_SIZE = 2
            session = SequencedSession({
                watch.page_url(SOURCE, 1): [PAGE_1, PAGE_1, PAGE_1],
                watch.page_url(SOURCE, 2): [changed_second_page, PAGE_2],
            })
            payload = watch.build_watch(session=session, source_specs=(SOURCE,), now=NOW, timeout=10)
        finally:
            watch.PAGE_SIZE = original_page_size
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["source_reports"]["klaravik-se"]["snapshot_attempts"], 2)

    def test_cross_domain_lot_url_is_rejected(self) -> None:
        with self.assertRaises(watch.KlaravikWatchError):
            watch.canonical_lot_url(SOURCE, "https://example.test/auction/1")

    def test_common_visible_powertrain_markers_are_not_left_unknown(self) -> None:
        self.assertEqual(watch.normalize_fuel("Volkswagen Touareg V6 TDI"), "diesel")
        self.assertEqual(watch.normalize_fuel("Mercedes-Benz E 220 CDI"), "diesel")
        self.assertEqual(watch.normalize_fuel("Mercedes-Benz E 220 d 4MATIC"), "diesel")
        self.assertEqual(watch.normalize_fuel("Veteranbil Mercedes-Benz 200D"), "diesel")
        self.assertEqual(watch.normalize_fuel("Audi A6 55 TFSI e Quattro"), "petrol/electric hybrid")


if __name__ == "__main__":
    unittest.main()
