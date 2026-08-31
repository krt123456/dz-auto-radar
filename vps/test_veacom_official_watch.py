#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import veacom_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
START = "2026-08-30T10:00:00+02:00"
END = "2026-08-31T12:00:00+02:00"


def catalogue_card(
    listing_id: int,
    title: str,
    *,
    price: int = 5300,
    year: str = "2021",
    mileage: str = "12 345 km",
    fuel: str = "Benzín",
) -> str:
    return f'''<div class="auction-row">
      <img src="/image/{listing_id}.jpg" />
      <a href="/cs/vehicle/detail/{listing_id}"><span class="name-ar">{title}</span></a>
      <div class="vehicle-info-ar">
        <div><span class="label-ar">Rok výroby</span> {year}</div>
        <div><span class="label-ar">Najeto</span> {mileage}</div>
        <div><span class="label-ar">Palivo</span> {fuel}</div>
      </div>
      <div class="price-ar"><span>{price:,}</span> CZK</div>
    </div>'''


def catalogue(*cards: str) -> str:
    return f'''<html><script type="application/ld+json">{{
      "@context":"https://schema.org", "@type":"Event",
      "startDate":"{START}", "endDate":"{END}",
      "location":{{"url":"https://www.veacom.cz/cs"+"/homepage/upcoming-auction"}}
    }}</script>{''.join(cards)}</html>'''


def detail(
    title: str,
    *,
    registration: str = "3/2021",
    mileage: str = "12 345 km",
    seats: str = "5",
    fuel: str = "Benzín",
    mass: str = "2 000 kg",
    product_url: str | None = None,
) -> str:
    product = ""
    if product_url:
        product = f'''<script type="application/ld+json">{{
          "@context":"https://schema.org/", "@type":"Product", "name":"{title}",
          "image":"https://images.example/{title}.jpg", "offers":{{"url":"{product_url}"}}
        }}</script>'''
    return f'''<html>{product}<div class="justify-row"><div class="bold-500">Jméno</div><div>{title}</div></div>
      <div class="justify-row"><div class="bold-500">První registrace</div><div>{registration}</div></div>
      <div class="justify-row"><div class="bold-500">Najeto</div><div>{mileage}</div></div>
      <div class="justify-row"><div class="bold-500">Palivo</div><div>{fuel}</div></div>
      <div class="justify-row"><div class="bold-500">Počet sedadel</div><div>{seats}</div></div>
      <div class="justify-row"><div class="bold-500">Nejvyšší povolená hmotnost</div><div>{mass}</div></div></html>'''


class Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


class Session:
    def __init__(self, catalogues: list[str], details: dict[str, str]) -> None:
        self.catalogues = catalogues
        self.details = details
        self.catalogue_calls = 0

    def get(self, url: str, **_: object) -> Response:
        if url == watch.CATALOGUE_URL:
            value = self.catalogues[min(self.catalogue_calls, len(self.catalogues) - 1)]
            self.catalogue_calls += 1
            return Response(value)
        if url not in self.details:
            raise AssertionError(url)
        return Response(self.details[url])


class VeacomWatchTest(unittest.TestCase):
    def test_two_public_index_passes_classify_every_listing(self) -> None:
        audi = catalogue_card(10, "AUDI A6 50 TDI")
        excavator = catalogue_card(20, "KOMATSU EXCAVATOR", mileage="5 000 MTH")
        source = catalogue(audi, excavator)
        session = Session(
            [source],
            {
                f"{watch.ROOT_URL}/cs/vehicle/detail/10": detail(
                    "AUDI A6 50 TDI",
                    product_url=f"{watch.ROOT_URL}/cs/vehicle/detail/10",
                ),
                f"{watch.ROOT_URL}/cs/vehicle/detail/20": detail(
                    "KOMATSU EXCAVATOR", mileage="5 000 MTH", seats="1"
                ),
            },
        )
        payload = watch.build_watch(session=session, now=NOW, timeout=5, workers=1, fx_rates={"CZK": (25.0, "explicit")})
        self.assertEqual(payload["row_count"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["id"], "veacom:10")
        self.assertEqual(row["category"], "car")
        self.assertEqual(row["fuel"], "petrol")
        self.assertEqual(row["price_amount"], 212)
        self.assertEqual(row["price_currency"], "EUR")
        self.assertEqual(row["price_eur"], 212)
        self.assertEqual(row["canonical_end_utc"], "2026-08-31T10:00:00+00:00")
        report = payload["source_reports"]["veacom"]
        self.assertEqual(report["declared"], 2)
        self.assertEqual(report["detail_pages_checked"], 2)
        self.assertEqual(report["source_excluded"], {"explicit_non_car_text": 1})
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_commercial_vehicle_is_rejected_at_source(self) -> None:
        listing = catalogue_card(11, "FORD TRANSIT CUSTOM 2.2 TDCI")
        source = catalogue(listing)
        session = Session(
            [source],
            {f"{watch.ROOT_URL}/cs/vehicle/detail/11": detail("FORD TRANSIT CUSTOM 2.2 TDCI")},
        )
        payload = watch.build_watch(session=session, now=NOW, timeout=5, workers=1, fx_rates={"CZK": (25.0, "explicit")})
        self.assertEqual(payload["row_count"], 0)
        self.assertEqual(
            payload["source_reports"]["veacom"]["source_excluded"], {"commercial_model": 1}
        )

    def test_two_seat_known_passenger_car_is_retained(self) -> None:
        listing = catalogue_card(12, "TESLA ROADSTER")
        source = catalogue(listing)
        session = Session(
            [source],
            {f"{watch.ROOT_URL}/cs/vehicle/detail/12": detail("TESLA ROADSTER", seats="2")},
        )
        payload = watch.build_watch(session=session, now=NOW, timeout=5, workers=1, fx_rates={"CZK": (25.0, "explicit")})
        self.assertEqual(payload["row_count"], 1)

    def test_heavy_truck_model_and_mass_are_rejected(self) -> None:
        truck_reason = watch.passenger_car_reason(
            title="MERCEDES-BENZ 2638 6X4",
            fields={
                "počet sedadel": "3", "najeto": "337 732 km",
                "první registrace": "1996", "nejvyšší povolená hmotnost": "26 000 kg",
            },
            now=NOW,
        )
        self.assertEqual(truck_reason, "heavy_truck_model")
        mass_reason = watch.passenger_car_reason(
            title="TESLA MODEL S",
            fields={
                "počet sedadel": "5", "najeto": "12 345 km",
                "první registrace": "2021", "nejvyšší povolená hmotnost": "4 000 kg",
            },
            now=NOW,
        )
        self.assertEqual(mass_reason, "heavy_vehicle_mass")

    def test_changed_second_index_pass_fails_closed(self) -> None:
        first = catalogue(catalogue_card(10, "AUDI A6 50 TDI"))
        changed = catalogue(catalogue_card(11, "AUDI A6 50 TDI"))
        session = Session([first, changed], {})
        with self.assertRaisesRegex(watch.VeacomWatchError, "final reconciliation"):
            watch.build_watch(session=session, now=NOW, timeout=5, workers=1, fx_rates={"CZK": (25.0, "explicit")})

    def test_ambiguous_two_seat_listing_is_rejected(self) -> None:
        reason = watch.passenger_car_reason(
            title="Unknown Road Vehicle",
            fields={"počet sedadel": "2", "najeto": "1 000 km", "první registrace": "2021"},
            now=NOW,
        )
        self.assertEqual(reason, "two_seats_without_passenger_make")


if __name__ == "__main__":
    unittest.main()
