#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import unittest

import build_auction_board as board
import rbauction_official_watch as watch


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
Params = dict[str, int | str]
ScriptEntry = tuple[Params, str]


def epoch(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def record(
    item_number: int,
    *,
    country: str = "DEU",
    buying_format: str = "Live Auction",
    title: str = "2024 Toyota Prius Hybrid Automobile",
    start_price: int | None = 5000,
    asset_guid: str | None = None,
    listing_id: str | None = None,
) -> dict[str, object]:
    end = dt.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    return {
        "itemNumber": str(item_number),
        "assetGUID": asset_guid or f"asset-guid-{item_number}",
        "listingId": listing_id or f"listing-{item_number}",
        "assetDescription": title,
        "modelLocalized": "Prius Hybrid",
        "rawModelName": "Prius",
        "locationCountry": country,
        "buyingFormat": buying_format,
        "listingStatus": "Open",
        "biddingEndTime": epoch(end),
        "eventEndDateTime": epoch(end + dt.timedelta(hours=2)),
        "priceCurrency": "EUR",
        "startPrice": start_price,
        "usageKilometers": 12345,
        "manufactureYear": 2024,
        "features": "Petrol Hybrid Engine",
        "eventAdvertisedName": "Germany Unreserved Auction",
        "locationCity": "Berlin",
        "saleEventID": "sale-1",
        "assetTypeLocalized": "Automobile",
    }


def page(total: int, records: list[dict[str, object]]) -> str:
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "results": {
                        "totalAmount": total,
                        "returnedAmount": len(records),
                        "records": records,
                    }
                }
            }
        }
    }
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def offset_params(offset: int) -> Params:
    return {"from": offset, "size": watch.PAGE_SIZE}


def exact_params(item_numbers: tuple[str, ...]) -> Params:
    return {
        "itemNumbers": ",".join(item_numbers),
        "size": watch.PAGE_SIZE,
    }


def discovery_requests(
    total: int,
    sweeps: list[dict[int, list[dict[str, object]]]],
) -> list[ScriptEntry]:
    script: list[ScriptEntry] = []
    for sweep in sweeps:
        for offset in range(0, total, watch.PAGE_SIZE):
            script.append((offset_params(offset), page(total, sweep[offset])))
        if total == 0:
            script.append((offset_params(0), page(0, [])))
    return script


def probe_request(total: int, records: list[dict[str, object]]) -> ScriptEntry:
    return (
        offset_params(0),
        page(total, records[: min(watch.PAGE_SIZE, total)]),
    )


def exact_round_requests(
    records_by_id: dict[str, dict[str, object]],
) -> list[ScriptEntry]:
    all_ids = tuple(sorted(records_by_id))
    script: list[ScriptEntry] = []
    for start in range(0, len(all_ids), watch.ITEM_FILTER_BATCH_SIZE):
        batch_ids = all_ids[start:start + watch.ITEM_FILTER_BATCH_SIZE]
        batch_records = [records_by_id[item_id] for item_id in batch_ids]
        script.append((exact_params(batch_ids), page(len(batch_ids), batch_records)))
    return script


def successful_script(
    *,
    total: int,
    discovery_sweeps: list[dict[int, list[dict[str, object]]]],
    validation_rounds: list[dict[str, dict[str, object]]],
    probe_records: list[dict[str, object]],
) -> list[ScriptEntry]:
    if len(validation_rounds) != watch.EXACT_VALIDATION_ROUNDS:
        raise AssertionError("test fixture must provide every validation round")
    script = discovery_requests(total, discovery_sweeps)
    script.append(probe_request(total, probe_records))
    for records_by_id in validation_rounds:
        script.extend(exact_round_requests(records_by_id))
        script.append(probe_request(total, probe_records))
    return script


def one_sweep(records: list[dict[str, object]]) -> dict[int, list[dict[str, object]]]:
    return {
        offset: records[offset:offset + watch.PAGE_SIZE]
        for offset in range(0, len(records), watch.PAGE_SIZE)
    }


def records_by_id(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["itemNumber"]): item for item in records}


class Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


class Session:
    def __init__(self, script: list[ScriptEntry]) -> None:
        self.script = list(script)
        self.calls: list[Params] = []

    def get(self, url: str, *, params: Params, **_: object) -> Response:
        if url != watch.CATALOGUE_URL:
            raise AssertionError(url)
        if not self.script:
            raise AssertionError(f"unexpected request {params!r}")
        expected, markup = self.script.pop(0)
        if params != expected:
            raise AssertionError(f"expected params {expected!r}, got {params!r}")
        self.calls.append(params)
        return Response(markup)

    def assert_exhausted(self) -> None:
        if self.script:
            raise AssertionError(f"{len(self.script)} scripted requests were not consumed")


class RitchieBrosWatchTest(unittest.TestCase):
    def stable_session(
        self,
        records: list[dict[str, object]],
        *,
        rounds: list[dict[str, dict[str, object]]] | None = None,
    ) -> Session:
        mapping = records_by_id(records)
        return Session(successful_script(
            total=len(records),
            discovery_sweeps=[one_sweep(records)],
            validation_rounds=rounds or [mapping, mapping],
            probe_records=records,
        ))

    def test_exact_validation_keeps_only_schengen_explicit_auction_cards(self) -> None:
        records = [
            record(1),
            record(2, country="ESP", buying_format="Make Offer"),
            record(3, country="USA"),
            record(4, country="FRA", buying_format="Online Auction"),
        ]
        session = self.stable_session(records)
        payload = watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()
        self.assertEqual(
            [row["id"] for row in payload["rows"]],
            ["rbauction-eu:1", "rbauction-eu:4"],
        )
        first = payload["rows"][0]
        self.assertEqual(first["country"], "DE")
        self.assertEqual(first["fuel"], "petrol/electric hybrid")
        self.assertEqual(first["price_kind"], "starting_bid")
        self.assertEqual(first["canonical_end_utc"], "2026-09-03T12:00:00+00:00")
        report = payload["source_reports"]["rbauction-eu"]
        self.assertEqual(report["catalogue_total"], 4)
        self.assertEqual(report["schengen_auction_rows"], 2)
        self.assertEqual(report["exact_validation_rounds"], 2)
        self.assertEqual(report["rejected_counts"], {
            "non_auction_format": 1,
            "non_schengen_asset": 1,
        })

    def test_cross_border_card_reaches_the_broad_watch(self) -> None:
        session = self.stable_session([record(1, country="DEU")])
        payload = watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()
        normalized, reason = board._normalize_monitored_row(
            payload["rows"][0], generated_at=NOW
        )
        self.assertEqual(reason, "")
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["country"], "DE")
        self.assertEqual(normalized["source"], "rbauction-eu")

    def test_121_cards_use_two_pages_and_bounded_exact_filter_batches(self) -> None:
        records = [record(number) for number in range(1, 122)]
        session = self.stable_session(records)
        payload = watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()
        self.assertEqual(payload["row_count"], 121)
        report = payload["source_reports"]["rbauction-eu"]
        self.assertEqual(report["listing_pages"], 2)
        self.assertEqual(report["candidate_discovery_page_fetches"], 2)
        self.assertEqual(report["exact_validation_batches"], 6)
        exact_calls = [call for call in session.calls if "itemNumbers" in call]
        self.assertEqual([len(str(call["itemNumbers"]).split(",")) for call in exact_calls], [50, 50, 21] * 2)
        self.assertTrue(report["full_catalogue_rechecked"])

    def test_complementary_incomplete_sweeps_are_only_candidate_discovery(self) -> None:
        records = [record(number) for number in range(1, 5)]
        mapping = records_by_id(records)
        first = {0: [records[0], records[1], records[2], records[2]]}
        second = {0: [records[0], records[1], records[3], records[3]]}
        session = Session(successful_script(
            total=4,
            discovery_sweeps=[first, second],
            validation_rounds=[mapping, mapping],
            probe_records=records,
        ))
        payload = watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()
        report = payload["source_reports"]["rbauction-eu"]
        self.assertEqual(payload["row_count"], 4)
        self.assertEqual(report["candidate_discovery_sweeps"], 2)
        self.assertEqual(report["candidate_discovery_incomplete_sweeps"], 2)
        self.assertEqual(report["candidate_discovery_duplicate_records"], 2)
        self.assertEqual(report["pagination_overlap_rows"], 0)

    def test_counter_gap_fails_closed(self) -> None:
        session = Session([(offset_params(0), page(2, [record(1)]))])
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "expected 2"):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()

    def test_persistent_incomplete_discovery_exhausts_bounded_sweeps(self) -> None:
        records = [record(number) for number in range(1, 5)]
        incomplete = {0: [records[0], records[1], records[2], records[2]]}
        session = Session(discovery_requests(
            4, [incomplete] * watch.MAX_DISCOVERY_SWEEPS
        ))
        with self.assertRaisesRegex(
            watch.RitchieBrosWatchError,
            r"bounded candidate discovery exhausted: sweeps=8 declared=4 candidate_union=3",
        ):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()

    def test_discovery_identity_conflict_fails_closed(self) -> None:
        first = {0: [record(1), record(2), record(2)]}
        second = {
            0: [record(1, asset_guid="changed-guid"), record(3), record(3)]
        }
        session = Session(discovery_requests(3, [first, second]))
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "identity changed"):
            watch.build_watch(session=session, now=NOW, timeout=5)

    def test_exact_filter_missing_item_fails_closed(self) -> None:
        records = [record(number) for number in range(1, 4)]
        ids = tuple(sorted(records_by_id(records)))
        script = discovery_requests(3, [one_sweep(records)])
        script.append(probe_request(3, records))
        script.append((exact_params(ids), page(2, records[:2])))
        session = Session(script)
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "incomplete batch"):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()

    def test_exact_filter_duplicate_item_fails_closed(self) -> None:
        records = [record(number) for number in range(1, 4)]
        ids = tuple(sorted(records_by_id(records)))
        script = discovery_requests(3, [one_sweep(records)])
        script.append(probe_request(3, records))
        script.append((exact_params(ids), page(3, [records[0], records[1], records[1]])))
        session = Session(script)
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "repeated item 2"):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()

    def test_exact_filter_unrequested_item_fails_closed(self) -> None:
        records = [record(number) for number in range(1, 4)]
        ids = tuple(sorted(records_by_id(records)))
        script = discovery_requests(3, [one_sweep(records)])
        script.append(probe_request(3, records))
        script.append((exact_params(ids), page(3, [records[0], records[1], record(4)])))
        session = Session(script)
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "unrequested item 4"):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()

    def test_exact_filter_identity_change_fails_closed(self) -> None:
        records = [record(number) for number in range(1, 4)]
        ids = tuple(sorted(records_by_id(records)))
        changed = [record(1, listing_id="changed-listing"), records[1], records[2]]
        script = discovery_requests(3, [one_sweep(records)])
        script.append(probe_request(3, records))
        script.append((exact_params(ids), page(3, changed)))
        session = Session(script)
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "identity changed"):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()

    def test_normalized_change_between_exact_rounds_fails_closed(self) -> None:
        records = [record(1), record(2)]
        first = records_by_id(records)
        second = records_by_id([
            record(1, title="2024 Toyota Prius Changed Automobile"),
            records[1],
        ])
        session = self.stable_session(records, rounds=[first, second])
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "rounds disagree"):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()

    def test_irrelevant_raw_change_between_exact_rounds_is_ignored(self) -> None:
        records = [record(1), record(2)]
        first = records_by_id(records)
        changed = dict(records[0])
        changed["indexedAt"] = "volatile-search-index-timestamp"
        second = records_by_id([changed, records[1]])
        session = self.stable_session(records, rounds=[first, second])
        payload = watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()
        self.assertEqual(payload["row_count"], 2)

    def test_total_change_between_exact_rounds_fails_closed(self) -> None:
        records = [record(number) for number in range(1, 4)]
        mapping = records_by_id(records)
        script = discovery_requests(3, [one_sweep(records)])
        script.append(probe_request(3, records))
        script.extend(exact_round_requests(mapping))
        grown = records + [record(4)]
        script.append(probe_request(4, grown))
        session = Session(script)
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "total changed"):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()

    def test_missing_stable_identity_fails_closed(self) -> None:
        item = record(1)
        del item["assetGUID"]
        session = Session(discovery_requests(1, [one_sweep([item])]))
        with self.assertRaisesRegex(watch.RitchieBrosWatchError, "stable asset/listing identity"):
            watch.build_watch(session=session, now=NOW, timeout=5)
        session.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
