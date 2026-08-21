#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import be_pl_pt_official_watch as module


UTC = dt.timezone.utc


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        content: bytes | None = None,
        payload=None,
        status: int = 200,
        url: str = "",
    ):
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.payload = payload
        self.status_code = status
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self.payload is not None:
            return self.payload
        return json.loads(self.text)


class FinShopTests(unittest.TestCase):
    def test_sealed_catalogue_filters_two_wheel_product_and_never_creates_price(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        index = """
        <a href='/event/vente-de-vehicules-du-26-08-2026-972/register'>vehicles</a>
        <a href='/event/vente-de-mobilier-976/register'>furniture</a>
        """
        event = """
        <html><head><title>Vente de véhicules du 26/08/2026 | Fin Shop</title></head>
        <body><h3><span>26 août 2026</span><br/><span>10:00</span></h3>
        Vente par soumission par mail
        <img src='/web/image/product.template/64929/image_1024/car'
             alt='Réf. 3-45585 - PEUGEOT 208 - 2021 - 37.280 KM'/>
        <img src='/web/image/product.template/63214/image_1024/bike'
             alt='Réf. 2-45008 - SYM JET 14 - 2022 - 12.404 KM'/>
        </body></html>
        """

        class Session:
            def get(self, url, headers, timeout):
                if url == module.FINSHOP_INDEX_URL:
                    return FakeResponse(text=index, url=url)
                return FakeResponse(text=event, url=url)

        rows, report = module.harvest_finshop(Session(), now=now, timeout=5)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_key"], "finshop")
        self.assertEqual(row["price_kind"], "sealed_bid")
        self.assertIsNone(row["price_amount"])
        self.assertIsNone(row["price_eur"])
        self.assertEqual(row["mileage_km"], 37_280)
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertEqual(row["canonical_end_utc"], "2026-08-26T08:00:00+00:00")
        self.assertEqual(row["last_seen_at"], now.isoformat())
        self.assertEqual(report["catalogue_products"], 2)
        self.assertEqual(report["excluded_non_car_or_truck"], 1)

    def test_professional_only_event_is_explicitly_not_eligible(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        row = module.finshop_product_to_row(
            product_id="64922",
            raw_title="[BXL-X] Lot 26_1484 - Peugeot 308",
            event_url="https://finshop.belgium.be/event/vehicle-994/register",
            event_title="Vente réservée aux professionnels de l'automobile",
            sale_at=dt.datetime(2026, 8, 27, 8, 0, tzinfo=UTC),
            now=now,
            professional_only=True,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertIn("professionals", row["eligibility_reason"])


class PolandTests(unittest.TestCase):
    @staticmethod
    def nuxt_markup() -> str:
        # A minimal devalue/Nuxt reference graph matching the official payload.
        data = [
            {"items": 1, "count": 2, "aggregations": 3},
            [4],
            1,
            [],
            {
                "id": 5, "title": 6, "openingValue": 7,
                "startAuctionAt": 8, "endAuctionAt": 9, "status": 10,
                "subCategory": 11, "eauction": 12, "joinable": 12,
                "estimate": 13, "province": 14,
            },
            80055,
            "Samochód FORD FOCUS 1.6 Diesel, rok produkcji 2004, Przebieg 220.656 km",
            4500,
            "2026-08-24T09:30:00+02:00",
            "2026-08-24T23:59:00+02:00",
            "CREATED",
            "CARS",
            True,
            6000,
            "wielkopolskie",
        ]
        return (
            '<script type="application/json" id="__NUXT_DATA__" data-nuxt-data="nuxt-app">'
            + json.dumps(data, ensure_ascii=False)
            + '</script><a href="/licytacje/80055/ford-focus">row</a>'
        )

    def test_nuxt_parser_and_opening_price_semantics(self):
        items, count, links = module.parse_poland_search_page(
            self.nuxt_markup(), category="CARS"
        )
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["id"], 80055)
        self.assertEqual(links[80055], "https://licytacje.komornik.pl/licytacje/80055/ford-focus")

        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        row = module.poland_item_to_row(
            items[0], category="CARS", detail_url=links[80055],
            now=now, pln_rate=4.25,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["price_kind"], "starting_bid")
        self.assertEqual(row["price_amount"], 4500)
        self.assertEqual(row["price_eur"], 1058.82)
        self.assertEqual(row["fuel"], "diesel")
        self.assertEqual(row["mileage_km"], 220_656)
        self.assertEqual(row["eligibility_status"], "not_eligible")
        self.assertNotEqual(row["price_kind"], "current_bid")

    def test_ended_polish_row_is_dropped(self):
        items, _, links = module.parse_poland_search_page(self.nuxt_markup(), category="CARS")
        row = module.poland_item_to_row(
            items[0], category="CARS", detail_url=links[80055],
            now=dt.datetime(2026, 8, 25, 0, 0, tzinfo=UTC), pln_rate=4.25,
        )
        self.assertIsNone(row)

    def test_poland_page_retries_ssl_and_connection_errors_then_succeeds(self):
        failures = [
            requests.exceptions.SSLError("temporary TLS close"),
            requests.exceptions.ConnectionError("connection reset"),
            requests.exceptions.ConnectionError("connection reset again"),
        ]

        class Session:
            calls = 0

            def get(self, url, headers, timeout):
                self.calls += 1
                if failures:
                    raise failures.pop(0)
                return FakeResponse(text=PolandTests.nuxt_markup(), url=url)

        session = Session()
        with mock.patch.object(module.time, "sleep") as sleep:
            items, links, advertised, pages = module.fetch_poland_category(
                session, category="CARS", timeout=5,
            )

        self.assertEqual(session.calls, 4)
        self.assertEqual([item["id"] for item in items], [80055])
        self.assertIn(80055, links)
        self.assertEqual(advertised, 1)
        self.assertEqual(pages, 1)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.2, 0.4, 0.8],
        )


class ELeiloesTests(unittest.TestCase):
    @staticmethod
    def item(
        item_id: int,
        *,
        modality: int = 1,
        subtype: int = 9,
        current: float = 1250,
        minimum: float = 1000,
        title: str = "Renault Clio gasolina de 2024",
    ):
        return {
            "id": item_id,
            "origem": item_id + 1000,
            "lanceAtual": current,
            "dataInicio": "2026-08-20T10:00:00",
            "dataFim": "2026-08-25T14:30:00",
            "cancelado": False,
            "modalidadeId": modality,
            "referencia": ("LO" if modality == 1 else "NP") + str(item_id),
            "tipoId": 2,
            "subtipoId": subtype,
            "titulo": title,
            "valorBase": 1200,
            "valorMinimo": minimum,
            "iniciado": True,
            "terminado": False,
            "moradaDistrito": "Lisboa",
            "moradaConcelho": "Lisboa",
        }

    def test_online_auction_uses_only_positive_public_current_bid(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        row = module.e_leiloes_item_to_row(self.item(101), now=now)
        self.assertIsNotNone(row)
        self.assertEqual(row["price_kind"], "current_bid")
        self.assertEqual(row["price_eur"], 1250)
        self.assertEqual(row["canonical_end_utc"], "2026-08-25T13:30:00+00:00")
        self.assertEqual(row["url"], "https://www.e-leiloes.pt/evento/LO101")
        self.assertEqual(row["eligibility_status"], "unknown")
        self.assertIn("Portuguese NIF", row["eligibility_reason"])
        self.assertIn("IBAN", row["eligibility_reason"])

    def test_online_auction_without_bid_uses_minimum_value(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        row = module.e_leiloes_item_to_row(
            self.item(102, current=0, minimum=850), now=now,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["price_kind"], "minimum_bid")
        self.assertEqual(row["price_eur"], 850)
        self.assertIn("sem lance atual", row["price_label"])

    def test_private_negotiation_never_relabels_highest_offer_as_auction_bid(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        row = module.e_leiloes_item_to_row(
            self.item(103, modality=2, current=4300, minimum=5000), now=now,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["price_kind"], "minimum_bid")
        self.assertEqual(row["price_eur"], 5000)
        self.assertEqual(row["official_highest_offer_eur"], 4300)
        self.assertIn("not_relabelled", row["bid_visibility"])

    def test_motorcycles_boats_and_tractors_are_not_car_radar_rows(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        for subtype in (13, 14, 29):
            with self.subTest(subtype=subtype):
                self.assertIsNone(module.e_leiloes_item_to_row(
                    self.item(200 + subtype, subtype=subtype), now=now,
                ))

    def test_catalogue_paginates_all_rows_with_stable_id_sort_and_filter(self):
        items = [self.item(item_id) for item_id in range(1, 15)]

        class Session:
            calls = []

            def get(self, url, *, headers, params, timeout, verify):
                self.calls.append((url, params, verify))
                table = json.loads(params["tableParams"])
                assert table["sortField"] == "id"
                assert table["filters"]["tipo"]["value"] == 2
                first = table["first"]
                page = items[first:first + module.E_LEILOES_PAGE_SIZE]
                return FakeResponse(
                    payload={
                        "list": page,
                        "pagination": {
                            "first": first,
                            "rows": module.E_LEILOES_PAGE_SIZE,
                            "total": len(items),
                        },
                        "errors": False,
                        "exception": False,
                    },
                    url=url,
                )

        session = Session()
        fetched, report = module.fetch_e_leiloes_catalogue(
            session, timeout=5, verify="/tmp/verified-ca.pem",
        )
        self.assertEqual(len(fetched), 14)
        self.assertEqual([row["id"] for row in fetched], list(range(1, 15)))
        self.assertEqual(report["pages"], 2)
        self.assertEqual(report["catalogue_total"], 14)
        self.assertTrue(all(call[2] != False for call in session.calls))

    def test_ca_bundle_combines_normal_roots_and_intermediate_then_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "roots.pem"
            intermediate = Path(directory) / "intermediate.pem"
            root.write_text(
                "-----BEGIN CERTIFICATE-----\nROOT\n-----END CERTIFICATE-----\n",
                encoding="ascii",
            )
            intermediate.write_text(
                "-----BEGIN CERTIFICATE-----\nINTERMEDIATE\n-----END CERTIFICATE-----\n",
                encoding="ascii",
            )
            with mock.patch.object(module.requests.certs, "where", return_value=str(root)):
                with module.e_leiloes_verify_bundle(intermediate) as bundle:
                    bundle_path = Path(bundle)
                    body = bundle_path.read_text(encoding="ascii")
                    self.assertIn("ROOT", body)
                    self.assertIn("INTERMEDIATE", body)
                    self.assertTrue(bundle_path.exists())
                self.assertFalse(bundle_path.exists())


class CombinedTests(unittest.TestCase):
    @staticmethod
    def row(source: str, row_id: str, *, seen: str, end: str = "2026-08-22T18:00:00+00:00"):
        return {
            "id": row_id,
            "source": source,
            "source_key": source,
            "last_seen_at": seen,
            "canonical_end_utc": end,
        }

    @staticmethod
    def payload(*, generated: str, rows, reports):
        return {
            "schema_version": 1,
            "lane": "official_auction_watch",
            "generated_at_utc": generated,
            "row_count": len(rows),
            "rows": rows,
            "source_reports": reports,
        }

    def test_blocked_sources_are_reported_as_zero_without_fabrication(self):
        class Session:
            def get(self, url, **kwargs):
                raise requests.ConnectTimeout("geo/WAF blocked")

        with mock.patch.object(
            module,
            "e_leiloes_verify_bundle",
            return_value=contextlib.nullcontext("verified-ca.pem"),
        ):
            payload = module.build_watch(
                Session(), sources=("e-leiloes",),
                now=dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC), timeout=3,
            )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["lane"], "official_auction_watch")
        self.assertEqual(payload["row_count"], 0)
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["source_reports"]["e-leiloes"]["status"], "error")
        self.assertIn("ConnectTimeout", payload["source_reports"]["e-leiloes"]["error"])

    def test_atomic_json_write(self):
        payload = {
            "schema_version": 1, "lane": "official_auction_watch",
            "generated_at_utc": "2026-08-21T12:00:00+00:00",
            "row_count": 0, "rows": [], "source_reports": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            module.write_payload(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_fallback_retains_only_the_failed_source_and_preserves_timestamp(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        seen = "2026-08-21T10:30:00+00:00"
        previous_rows = [
            self.row("finshop", "finshop:old", seen=seen),
            self.row("licytacje-komornik", "licytacje-komornik:old", seen=seen),
        ]
        previous = self.payload(
            generated="2026-08-21T10:30:00+00:00",
            rows=previous_rows,
            reports={
                "finshop": {"status": "ok", "current_or_future_rows": 1},
                "licytacje-komornik": {"status": "ok", "current_or_future_rows": 1},
            },
        )
        new_poland = self.row(
            "licytacje-komornik", "licytacje-komornik:new",
            seen=now.isoformat(),
        )
        current = self.payload(
            generated=now.isoformat(),
            rows=[new_poland],
            reports={
                "finshop": {
                    "status": "error", "current_or_future_rows": 0,
                    "error": "ConnectTimeout: upstream unavailable",
                },
                "licytacje-komornik": {"status": "ok", "current_or_future_rows": 1},
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            path.write_text(json.dumps(previous), encoding="utf-8")
            result = module.apply_previous_snapshot_fallback(current, path, now=now)

        self.assertEqual(
            {row["id"] for row in result["rows"]},
            {"finshop:old", "licytacje-komornik:new"},
        )
        retained = next(row for row in result["rows"] if row["id"] == "finshop:old")
        self.assertEqual(retained["last_seen_at"], seen)
        report = result["source_reports"]["finshop"]
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["current_or_future_rows"], 1)
        self.assertEqual(report["connector_error"]["status"], "error")
        self.assertTrue(report["fallback"]["used"])
        self.assertTrue(report["fallback"]["row_timestamps_preserved"])

    def test_stale_previous_snapshot_is_rejected(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        old = self.payload(
            generated="2026-08-21T03:59:59+00:00",
            rows=[self.row("finshop", "finshop:old", seen="2026-08-21T03:59:59+00:00")],
            reports={"finshop": {"status": "ok", "current_or_future_rows": 1}},
        )
        current = self.payload(
            generated=now.isoformat(), rows=[],
            reports={"finshop": {
                "status": "error", "current_or_future_rows": 0, "error": "timeout",
            }},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            path.write_text(json.dumps(old), encoding="utf-8")
            result = module.apply_previous_snapshot_fallback(current, path, now=now)

        self.assertEqual(result["rows"], [])
        self.assertEqual(result["source_reports"]["finshop"]["status"], "error")

    def test_successful_fresh_source_replaces_previous_rows(self):
        now = dt.datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        previous = self.payload(
            generated="2026-08-21T11:00:00+00:00",
            rows=[self.row("finshop", "finshop:old", seen="2026-08-21T11:00:00+00:00")],
            reports={"finshop": {"status": "ok", "current_or_future_rows": 1}},
        )
        fresh = self.row("finshop", "finshop:new", seen=now.isoformat())
        current = self.payload(
            generated=now.isoformat(), rows=[fresh],
            reports={"finshop": {"status": "ok", "current_or_future_rows": 1}},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            path.write_text(json.dumps(previous), encoding="utf-8")
            result = module.apply_previous_snapshot_fallback(current, path, now=now)

        self.assertEqual([row["id"] for row in result["rows"]], ["finshop:new"])
        self.assertNotIn("fallback", result["source_reports"]["finshop"])


if __name__ == "__main__":
    unittest.main()
