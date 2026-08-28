#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import build_auction_board as board
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


def pagination(*pages: int) -> str:
    links = "".join(
        f'<a href="/pro/vehicle/list?page={page}">{page}</a>' for page in pages
    )
    return f'<nav class="pagination">{links}</nav>'


HOME = '''<button>Search (3 vehicles)</button>
  <a href="/search/sale/SALEA">A descriptive sale Auction 2</a>
  <a href="/search/sale/SALEA">Access the sale</a>
  <a href="/search/sale/SALEB">B descriptive sale Auction 1</a>'''
GLOBAL_PAGE_1 = (
    card(101, "PEUGEOT", "308", 2024, 12000, "vehA")
    + card(103, "RENAULT", "Megane", 2023, 45000, "vehC")
    + pagination(2)
)
GLOBAL_PAGE_2 = (
    card(102, "TOYOTA", "Yaris", 2025, 8000, "vehB") + pagination(1)
)


class Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def get(self, url: str, **_: object) -> Response:
        self.requests.append(url)
        return Response(self.responses.get(url, ""))


class FluctuatingHomeSession(Session):
    def __init__(self, responses: dict[str, str], *, final_total: int) -> None:
        super().__init__(responses)
        self.final_total = final_total
        self.home_requests = 0

    def get(self, url: str, **_: object) -> Response:
        if url == watch.CATALOGUE_URL:
            self.home_requests += 1
            if self.home_requests > 1:
                return Response(
                    self.responses[url].replace(
                        "Search (3 vehicles)",
                        f"Search ({self.final_total} vehicles)",
                    )
                )
        return super().get(url)


class ChangingCatalogueSession(Session):
    def __init__(self, responses: dict[str, str], snapshots: list[dict[str, str]]) -> None:
        super().__init__(responses)
        self.snapshots = snapshots
        self.global_requests = 0

    def get(self, url: str, **_: object) -> Response:
        if url.startswith("https://vpauto.eu/pro/vehicle/list?page="):
            pass_index = min(self.global_requests // 2, len(self.snapshots) - 1)
            self.global_requests += 1
            self.requests.append(url)
            return Response(self.snapshots[pass_index].get(url, ""))
        return super().get(url)


class VPAutoWatchTest(unittest.TestCase):
    def responses(self, *, total: int = 3) -> dict[str, str]:
        home = HOME.replace("Search (3 vehicles)", f"Search ({total} vehicles)")
        return {
            watch.CATALOGUE_URL: home,
            "https://vpauto.eu/pro/vehicle/list?page=1": GLOBAL_PAGE_1,
            "https://vpauto.eu/pro/vehicle/list?page=2": GLOBAL_PAGE_2,
            "https://vpauto.eu/vehicle/vehA/peugeot-101": detail(
                101, "ESSENCE", "2026-08-28T12:00:00+02:00"
            ),
            "https://vpauto.eu/vehicle/vehB/toyota-102": detail(
                102, "HYBRIDE", "2026-08-29T12:00:00+02:00"
            ),
            "https://vpauto.eu/vehicle/vehC/renault-103": detail(
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
        self.assertEqual(payload["source_reports"]["vpauto"]["visited_listing_pages"], 2)
        self.assertEqual(payload["source_reports"]["vpauto"]["reconciliation_attempts"], 2)
        self.assertTrue(all(row["adapter_authorized"] for row in rows.values()))
        self.assertEqual(rows["vpauto:101:vehA"]["fuel"], "petrol")
        self.assertEqual(rows["vpauto:102:vehB"]["fuel"], "hybrid")
        self.assertEqual(rows["vpauto:103:vehC"]["fuel"], "diesel")
        self.assertEqual(rows["vpauto:101:vehA"]["year"], 2024)
        self.assertEqual(rows["vpauto:101:vehA"]["mileage_km"], 12000)
        self.assertEqual(rows["vpauto:101:vehA"]["canonical_end_utc"], "2026-08-28T12:00:00+02:00")
        self.assertIsNone(rows["vpauto:101:vehA"]["price_amount"])
        self.assertEqual(rows["vpauto:101:vehA"]["price_kind"], "unknown")
        self.assertEqual(rows["vpauto:101:vehA"]["source_vehicle_id"], "101")
        self.assertEqual(rows["vpauto:101:vehA"]["source_offer_id"], "vehA")
        normalized, reason = board._normalize_monitored_row(
            rows["vpauto:101:vehA"], generated_at=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC)
        )
        self.assertEqual(reason, "")
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["id"], "vpauto:101:vehA")

    def test_declared_total_mismatch_fails_closed(self) -> None:
        with self.assertRaises(watch.VPAutoWatchError):
            watch.build_watch(
                session=Session(self.responses(total=4)),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
                fetch_details=False,
            )

    def test_global_catalogue_is_seeded_and_never_uses_sale_filter(self) -> None:
        session = Session(self.responses())
        payload = watch.build_watch(
            session=session,
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
            fetch_details=False,
        )
        report = payload["source_reports"]["vpauto"]
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(report["catalogue_count_delta"], 0)
        self.assertEqual(report["count_reconciliation"], "exact")
        self.assertEqual(session.requests[0], watch.CATALOGUE_URL)
        self.assertTrue(any(url.endswith("?page=1") for url in session.requests))
        self.assertFalse(any("sale=" in url for url in session.requests))

    def test_lagging_landing_total_does_not_stop_at_a_page_boundary(self) -> None:
        payload = watch.build_watch(
            session=Session(self.responses(total=2)),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
            fetch_details=False,
        )
        report = payload["source_reports"]["vpauto"]
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(report["catalogue_count_delta"], 1)
        self.assertEqual(report["count_reconciliation"], "landing_total_lags_unique_cards")

    def test_lagging_landing_total_retains_surplus_within_terminal_page(self) -> None:
        responses = self.responses(total=2)
        responses["https://vpauto.eu/pro/vehicle/list?page=1"] = (
            card(101, "PEUGEOT", "308", 2024, 12000, "offerA")
            + card(102, "TOYOTA", "Yaris", 2025, 8000, "offerB")
            + card(103, "RENAULT", "Megane", 2023, 45000, "offerC")
        )
        payload = watch.build_watch(
            session=Session(responses),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
            fetch_details=False,
        )
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(payload["source_reports"]["vpauto"]["catalogue_count_delta"], 1)

    def test_rechecked_landing_total_can_change_without_losing_cards(self) -> None:
        payload = watch.build_watch(
            session=FluctuatingHomeSession(self.responses(), final_total=4),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
            fetch_details=False,
        )
        report = payload["source_reports"]["vpauto"]
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(report["declared"], 3)
        self.assertEqual(report["declared_rechecked"], 4)
        self.assertTrue(report["declared_total_changed_during_scan"])

    def test_one_vehicle_can_have_two_distinct_public_offers(self) -> None:
        responses = self.responses()
        responses["https://vpauto.eu/pro/vehicle/list?page=1"] = (
            card(101, "PEUGEOT", "308", 2024, 12000, "offerA")
            + card(101, "PEUGEOT", "308", 2024, 12000, "offerB")
            + pagination(2)
        )
        responses["https://vpauto.eu/pro/vehicle/list?page=2"] = (
            card(102, "TOYOTA", "Yaris", 2025, 8000, "offerC") + pagination(1)
        )
        payload = watch.build_watch(
            session=Session(responses),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            workers=1,
            fetch_details=False,
        )
        self.assertEqual(payload["row_count"], 3)
        self.assertEqual(
            [row["id"] for row in payload["rows"]],
            ["vpauto:101:offerA", "vpauto:101:offerB", "vpauto:102:offerC"],
        )
        report = payload["source_reports"]["vpauto"]
        self.assertEqual(report["physical_vehicle_ids"], 2)
        self.assertEqual(report["cross_listed_offer_rows"], 1)

    def test_reused_offer_token_fails_closed(self) -> None:
        responses = self.responses(total=2)
        responses["https://vpauto.eu/pro/vehicle/list?page=1"] = (
            card(101, "PEUGEOT", "308", 2024, 12000, "sameToken")
            + card(102, "TOYOTA", "Yaris", 2025, 8000, "sameToken")
        )
        with self.assertRaisesRegex(watch.VPAutoWatchError, "did not stabilize"):
            watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
                fetch_details=False,
            )

    def test_same_offer_repeated_across_pages_fails_closed(self) -> None:
        responses = self.responses()
        responses["https://vpauto.eu/pro/vehicle/list?page=1"] = (
            card(101, "PEUGEOT", "308", 2024, 12000, "offerA")
            + card(102, "TOYOTA", "Yaris", 2025, 8000, "offerB")
            + pagination(2)
        )
        responses["https://vpauto.eu/pro/vehicle/list?page=2"] = (
            card(102, "TOYOTA", "Yaris", 2025, 8000, "offerB")
            + card(103, "RENAULT", "Megane", 2023, 45000, "offerC")
            + pagination(1)
        )
        with self.assertRaisesRegex(watch.VPAutoWatchError, "did not stabilize"):
            watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
                fetch_details=False,
            )

    def test_invalid_offer_token_fails_closed(self) -> None:
        responses = self.responses(total=1)
        responses["https://vpauto.eu/pro/vehicle/list?page=1"] = card(
            101, "PEUGEOT", "308", 2024, 12000, "bad-token"
        )
        with self.assertRaisesRegex(watch.VPAutoWatchError, "offer token"):
            watch.build_watch(
                session=Session(responses),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
                fetch_details=False,
            )

    def test_complete_but_changing_global_passes_fail_closed(self) -> None:
        base = self.responses()
        first = {
            "https://vpauto.eu/pro/vehicle/list?page=1": GLOBAL_PAGE_1,
            "https://vpauto.eu/pro/vehicle/list?page=2": GLOBAL_PAGE_2,
        }
        second = {
            "https://vpauto.eu/pro/vehicle/list?page=1": (
                card(101, "PEUGEOT", "308", 2024, 12000, "changedA")
                + card(103, "RENAULT", "Megane", 2023, 45000, "changedC")
                + pagination(2)
            ),
            "https://vpauto.eu/pro/vehicle/list?page=2": (
                card(102, "TOYOTA", "Yaris", 2025, 8000, "changedB") + pagination(1)
            ),
        }
        snapshots = [first, second] * (watch.MAX_RECONCILIATION_ATTEMPTS // 2)
        with self.assertRaisesRegex(watch.VPAutoWatchError, "did not stabilize"):
            watch.build_watch(
                session=ChangingCatalogueSession(base, snapshots),
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
                workers=1,
                fetch_details=False,
            )

    def test_cli_uses_the_current_sale_route_report_key(self) -> None:
        payload = {
            "schema_version": 1,
            "lane": "official_auction_watch",
            "generated_at_utc": "2026-08-27T20:00:00+00:00",
            "research_only": True,
            "publication_status": "review_required",
            "row_count": 0,
            "rows": [],
            "source_reports": {
                "vpauto": {
                    "sale_routes_at_start": 2,
                    "visited_listing_pages": 2,
                    "detail_pages_ok": 0,
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "watch.json"
            stdout = io.StringIO()
            with (
                mock.patch.object(watch, "build_watch", return_value=payload),
                mock.patch.object(sys, "argv", ["vpauto_official_watch.py", "--out", str(output)]),
                redirect_stdout(stdout),
            ):
                self.assertEqual(watch.main(), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
            self.assertEqual(json.loads(stdout.getvalue())["sale_routes"], 2)


if __name__ == "__main__":
    unittest.main()
