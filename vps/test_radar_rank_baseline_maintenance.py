#!/usr/bin/env python3
"""Adversarial tests for dark rank-baseline retention and monitoring."""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

try:
    from . import radar_rank_baseline as baseline
    from . import radar_rank_baseline_monitor as monitor
    from . import radar_rank_baseline_retention as retention
    from .test_radar_rank_baseline import fixture, retime
except ImportError:
    import radar_rank_baseline as baseline
    import radar_rank_baseline_monitor as monitor
    import radar_rank_baseline_retention as retention
    from test_radar_rank_baseline import fixture, retime


START = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)


def timestamp(value: datetime) -> str:
    return value.isoformat()


def create_store(
    root: Path,
    count: int = 5,
    *,
    step: timedelta = timedelta(hours=1),
) -> dict[str, object]:
    artifacts = root / "artifacts"
    artifacts.mkdir()
    entries: list[tuple[dict[str, object], Path, str]] = []
    for index in range(count):
        value = fixture()
        generated = START + step * index
        retime(value, timestamp(generated), timestamp(generated + timedelta(hours=8)))
        path, digest = baseline.write_content_addressed(artifacts, value)
        entries.append((value, path, digest))
    latest, latest_path, latest_digest = entries[-1]
    pointer = baseline.build_latest_accepted_pointer(
        latest, latest_path, latest_digest, now=START + step * count
    )
    pointer_path = root / "latest_accepted.json"
    baseline.write_latest_accepted_pointer(
        pointer_path, pointer, now=START + step * count
    )
    pointer_sha256 = hashlib.sha256(pointer_path.read_bytes()).hexdigest()
    receipts = root / "receipts"
    receipts.mkdir()
    return {
        "artifacts": artifacts,
        "entries": entries,
        "pointer": pointer,
        "pointer_path": pointer_path,
        "pointer_sha256": pointer_sha256,
        "minimum": latest["data_generated_at_utc"],
        "receipt_path": receipts / "retention-operation.json",
    }


def artifact_names(store: dict[str, object]) -> set[str]:
    return {path.name for path in store["artifacts"].iterdir()}


def operation_record(store: dict[str, object]) -> dict[str, object]:
    return retention._load_current_operation_record(store["receipt_path"])[1]


class RetentionTests(unittest.TestCase):
    def call(self, store: dict[str, object], **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "artifact_dir": store["artifacts"],
            "pointer_path": store["pointer_path"],
            "trusted_pointer_sha256": store["pointer_sha256"],
            "minimum_data_generated_at_utc": store["minimum"],
            "max_artifacts": 3,
            "max_bytes": retention.HARD_MAX_BYTES,
            "apply": False,
            "allow_unlocked_test_apply": True,
            "receipt_path": store["receipt_path"],
        }
        arguments.update(overrides)
        return retention.retain(**arguments)

    def test_dry_run_and_apply_are_bounded_and_preserve_pointed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            before = artifact_names(store)
            planned = self.call(store)
            self.assertFalse(planned["applied"])
            self.assertEqual(planned["after_artifacts"], 3)
            self.assertEqual(artifact_names(store), before)
            applied = self.call(store, apply=True)
            self.assertTrue(applied["applied"])
            self.assertEqual(len(artifact_names(store)), 3)
            self.assertIn(store["pointer"]["artifact_file"], artifact_names(store))
            self.assertTrue(applied["shared_exporter_lock_verified"])
            self.assertTrue(applied["durable_operation_receipt_verified"])
            self.assertTrue(applied["exact_post_rescan_verified"])
            operation = operation_record(store)
            self.assertEqual(operation["phase"], "complete")
            self.assertEqual(store["receipt_path"].stat().st_mode & 0o777, 0o600)
            baseline.validate_latest_accepted_pointer(
                store["pointer"], store["artifacts"], now=START + timedelta(hours=5)
            )

    def test_byte_limit_is_enforced_without_deleting_the_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory), count=4)
            pointed = store["artifacts"] / store["pointer"]["artifact_file"]
            budget = pointed.stat().st_size + 1
            receipt = self.call(
                store, max_artifacts=retention.HARD_MAX_ARTIFACTS,
                max_bytes=budget, apply=True,
            )
            self.assertLessEqual(receipt["after_bytes"], budget)
            self.assertTrue(pointed.is_file())

    def test_limits_cannot_exceed_hard_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory), count=1)
            with self.assertRaisesRegex(retention.RetentionError, "hard bound"):
                self.call(store, max_artifacts=65)
            with self.assertRaisesRegex(retention.RetentionError, "hard bound"):
                self.call(store, max_bytes=retention.HARD_MAX_BYTES + 1)

    def test_bad_anchor_and_rollback_floor_fail_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            before = artifact_names(store)
            with self.assertRaisesRegex(retention.RetentionError, "trusted anchor"):
                self.call(store, trusted_pointer_sha256="f" * 64, apply=True)
            with self.assertRaisesRegex(retention.RetentionError, "rollback floor"):
                self.call(
                    store,
                    minimum_data_generated_at_utc=timestamp(START + timedelta(days=1)),
                    apply=True,
                )
            self.assertEqual(artifact_names(store), before)

    def test_symlink_and_malformed_namespace_fail_closed(self) -> None:
        for kind in ("symlink", "malformed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                store = create_store(Path(directory))
                before = artifact_names(store)
                if kind == "symlink":
                    name = f"{baseline.CONTRACT}.{'e' * 64}.json"
                    (store["artifacts"] / name).symlink_to(store["entries"][0][1])
                else:
                    (store["artifacts"] / f"{baseline.CONTRACT}.bad.json").write_text("{}\n")
                with self.assertRaises(retention.RetentionError):
                    self.call(store, apply=True)
                self.assertTrue(before.issubset(artifact_names(store)))

    def test_pointer_path_escape_is_rejected_even_when_resealed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            pointer = deepcopy(store["pointer"])
            pointer["artifact_file"] = "../escape.json"
            core = {key: value for key, value in pointer.items() if key != "pointer_payload_sha256"}
            pointer["pointer_payload_sha256"] = baseline.canonical_sha256(core)
            store["pointer_path"].write_bytes(baseline.canonical_bytes(pointer) + b"\n")
            store["pointer_sha256"] = hashlib.sha256(store["pointer_path"].read_bytes()).hexdigest()
            with self.assertRaises(retention.RetentionError):
                self.call(store, apply=True)

    def test_large_inventory_is_pruned_to_exact_hard_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(
                Path(directory), count=70, step=timedelta(minutes=1)
            )
            receipt = self.call(
                store,
                max_artifacts=retention.HARD_MAX_ARTIFACTS,
                apply=True,
            )
            self.assertEqual(receipt["after_artifacts"], 64)
            self.assertEqual(len(artifact_names(store)), 64)
            self.assertIn(store["pointer"]["artifact_file"], artifact_names(store))
            self.assertFalse(receipt["production_ready"])
            self.assertTrue(receipt["shared_exporter_lock_verified"])

    def test_unrelated_entry_is_reported_and_blocks_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            unrelated = store["artifacts"] / "operator-note.txt"
            unrelated.write_text("keep\n", encoding="utf-8")
            receipt = self.call(store)
            self.assertIn(unrelated.name, receipt["unknown_entries"])
            with self.assertRaisesRegex(retention.RetentionError, "unknown entries"):
                self.call(store, apply=True)
            self.assertTrue(unrelated.is_file())

    def test_unlocked_apply_requires_explicit_test_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            before = artifact_names(store)
            with self.assertRaisesRegex(retention.RetentionError, "disabled"):
                self.call(
                    store,
                    apply=True,
                    allow_unlocked_test_apply=False,
                )
            self.assertEqual(artifact_names(store), before)

    def test_hardlink_inventory_fails_before_any_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            before = artifact_names(store)
            original = store["entries"][0][1]
            linked_name = f"{baseline.CONTRACT}.{'e' * 64}.json"
            os.link(original, store["artifacts"] / linked_name)
            with self.assertRaises(retention.RetentionError):
                self.call(store, apply=True)
            self.assertTrue(before.issubset(artifact_names(store)))

    def test_export_and_retention_use_the_same_exclusive_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            candidate = fixture()
            retime(
                candidate,
                timestamp(START + timedelta(hours=5)),
                timestamp(START + timedelta(hours=13)),
            )
            completed = threading.Event()
            failure: list[BaseException] = []

            def export() -> None:
                try:
                    baseline.export_accepted_baseline(
                        store["artifacts"],
                        candidate,
                        now=START + timedelta(hours=5, minutes=1),
                    )
                except BaseException as exc:  # captured for the parent thread
                    failure.append(exc)
                finally:
                    completed.set()

            with baseline.exclusive_store_lock(store["artifacts"]):
                worker = threading.Thread(target=export, daemon=True)
                worker.start()
                time.sleep(0.05)
                self.assertFalse(completed.is_set())
            worker.join(timeout=2)
            self.assertTrue(completed.is_set())
            self.assertEqual(failure, [])

    def test_lock_symlink_and_candidate_substitution_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            lock_path = baseline.canonical_store_lock_path(store["artifacts"])
            lock_path.symlink_to(store["pointer_path"])
            with self.assertRaises(baseline.BaselineError):
                self.call(store)

        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            original = retention._quarantine_candidate_locked
            substituted = False

            def substitute(item, directory_fd, **kwargs):
                nonlocal substituted
                if not substituted:
                    substituted = True
                    item.path.unlink()
                    item.path.write_bytes(b"substituted\n")
                    item.path.chmod(0o600)
                return original(item, directory_fd, **kwargs)

            with mock.patch.object(
                retention, "_quarantine_candidate_locked", side_effect=substitute
            ), self.assertRaisesRegex(retention.RetentionError, "identity"):
                self.call(store, apply=True)
            operation = operation_record(store)
            self.assertEqual(operation["phase"], "failed")
            self.assertEqual(operation["completed_removals"], [])
            self.assertEqual(next(
                path.read_bytes()
                for path in store["artifacts"].iterdir()
                if path.read_bytes() == b"substituted\n"
            ), b"substituted\n")

    def test_partial_failure_is_durably_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory), count=5)
            original = retention._quarantine_candidate_locked
            calls = 0

            def fail_second(item, directory_fd, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise retention.RetentionError("injected partial failure")
                return original(item, directory_fd, **kwargs)

            with mock.patch.object(
                retention, "_quarantine_candidate_locked", side_effect=fail_second
            ), self.assertRaisesRegex(retention.RetentionError, "partial failure"):
                self.call(store, apply=True, max_artifacts=2)
            operation = operation_record(store)
            self.assertEqual(operation["phase"], "failed")
            self.assertEqual(len(operation["completed_removals"]), 1)
            self.assertEqual(operation["error"]["type"], "RetentionError")

    def test_rename_noreplace_rejects_injected_destination_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            original = retention._rename_noreplace_at
            observed: dict[str, str] = {}

            def race(directory_fd, source, destination):
                observed.update(source=source, destination=destination)
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.write(descriptor, b"racer-owned\n")
                os.fsync(descriptor)
                os.close(descriptor)
                return original(directory_fd, source, destination)

            with mock.patch.object(
                retention, "_rename_noreplace_at", side_effect=race
            ), self.assertRaisesRegex(retention.RetentionError, "EEXIST"):
                self.call(store, apply=True)
            self.assertTrue((store["artifacts"] / observed["source"]).is_file())
            self.assertEqual(
                (store["artifacts"] / observed["destination"]).read_bytes(),
                b"racer-owned\n",
            )
            self.assertEqual(operation_record(store)["phase"], "failed")

    def test_failed_quarantine_can_be_exactly_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            with mock.patch.object(
                retention,
                "_unlink_quarantined_locked",
                side_effect=retention.RetentionError("injected unlink failure"),
            ), self.assertRaisesRegex(retention.RetentionError, "unlink failure"):
                self.call(store, apply=True, max_artifacts=4)
            failed = operation_record(store)
            self.assertEqual(failed["phase"], "failed")
            self.assertEqual(len(failed["quarantine_state"]), 1)
            original_name = failed["planned_removals"][0]["name"]
            self.assertFalse((store["artifacts"] / original_name).exists())
            result = retention.reconcile_failed_quarantine(
                artifact_dir=store["artifacts"],
                receipt_path=store["receipt_path"],
                apply=True,
                allow_test_apply=True,
            )
            self.assertEqual(result["restored"], [original_name])
            self.assertTrue((store["artifacts"] / original_name).is_file())
            self.assertEqual(operation_record(store)["phase"], "reconciled")

    def test_pointer_cas_failure_can_append_distinct_failed_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            original = retention._replace_private_at
            injected = False

            def fail_quarantined_pointer(parent_fd, name, data):
                nonlocal injected
                pointer = json.loads(data)
                if pointer.get("phase") == "quarantined" and not injected:
                    injected = True
                    raise retention.RetentionError("injected pointer CAS failure")
                return original(parent_fd, name, data)

            with mock.patch.object(
                retention, "_replace_private_at", side_effect=fail_quarantined_pointer
            ), self.assertRaisesRegex(retention.RetentionError, "pointer CAS failure"):
                self.call(store, apply=True, max_artifacts=4)
            failed = operation_record(store)
            self.assertEqual(failed["phase"], "failed")
            self.assertEqual(len(failed["quarantine_state"]), 1)
            branch_records = list(
                store["receipt_path"].parent.glob(
                    f"{store['receipt_path'].name}.*.000003.*.json"
                )
            )
            self.assertEqual(len(branch_records), 2)
            self.assertNotEqual(
                branch_records[0].read_bytes(), branch_records[1].read_bytes()
            )
            result = retention.reconcile_failed_quarantine(
                artifact_dir=store["artifacts"],
                receipt_path=store["receipt_path"],
                apply=True,
                allow_test_apply=True,
            )
            self.assertEqual(len(result["restored"]), 1)
            self.assertEqual(operation_record(store)["phase"], "reconciled")

    def test_intent_interruption_is_receipt_bound_and_reconcilable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            with mock.patch.object(
                retention,
                "_quarantine_candidate_locked",
                side_effect=KeyboardInterrupt("intent interruption"),
            ), self.assertRaisesRegex(KeyboardInterrupt, "intent interruption"):
                self.call(store, apply=True, max_artifacts=4)
            intent = operation_record(store)
            self.assertEqual(intent["phase"], "intent")
            original = intent["current_item"]["name"]
            result = retention.reconcile_failed_quarantine(
                artifact_dir=store["artifacts"],
                receipt_path=store["receipt_path"],
                apply=True,
                allow_test_apply=True,
            )
            self.assertEqual(result["restored"], [])
            self.assertEqual(result["verified_existing"], [original])
            self.assertEqual(operation_record(store)["phase"], "reconciled")

    def test_quarantined_interruption_is_exactly_reconcilable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            with mock.patch.object(
                retention,
                "_unlink_quarantined_locked",
                side_effect=KeyboardInterrupt("quarantined interruption"),
            ), self.assertRaisesRegex(KeyboardInterrupt, "quarantined interruption"):
                self.call(store, apply=True, max_artifacts=4)
            quarantined = operation_record(store)
            self.assertEqual(quarantined["phase"], "quarantined")
            original = quarantined["current_item"]["name"]
            self.assertFalse((store["artifacts"] / original).exists())
            result = retention.reconcile_failed_quarantine(
                artifact_dir=store["artifacts"],
                receipt_path=store["receipt_path"],
                apply=True,
                allow_test_apply=True,
            )
            self.assertEqual(result["restored"], [original])
            self.assertTrue((store["artifacts"] / original).is_file())
            self.assertEqual(operation_record(store)["phase"], "reconciled")

    def test_post_unlink_interruption_is_recovered_as_bound_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            original_write = retention._atomic_write_operation_receipt
            interrupted = False

            def interrupt_deleted(path, receipt, *, create):
                nonlocal interrupted
                if receipt.get("phase") == "deleted" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("post-unlink interruption")
                return original_write(path, receipt, create=create)

            with mock.patch.object(
                retention,
                "_atomic_write_operation_receipt",
                side_effect=interrupt_deleted,
            ), self.assertRaisesRegex(KeyboardInterrupt, "post-unlink interruption"):
                self.call(store, apply=True, max_artifacts=4)
            quarantined = operation_record(store)
            self.assertEqual(quarantined["phase"], "quarantined")
            original = quarantined["current_item"]["name"]
            quarantine = quarantined["current_quarantine"]["quarantine_name"]
            self.assertFalse((store["artifacts"] / original).exists())
            self.assertFalse((store["artifacts"] / quarantine).exists())

            result = retention.reconcile_failed_quarantine(
                artifact_dir=store["artifacts"],
                receipt_path=store["receipt_path"],
                apply=True,
                allow_test_apply=True,
            )
            self.assertEqual(result["restored"], [])
            self.assertEqual(result["recovered_deleted"], [original])
            self.assertFalse((store["artifacts"] / original).exists())
            recovered = operation_record(store)
            self.assertEqual(recovered["phase"], "reconciled")
            self.assertEqual(
                [item["name"] for item in recovered["recovered_deleted_artifacts"]],
                [original],
            )
            self.assertIn(
                original,
                [item["name"] for item in recovered["completed_removals"]],
            )

    def test_post_restore_interruption_is_idempotently_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            with mock.patch.object(
                retention,
                "_unlink_quarantined_locked",
                side_effect=KeyboardInterrupt("quarantined interruption"),
            ), self.assertRaisesRegex(KeyboardInterrupt, "quarantined interruption"):
                self.call(store, apply=True, max_artifacts=4)
            quarantined = operation_record(store)
            self.assertEqual(quarantined["phase"], "quarantined")
            original = quarantined["current_item"]["name"]
            quarantine = quarantined["current_quarantine"]["quarantine_name"]

            original_write = retention._atomic_write_operation_receipt
            interrupted = False

            def interrupt_reconciled(path, receipt, *, create):
                nonlocal interrupted
                if receipt.get("phase") == "reconciled" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("post-restore interruption")
                return original_write(path, receipt, create=create)

            with mock.patch.object(
                retention,
                "_atomic_write_operation_receipt",
                side_effect=interrupt_reconciled,
            ), self.assertRaisesRegex(KeyboardInterrupt, "post-restore interruption"):
                retention.reconcile_failed_quarantine(
                    artifact_dir=store["artifacts"],
                    receipt_path=store["receipt_path"],
                    apply=True,
                    allow_test_apply=True,
                )
            self.assertTrue((store["artifacts"] / original).is_file())
            self.assertFalse((store["artifacts"] / quarantine).exists())
            self.assertEqual(operation_record(store)["phase"], "quarantined")

            result = retention.reconcile_failed_quarantine(
                artifact_dir=store["artifacts"],
                receipt_path=store["receipt_path"],
                apply=True,
                allow_test_apply=True,
            )
            self.assertEqual(result["restored"], [])
            self.assertEqual(result["already_restored"], [original])
            self.assertEqual(result["recovered_deleted"], [])
            self.assertTrue((store["artifacts"] / original).is_file())
            self.assertEqual(operation_record(store)["phase"], "reconciled")

    def test_failed_post_unlink_state_recovers_only_bound_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            original_unlink = retention._unlink_quarantined_locked

            def unlink_then_fail(evidence, directory_fd):
                original_unlink(evidence, directory_fd)
                raise retention.RetentionError("post-unlink failure")

            with mock.patch.object(
                retention,
                "_unlink_quarantined_locked",
                side_effect=unlink_then_fail,
            ), self.assertRaisesRegex(retention.RetentionError, "post-unlink failure"):
                self.call(store, apply=True, max_artifacts=4)
            failed = operation_record(store)
            self.assertEqual(failed["phase"], "failed")
            self.assertEqual(failed["quarantine_state"], [])
            original = failed["current_item"]["name"]
            self.assertFalse((store["artifacts"] / original).exists())

            result = retention.reconcile_failed_quarantine(
                artifact_dir=store["artifacts"],
                receipt_path=store["receipt_path"],
                apply=True,
                allow_test_apply=True,
            )
            self.assertEqual(result["recovered_deleted"], [original])
            self.assertEqual(result["restored"], [])
            self.assertEqual(operation_record(store)["phase"], "reconciled")

    def test_failed_post_restore_interruption_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            with mock.patch.object(
                retention,
                "_unlink_quarantined_locked",
                side_effect=retention.RetentionError("injected unlink failure"),
            ), self.assertRaisesRegex(retention.RetentionError, "unlink failure"):
                self.call(store, apply=True, max_artifacts=4)
            failed = operation_record(store)
            self.assertEqual(failed["phase"], "failed")
            original = failed["current_item"]["name"]
            quarantine = failed["quarantine_state"][0]["quarantine_name"]

            original_write = retention._atomic_write_operation_receipt
            interrupted = False

            def interrupt_reconciled(path, receipt, *, create):
                nonlocal interrupted
                if receipt.get("phase") == "reconciled" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("failed post-restore interruption")
                return original_write(path, receipt, create=create)

            with mock.patch.object(
                retention,
                "_atomic_write_operation_receipt",
                side_effect=interrupt_reconciled,
            ), self.assertRaisesRegex(
                KeyboardInterrupt, "failed post-restore interruption"
            ):
                retention.reconcile_failed_quarantine(
                    artifact_dir=store["artifacts"],
                    receipt_path=store["receipt_path"],
                    apply=True,
                    allow_test_apply=True,
                )
            self.assertTrue((store["artifacts"] / original).is_file())
            self.assertFalse((store["artifacts"] / quarantine).exists())
            self.assertEqual(operation_record(store)["phase"], "failed")

            result = retention.reconcile_failed_quarantine(
                artifact_dir=store["artifacts"],
                receipt_path=store["receipt_path"],
                apply=True,
                allow_test_apply=True,
            )
            self.assertEqual(result["already_restored"], [original])
            self.assertEqual(result["restored"], [])
            self.assertEqual(operation_record(store)["phase"], "reconciled")

    def test_exact_post_rescan_is_required_for_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            original = retention._scan_artifacts_locked
            calls = 0

            def inconsistent_post_scan(*args, **kwargs):
                nonlocal calls
                calls += 1
                artifacts, unknown = original(*args, **kwargs)
                if calls == 2:
                    return artifacts[:-1], unknown
                return artifacts, unknown

            with mock.patch.object(
                retention,
                "_scan_artifacts_locked",
                side_effect=inconsistent_post_scan,
            ), self.assertRaisesRegex(retention.RetentionError, "exact plan"):
                self.call(store, apply=True)
            operation = operation_record(store)
            self.assertEqual(operation["phase"], "failed")


class MonitorTests(unittest.TestCase):
    def call(
        self, store: dict[str, object], *, now: datetime | None = None,
        **overrides: object,
    ) -> dict[str, object]:
        arguments: dict[str, object] = {
            "artifact_dir": store["artifacts"],
            "pointer_path": store["pointer_path"],
            "trusted_pointer_sha256": store["pointer_sha256"],
            "minimum_data_generated_at_utc": store["minimum"],
            "now": now or START + timedelta(hours=5),
        }
        arguments.update(overrides)
        return monitor.evaluate(**arguments)

    def test_healthy_receipt_is_independent_and_15_minute_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            receipt = self.call(store)
            self.assertTrue(receipt["healthy"])
            self.assertIsNone(receipt["incident"])
            self.assertEqual(receipt["recommended_interval_seconds"], 900)
            self.assertEqual(receipt["artifact_sha256"], store["pointer"]["artifact_sha256"])
            self.assertFalse(receipt["production_ready"])
            self.assertFalse(receipt["lkg_watermark_enforced"])

    def test_expiry_emits_stable_unsent_incident_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            first = self.call(store, now=START + timedelta(hours=13))
            second = self.call(store, now=START + timedelta(hours=14))
            self.assertFalse(first["healthy"])
            self.assertEqual(first["incident"]["reason_code"], "artifact_expired")
            self.assertEqual(first["incident"]["incident_key"], second["incident"]["incident_key"])
            self.assertFalse(first["incident"]["delivery_attempted"])

    def test_anchor_corruption_and_rollback_have_distinct_incidents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            anchor = self.call(store, trusted_pointer_sha256="f" * 64)
            rollback = self.call(
                store,
                minimum_data_generated_at_utc=timestamp(START + timedelta(days=1)),
            )
            self.assertEqual(anchor["incident"]["reason_code"], "pointer_anchor_mismatch")
            self.assertEqual(rollback["incident"]["reason_code"], "pointer_rollback")
            self.assertNotEqual(anchor["incident"]["incident_key"], rollback["incident"]["incident_key"])

    def test_artifact_hash_corruption_is_degraded_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            pointed = store["artifacts"] / store["pointer"]["artifact_file"]
            pointed.write_bytes(pointed.read_bytes() + b" ")
            receipt = self.call(store)
            self.assertFalse(receipt["healthy"])
            self.assertEqual(receipt["incident"]["reason_code"], "artifact_hash_mismatch")
            self.assertFalse(receipt["incident"]["delivery_attempted"])

    def test_symlinked_pointer_and_missing_artifact_fail_closed(self) -> None:
        for kind in ("pointer_symlink", "missing_artifact"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                store = create_store(Path(directory))
                if kind == "pointer_symlink":
                    target = Path(directory) / "pointer-target.json"
                    store["pointer_path"].rename(target)
                    store["pointer_path"].symlink_to(target)
                else:
                    (store["artifacts"] / store["pointer"]["artifact_file"]).unlink()
                receipt = self.call(store)
                self.assertFalse(receipt["healthy"])
                self.assertIn(
                    receipt["incident"]["reason_code"],
                    {"path_alias", "path_missing", "artifact_missing"},
                )

    def test_static_boundary_has_no_network_delivery_or_production_paths(self) -> None:
        forbidden_imports = {"http", "requests", "socket", "smtplib", "subprocess", "urllib"}
        for module in (retention, monitor):
            source = Path(module.__file__).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in (
                    node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
                )
            }
            self.assertTrue(imported.isdisjoint(forbidden_imports))
            self.assertNotIn("/var/lib/sonardeals", source)
            self.assertNotIn("/opt/sonardeals", source)
            self.assertNotIn("systemctl", source)
        monitor_source = Path(monitor.__file__).read_text(encoding="utf-8")
        self.assertIn("import radar_rank_baseline", monitor_source)

    def test_store_cap_excess_is_degraded_before_pointer_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(
                Path(directory), count=monitor.HARD_MAX_ARTIFACTS + 1
            )
            receipt = self.call(store)
            self.assertFalse(receipt["healthy"])
            self.assertEqual(
                receipt["incident"]["reason_code"], "store_cap_exceeded"
            )

    def test_unrelated_store_entry_is_degraded_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = create_store(Path(directory))
            unrelated = store["artifacts"] / "README.operator"
            unrelated.write_text("keep\n", encoding="utf-8")
            receipt = self.call(store)
            self.assertFalse(receipt["healthy"])
            self.assertEqual(
                receipt["incident"]["reason_code"],
                "artifact_store_unknown_entries",
            )
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep\n")

    def test_canonical_validator_rejects_nested_and_validity_false_health(self) -> None:
        for mutation in ("empty_proof", "long_validity"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                store = create_store(Path(directory))
                pointed = store["artifacts"] / store["pointer"]["artifact_file"]
                artifact = json.loads(pointed.read_text(encoding="utf-8"))
                if mutation == "empty_proof":
                    artifact["proof"] = {}
                else:
                    artifact["valid_until_utc"] = timestamp(
                        START + timedelta(days=30)
                    )
                unsigned = {
                    key: value
                    for key, value in artifact.items()
                    if key != "artifact_payload_sha256"
                }
                artifact["artifact_payload_sha256"] = baseline.canonical_sha256(
                    unsigned
                )
                raw = baseline.canonical_bytes(artifact) + b"\n"
                digest = hashlib.sha256(raw).hexdigest()
                replacement = (
                    store["artifacts"]
                    / f"{baseline.CONTRACT}.{digest}.json"
                )
                replacement.write_bytes(raw)
                replacement.chmod(0o600)
                pointer = deepcopy(store["pointer"])
                pointer["artifact_file"] = replacement.name
                pointer["artifact_sha256"] = digest
                pointer["artifact_payload_sha256"] = artifact[
                    "artifact_payload_sha256"
                ]
                if mutation == "long_validity":
                    pointer["valid_until_utc"] = artifact["valid_until_utc"]
                core = {
                    key: value
                    for key, value in pointer.items()
                    if key != "pointer_payload_sha256"
                }
                pointer["pointer_payload_sha256"] = baseline.canonical_sha256(
                    core
                )
                store["pointer_path"].write_bytes(
                    baseline.canonical_bytes(pointer) + b"\n"
                )
                store["pointer_path"].chmod(0o600)
                store["pointer_sha256"] = hashlib.sha256(
                    store["pointer_path"].read_bytes()
                ).hexdigest()
                receipt = self.call(store)
                self.assertFalse(receipt["healthy"])
                self.assertEqual(
                    receipt["incident"]["reason_code"],
                    "artifact_canonical_validation_failed",
                )


if __name__ == "__main__":
    unittest.main()
