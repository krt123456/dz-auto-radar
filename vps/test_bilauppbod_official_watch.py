#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import bilauppbod_official_watch as watch

UTC = dt.timezone.utc


def card(listing_id: str, title: str, *, price: str = "255.000 kr.", date: str = "30.08.2026", end_time: str = "20:00", registration: str = "19.05.2017", mileage: str = "123988") -> str:
    return f'''<a class="carCardLink" href="/auction/view/{listing_id}"><div class="auctionTitle">{title}</div><div class="priceTxt">{price}</div><span class="aucEndDtTxt">{date}</span><span class="aucEndDtTxt endTime">{end_time}</span><div class="aucProp"><img alt="Fyrsti skráningard."/><span class="aucPropTxt">{registration}</span></div><div class="aucProp"><img alt="Akstur (km/mílur)"/><span class="aucPropTxt">{mileage}</span></div><span class="seljandiTxt">Seller</span></a>'''


def page(cards: str, *, last_page: int = 1) -> str:
    pager = "" if last_page == 1 else f'<a href="/?page={last_page}">{last_page}</a>'
    return f"<html><body><div class='auctionitems__'>{cards}</div>{pager}</body></html>"


def detail(manufacturer: str, doors: str, fuel: str = "Bensín", mileage: str = "123988") -> str:
    return f"<html><head><meta charset='utf-8'></head><table><tr><td>Framleiðandi</td><td>{manufacturer}</td></tr><tr><td>Dyr</td><td>{doors}</td></tr><tr><td>Vélargerð (eldsneyti)</td><td>{fuel}</td></tr><tr><td>Akstur (km/mílur)</td><td>{mileage}</td></tr><tr><td>Seljandi</td><td>Official seller</td></tr></table></html>"


class Response:
    def __init__(self, body: str) -> None:
        self.content = body.encode("utf-8")
    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, str | list[str]]) -> None:
        self.responses = responses
    def get(self, url: str, **_: object) -> Response:
        body = self.responses[url]
        if isinstance(body, list):
            if not body:
                raise AssertionError(f"no remaining response for {url}")
            body = body.pop(0)
        return Response(body)
    def close(self) -> None:
        return None


class BilauppbodWatchTest(unittest.TestCase):
    def test_card_parses_isk_price_mileage_and_reykjavik_time(self) -> None:
        parsed = watch.parse_page(page(card("100", "MAZDA 6")))
        value = parsed[1][0]
        self.assertEqual(value.price_amount, 255000)
        self.assertEqual(value.mileage_km, 123988)
        self.assertEqual(value.end_utc, dt.datetime(2026, 8, 30, 20, 0, tzinfo=UTC))

    def test_two_pass_reconciles_and_excludes_non_car(self) -> None:
        first, second = page(card("100", "MAZDA 6"), last_page=2), page(card("101", "FORD TRANSIT"), last_page=2)
        responses: dict[str, str | list[str]] = {
            watch.SOURCE_URL: [first, first], f"{watch.SOURCE_URL}?page=2": [second, second],
            f"{watch.SOURCE_URL}auction/view/100": detail("Mazda", "4", "Bensín/Rafmagn"),
            f"{watch.SOURCE_URL}auction/view/101": detail("Ford", "4", "Dísel"),
        }
        payload = watch.build_watch(session=Session(responses), now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC), workers=1)
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["id"], "bilauppbod:100")
        self.assertEqual(payload["rows"][0]["fuel"], "hybrid")
        self.assertEqual(payload["rows"][0]["mileage_km"], 123988)
        self.assertEqual(report["declared"], 2)
        self.assertEqual(report["detail_visited"], 2)
        self.assertEqual(report["source_excluded"], {"explicit_non_passenger_title": 1})
        self.assertEqual(report["mileage_values_suppressed"], 0)

    def test_card_change_between_passes_fails_closed(self) -> None:
        with self.assertRaisesRegex(watch.BilauppbodWatchError, "coherent snapshot"):
            watch.build_watch(session=Session({watch.SOURCE_URL: [page(card("100", "MAZDA 6")), page(card("100", "TOYOTA RAV4"))]}), now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC), workers=1, snapshot_attempts=1)

    def test_later_coherent_snapshot_is_accepted(self) -> None:
        stable = page(card("100", "MAZDA 6"))
        changed = page(card("100", "TOYOTA RAV4"))
        responses: dict[str, str | list[str]] = {
            watch.SOURCE_URL: [stable, changed, stable, stable],
            f"{watch.SOURCE_URL}auction/view/100": detail("Mazda", "4"),
        }
        payload = watch.build_watch(session=Session(responses), now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC), workers=1, snapshot_attempts=2)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["source_reports"][watch.SOURCE_KEY]["snapshot_attempt"], 2)

    def test_index_mileage_split_brain_is_suppressed_without_retry(self) -> None:
        first = page(card("100", "TOYOTA RAV4", mileage="Ekki skráð"))
        second = page(card("100", "TOYOTA RAV4", mileage="41847"))
        responses: dict[str, str | list[str]] = {
            watch.SOURCE_URL: [first, second],
            f"{watch.SOURCE_URL}auction/view/100": detail("Toyota", "4", mileage="41847"),
        }
        payload = watch.build_watch(
            session=Session(responses),
            now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC),
            workers=1,
            snapshot_attempts=1,
        )
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(report["snapshot_attempt"], 1)
        self.assertIsNone(payload["rows"][0]["mileage_km"])
        self.assertIsNone(payload["rows"][0]["mileage"])
        self.assertEqual(report["index_mileage_disagreements"], 1)
        self.assertEqual(report["index_detail_mileage_disagreements"], 0)
        self.assertEqual(report["mileage_values_suppressed"], 1)
        self.assertEqual(report["mileage_disagreement_ids"], ["100"])

    def test_index_detail_mileage_disagreement_is_suppressed(self) -> None:
        stable = page(card("100", "TOYOTA RAV4", mileage="41847"))
        responses: dict[str, str | list[str]] = {
            watch.SOURCE_URL: [stable, stable],
            f"{watch.SOURCE_URL}auction/view/100": detail("Toyota", "4", mileage="Ekki skráð"),
        }
        payload = watch.build_watch(
            session=Session(responses),
            now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC),
            workers=1,
            snapshot_attempts=1,
        )
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertIsNone(payload["rows"][0]["mileage_km"])
        self.assertEqual(report["index_mileage_disagreements"], 0)
        self.assertEqual(report["index_detail_mileage_disagreements"], 1)
        self.assertEqual(report["mileage_values_suppressed"], 1)

    def test_mileage_policy_requires_three_equal_observations(self) -> None:
        cases = [
            ((None, None, None), (None, "")),
            ((41847, 41847, 41847), (41847, "")),
            ((None, 41847, 41847), (None, "index_disagreement")),
            ((41847, None, None), (None, "index_disagreement")),
            ((41847, 41847, None), (None, "index_detail_disagreement")),
            ((None, None, 41847), (None, "index_detail_disagreement")),
            ((41847, 41847, 50000), (None, "index_detail_disagreement")),
        ]
        for evidence, expected in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(watch.resolve_mileage(*evidence), expected)

    def test_lifecycle_instability_exhausts_every_pair_before_details(self) -> None:
        stable = page(card("100", "MAZDA 6"))
        changed = page(card("100", "TOYOTA RAV4"))
        responses = {watch.SOURCE_URL: [stable, changed] * 4}
        with self.assertRaisesRegex(watch.BilauppbodWatchError, "coherent snapshot"):
            watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC),
                workers=1,
                snapshot_attempts=4,
            )
        self.assertEqual(responses[watch.SOURCE_URL], [])

    def test_closing_time_instability_remains_fail_closed(self) -> None:
        stable = page(card("100", "MAZDA 6", end_time="20:00"))
        extended = page(card("100", "MAZDA 6", end_time="20:15"))
        responses = {watch.SOURCE_URL: [stable, extended] * 4}
        with self.assertRaisesRegex(watch.BilauppbodWatchError, "coherent snapshot"):
            watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 28, 20, tzinfo=UTC),
                workers=1,
                snapshot_attempts=4,
            )
        self.assertEqual(responses[watch.SOURCE_URL], [])

    def test_door_count_and_recreational_title_are_excluded(self) -> None:
        vehicle = watch.Card("100", "https://www.bilauppbod.is/auction/view/100", "SUZUKI VITARA", "0 kr.", 0, dt.datetime(2026, 8, 30, 20, tzinfo=UTC), "19.05.2017", 123, "", None)
        self.assertEqual(watch.passenger_exclusion_reason(vehicle, watch.Detail("100", "Suzuki", "6", "Bensín", 123, "")), "door_count_not_passenger")
        camper = watch.Card("101", "https://www.bilauppbod.is/auction/view/101", "WEINSBERG R47", "0 kr.", 0, dt.datetime(2026, 8, 30, 20, tzinfo=UTC), "19.05.2017", 123, "", None)
        self.assertEqual(watch.passenger_exclusion_reason(camper, watch.Detail("101", "", "4", "Dísel", 123, "")), "explicit_non_passenger_title")
        kangoo = watch.Card("102", "https://www.bilauppbod.is/auction/view/102", "RENAULT KANGOO", "0 kr.", 0, dt.datetime(2026, 8, 30, 20, tzinfo=UTC), "19.05.2017", 123, "", None)
        self.assertEqual(watch.passenger_exclusion_reason(kangoo, watch.Detail("102", "Renault", "4", "Dísel", 123, "")), "explicit_non_passenger_title")

    def test_zero_current_bid_stays_visible_without_claiming_a_price(self) -> None:
        vehicle = watch.Card("100", "https://www.bilauppbod.is/auction/view/100", "MAZDA 6", "0 kr.", 0, dt.datetime(2026, 8, 30, 20, tzinfo=UTC), "19.05.2017", 123, "", None)
        row = watch.normalize_card(vehicle, watch.Detail("100", "Mazda", "4", "Bensín", 123, ""), observed_at="2026-08-28T20:00:00+00:00")
        self.assertEqual(row["price_amount"], 0)
        self.assertEqual(row["price_kind"], "unknown")


if __name__ == "__main__":
    unittest.main()
