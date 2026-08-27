#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import unittest

import ponip_official_watch as watch


UTC = dt.timezone.utc
HEADER = (
    '"Opis";"Vrsta predmeta prodaje";"ID nadmetanja";'
    '"Datum i vrijeme početka nadmetanja";'
    '"Datum i vrijeme završetka nadmetanja";'
    '"Početna cijena za nadmetanje"\n'
)


class Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [self.payload[index:index + chunk_size] for index in range(0, len(self.payload), chunk_size)]

    def close(self) -> None:
        return None


class Session:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get(self, url: str, **_: object) -> Response:
        if url != watch.SOURCE_URL:
            raise AssertionError(url)
        return Response(self.payload)


class PonipWatchTest(unittest.TestCase):
    def test_complete_export_keeps_only_current_vehicle_rows(self) -> None:
        export = HEADER + (
            '"Osobni automobil Toyota Prius hibrid";"pokretnina";"1001";'
            '"2026-08-27 10:00:00";"2026-08-30 12:00:00";"5300.00"\n'
            '"Industrijski stroj";"pokretnina";"1002";'
            '"2026-08-27 10:00:00";"2026-08-30 12:00:00";"900.00"\n'
            '"Osobni automobil, završen";"pokretnina";"1003";'
            '"2026-08-20 10:00:00";"2026-08-21 12:00:00";"1000.00"\n'
        )
        payload = watch.build_watch(
            session=Session(export.encode("utf-8")),
            now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            timeout=5,
        )
        self.assertEqual(payload["row_count"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["id"], "fina-ponip:1001")
        self.assertEqual(row["price_kind"], "starting_bid")
        self.assertEqual(row["price_eur"], 5300)
        self.assertEqual(row["fuel"], "unknown")
        self.assertEqual(row["canonical_end_utc"], "2026-08-30T10:00:00+00:00")
        self.assertEqual(row["url"], f"{watch.SEARCH_URL}?idNadmetanja=1001")
        report = payload["source_reports"]["fina-ponip"]
        self.assertEqual(report["csv_rows"], 3)
        self.assertEqual(report["future_or_current_rows"], 2)
        self.assertEqual(report["vehicle_rows"], 1)

    def test_duplicate_export_identity_fails_closed(self) -> None:
        export = HEADER + (
            '"Osobni automobil A";"pokretnina";"1001";'
            '"2026-08-27 10:00:00";"2026-08-30 12:00:00";"1000"\n'
            '"Osobni automobil B";"pokretnina";"1001";'
            '"2026-08-27 10:00:00";"2026-08-30 12:00:00";"1000"\n'
        )
        with self.assertRaises(watch.PonipWatchError):
            watch.parse_export(
                export.encode("utf-8"),
                observed_at="2026-08-27T20:00:00+00:00",
                now=dt.datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
