#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import vpauto_official_watch as watch


UTC = dt.timezone.utc


def card(vehicle_id: int, brand: str, model: str, year: int, mileage: int, slug: str) -> str:
    return f'''<article class="element" data-vehicle-etincelle-id="{vehicle_id}">
      <a href="/vehicle/{slug}/{brand.lower()}-{vehicle_id}">
        <div class="elmt-marque"><h2>{brand}</h2></div>
        <span class="elmt-ville">LOC: 76</span>
        <div class="elmt-modele"><h3>{model}</h3>
        <div><span>{year}</span> - <span>{mileage} Km</span></div></div>
      </a></article>'''


def detail(vehicle_id: int, energy: str, end: str) -> str:
    return (
        '<script>var offer = {"viewItem": "'
        + str(vehicle_id)
        + '", "energy": "'
        + energy
        + '", "sale_end_date_complete": "'
        + end
        + '"};</script>'
    )


HOME = '''<button>Search (3 vehicles)</button>
  <a href="/search/sale/SALEA">A descriptive sale Auction 2</a>
  <a href="/search/sale/SALEA">Access the sale</a>
  <a href="/search/sale/SALEB">B descriptive sale Auction 1</a>'''
SALE_A = card(101, "PEUGEOT", "308", 2024, 12000, "veh-a") + (
    '<nav class="pagination"><a href="/pro/vehicle/list?sale=SALEA&amp;page=2">2</a></nav>'
)
SALE_A_PAGE_2 = card(102, "TOYOTA", "Yaris", 2025, 8000, "veh-b") + (
    '<nav class="pagination"><a href="/pro/vehicle/list?sale=SALEA&amp;page=2">2</a></nav>'
)
SALE_B = card(103, "RENAULT", "Megane", 2023, 45000, "veh-c")


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


class VPAutoWatchTest(unittest.TestCase):
    def responses(self, *, total: int = 3) -> dict[str, str]:
        home = HOME.replace("Search (3 vehicles)", f"Search ({total} vehicles)")
        return {
            watch.CATALOGUE_URL: home,
            "https://vpauto.eu/search/sale/SALEA": SALE_A,
            "https://vpauto.eu/search/sale/SALEB": SALE_B,
            "https://vpauto.eu/pro/vehicle/list?sale=SALEA&page=2": SALE_A_PAGE_2,
            "https://vpauto.eu/vehicle/veh-a/peugeot-101": detail(
                101, "ESSENCE", "2026-08-28T12:00:00+02:00"
            ),
            "https://vpauto.eu/vehicle/veh-b/toyota-102": detail(
                102, "HYBRIDE", "2026-08-29T12:00:00+02:00"
            ),
            "https://vpauto.eu/vehicle/veh-c/renault-103": detail(
                103, "DIESEL", "2026-08-30T12:00:00+02:00"
            ),
        }

    def test_complete_catalogue_is_reconciled_and_detail_fields_are_explicit(self) -> None:
        payload = watch.build_watch(
            session=Session(self.responses()),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
        )
        rows = {row["id"]: row for row in payload["rows"]}
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["source_reports"]["vpauto"]["declared"], 3)
        self.assertEqual(payload["source_reports"]["vpauto"]["visited_listing_pages"], 3)
        self.assertTrue(all(row["adapter_authorized"] for row in rows.values()))
        self.assertEqual(rows["vpauto:101"]["fuel"], "petrol")
        self.assertEqual(rows["vpauto:102"]["fuel"], "hybrid")
        self.assertEqual(rows["vpauto:103"]["fuel"], "diesel")
        self.assertEqual(rows["vpauto:101"]["year"], 2024)
        self.assertEqual(rows["vpauto:101"]["mileage_km"], 12000)
        self.assertEqual(rows["vpauto:101"]["canonical_end_utc"], "2026-08-28T12:00:00+02:00")
        self.assertIsNone(rows["vpauto:101"]["price_amount"])
        self.assertEqual(rows["vpauto:101"]["price_kind"], "unknown")

    def test_declared_total_mismatch_fails_closed(self) -> None:
        with self.assertRaises(watch.VPAutoWatchError):
            watch.build_watch(
                session=Session(self.responses(total=4)),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
                fetch_details=False,
            )

    def test_one_card_surplus_is_retained_when_the_landing_total_lags(self) -> None:
        payload = watch.build_watch(
            session=Session(self.responses(total=2)),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
            fetch_details=False,
        )
        report = payload["source_reports"]["vpauto"]
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(report["catalogue_count_delta"], 1)
        self.assertEqual(
            report["count_reconciliation"],
            "landing_total_lags_unique_cards_by_one",
        )


if __name__ == "__main__":
    unittest.main()
