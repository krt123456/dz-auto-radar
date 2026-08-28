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


class SequencedSession:
    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = {url: list(bodies) for url, bodies in responses.items()}

    def get(self, url: str, **_: object) -> Response:
        bodies = self.responses[url]
        if not bodies:
            raise AssertionError(f"unexpected extra request for {url}")
        return Response(bodies.pop(0))


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
        self.assertTrue(watch.SOURCE_URL.endswith("/osasto/henkiloautot"))
        self.assertTrue(all(row["category"] == "car" for row in rows.values()))
        self.assertEqual(rows["huutokaupat:11"]["category_raw"], "Henkilöautot")
        self.assertEqual(rows["huutokaupat:11"]["price_eur"], 450)
        self.assertEqual(rows["huutokaupat:11"]["bid_count"], 5)
        self.assertEqual(rows["huutokaupat:13"]["auction_status"], "ended")
        self.assertTrue(all(row["adapter_authorized"] for row in rows.values()))

    def test_retries_when_a_live_page_moves_between_snapshot_attempts(self) -> None:
        changed_page = PAGE_2.replace("3 ilmoitusta, sivu 2", "4 ilmoitusta, sivu 2")
        session = SequencedSession({
            watch.SOURCE_URL: [PAGE_1, PAGE_1, PAGE_1],
            watch.page_url(2): [changed_page, PAGE_2],
        })
        payload = watch.build_watch(
            session=session,
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
        )
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["source_reports"]["huutokaupat"]["snapshot_attempts"], 2)

    def test_continuously_advancing_category_uses_complete_moving_coverage(self) -> None:
        first = PAGE_1
        changed_second = page(
            4,
            2,
            2,
            card(13, "Ford Focus, 2023, Kuopio", "Diesel, 45000 km", 3600, 2)
            + card(14, "Kia Niro, 2024, Espoo", "Hybridi, 9000 km", 11800, 4),
        )
        final = page(
            4,
            1,
            2,
            card(11, "Mazda 5, 2024, Raisio", "1.8 l, Bensiini, 85 kW, 12000 km", 450, 5)
            + card(12, "Toyota Yaris, 2025, Turku", "Hybridi, 8000 km", 8200, 3),
        )
        responses = {
            watch.SOURCE_URL: [first, first, first, first, final],
            watch.page_url(2): [changed_second, changed_second, changed_second, changed_second],
        }
        original_factory = watch.configured_session
        sequence_session = SequencedSession(responses)
        try:
            watch.configured_session = lambda: sequence_session  # type: ignore[assignment]
            payload = watch.build_watch(
                session=sequence_session,
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
            )
        finally:
            watch.configured_session = original_factory
        report = payload["source_reports"]["huutokaupat"]
        self.assertEqual(payload["row_count"], 4)
        self.assertEqual(report["declared"], 4)
        self.assertEqual(report["snapshot_mode"], "moving_catalogue_coverage")
        self.assertEqual(report["strict_snapshot_failures"], 3)
        self.assertTrue(report["final_first_page_rechecked"])

    def test_moving_page_accepts_counter_ahead_of_terminal_pagination(self) -> None:
        parsed = watch.parse_page(
            page(
                5,
                1,
                2,
                card(11, "Mazda 5, 2024, Raisio", "Bensiini, 12000 km", 450, 5)
                + card(12, "Toyota Yaris, 2025, Turku", "Hybridi, 8000 km", 8200, 3),
            ),
            observed_at="2026-08-27T20:00:00+00:00",
        )
        self.assertTrue(watch._validate_moving_page(parsed, page=1, page_size=2))

    def test_moving_coverage_skips_a_page_that_falls_off_the_catalogue(self) -> None:
        final = page(
            2,
            1,
            1,
            card(11, "Mazda 5, 2024, Raisio", "Bensiini, 12000 km", 450, 5)
            + card(12, "Toyota Yaris, 2025, Turku", "Hybridi, 8000 km", 8200, 3),
        )
        disappeared_page = page(
            2,
            2,
            1,
            card(13, "Ford Focus, 2023, Kuopio", "Diesel, 45000 km", 3600, 2),
        )
        sequence_session = SequencedSession({
            watch.SOURCE_URL: [PAGE_1, final],
            watch.page_url(2): [disappeared_page],
        })
        original_factory = watch.configured_session
        try:
            watch.configured_session = lambda: sequence_session  # type: ignore[assignment]
            payload = watch._build_moving_catalogue_snapshot(
                session=sequence_session,
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
            )
        finally:
            watch.configured_session = original_factory
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["source_reports"]["huutokaupat"]["declared"], 2)

    def test_page_beyond_current_pagination_is_a_snapshot_change(self) -> None:
        disappeared_page = page(
            2,
            2,
            1,
            card(13, "Ford Focus, 2023, Kuopio", "Diesel, 45000 km", 3600, 2),
        )
        with self.assertRaises(watch.HuutokaupatPageUnavailable):
            watch.parse_page(disappeared_page, observed_at="2026-08-27T20:00:00+00:00")

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
