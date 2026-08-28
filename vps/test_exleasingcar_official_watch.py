#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import exleasingcar_official_watch as watch


UTC = dt.timezone.utc


def card(vehicle_id: int, country: str, title: str, *, price: int) -> str:
    return f'''<div class="auto-block" car-id="{vehicle_id}">
      <span class="flag-image flag-{country.lower()}"></span>
      <h5>{title}</h5><div>01.2021 Benzyna 107,671km</div>
      <span>Minimum price: € {price}</span></div>'''


def page(total: int, cards: str) -> str:
    return f'''<input id="count_viso_auto" value="Filter ({total})" />
      <nav><a class="pagination-page" href="https://www.exleasingcar.com/en/auto-auction/show-20/1">1</a>
      <a class="pagination-page" href="https://www.exleasingcar.com/en/auto-auction/show-20/2">2</a></nav>
      {cards}'''


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses

    def get(self, url: str, **_: object) -> Response:
        response = self.responses[url]
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"no remaining response for {url}")
            response = response.pop(0)
        return Response(response)


class ExleasingcarWatchTest(unittest.TestCase):
    def test_default_catalogue_route_uses_largest_public_page_size(self) -> None:
        self.assertEqual(watch.PAGE_SIZE, 60)
        self.assertEqual(watch.DEFAULT_WORKERS, 16)
        self.assertEqual(watch.STRICT_SNAPSHOT_ATTEMPTS, 2)
        self.assertEqual(watch.MOVING_CATALOGUE_PASSES, 4)
        self.assertTrue(watch.SOURCE_URL.endswith("/show-60/1"))

    def test_current_show_results_counter_is_accepted(self) -> None:
        markup = page(2, card(1, "DE", "ABARTH 500", price=7098)).replace(
            "Filter (2)", "Show results (2)"
        )
        parsed = watch.parse_page(markup, observed_at="2026-08-27T20:00:00+00:00")
        self.assertEqual(parsed.total, 2)

    def test_complete_catalogue_is_reconciled_and_marked_for_broad_watch(self) -> None:
        first = page(3, card(1, "DE", "ABARTH 500", price=7098) + card(2, "FR", "PEUGEOT 308", price=8200))
        second = page(3, card(3, "NL", "TOYOTA YARIS", price=6300))
        responses = {
            watch.SOURCE_URL: first,
            "https://www.exleasingcar.com/en/auto-auction/show-20/2": second,
        }
        original_page_size = watch.PAGE_SIZE
        original_factory = watch.configured_session
        try:
            watch.PAGE_SIZE = 2
            watch.configured_session = lambda: Session(responses)  # type: ignore[assignment]
            payload = watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
            )
        finally:
            watch.PAGE_SIZE = original_page_size
            watch.configured_session = original_factory
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["source_reports"]["exleasingcar"]["declared"], 3)
        self.assertTrue(all(row["adapter_authorized"] for row in payload["rows"]))
        self.assertEqual(payload["rows"][0]["fuel"], "petrol")
        self.assertEqual(payload["rows"][0]["year"], 2021)
        self.assertEqual(payload["rows"][0]["country"], "DE")

    def test_catalogue_change_restarts_from_page_one_before_publish(self) -> None:
        first = page(3, card(1, "DE", "ABARTH 500", price=7098) + card(2, "FR", "PEUGEOT 308", price=8200))
        changed_second = page(4, card(3, "NL", "TOYOTA YARIS", price=6300) + card(4, "NL", "VW POLO", price=6400))
        second = page(3, card(3, "NL", "TOYOTA YARIS", price=6300))
        responses = {
            watch.SOURCE_URL: [first, first, first, first, first],
            "https://www.exleasingcar.com/en/auto-auction/show-20/2": [changed_second, second],
        }
        original_page_size = watch.PAGE_SIZE
        original_factory = watch.configured_session
        try:
            watch.PAGE_SIZE = 2
            watch.configured_session = lambda: Session(responses)  # type: ignore[assignment]
            payload = watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
            )
        finally:
            watch.PAGE_SIZE = original_page_size
            watch.configured_session = original_factory
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["source_reports"]["exleasingcar"]["snapshot_attempts"], 2)

    def test_continuously_advancing_catalogue_uses_complete_moving_coverage(self) -> None:
        first = page(3, card(1, "DE", "ABARTH 500", price=7098) + card(2, "FR", "PEUGEOT 308", price=8200))
        changed = page(4, card(3, "NL", "TOYOTA YARIS", price=6300) + card(4, "NL", "VW POLO", price=6400))
        final = page(4, card(1, "DE", "ABARTH 500", price=7098) + card(2, "FR", "PEUGEOT 308", price=8200))
        responses = {
            watch.SOURCE_URL: [first, first, first, first, first, final],
            "https://www.exleasingcar.com/en/auto-auction/show-20/2": [changed, changed, changed],
        }
        original_page_size = watch.PAGE_SIZE
        original_factory = watch.configured_session
        try:
            watch.PAGE_SIZE = 2
            watch.configured_session = lambda: Session(responses)  # type: ignore[assignment]
            payload = watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
            )
        finally:
            watch.PAGE_SIZE = original_page_size
            watch.configured_session = original_factory
        report = payload["source_reports"]["exleasingcar"]
        self.assertEqual(payload["row_count"], 4)
        self.assertEqual(report["declared"], 4)
        self.assertEqual(report["snapshot_mode"], "moving_catalogue_coverage")
        self.assertEqual(report["strict_snapshot_failures"], 2)
        self.assertTrue(report["final_first_page_rechecked"])

    def test_moving_coverage_uses_the_extra_reconciliation_pass(self) -> None:
        strict = page(3, card(1, "DE", "ABARTH 500", price=7098) + card(2, "FR", "PEUGEOT 308", price=8200))
        changed = page(4, card(3, "NL", "TOYOTA YARIS", price=6300) + card(4, "NL", "VW POLO", price=6400))
        moving = page(4, card(1, "DE", "ABARTH 500", price=7098) + card(2, "FR", "PEUGEOT 308", price=8200))
        only_three = page(4, card(3, "NL", "TOYOTA YARIS", price=6300))
        only_four = page(4, card(4, "NL", "VW POLO", price=6400))
        responses = {
            # Two strict passes consume the first four probes.  Moving mode
            # then needs four coverage passes before the final ID arrives.
            watch.SOURCE_URL: [strict, strict, strict, strict] + [moving] * 8,
            "https://www.exleasingcar.com/en/auto-auction/show-20/2": [
                changed, changed, only_three, only_three, only_three, only_four,
            ],
        }
        original_page_size = watch.PAGE_SIZE
        original_factory = watch.configured_session
        try:
            watch.PAGE_SIZE = 2
            watch.configured_session = lambda: Session(responses)  # type: ignore[assignment]
            payload = watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
            )
        finally:
            watch.PAGE_SIZE = original_page_size
            watch.configured_session = original_factory
        report = payload["source_reports"]["exleasingcar"]
        self.assertEqual(payload["row_count"], 4)
        self.assertEqual(report["declared"], 4)
        self.assertEqual(report["coverage_passes"], 4)
        self.assertEqual(report["strict_snapshot_failures"], 2)

    def test_moving_page_accepts_a_counter_that_precedes_terminal_pagination(self) -> None:
        original_page_size = watch.PAGE_SIZE
        try:
            watch.PAGE_SIZE = 2
            parsed = watch.parse_page(
                page(5, card(1, "DE", "ABARTH 500", price=7098) + card(2, "FR", "PEUGEOT 308", price=8200)),
                observed_at="2026-08-27T20:00:00+00:00",
            )
            self.assertTrue(watch._validate_moving_page(parsed, page=1))
        finally:
            watch.PAGE_SIZE = original_page_size

    def test_missing_country_is_rejected(self) -> None:
        markup = page(1, '<div class="auto-block" car-id="1"><h5>Car</h5></div>')
        with self.assertRaises(watch.ExleasingcarWatchError):
            watch.parse_page(markup, observed_at="2026-08-27T20:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
