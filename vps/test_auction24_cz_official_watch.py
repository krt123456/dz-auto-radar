#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import auction24_cz_official_watch as watch


UTC = dt.timezone.utc


def card(
    listing_id: str,
    title: str,
    *,
    meta: str = "2021 · 12 345 km",
    price: str = "12,500 €",
    status: str = "Buy now",
    country: str = "Česká republika",
) -> str:
    return f'''<a class="card group" href="/en/item/{listing_id}">
      <svg class="flag-badge" aria-label="{country}"></svg>
      <div class="status-overlay">{status}</div>
      <p class="title">{title}</p><p class="title-meta">{meta}</p>
      <div class="price-row">{price}</div></a>'''


def page(total: int, cards: str) -> str:
    return f'''<html><head><script type="application/ld+json">
      {{"@context":"https://schema.org","@type":"ItemList","numberOfItems":{total}}}
      </script></head><body>{cards}</body></html>'''


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, str | list[str]]) -> None:
        self.responses = responses

    def get(self, url: str, **_: object) -> Response:
        response = self.responses[url]
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"no remaining response for {url}")
            response = response.pop(0)
        return Response(response)

    def close(self) -> None:
        return None


class Auction24CzWatchTest(unittest.TestCase):
    def test_card_page_parses_the_official_jsonld_total(self) -> None:
        parsed = watch.parse_page(page(1, card("a1", "Škoda Octavia")))
        self.assertEqual(parsed.total, 1)
        self.assertEqual(parsed.cards[0].listing_id, "a1")
        self.assertEqual(parsed.cards[0].country, "CZ")
        self.assertEqual(parsed.cards[0].status_label, "Buy now")

    def test_two_pass_reconciles_current_passenger_cars_only(self) -> None:
        first = page(4, card("a1", "Škoda Octavia") + card("a2", "Ford Transit Custom"))
        second = page(4, card("a3", "Audi A6", status="Sold") + card("a4", "Toyota Corolla"))
        responses: dict[str, str | list[str]] = {
            watch.SOURCE_URL: [first, first],
            f"{watch.SOURCE_URL}?page=2": [second, second],
        }
        original_page_size = watch.PAGE_SIZE
        original_factory = watch.configured_session
        try:
            watch.PAGE_SIZE = 2
            watch.configured_session = lambda: Session(responses)  # type: ignore[assignment]
            payload = watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
                workers=1,
            )
        finally:
            watch.PAGE_SIZE = original_page_size
            watch.configured_session = original_factory
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual([row["id"] for row in payload["rows"]], ["auction24-cz:a1", "auction24-cz:a4"])
        self.assertEqual(report["declared"], 4)
        self.assertEqual(report["visited"], 4)
        self.assertEqual(report["active_cards"], 3)
        self.assertEqual(report["source_excluded"], {"inactive_status": 1, "non_passenger_title": 1})
        self.assertTrue(report["two_pass_verified"])

    def test_lifecycle_change_between_passes_fails_closed(self) -> None:
        current = page(1, card("a1", "Škoda Octavia", status="Buy now"))
        sold = page(1, card("a1", "Škoda Octavia", status="Sold"))
        with self.assertRaisesRegex(watch.Auction24CzWatchError, "lifecycle changed"):
            watch.build_watch(
                session=Session({watch.SOURCE_URL: [current, sold]}),
                now=dt.datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
                workers=1,
            )

    def test_unsupported_country_fails_closed(self) -> None:
        with self.assertRaisesRegex(watch.Auction24CzWatchError, "unsupported country flag"):
            watch.parse_page(page(1, card("a1", "Škoda Octavia", country="Slovensko")))

    def test_missing_optional_flag_uses_the_czech_source_country(self) -> None:
        markup = page(1, card("a1", "Škoda Octavia").replace(' class="flag-badge" aria-label="Česká republika"', ""))
        self.assertEqual(watch.parse_page(markup).cards[0].country, "CZ")

    def test_year_can_fall_back_to_the_public_title(self) -> None:
        row = watch.normalize_card(
            watch.Card(
                listing_id="a1",
                url="https://auction24.cz/en/item/a1",
                title="FIAT FREEMONT / 2015",
                meta="217 200",
                price_label="7,000 €",
                status_label="Buy now",
                country="CZ",
            ),
            observed_at="2026-08-28T20:00:00+00:00",
        )
        self.assertEqual(row["year"], 2015)


if __name__ == "__main__":
    unittest.main()
