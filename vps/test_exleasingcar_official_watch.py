#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import exleasingcar_official_watch as watch


UTC = dt.timezone.utc
DEFAULT_PAGE_PREFIX = "https://www.exleasingcar.com/en/auto-auction/show-60"


def card(vehicle_id: int, country: str, title: str, *, price: int) -> str:
    return f'''<div class="auto-block" car-id="{vehicle_id}">
      <span class="flag-image flag-{country.lower()}"></span>
      <h5>{title}</h5><div>01.2021 Benzyna 107,671km</div>
      <span>Minimum price: € {price}</span></div>'''


def page(
    total: int,
    cards: str,
    *,
    page_count: int = 2,
    prefix: str = watch.PAGE_URL_PREFIX,
) -> str:
    anchors = "".join(
        f'<a class="pagination-page" href="{prefix}/{number}">{number}</a>'
        for number in sorted({1, page_count})
    )
    return f'''<input id="count_viso_auto" value="Filter ({total})" />
      <nav>{anchors}</nav>{cards}'''


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, object], calls: list[str] | None = None) -> None:
        self.responses = responses
        self.calls = calls

    def get(self, url: str, **_: object) -> Response:
        if self.calls is not None:
            self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, list):
            if not response:
                raise AssertionError(f"no remaining response for {url}")
            response = response.pop(0)
        if not isinstance(response, str):
            raise AssertionError(f"invalid response for {url}")
        return Response(response)


class ExleasingcarWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_page_size = watch.PAGE_SIZE
        self.original_factory = watch.configured_session

    def tearDown(self) -> None:
        watch.PAGE_SIZE = self.original_page_size
        watch.configured_session = self.original_factory

    def build(self, responses: dict[str, object], *, calls: list[str] | None = None) -> dict:
        watch.PAGE_SIZE = 2
        watch.configured_session = lambda: Session(responses, calls)  # type: ignore[assignment]
        return watch.build_watch(
            session=Session(responses, calls),
            now=dt.datetime(2026, 8, 28, 19, 0, tzinfo=UTC),
            workers=1,
        )

    def test_catalogue_route_is_explicit_recent_order_with_bounded_retries(self) -> None:
        self.assertEqual(watch.PAGE_SIZE, 60)
        self.assertEqual(watch.DEFAULT_WORKERS, 16)
        self.assertEqual(watch.STRICT_SNAPSHOT_ATTEMPTS, 6)
        self.assertEqual(
            watch.PAGE_URL_PREFIX,
            "https://www.exleasingcar.com/en/auto-auction/order-9/show-60",
        )
        self.assertEqual(watch.SOURCE_URL, f"{watch.PAGE_URL_PREFIX}/1")
        self.assertFalse(hasattr(watch, "_build_moving_catalogue_snapshot"))

    def test_current_show_results_counter_is_accepted(self) -> None:
        markup = page(
            2,
            card(1, "DE", "ABARTH 500", price=7098),
        ).replace("Filter (2)", "Show results (2)")
        parsed = watch.parse_page(markup, observed_at="2026-08-28T19:00:00+00:00")
        self.assertEqual(parsed.total, 2)

    def test_default_mutable_order_prefix_is_rejected(self) -> None:
        markup = page(
            1,
            card(1, "DE", "ABARTH 500", price=7098),
            page_count=1,
            prefix=DEFAULT_PAGE_PREFIX,
        )
        with self.assertRaisesRegex(
            watch.ExleasingcarWatchError,
            "escaped the explicit recent-order route",
        ):
            watch.build_watch(
                session=Session({watch.SOURCE_URL: markup}),
                now=dt.datetime(2026, 8, 28, 19, 0, tzinfo=UTC),
                workers=1,
            )

    def test_complete_ordered_catalogue_is_reconciled(self) -> None:
        first = page(
            3,
            card(1, "DE", "ABARTH 500", price=7098)
            + card(2, "FR", "PEUGEOT 308", price=8200),
        )
        second_url = f"{watch.PAGE_URL_PREFIX}/2"
        second = page(3, card(3, "NL", "TOYOTA YARIS", price=6300))
        calls: list[str] = []
        payload = self.build({watch.SOURCE_URL: first, second_url: second}, calls=calls)

        report = payload["source_reports"]["exleasingcar"]
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(report["declared"], 3)
        self.assertEqual(report["snapshot_attempts"], 1)
        self.assertTrue(report["first_page_preflight_rechecked"])
        self.assertTrue(all(row["adapter_authorized"] for row in payload["rows"]))
        self.assertEqual(calls.count(watch.SOURCE_URL), 3)
        self.assertEqual(calls.count(second_url), 1)
        self.assertTrue(all(url.startswith(watch.PAGE_URL_PREFIX) for url in calls))

    def test_catalogue_change_restarts_and_then_publishes_one_coherent_pass(self) -> None:
        first = page(
            3,
            card(1, "DE", "ABARTH 500", price=7098)
            + card(2, "FR", "PEUGEOT 308", price=8200),
        )
        changed_second = page(
            4,
            card(3, "NL", "TOYOTA YARIS", price=6300)
            + card(4, "NL", "VW POLO", price=6400),
        )
        stable_second = page(3, card(3, "NL", "TOYOTA YARIS", price=6300))
        second_url = f"{watch.PAGE_URL_PREFIX}/2"
        payload = self.build(
            {
                watch.SOURCE_URL: first,
                second_url: [changed_second, stable_second],
            }
        )
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["source_reports"]["exleasingcar"]["snapshot_attempts"], 2)

    def test_terminal_page_cardinality_exactly_reconciles_declared_total(self) -> None:
        first = page(
            5,
            card(1, "DE", "CAR 1", price=1) + card(2, "DE", "CAR 2", price=2),
            page_count=3,
        )
        second = page(
            5,
            card(3, "DE", "CAR 3", price=3) + card(4, "DE", "CAR 4", price=4),
            page_count=3,
        )
        terminal = page(5, card(5, "DE", "CAR 5", price=5), page_count=3)
        payload = self.build(
            {
                watch.SOURCE_URL: first,
                f"{watch.PAGE_URL_PREFIX}/2": second,
                f"{watch.PAGE_URL_PREFIX}/3": terminal,
            }
        )
        self.assertEqual(payload["row_count"], 5)
        self.assertEqual(payload["source_reports"]["exleasingcar"]["pages"], 3)

    def test_stale_and_missing_equal_cardinality_is_rejected_after_bound(self) -> None:
        # The captured four IDs are {1,2,3,4}.  By the final authoritative
        # recent-order probe, ID 1 is stale and ID 5 is current but missing.
        # Cardinality is still four, so a count-only moving union would be an
        # unsafe false success; the ordered first-page tuple must reject it.
        captured_first = page(
            4,
            card(1, "DE", "STALE", price=1) + card(2, "DE", "CAR 2", price=2),
        )
        captured_second = page(
            4,
            card(3, "DE", "CAR 3", price=3) + card(4, "DE", "CAR 4", price=4),
        )
        current_first = page(
            4,
            card(5, "DE", "NEW", price=5) + card(2, "DE", "CAR 2", price=2),
        )
        second_url = f"{watch.PAGE_URL_PREFIX}/2"
        calls: list[str] = []
        with self.assertRaisesRegex(
            watch.ExleasingcarSnapshotChanged,
            "changed before the final check",
        ):
            self.build(
                {
                    watch.SOURCE_URL: [captured_first, captured_first, current_first]
                    * watch.STRICT_SNAPSHOT_ATTEMPTS,
                    second_url: [captured_second] * watch.STRICT_SNAPSHOT_ATTEMPTS,
                },
                calls=calls,
            )
        self.assertEqual(calls.count(watch.SOURCE_URL), 3 * watch.STRICT_SNAPSHOT_ATTEMPTS)
        self.assertEqual(calls.count(second_url), watch.STRICT_SNAPSHOT_ATTEMPTS)

    def test_page_one_preflight_short_circuits_each_strict_attempt(self) -> None:
        original = page(
            4,
            card(1, "DE", "CAR 1", price=1) + card(2, "DE", "CAR 2", price=2),
        )
        changed = page(
            4,
            card(5, "DE", "NEW", price=5) + card(1, "DE", "CAR 1", price=1),
        )
        second_url = f"{watch.PAGE_URL_PREFIX}/2"
        calls: list[str] = []
        with self.assertRaisesRegex(
            watch.ExleasingcarSnapshotChanged,
            "changed before pagination",
        ):
            self.build(
                {
                    watch.SOURCE_URL: [original, changed] * watch.STRICT_SNAPSHOT_ATTEMPTS,
                    second_url: page(
                        4,
                        card(3, "DE", "CAR 3", price=3) + card(4, "DE", "CAR 4", price=4),
                    ),
                },
                calls=calls,
            )
        self.assertEqual(calls.count(watch.SOURCE_URL), 2 * watch.STRICT_SNAPSHOT_ATTEMPTS)
        self.assertNotIn(second_url, calls)

    def test_missing_country_is_rejected_without_retry(self) -> None:
        markup = page(1, '<div class="auto-block" car-id="1"><h5>Car</h5></div>', page_count=1)
        calls: list[str] = []
        with self.assertRaises(watch.ExleasingcarWatchError):
            watch.build_watch(
                session=Session({watch.SOURCE_URL: markup}, calls),
                now=dt.datetime(2026, 8, 28, 19, 0, tzinfo=UTC),
                workers=1,
            )
        self.assertEqual(calls, [watch.SOURCE_URL])


if __name__ == "__main__":
    unittest.main()
