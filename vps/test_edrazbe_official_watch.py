#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import unittest

import edrazbe_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def item_payload(
    sale_id: str = "4e3d723f-4b63-402b-86b7-5d082b4f2f1b",
    description: str = "RENAULT CLIO 1.5 dCi 2016. prva registracija 2016, 120.000 km",
    *,
    subject_code: str = "090",
    subject_label: str = "Cars",
    status: str = "pending",
    end: str = "2026-09-10T09:00:00Z",
    estimated: int | None = 4500,
    case_number: str = "4934-25360",
) -> dict:
    return {
        "id": sale_id,
        "publicationId": "20260828-02110-001",
        "caseNumber": case_number,
        "caseYear": 2026,
        "status": status,
        "saleStartAt": "2026-09-10T08:00:00Z",
        "saleEndAt": end,
        "subjectTypeRelation": {
            "groupCode": "subject_type",
            "valueCode": subject_code,
            "valueContent": subject_label,
        },
        "saleMethodRelation": {"valueContent": "Ascending auction"},
        "saleSubjectRelation": {"valueContent": "Movable property"},
        "subjectEstimatedPrice": estimated,
        "description": description,
        "organization": "77695771",
    }


def xhr(url: str, body: list | dict) -> dict:
    return {"url": url, "body": json.dumps(body)}


def capture_of(items: list) -> list:
    return [xhr("https://api.sys.edrazbe.si/public/publication/list", items)]


CAR = item_payload()


class EdrazbeWatchTest(unittest.TestCase):
    def test_parses_sale_public_facts(self) -> None:
        sale = watch.parse_item(CAR, context="t")
        self.assertEqual(sale.sale_id, "4e3d723f-4b63-402b-86b7-5d082b4f2f1b")
        self.assertEqual(sale.subject_type_code, "090")
        self.assertEqual(sale.sale_end_utc, dt.datetime(2026, 9, 10, 9, 0, tzinfo=UTC))
        self.assertEqual(sale.estimated_price_eur, 4500)

    def test_two_pass_reconciles_and_filters_non_cars(self) -> None:
        car = CAR
        van = item_payload(
            sale_id="a1a1a1a1-0000-0000-0000-000000000001",
            description="FORD TRANSIT CUSTOM 2.2 TDCi 2014",
            subject_code="110",
            subject_label="Commercial vehicles",
        )
        ended = item_payload(
            sale_id="a1a1a1a1-0000-0000-0000-000000000002",
            description="OPEL ASTRA 1.6 2008",
            end="2026-08-01T09:00:00Z",
        )
        captures = {
            "list": capture_of([car, van, ended]),
            "empty": xhr("https://api.sys.edrazbe.si/public/publication/list", []),
        }
        calls = {"n": 0}

        def capture():
            result = capture_of([car, van, ended])
            return result

        payload = watch.build_watch(capture=capture, now=NOW, snapshot_attempts=1)
        report = payload["source_reports"][watch.SOURCE_KEY]
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["id"], "edrazbe-si:4e3d723f-4b63-402b-86b7-5d082b4f2f1b")
        self.assertEqual(report["source_excluded"], {
            "not_car_subject_type": 1,
            "ended_sale": 1,
        })
        self.assertTrue(report["two_pass_verified"])

    def test_row_fields_and_url(self) -> None:
        sale = watch.parse_item(CAR, context="t")
        row = watch.normalize_sale(sale, observed_at="2026-08-30T12:00:00+00:00")
        self.assertEqual(row["category"], "car")
        self.assertEqual(row["country"], "SI")
        self.assertEqual(row["price_currency"], "EUR")
        self.assertEqual(row["price_amount"], 4500)
        self.assertEqual(row["year"], 2016)
        self.assertEqual(row["mileage_km"], 120000)
        self.assertIn(sale.sale_id, row["url"])
        self.assertTrue(row["url"].startswith("https://www.edrazbe.si/en/single/"))

    def test_missing_price_is_unknown(self) -> None:
        sale = watch.parse_item(item_payload(estimated=None), context="t")
        row = watch.normalize_sale(sale, observed_at="2026-08-30T12:00:00+00:00")
        self.assertIsNone(row["price_amount"])
        self.assertEqual(row["price_kind"], "unknown")
        self.assertEqual(row["price_currency"], "")

    def test_missing_description_fails_closed(self) -> None:
        payload = item_payload()
        payload["description"] = ""
        with self.assertRaisesRegex(watch.EdrazbeWatchError, "no public description"):
            watch.parse_item(payload, context="t")

    def test_missing_subject_type_fails_closed(self) -> None:
        payload = item_payload()
        payload["subjectTypeRelation"] = {}
        with self.assertRaisesRegex(watch.EdrazbeWatchError, "no subject type"):
            watch.parse_item(payload, context="t")

    def test_bad_end_time_fails_closed(self) -> None:
        with self.assertRaisesRegex(watch.EdrazbeWatchError, "end time"):
            watch.parse_item(item_payload(end="next tuesday"), context="t")

    def test_ids_changed_between_passes_fails_closed(self) -> None:
        base = [CAR]
        other = [item_payload(sale_id="b2b2b2b2-0000-0000-0000-000000000009")]
        calls = iter([base, other, base, other])

        def capture():
            return capture_of(next(calls))

        with self.assertRaisesRegex(watch.EdrazbeWatchError, "ids changed"):
            watch.build_watch(capture=capture, now=NOW, snapshot_attempts=1)

    def test_facts_changed_between_passes_fails_closed(self) -> None:
        first = [CAR]
        second = [item_payload(case_number="9999-11111")]
        calls = iter([first, second, first, second])

        def capture():
            return capture_of(next(calls))

        with self.assertRaisesRegex(watch.EdrazbeWatchError, "facts changed"):
            watch.build_watch(capture=capture, now=NOW, snapshot_attempts=1)

    def test_merges_multiple_list_responses_and_dedupes(self) -> None:
        a = [CAR]
        b = [item_payload(sale_id="c3c3c3c3-0000-0000-0000-000000000007",
                          description="TOYOTA YARIS HYBRID 2020"), CAR]
        items = watch.extract_items(
            "ignored",
            [xhr("https://api.sys.edrazbe.si/public/publication/list", a),
             xhr("https://api.sys.edrazbe.si/public/publication/list", b)],
        )
        self.assertEqual(len(items), 2)


if __name__ == "__main__":
    unittest.main()
