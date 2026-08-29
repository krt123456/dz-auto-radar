#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import unittest

import troostwijk_official_watch as watch


UTC = dt.timezone.utc

LOT_UUID = "8b13abe5-70c0-434a-811e-03881e56d5a6"
DISPLAY_ID = "A1-39129-39"


def lot_payload(
    *,
    lot_uuid: str = LOT_UUID,
    display_id: str = DISPLAY_ID,
    title: str = "2004 BMW serie 645Ci S Passenger car",
    url_slug: str | None = None,
    end: int = 1788024540,
    cents: int | None = 2_000_000,
    country: str = "nl",
    status: str = "BIDDING_OPEN",
) -> dict:
    return {
        "auctionId": "cb479efb-1e21-403b-9340-4c011d405493",
        "biddingStatus": status,
        "bidsCount": 6,
        "currentBidAmount": None if cents is None else {"cents": cents, "currency": "EUR"},
        "displayId": display_id,
        "endDate": end,
        "id": lot_uuid,
        "image": {"alt": None, "url": "https://media.tbauctions.com/image-media/x/file"},
        "itemId": "a9d5fa18-1f1d-4a03-9a2a-f28019ebcc10",
        "location": {"city": "Drachten", "countryCode": country},
        "saleTerm": "GUARANTEED_SALE",
        "startDate": 1787580000,
        "title": title,
        "urlSlug": url_slug if url_slug is not None else f"{title.lower().replace(' ', '-')}-{display_id}",
        "description": None,
        "followersCount": 6,
        "platform": "TWK",
        "directSaleType": "NOT_SET",
    }


def next_data_html(*, results: list, total_size: int, page_size: int = 48,
                   level3: list[str] | None = None) -> str:
    if level3 is None:
        level3 = ["Cars", "Vans", "Oldtimers", "Other vehicles",
                  "Classic cars >15", "Fire Fighting Trucks", "Ambulances"]
    payload = {
        "props": {"pageProps": {
            "lotsData": {"results": results, "totalSize": total_size},
            "pageSize": page_size,
            "initialFilters": [
                {"identifier": "categoryLevel3", "filters": [{"name": n, "count": 1} for n in level3]},
            ],
        }}
    }
    body = json.dumps(payload).replace("</", "<\\/")
    return f"<html><script id=\"__NEXT_DATA__\" type=\"application/json\">{body}</script></html>"


def subcategory_page(*, results: list, total_size: int, page_size: int = 48) -> str:
    return next_data_html(results=results, total_size=total_size, page_size=page_size)


def root_page() -> str:
    return next_data_html(results=[], total_size=10)


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, list[str] | str]) -> None:
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


def page_url(path: str, page: int) -> str:
    return watch.lot_page_url(path, page)


CAR = lot_payload()
CAR_PATH = watch.PASSENGER_SUBCATEGORIES[0][1]
OLDT_PATH = watch.PASSENGER_SUBCATEGORIES[1][1]


class TroostwijkWatchTest(unittest.TestCase):
    def test_parses_lot_public_facts(self) -> None:
        total, size, lots = watch.parse_page(subcategory_page(results=[CAR], total_size=1), context="t")
        self.assertEqual((total, size), (1, 48))
        lot = lots[0]
        self.assertEqual(lot.lot_uuid, LOT_UUID)
        self.assertEqual(lot.display_id, DISPLAY_ID)
        self.assertEqual(lot.current_bid_eur, 20000)
        self.assertEqual(lot.country_code, "NL")
        self.assertEqual(lot.end_utc, dt.datetime.fromtimestamp(1788024540, tz=UTC))

    def test_detail_url_matches_public_double_encoding(self) -> None:
        slug = "audi-a3-sportback-2012-%7C-93-tgg-5-A1-44180-4886"
        lot = watch.Lot(
            lot_uuid=LOT_UUID,
            display_id=DISPLAY_ID,
            url_slug=slug,
            title="x",
            end_utc=dt.datetime(2026, 9, 1, tzinfo=UTC),
            current_bid_eur=100,
            currency="EUR",
            country_code="NL",
            city="",
            image_url="",
            bidding_status="BIDDING_OPEN",
        )
        self.assertEqual(
            watch.lot_detail_url(lot),
            "https://www.troostwijkauctions.com/en/l/audi-a3-sportback-2012-%257C-93-tgg-5-A1-44180-4886",
        )

    def test_missing_bid_stays_unknown_price(self) -> None:
        lot = watch.parse_lot(lot_payload(cents=None), context="t")
        self.assertIsNone(lot.current_bid_eur)
        row = watch.normalize_lot(lot, subcategory="cars", observed_at="2026-08-29T00:00:00+00:00")
        self.assertIsNone(row["price_amount"])
        self.assertEqual(row["price_kind"], "unknown")

    def test_zero_bid_stays_unknown_price(self) -> None:
        lot = watch.parse_lot(lot_payload(cents=0), context="t")
        self.assertIsNone(lot.current_bid_eur)

    def test_non_eur_bid_fails_closed(self) -> None:
        payload = lot_payload()
        payload["currentBidAmount"] = {"cents": 100, "currency": "DKK"}
        with self.assertRaisesRegex(watch.TroostwijkWatchError, "non-EUR"):
            watch.parse_lot(payload, context="t")

    def test_slug_without_display_id_fails_closed(self) -> None:
        with self.assertRaisesRegex(watch.TroostwijkWatchError, "invalid public slug"):
            watch.parse_lot(lot_payload(url_slug="totally-different-slug"), context="t")

    def test_taxonomy_change_fails_closed(self) -> None:
        session = Session({f"{watch.SOURCE_BASE}{watch.CARS_ROOT_PATH}": next_data_html(
            results=[], total_size=0,
            level3=["Cars", "Vans", "Oldtimers", "Other vehicles",
                    "Classic cars >15", "Fire Fighting Trucks", "Ambulances", "New Unknown"],
        )})
        with self.assertRaisesRegex(watch.TroostwijkWatchError, "taxonomy changed"):
            watch.build_watch(session=session, now=dt.datetime(2026, 8, 29, tzinfo=UTC), workers=1)

    def test_two_pass_reconciles_and_excludes_ended_and_non_cars(self) -> None:
        car = lot_payload()
        van = lot_payload(
            lot_uuid="van-uuid-1", display_id="A1-1-1",
            title="Ford Transit bestelwagen 2015", url_slug=f"ford-transit-bestelwagen-A1-1-1",
        )
        ended = lot_payload(
            lot_uuid="ended-uuid", display_id="A1-2-2", end=1_700_000_000,
            url_slug="old-car-A1-2-2",
        )
        oldtimer = lot_payload(
            lot_uuid="oldt-uuid", display_id="A1-3-3",
            title="1959 Peugeot Coupe 203C Classic Car", url_slug="peugeot-coupe-A1-3-3",
        )
        responses: dict[str, list[str] | str] = {
            f"{watch.SOURCE_BASE}{watch.CARS_ROOT_PATH}": [root_page(), root_page()],
            page_url(CAR_PATH, 1): [subcategory_page(results=[car, van, ended], total_size=3)] * 2,
            page_url(OLDT_PATH, 1): [subcategory_page(results=[oldtimer], total_size=1)] * 2,
            page_url(watch.PASSENGER_SUBCATEGORIES[2][1], 1): [subcategory_page(results=[], total_size=0)] * 2,
        }
        payload = watch.build_watch(
            session=Session(responses),
            now=dt.datetime(2026, 8, 29, tzinfo=UTC),
            workers=1,
        )
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 2)
        ids = {row["id"] for row in payload["rows"]}
        self.assertEqual(ids, {f"troostwijk:{LOT_UUID}", "troostwijk:oldt-uuid"})
        self.assertEqual(report["declared"], 4)
        self.assertEqual(report["visited"], 4)
        self.assertEqual(report["source_excluded"], {
            "ended_lot": 1,
            "commercial_or_non_passenger_title": 1,
        })
        self.assertTrue(report["two_pass_verified"])
        row = payload["rows"][0]
        self.assertEqual(row["url"], f"https://www.troostwijkauctions.com/en/l/2004-bmw-serie-645ci-s-passenger-car-{DISPLAY_ID}")
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_amount"], 20000)
        self.assertEqual(row["year"], 2004)
        self.assertEqual(row["category"], "car")
        self.assertEqual(row["country"], "NL")

    def test_cross_category_duplicate_counts_once(self) -> None:
        car = lot_payload()
        classic_path = watch.PASSENGER_SUBCATEGORIES[2][1]
        responses: dict[str, list[str] | str] = {
            f"{watch.SOURCE_BASE}{watch.CARS_ROOT_PATH}": root_page(),
            page_url(CAR_PATH, 1): subcategory_page(results=[car], total_size=1),
            page_url(OLDT_PATH, 1): subcategory_page(results=[], total_size=0),
            page_url(classic_path, 1): subcategory_page(results=[car], total_size=1),
        }
        payload = watch.build_watch(
            session=Session(responses), now=dt.datetime(2026, 8, 29, tzinfo=UTC), workers=1
        )
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(report["source_excluded"], {"cross_category_duplicate": 1})

    def test_lot_ids_changed_between_passes_fails_closed(self) -> None:
        car = lot_payload()
        moved = lot_payload(lot_uuid="new-uuid")
        responses: dict[str, list[str] | str] = {
            f"{watch.SOURCE_BASE}{watch.CARS_ROOT_PATH}": root_page(),
            page_url(CAR_PATH, 1): [subcategory_page(results=[car], total_size=1),
                                    subcategory_page(results=[moved], total_size=1)],
            page_url(OLDT_PATH, 1): subcategory_page(results=[], total_size=0),
            page_url(watch.PASSENGER_SUBCATEGORIES[2][1], 1): subcategory_page(results=[], total_size=0),
        }
        with self.assertRaisesRegex(watch.TroostwijkWatchError, "lot IDs changed"):
            watch.build_watch(
                session=Session(responses), now=dt.datetime(2026, 8, 29, tzinfo=UTC), workers=1,
                snapshot_attempts=1,
            )

    def test_declared_total_changed_between_passes_fails_closed(self) -> None:
        car = lot_payload()
        responses: dict[str, list[str] | str] = {
            f"{watch.SOURCE_BASE}{watch.CARS_ROOT_PATH}": root_page(),
            page_url(CAR_PATH, 1): [subcategory_page(results=[car], total_size=1),
                                    subcategory_page(results=[car], total_size=2)],
            page_url(OLDT_PATH, 1): subcategory_page(results=[], total_size=0),
            page_url(watch.PASSENGER_SUBCATEGORIES[2][1], 1): subcategory_page(results=[], total_size=0),
        }
        with self.assertRaisesRegex(watch.TroostwijkWatchError, "total changed"):
            watch.build_watch(
                session=Session(responses), now=dt.datetime(2026, 8, 29, tzinfo=UTC), workers=1,
                snapshot_attempts=1,
            )

    def test_title_gate_excludes_misfiled_non_cars(self) -> None:
        for title in (
            "Mercedes Sprinter bestelwagen 2019",
            "Fire Fighting Truck MAN 2010",
            "Volkswagen Transporter camper 1991",
            "Piaggio Ape trailer kipper 2005",
            "Honda CB750 motorfiets 1980",
            "Sloep boot 700 1998",
        ):
            lot = watch.parse_lot(
                lot_payload(title=title, url_slug=f"some-lot-{DISPLAY_ID}"), context="t"
            )
            self.assertEqual(
                watch.passenger_exclusion_reason(lot),
                "commercial_or_non_passenger_title",
                msg=title,
            )

    def test_passenger_titles_are_kept(self) -> None:
        for title in (
            "2004 BMW serie 645Ci S Passenger car",
            "Toyota Corolla 1.8 Hybrid 2021",
            "Volkswagen Golf 1.4 TSI 2015",
        ):
            lot = watch.parse_lot(
                lot_payload(title=title, url_slug=f"some-lot-{DISPLAY_ID}"), context="t"
            )
            self.assertEqual(watch.passenger_exclusion_reason(lot), "", msg=title)

    def test_year_and_fuel_inference(self) -> None:
        lot = watch.parse_lot(
            lot_payload(title="Toyota Corolla 1.8 Hybrid 2021", url_slug=f"x-{DISPLAY_ID}"),
            context="t",
        )
        row = watch.normalize_lot(lot, subcategory="cars", observed_at="2026-08-29T00:00:00+00:00")
        self.assertEqual(row["year"], 2021)
        self.assertEqual(row["fuel"], "hybrid")

    def test_asset_country_is_preserved(self) -> None:
        lot = watch.parse_lot(lot_payload(country="be"), context="t")
        row = watch.normalize_lot(lot, subcategory="cars", observed_at="2026-08-29T00:00:00+00:00")
        self.assertEqual(row["country"], "BE")
        self.assertEqual(row["asset_country"], "BE")

    def test_pagination_walks_all_pages(self) -> None:
        page_one = [lot_payload(lot_uuid=f"u-{n}", display_id=f"A1-9-{n}",
                                url_slug=f"car-{n}-A1-9-{n}") for n in range(48)]
        page_two = [lot_payload(lot_uuid="u-48", display_id="A1-9-48",
                                url_slug="car-48-A1-9-48")]
        responses: dict[str, list[str] | str] = {
            f"{watch.SOURCE_BASE}{watch.CARS_ROOT_PATH}": root_page(),
            page_url(CAR_PATH, 1): subcategory_page(results=page_one, total_size=49),
            page_url(CAR_PATH, 2): subcategory_page(results=page_two, total_size=49),
            page_url(OLDT_PATH, 1): subcategory_page(results=[], total_size=0),
            page_url(watch.PASSENGER_SUBCATEGORIES[2][1], 1): subcategory_page(results=[], total_size=0),
        }
        payload = watch.build_watch(
            session=Session(responses), now=dt.datetime(2026, 8, 29, tzinfo=UTC), workers=1
        )
        self.assertEqual(payload["row_count"], 49)
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(report["subcategories"]["cars"]["declared_total"], 49)
        self.assertEqual(report["subcategories"]["cars"]["visited"], 49)


if __name__ == "__main__":
    unittest.main()
