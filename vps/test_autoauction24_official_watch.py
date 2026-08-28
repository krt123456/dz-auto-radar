#!/usr/bin/env python3
from __future__ import annotations

import copy
import datetime as dt
import unittest

import autoauction24_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def car(
    car_id: int,
    *,
    make: str = "BMW",
    model: str = "X3 xDrive",
    body: str = "SUV/Offroader",
    fuel: str = "Diesel",
    current_bid: int = 1200,
    minimum: int = 8000,
    end: str = "2026-08-30T18:00:00+00:00",
) -> dict:
    return {
        "id": car_id,
        "eventid": 208,
        "car_name": make,
        "carmodel": model,
        "registercartype": "Automatic",
        "body": body,
        "fuel": fuel,
        "first_reg": "05/2023",
        "mileage": 12345,
        "currentbidprice": current_bid,
        "minimumprice": minimum,
        "number_of_seats": 5,
        "auction_end_time": end,
        "transport_by": "AG",
        "otherdescription": "",
    }


def document(*cars: dict) -> list[dict]:
    return [{"id": 208, "complete": False, "cars": list(cars)}]


class Response:
    def __init__(self, value: object) -> None:
        self.value = value

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.value


class Session:
    def __init__(self, values: list[object]) -> None:
        self.values = list(values)

    def get(self, _url: str, **_kwargs: object) -> Response:
        if not self.values:
            raise AssertionError("unexpected AutoAuction24 request")
        return Response(self.values.pop(0))


class AutoAuction24WatchTest(unittest.TestCase):
    def test_reconciles_every_api_car_and_uses_latest_bid(self) -> None:
        first = document(
            car(1),
            car(2, make="Tesla", model="Model 3", body="Limousine", fuel="Electric"),
            car(3, make="VW", model="Transporter", body="Van"),
        )
        second = copy.deepcopy(first)
        second[0]["cars"][0]["currentbidprice"] = 1450
        payload = watch.build_watch(session=Session([first, second]), now=NOW)
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(report["declared"], 3)
        self.assertEqual(report["visited"], 3)
        self.assertEqual(report["source_excluded"], {"body_not_passenger_car": 1})
        self.assertTrue(report["two_pass_membership_verified"])
        rows = {row["id"]: row for row in payload["rows"]}
        self.assertEqual(rows["autoauction24-ch:1"]["price_amount"], 1450)
        self.assertEqual(rows["autoauction24-ch:2"]["fuel"], "electric")
        self.assertTrue(all(row["category"] == "car" for row in rows.values()))
        self.assertTrue(all(row["country"] == "CH" for row in rows.values()))

    def test_detects_membership_change_between_passes(self) -> None:
        first = document(car(1), car(2))
        second = document(car(1), car(3))
        with self.assertRaises(watch.AutoAuction24SnapshotChanged):
            watch.build_watch(session=Session([first, second]), now=NOW)

    def test_rejects_duplicate_car_ids(self) -> None:
        duplicate = document(car(1), car(1, make="Audi"))
        with self.assertRaises(watch.AutoAuction24WatchError):
            watch.build_watch(session=Session([duplicate]), now=NOW)

    def test_rejects_non_car_text_even_with_passenger_body(self) -> None:
        truck = car(1, make="MAN", model="Truck", body="SUV/Offroader")
        snapshot = watch.parse_snapshot(document(truck), now=NOW, observed_at=NOW.isoformat())
        self.assertEqual(snapshot.rows, [])
        self.assertEqual(snapshot.exclusions, {"explicit_non_car_text": 1})


if __name__ == "__main__":
    unittest.main()
