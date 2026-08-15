#!/usr/bin/env python3
"""Deterministic tests for the dark strict-newest incremental ingest primitive."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

try:
    from . import incremental_frontier as frontier
    from . import radar_incremental_ingest as ingest
except ImportError:
    import incremental_frontier as frontier
    import radar_incremental_ingest as ingest


SORT_HASH = "a" * 64
OTHER_SORT_HASH = "b" * 64
SOURCE = "offers.example"
PARTITION = "cars-2023-plus"


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def contract(sort_hash: str = SORT_HASH) -> frontier.SourceContract:
    return frontier.SourceContract(
        source_key=SOURCE,
        partition_key=PARTITION,
        sort_contract_sha256=sort_hash,
        max_pages=4,
        frontier_cap=100,
        strict_newest=True,
        stop_after_known_pages=2,
    )


def allowed(*contracts: frontier.SourceContract) -> frozenset[frontier.ContractKey]:
    return frozenset(value.key for value in contracts)


def offer(native_id: str, *, title: str | None = None) -> dict[str, object]:
    return {
        "source": SOURCE,
        "source_listing_id": native_id,
        "source_url": f"https://offers.example/{native_id}",
        "title": title or f"Offer {native_id}",
        "make_model": "model-a",
        "variant": "manual",
        "country": "DE",
        "price_eur": 10_000,
        "raw_price": "10000",
        "currency": "EUR",
        "year": 2025,
        "mileage_km": 1_000,
        "fuel": "petrol",
        "seller_type": "dealer",
        "location": "Berlin",
        "raw_json": {"native_id": native_id},
    }


def item(
    native_id: str,
    sort_value: int,
    *,
    accepted: bool = True,
    title: str | None = None,
) -> dict[str, object]:
    return {
        "native_id": native_id,
        "sort_value": sort_value,
        "offer": offer(native_id, title=title) if accepted else None,
    }


def page(number: int, *items: dict[str, object]) -> dict[str, object]:
    return {"number": number, "items": list(items)}


def complete_bootstrap_pages() -> list[dict[str, object]]:
    return [
        page(1, item("n6", 106), item("n5", 105)),
        page(2, item("n4", 104), item("n3", 103)),
        page(3, item("n2", 102), item("n1", 101)),
        page(4),
    ]


class IncrementalIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "universe.sqlite"
        self.connection = ingest.connect(self.database, timeout_seconds=0.05)
        self.contract = contract()
        self.allowlist = allowed(self.contract)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def run_ingest(
        self,
        run_id: str,
        pages,
        *,
        observed_at: str = "2026-08-14T12:00:00Z",
        request_label: str | None = None,
        selected_contract: frontier.SourceContract | None = None,
        selected_allowlist: frozenset[frontier.ContractKey] | None = None,
    ) -> dict[str, object]:
        return ingest.ingest_incremental_run(
            self.connection,
            contract=selected_contract or self.contract,
            allowlist=selected_allowlist or self.allowlist,
            run_id=run_id,
            request_sha256=digest(request_label or run_id),
            observed_at_utc=observed_at,
            pages=pages,
        )

    def bootstrap(self) -> dict[str, object]:
        return self.run_ingest("bootstrap", complete_bootstrap_pages())

    def test_source_exhaustion_bootstrap_is_atomic(self) -> None:
        receipt = self.bootstrap()
        self.assertEqual(receipt["stop_reason"], "source_exhausted")
        self.assertEqual(receipt["processed_pages"], 4)
        self.assertEqual(receipt["new_native_id_count"], 6)
        self.assertEqual(receipt["observed_offer_count"], 6)
        self.assertEqual(receipt["inserted_offer_count"], 6)
        self.assertEqual(receipt["changed_offer_count"], 0)
        self.assertEqual(receipt["refreshed_offer_count"], 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 6
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_frontier_ids"
            ).fetchone()[0],
            6,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_runs"
            ).fetchone()[0],
            1,
        )
        changes = self.connection.execute(
            """
            SELECT change_kind, prior_material_sha256, material_sha256, offer_json
              FROM radar_incremental_changes ORDER BY source_listing_id
            """
        ).fetchall()
        self.assertEqual(len(changes), 6)
        for change in changes:
            payload = json.loads(change["offer_json"])
            self.assertEqual(change["change_kind"], "inserted")
            self.assertIsNone(change["prior_material_sha256"])
            self.assertEqual(set(payload), set(ingest.MATERIAL_OFFER_FIELDS))
            self.assertFalse(
                {"source", "source_listing_id", "first_seen_at", "last_seen_at",
                 "fetched_at", "raw_json"} & set(payload)
            )
            self.assertEqual(
                change["material_sha256"],
                hashlib.sha256(change["offer_json"].encode("utf-8")).hexdigest(),
            )

    def test_identical_offer_refreshes_only_liveness_and_writes_no_change(self) -> None:
        self.bootstrap()
        before = self.connection.execute(
            """
            SELECT first_seen_at, raw_json, title, price_eur
              FROM offers WHERE source_listing_id='n6'
            """
        ).fetchone()
        noisy = item("n6", 106)
        assert isinstance(noisy["offer"], dict)
        noisy["offer"]["raw_json"] = '{"transport":"changed","native_id":"n6"}'
        noisy["offer"]["fetched_at"] = "2026-08-14T12:59:59Z"
        receipt = self.run_ingest(
            "no-op-refresh",
            [
                page(1, noisy, item("n5", 105)),
                page(2, item("n4", 104), item("n3", 103)),
            ],
            observed_at="2026-08-14T13:00:00Z",
        )
        self.assertEqual(receipt["observed_offer_count"], 4)
        self.assertEqual(receipt["inserted_offer_count"], 0)
        self.assertEqual(receipt["changed_offer_count"], 0)
        self.assertEqual(receipt["refreshed_offer_count"], 4)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_changes WHERE run_id=?",
                ("no-op-refresh",),
            ).fetchone()[0],
            0,
        )
        after = self.connection.execute(
            """
            SELECT first_seen_at, raw_json, title, price_eur, fetched_at, last_seen_at
              FROM offers WHERE source_listing_id='n6'
            """
        ).fetchone()
        self.assertEqual(after["first_seen_at"], before["first_seen_at"])
        self.assertEqual(after["raw_json"], before["raw_json"])
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["price_eur"], before["price_eur"])
        self.assertEqual(after["fetched_at"], "2026-08-14T12:59:59+00:00")
        self.assertEqual(after["last_seen_at"], "2026-08-14T13:00:00+00:00")

    def test_equal_timestamp_material_update_is_ledged_and_preserves_first_seen(self) -> None:
        self.bootstrap()
        before = self.connection.execute(
            "SELECT * FROM offers WHERE source_listing_id='n6'"
        ).fetchone()
        changed = item("n6", 106, title="Material title change")
        assert isinstance(changed["offer"], dict)
        changed["offer"]["price_eur"] = 12_345
        changed["offer"]["raw_price"] = "12345"
        receipt = self.run_ingest(
            "material-change",
            [
                page(1, changed, item("n5", 105)),
                page(2, item("n4", 104), item("n3", 103)),
            ],
            observed_at="2026-08-14T12:00:00Z",
        )
        self.assertEqual(receipt["observed_offer_count"], 4)
        self.assertEqual(receipt["inserted_offer_count"], 0)
        self.assertEqual(receipt["changed_offer_count"], 1)
        self.assertEqual(receipt["refreshed_offer_count"], 3)
        after = self.connection.execute(
            "SELECT * FROM offers WHERE source_listing_id='n6'"
        ).fetchone()
        self.assertEqual(after["first_seen_at"], before["first_seen_at"])
        self.assertEqual(after["title"], "Material title change")
        self.assertEqual(after["price_eur"], 12_345)
        change = self.connection.execute(
            "SELECT * FROM radar_incremental_changes WHERE run_id=?",
            ("material-change",),
        ).fetchone()
        self.assertEqual(change["change_kind"], "material_update")
        self.assertEqual(
            change["prior_material_sha256"],
            ingest._material_sha256(ingest._canonical_material_json(before)),
        )
        self.assertEqual(
            change["material_sha256"],
            ingest._material_sha256(ingest._canonical_material_json(after)),
        )
        self.assertNotEqual(
            change["prior_material_sha256"], change["material_sha256"]
        )

    def test_frontier_cap_retains_newest_ids_for_next_known_stop(self) -> None:
        capped = frontier.SourceContract(
            source_key=SOURCE,
            partition_key="capped-cars",
            sort_contract_sha256=SORT_HASH,
            max_pages=4,
            frontier_cap=4,
            strict_newest=True,
            stop_after_known_pages=2,
        )
        capped_allowlist = allowed(capped)
        self.run_ingest(
            "capped-bootstrap",
            complete_bootstrap_pages(),
            selected_contract=capped,
            selected_allowlist=capped_allowlist,
        )
        retained = {
            row[0]
            for row in self.connection.execute(
                """SELECT native_id FROM radar_incremental_frontier_ids
                     WHERE source_key=? AND partition_key=?""",
                (SOURCE, capped.partition_key),
            )
        }
        self.assertEqual(retained, {"n6", "n5", "n4", "n3"})
        receipt = self.run_ingest(
            "capped-steady",
            [
                page(1, item("n6", 106), item("n5", 105)),
                page(2, item("n4", 104), item("n3", 103)),
                page(3, item("must-not-be-read", 1)),
            ],
            selected_contract=capped,
            selected_allowlist=capped_allowlist,
        )
        self.assertEqual(receipt["stop_reason"], "known_frontier_reached")
        self.assertEqual(receipt["processed_pages"], 2)

    def test_two_fully_known_pages_stop_without_consuming_later_page(self) -> None:
        self.bootstrap()
        consumed: list[int] = []

        def pages():
            for value in [
                page(1, item("n8", 108), item("n7", 107)),
                page(2, item("n6", 106), item("n5", 105)),
                page(3, item("n4", 104), item("n3", 103)),
                page(4, item("must-not-be-read", 1)),
            ]:
                consumed.append(value["number"])
                yield value

        receipt = self.run_ingest("steady", pages())
        self.assertEqual(receipt["stop_reason"], "known_frontier_reached")
        self.assertEqual(consumed, [1, 2, 3])
        self.assertEqual(receipt["new_native_id_count"], 2)

    def test_mixed_new_and_known_page_resets_known_streak(self) -> None:
        self.bootstrap()
        receipt = self.run_ingest(
            "mixed",
            [
                page(1, item("n7", 107), item("n6", 106)),
                page(2, item("n5", 105), item("n4", 104)),
                page(3, item("n3", 103), item("late-new", 100)),
                page(4),
            ],
        )
        self.assertEqual(receipt["stop_reason"], "source_exhausted")
        self.assertEqual(receipt["processed_pages"], 4)
        self.assertEqual(receipt["new_native_id_count"], 2)

    def test_filtered_raw_ids_enter_frontier_but_not_offers(self) -> None:
        receipt = self.run_ingest(
            "filtered",
            [
                page(1, item("accepted", 2), item("filtered", 1, accepted=False)),
                page(2),
            ],
        )
        self.assertEqual(receipt["raw_item_count"], 2)
        self.assertEqual(receipt["observed_offer_count"], 1)
        ids = {
            row[0]
            for row in self.connection.execute(
                "SELECT native_id FROM radar_incremental_frontier_ids"
            )
        }
        self.assertEqual(ids, {"accepted", "filtered"})
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 1
        )

    def test_incomplete_max_page_bootstrap_does_not_teach_false_frontier(self) -> None:
        partial = [
            page(1, item("n6", 106), item("n5", 105)),
            page(2, item("n4", 104), item("n3", 103)),
            page(3, item("n2", 102), item("n1", 101)),
            page(4, item("older", 100)),
        ]
        with self.assertRaises(frontier.FrontierBoundaryNotReached):
            self.run_ingest("partial-bootstrap", partial)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 0
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_frontiers"
            ).fetchone()[0],
            0,
        )
        with self.assertRaises(frontier.FrontierBoundaryNotReached):
            self.run_ingest("partial-retry", partial)

    def test_non_monotonic_pages_and_early_eof_fail_without_state(self) -> None:
        with self.assertRaises(frontier.PageContractError):
            self.run_ingest(
                "non-monotonic",
                [page(1, item("old", 1), item("newer", 2)), page(2)],
            )
        with self.assertRaises(frontier.PageContractError):
            self.run_ingest(
                "ambiguous-tie",
                [page(1, item("tie-a", 2), item("tie-b", 2)), page(2)],
            )
        with self.assertRaises(frontier.PageContractError):
            self.run_ingest("early-eof", [page(1, item("one", 1))])
        with self.assertRaises(frontier.PageContractError):
            self.run_ingest(
                "overlap",
                [page(1, item("same", 2)), page(2, item("same", 2)), page(3)],
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_runs"
            ).fetchone()[0],
            0,
        )

    def test_baseline_sort_and_offer_identity_aliases_fail_closed(self) -> None:
        self.bootstrap()
        with self.assertRaises(frontier.PageContractError):
            self.run_ingest(
                "sort-alias",
                [page(1, item("different-native", 105)), page(2)],
            )
        aliased = item("different-native", 107)
        aliased["offer"] = offer("n6")
        with self.assertRaises(frontier.PageContractError):
            self.run_ingest(
                "offer-alias",
                [page(1, aliased), page(2)],
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_runs"
            ).fetchone()[0],
            1,
        )

    def test_noncanonical_offer_identity_cannot_bypass_alias_guard(self) -> None:
        canonical = item("raw-a", 2)
        canonical["offer"] = offer("offer-1")
        spaced = item("raw-b", 1)
        spaced["offer"] = offer(" offer-1 ")
        with self.assertRaises(frontier.PageContractError):
            self.run_ingest(
                "normalized-alias",
                [page(1, canonical, spaced), page(2)],
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0],
            0,
        )

    def test_sort_value_must_fit_sqlite_signed_integer(self) -> None:
        with self.assertRaises(frontier.PageContractError):
            self.run_ingest(
                "oversized-sort",
                [page(1, item("too-large", 2**63)), page(2)],
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_runs"
            ).fetchone()[0],
            0,
        )

    def test_contract_must_be_allowlisted_and_drift_fails_closed(self) -> None:
        unlisted = contract(OTHER_SORT_HASH)
        with self.assertRaises(frontier.ContractError):
            self.run_ingest(
                "not-allowed",
                [page(1)],
                selected_contract=unlisted,
            )
        self.bootstrap()
        with self.assertRaises(frontier.ContractDriftError):
            self.run_ingest(
                "drift",
                [page(1)],
                selected_contract=unlisted,
                selected_allowlist=allowed(self.contract, unlisted),
            )

    def test_boolean_contract_bounds_are_rejected(self) -> None:
        for field in ("max_pages", "frontier_cap"):
            values = {
                "source_key": SOURCE,
                "partition_key": f"boolean-{field}",
                "sort_contract_sha256": SORT_HASH,
                "max_pages": 4,
                "frontier_cap": 100,
                field: True,
            }
            invalid = frontier.SourceContract(**values)
            with self.subTest(field=field), self.assertRaises(frontier.ContractError):
                invalid.validate(allowed(invalid))

    def test_corrupt_state_digest_fails_closed(self) -> None:
        self.bootstrap()
        self.connection.execute(
            "UPDATE radar_incremental_frontiers SET frontier_sha256=?",
            ("0" * 64,),
        )
        self.connection.commit()
        with self.assertRaises(frontier.StateCorruptionError):
            frontier.load_frontier(self.connection, self.contract, self.allowlist)

    def test_offer_frontier_and_receipt_roll_back_together(self) -> None:
        with mock.patch.object(
            ingest, "_write_run_receipt", side_effect=RuntimeError("injected")
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.run_ingest("rollback", complete_bootstrap_pages())
        for table in (
            "offers",
            "radar_incremental_frontiers",
            "radar_incremental_frontier_ids",
            "radar_incremental_runs",
            "radar_incremental_changes",
        ):
            self.assertEqual(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                table,
            )

    def test_change_ledger_failure_rolls_back_offer_frontier_run_and_ledger(self) -> None:
        with mock.patch.object(
            ingest, "_write_change", side_effect=RuntimeError("ledger injected")
        ):
            with self.assertRaisesRegex(RuntimeError, "ledger injected"):
                self.run_ingest("ledger-rollback", complete_bootstrap_pages())
        self.assertFalse(self.connection.in_transaction)
        for table in (
            "offers",
            "radar_incremental_frontiers",
            "radar_incremental_frontier_ids",
            "radar_incremental_runs",
            "radar_incremental_changes",
        ):
            self.assertEqual(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                table,
            )

    def test_base_exception_rolls_back_and_closes_transaction(self) -> None:
        class InjectedCrash(BaseException):
            pass

        with mock.patch.object(
            ingest, "_write_run_receipt", side_effect=InjectedCrash("injected")
        ):
            with self.assertRaises(InjectedCrash):
                self.run_ingest("base-exception", complete_bootstrap_pages())
        self.assertFalse(self.connection.in_transaction)
        for table in (
            "offers",
            "radar_incremental_frontiers",
            "radar_incremental_frontier_ids",
            "radar_incremental_runs",
            "radar_incremental_changes",
        ):
            self.assertEqual(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                table,
            )

    def test_retry_is_idempotent_and_does_not_consume_pages(self) -> None:
        first = self.bootstrap()
        first_change_count = self.connection.execute(
            "SELECT COUNT(*) FROM radar_incremental_changes"
        ).fetchone()[0]

        def must_not_run():
            raise AssertionError("idempotent retry consumed the source")
            yield  # pragma: no cover

        second = self.run_ingest("bootstrap", must_not_run())
        self.assertEqual(second, first)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 6
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_changes"
            ).fetchone()[0],
            first_change_count,
        )
        with self.assertRaises(ingest.RunConflictError):
            self.run_ingest(
                "bootstrap",
                must_not_run(),
                request_label="different-request",
            )

    def test_corrupt_change_ledger_is_not_accepted_as_idempotent_success(self) -> None:
        self.bootstrap()
        self.connection.execute(
            "DELETE FROM radar_incremental_changes WHERE run_id=? AND source_listing_id=?",
            ("bootstrap", "n1"),
        )
        self.connection.commit()

        def must_not_run():
            raise AssertionError("corrupt-retry consumed the source")
            yield  # pragma: no cover

        with self.assertRaises(ingest.RunConflictError):
            self.run_ingest("bootstrap", must_not_run())

    def test_corrupt_receipt_is_not_accepted_as_idempotent_success(self) -> None:
        self.bootstrap()
        original = self.connection.execute(
            "SELECT receipt_json FROM radar_incremental_runs WHERE run_id=?",
            ("bootstrap",),
        ).fetchone()[0]
        self.connection.execute(
            "UPDATE radar_incremental_runs SET receipt_json='{}' WHERE run_id=?",
            ("bootstrap",),
        )
        self.connection.commit()
        with self.assertRaises(ingest.RunConflictError):
            self.run_ingest("bootstrap", [])
        type_confused = json.loads(original)
        type_confused["processed_pages"] = float(type_confused["processed_pages"])
        type_confused["raw_item_count"] = False
        self.connection.execute(
            "UPDATE radar_incremental_runs SET receipt_json=? WHERE run_id=?",
            (json.dumps(type_confused), "bootstrap"),
        )
        self.connection.commit()
        with self.assertRaises(ingest.RunConflictError):
            self.run_ingest("bootstrap", [])

    def test_existing_caller_transaction_is_left_untouched(self) -> None:
        self.connection.execute("CREATE TABLE caller_work(marker TEXT NOT NULL)")
        self.connection.execute("INSERT INTO caller_work(marker) VALUES('keep-me')")
        self.assertTrue(self.connection.in_transaction)
        with self.assertRaisesRegex(ingest.IngestError, "clean connection"):
            self.run_ingest("nested", complete_bootstrap_pages())
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute("SELECT marker FROM caller_work").fetchone()[0],
            "keep-me",
        )
        self.connection.rollback()

    def test_stale_observation_cannot_overwrite_newer_offer_or_frontier(self) -> None:
        self.bootstrap()
        before_offer = self.connection.execute(
            "SELECT title, last_seen_at FROM offers WHERE source_listing_id='n6'"
        ).fetchone()
        before_frontier = self.connection.execute(
            "SELECT revision, frontier_sha256 FROM radar_incremental_frontiers"
        ).fetchone()
        before_run_count = self.connection.execute(
            "SELECT COUNT(*) FROM radar_incremental_runs"
        ).fetchone()[0]

        with self.assertRaises(ingest.StaleObservationError):
            self.run_ingest(
                "stale",
                [
                    page(
                        1,
                        item("new-before-stale", 107),
                        item("n6", 106, title="Stale overwrite"),
                    ),
                    page(2, item("n5", 105), item("n4", 104)),
                    page(3, item("n3", 103), item("n2", 102)),
                ],
                observed_at="2026-08-14T11:00:00Z",
            )

        self.assertEqual(
            self.connection.execute(
                "SELECT title, last_seen_at FROM offers WHERE source_listing_id='n6'"
            ).fetchone(),
            before_offer,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT revision, frontier_sha256 FROM radar_incremental_frontiers"
            ).fetchone(),
            before_frontier,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_runs"
            ).fetchone()[0],
            before_run_count,
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM offers WHERE source_listing_id='new-before-stale'"
            ).fetchone()
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM radar_incremental_changes"
            ).fetchone()[0],
            6,
        )

    def test_only_observed_rows_refresh_last_seen(self) -> None:
        self.connection.execute(
            """
            INSERT INTO offers (
              source, source_listing_id, source_url, title, make_model, variant,
              country, price_eur, raw_price, currency, year, mileage_km, fuel,
              seller_type, location, fetched_at, first_seen_at, last_seen_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                SOURCE, "untouched", "https://offers.example/untouched", "Old",
                "model-a", "", "DE", 9000, "9000", "EUR", 2024, 2000,
                "petrol", "dealer", "", "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00", "",
            ),
        )
        self.connection.commit()
        self.run_ingest(
            "observed-only",
            [page(1, item("observed", 1)), page(2)],
            observed_at="2026-08-14T13:00:00Z",
        )
        rows = dict(
            self.connection.execute(
                "SELECT source_listing_id, last_seen_at FROM offers"
            ).fetchall()
        )
        self.assertEqual(rows["untouched"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(rows["observed"], "2026-08-14T13:00:00+00:00")

    def test_active_writer_lock_fails_without_partial_commit(self) -> None:
        blocker = sqlite3.connect(self.database, timeout=0.05)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(frontier.ConcurrentFrontierUpdate):
                self.run_ingest("busy", complete_bootstrap_pages())
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 0
        )

    def test_legacy_v1_run_schema_is_rejected_without_partial_upgrade(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.sqlite"
        legacy = sqlite3.connect(legacy_path)
        try:
            legacy.execute(
                """
                CREATE TABLE radar_incremental_runs (
                  run_id TEXT PRIMARY KEY,
                  updated_offer_count INTEGER NOT NULL
                )
                """
            )
            legacy.commit()
        finally:
            legacy.close()
        with self.assertRaisesRegex(ingest.SchemaError, "sealed incremental schema"):
            ingest.connect(legacy_path)
        audit = sqlite3.connect(legacy_path)
        try:
            tables = {
                row[0] for row in audit.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            audit.close()
        self.assertEqual(tables, {"radar_incremental_runs"})


if __name__ == "__main__":
    unittest.main()
