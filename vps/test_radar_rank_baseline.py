#!/usr/bin/env python3
"""Focused unit, golden, corruption, ordering and round-trip baseline tests."""

from __future__ import annotations

import ast
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

try:
    from . import radar_rank_baseline as baseline
except ImportError:
    import radar_rank_baseline as baseline


NOW = datetime.fromisoformat("2026-08-15T01:00:00+00:00")


def _offer(index: int) -> dict[str, object]:
    price = 10_000 + index
    lower = 15_000
    median = 16_000
    savings = lower - price
    conservative_bps = baseline.publisher.integer(10_000 * savings / lower)
    median_bps = baseline.publisher.integer(10_000 * (median - price) / median)
    return {
        "id": hashlib.sha256(f"offer-{index}".encode()).hexdigest(),
        "m": "model-a",
        "t": f"Model A offer {index}",
        "p": price,
        "q1": lower,
        "mp": median,
        "sv": savings,
        "sp": round(conservative_bps / 100, 2),
        "dp": round(median_bps / 100, 2),
        "pn": 40,
        "ps": 5,
        "pc": 3,
        "y": 2025,
        "km": 10_000,
        "f": "petrol",
        "c": "DE",
        "s": "source-a",
        "u": f"https://offers.example/{index}",
        "ls": "2026-08-15T00:00:00+00:00",
        "v": 1,
    }


def fixture() -> dict[str, object]:
    offers = sorted((_offer(index) for index in range(50)), key=baseline.publisher.rank_key)
    selection = [
        {
            "rank": rank,
            "public_offer_id": offer["id"],
            "normalized_url": offer["u"],
            "rank_tuple": baseline.rank_tuple(offer),
            "compact_payload": offer,
        }
        for rank, offer in enumerate(offers, 1)
    ]
    peers = [
        {
            "model": "model-a",
            "year": 2025,
            "fuel": "petrol",
            "mileage_band_start_km": 0,
            "excluded_source_family": "source-a",
            "lower_quartile_eur": 15_000,
            "median_eur": 16_000,
            "peer_count": 40,
            "peer_source_count": 5,
            "peer_country_count": 3,
            "peer_dispersion_bps": 500,
        }
    ]
    selected_ids = baseline.publisher.digest_ids(offers)
    selected_fields = baseline.publisher.digest_fields(offers)
    generation = hashlib.sha256(
        (
            f"{baseline.publisher.ALGORITHM_VERSION}\n2026-08-15T00:00:00+00:00\n"
            f"{selected_fields}\n{selected_fields}\n"
        ).encode()
    ).hexdigest()[:16]
    hashes = {
        key: hashlib.sha256(key.encode()).hexdigest()
        for key in baseline.HASH_FIELDS
    }
    hashes.update(
        {
            "quarantine_manifest_sha256": None,
            "candidate_ids_sha256": selected_ids,
            "selected_ids_sha256": selected_ids,
            "candidate_fields_sha256": selected_fields,
            "selected_fields_sha256": selected_fields,
            "ranking_contract_sha256": baseline.canonical_sha256(baseline.RANKING_CONTRACT),
            "source_family_contract_sha256": baseline.canonical_sha256(baseline.source_family_contract()),
            "peer_stats_sha256": baseline.canonical_sha256(peers),
            "published_selection_sha256": baseline.canonical_sha256(selection),
        }
    )
    value: dict[str, object] = {
        "contract": baseline.CONTRACT,
        "schema_version": baseline.SCHEMA_VERSION,
        "algorithm": baseline.publisher.ALGORITHM_VERSION,
        "generation_id": generation,
        "data_generated_at_utc": "2026-08-15T00:00:00+00:00",
        "valid_until_utc": "2026-08-15T08:00:00+00:00",
        "ranking_contract": dict(baseline.RANKING_CONTRACT),
        "source_family_contract": baseline.source_family_contract(),
        "proof": {
            "published_verified_count": 50,
            "verified_target": 10_000,
            "target_reached": False,
            "pool_exhausted": True,
            "ranked_pool_count": 50,
            "ranked_universe_exhausted": True,
            "full_input_coverage": True,
            "direct_attempted_count": 50,
            "browser_target_count": 0,
            "browser_attempted_count": 0,
            "selection_horizon_rank": 50,
            "selection_audit_pass": True,
            "live_convergence_pass": True,
        },
        "hashes": hashes,
        "code_provenance": baseline.current_code_provenance(),
        "cutoffs": {
            "rank_50": {key: selection[49][key] for key in baseline.CUTOFF_ROW_FIELDS},
            "rank_horizon": {key: selection[-1][key] for key in baseline.CUTOFF_ROW_FIELDS},
        },
        "peer_stats": peers,
        "published_selection": selection,
    }
    value["artifact_payload_sha256"] = baseline.canonical_sha256(value)
    return value


def resign(value: dict[str, object]) -> None:
    value["artifact_payload_sha256"] = baseline.canonical_sha256(
        {key: item for key, item in value.items() if key != "artifact_payload_sha256"}
    )


def retime(
    value: dict[str, object], data_generated_at_utc: str, valid_until_utc: str,
) -> None:
    value["data_generated_at_utc"] = data_generated_at_utc
    value["valid_until_utc"] = valid_until_utc
    hashes = value["hashes"]
    value["generation_id"] = hashlib.sha256(
        (
            f"{value['algorithm']}\n{data_generated_at_utc}\n"
            f"{hashes['candidate_fields_sha256']}\n"
            f"{hashes['selected_fields_sha256']}\n"
        ).encode("utf-8")
    ).hexdigest()[:16]
    resign(value)


def cli_inputs() -> list[str]:
    return [
        "--ranked-board", "ranked.json",
        "--source-board", "source.json",
        "--validation-report", "validation.json",
        "--publication-manifest", "publication.json",
        "--selection-audit", "selection-audit.json",
        "--live-audit", "live-audit.json",
        "--source-policy", "source-policy.json",
    ]


class RankBaselineTests(unittest.TestCase):
    def test_golden_round_trip_and_content_address(self) -> None:
        value = fixture()
        receipt = baseline.validate_baseline_structure(value)
        self.assertEqual(receipt["published_verified_count"], 50)
        freshness = baseline.assess_baseline_freshness(value, now=NOW)
        self.assertEqual(freshness["freshness_status"], "fresh")
        self.assertEqual(freshness["age_seconds"], 3600)
        encoded = baseline.artifact_bytes(value)
        decoded = baseline.loads_strict(encoded, "golden")
        self.assertEqual(decoded, value)
        # This is a golden over the complete deterministic fixture and current
        # exporter contract, not merely over a subset of fields.
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            "3a750a796cac86d1fcd85f10be2f3f16ff326dd7a3a99b2063df40af580a1817",
        )
        with tempfile.TemporaryDirectory() as directory:
            first_path, first_hash = baseline.write_content_addressed(Path(directory), value)
            second_path, second_hash = baseline.write_content_addressed(Path(directory), value)
            self.assertEqual((first_path, first_hash), (second_path, second_hash))
            self.assertEqual(first_path.read_bytes(), encoded)

    def test_duplicate_keys_nan_unknown_and_internal_corruption_reject(self) -> None:
        with self.assertRaisesRegex(baseline.BaselineError, "duplicate JSON key"):
            baseline.loads_strict(b'{"x":1,"x":2}', "duplicate")
        with self.assertRaisesRegex(baseline.BaselineError, "non-finite"):
            baseline.loads_strict(b'{"x":NaN}', "nan")
        unknown = fixture()
        unknown["surprise"] = True
        resign(unknown)
        with self.assertRaisesRegex(baseline.BaselineError, "fields differ"):
            baseline.validate_baseline_structure(unknown)
        corrupt = fixture()
        corrupt["published_selection"][0]["compact_payload"]["p"] += 1
        with self.assertRaisesRegex(baseline.BaselineError, "internal payload hash"):
            baseline.validate_baseline_structure(corrupt)

    def test_reordered_rows_and_bad_cutoff_reject_even_when_resigned(self) -> None:
        reordered = fixture()
        rows = reordered["published_selection"]
        rows[0], rows[1] = rows[1], rows[0]
        reordered["hashes"]["published_selection_sha256"] = baseline.canonical_sha256(rows)
        resign(reordered)
        with self.assertRaisesRegex(baseline.BaselineError, "selection row 1 contract"):
            baseline.validate_baseline_structure(reordered)
        cutoff = fixture()
        cutoff["cutoffs"]["rank_50"]["public_offer_id"] = "f" * 64
        resign(cutoff)
        with self.assertRaisesRegex(baseline.BaselineError, "cutoffs"):
            baseline.validate_baseline_structure(cutoff)

    def test_structural_history_survives_expiry_but_freshness_fails_at_boundary(self) -> None:
        value = fixture()
        expired_at = datetime.fromisoformat("2026-08-15T08:00:00+00:00")
        self.assertEqual(
            baseline.validate_baseline_structure(value)["valid_until_utc"],
            "2026-08-15T08:00:00+00:00",
        )
        self.assertEqual(
            baseline.assess_baseline_freshness(value, now=expired_at)["freshness_status"],
            "expired",
        )
        baseline.require_fresh_baseline(
            value, now=datetime.fromisoformat("2026-08-15T07:59:59.999999+00:00")
        )
        with self.assertRaisesRegex(baseline.BaselineError, "expired"):
            baseline.require_fresh_baseline(value, now=expired_at)

        incomplete = fixture()
        incomplete["proof"]["pool_exhausted"] = False
        resign(incomplete)
        with self.assertRaisesRegex(baseline.BaselineError, "pool-exhaustion"):
            baseline.validate_baseline_structure(incomplete)

    def test_historical_code_provenance_is_structural_but_compatibility_is_opt_in(self) -> None:
        historical = fixture()
        historical["code_provenance"]["exporter_code_sha256"] = "f" * 64
        resign(historical)
        baseline.validate_baseline_structure(historical)
        self.assertEqual(
            baseline.assess_code_provenance(historical)["code_provenance_status"],
            "declared_unanchored",
        )
        with self.assertRaisesRegex(baseline.BaselineError, "trusted artifact anchor"):
            baseline.require_current_code_compatibility(historical)
        with tempfile.TemporaryDirectory() as directory:
            artifact, artifact_sha256 = baseline.write_content_addressed(
                Path(directory), historical
            )
            with self.assertRaisesRegex(baseline.BaselineError, "code provenance mismatch"):
                baseline.require_current_code_compatibility(
                    historical,
                    artifact_path=artifact,
                    trusted_artifact_sha256=artifact_sha256,
                )

    def test_self_asserted_current_hashes_remain_unanchored_in_standalone_reconstruction(self) -> None:
        forged = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact, artifact_sha256 = baseline.write_content_addressed(root, forged)
            base_args = ["validate", *cli_inputs(), "--artifact", str(artifact)]

            stdout = io.StringIO()
            with mock.patch.object(
                baseline, "_build_from_args", return_value=deepcopy(forged)
            ), redirect_stdout(stdout):
                self.assertEqual(baseline.main(base_args), 0)
            receipt = json.loads(stdout.getvalue())
            self.assertEqual(receipt["code_provenance_status"], "declared_unanchored")
            self.assertEqual(receipt["artifact_anchor_status"], "unanchored")

            stderr = io.StringIO()
            with mock.patch.object(
                baseline, "_build_from_args", return_value=deepcopy(forged)
            ), redirect_stderr(stderr):
                self.assertEqual(
                    baseline.main([*base_args, "--require-current-code-compatibility"]),
                    1,
                )
            self.assertIn("trusted artifact anchor", stderr.getvalue())

            stdout = io.StringIO()
            with mock.patch.object(
                baseline, "_build_from_args", return_value=deepcopy(forged)
            ), redirect_stdout(stdout):
                self.assertEqual(
                    baseline.main([
                        *base_args,
                        "--trusted-artifact-sha256", artifact_sha256,
                        "--require-current-code-compatibility",
                    ]),
                    0,
                )
            anchored = json.loads(stdout.getvalue())
            self.assertEqual(
                anchored["code_provenance_status"], "compatible_anchored"
            )
            self.assertEqual(anchored["artifact_anchor_status"], "trusted_sha256")

    def test_latest_accepted_pointer_binds_generation_and_evidence_hashes(self) -> None:
        value = fixture()
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "artifacts"
            artifact, artifact_sha256 = baseline.write_content_addressed(
                output_dir, value
            )
            pointer = baseline.build_latest_accepted_pointer(
                value, artifact, artifact_sha256
            )
            receipt = baseline.validate_latest_accepted_pointer(
                pointer, output_dir, now=NOW
            )
            self.assertEqual(
                receipt["result"], "RADAR_RANK_BASELINE_LATEST_ACCEPTED_V2_PASS"
            )
            self.assertEqual(receipt["generation_id"], value["generation_id"])
            self.assertEqual(receipt["artifact_sha256"], artifact_sha256)

            for field, replacement in (
                ("generation_id", "f" * 16),
                ("live_convergence_audit_sha256", "f" * 64),
                ("selected_fields_sha256", "e" * 64),
            ):
                corrupt = deepcopy(pointer)
                corrupt[field] = replacement
                core = {
                    key: item for key, item in corrupt.items()
                    if key != "pointer_payload_sha256"
                }
                corrupt["pointer_payload_sha256"] = baseline.canonical_sha256(core)
                with self.subTest(field=field), self.assertRaisesRegex(
                    baseline.BaselineError, f"latest-accepted pointer mismatch at {field}"
                ):
                    baseline.validate_latest_accepted_pointer(corrupt, output_dir, now=NOW)

            forged_anchor = deepcopy(pointer)
            forged_anchor["artifact_sha256"] = "f" * 64
            forged_anchor["artifact_file"] = f"{baseline.CONTRACT}.{'f' * 64}.json"
            forged_core = {
                key: item for key, item in forged_anchor.items()
                if key != "pointer_payload_sha256"
            }
            forged_anchor["pointer_payload_sha256"] = baseline.canonical_sha256(
                forged_core
            )
            with self.assertRaisesRegex(
                baseline.BaselineError, "pointer artifact SHA-256 mismatch"
            ):
                baseline.validate_latest_accepted_anchor(
                    forged_anchor, value, artifact_sha256, now=NOW
                )

            expired_receipt = baseline.validate_latest_accepted_pointer(
                pointer,
                output_dir,
                now=datetime.fromisoformat("2026-08-15T08:00:00+00:00"),
            )
            self.assertEqual(expired_receipt["freshness_status"], "expired")
            self.assertEqual(expired_receipt["valid_until_utc"], value["valid_until_utc"])

    def test_latest_accepted_pointer_is_atomic_idempotent_and_preserves_prior_on_failure(self) -> None:
        value = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "artifacts"
            artifact, artifact_sha256 = baseline.write_content_addressed(
                output_dir, value
            )
            pointer = baseline.build_latest_accepted_pointer(
                value, artifact, artifact_sha256
            )
            latest = root / "latest_accepted.json"
            self.assertTrue(baseline.write_latest_accepted_pointer(latest, pointer))
            self.assertEqual(latest.stat().st_mode & 0o777, 0o600)
            before = latest.read_bytes()
            before_inode = latest.stat().st_ino
            self.assertFalse(baseline.write_latest_accepted_pointer(latest, pointer))
            self.assertEqual(latest.read_bytes(), before)
            self.assertEqual(latest.stat().st_ino, before_inode)

            replacement = deepcopy(pointer)
            replacement["generation_id"] = "f" * 16
            replacement["data_generated_at_utc"] = "2026-08-15T00:00:01+00:00"
            core = {
                key: item for key, item in replacement.items()
                if key != "pointer_payload_sha256"
            }
            replacement["pointer_payload_sha256"] = baseline.canonical_sha256(core)
            with mock.patch.object(
                baseline.os, "replace", side_effect=OSError("injected replace failure")
            ), self.assertRaisesRegex(OSError, "injected replace failure"):
                baseline.write_latest_accepted_pointer(latest, replacement)
            self.assertEqual(latest.read_bytes(), before)
            self.assertEqual(
                list(root.glob(f".{latest.name}.*")), [],
                "failed pointer replacement left a temporary file",
            )

    def test_latest_accepted_pointer_cannot_regress_or_conflict_at_same_timestamp(self) -> None:
        value = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "artifacts"
            artifact, artifact_sha256 = baseline.write_content_addressed(
                output_dir, value
            )
            latest_pointer = baseline.build_latest_accepted_pointer(
                value, artifact, artifact_sha256
            )
            latest = root / "latest_accepted.json"
            baseline.write_latest_accepted_pointer(latest, latest_pointer)
            before = latest.read_bytes()

            older = deepcopy(latest_pointer)
            older["data_generated_at_utc"] = "2026-08-14T23:59:59+00:00"
            older["valid_until_utc"] = "2026-08-15T07:59:59+00:00"
            older_core = {
                key: item for key, item in older.items()
                if key != "pointer_payload_sha256"
            }
            older["pointer_payload_sha256"] = baseline.canonical_sha256(older_core)
            with self.assertRaisesRegex(baseline.BaselineError, "regress"):
                baseline.write_latest_accepted_pointer(latest, older)
            self.assertEqual(latest.read_bytes(), before)

            conflict = deepcopy(latest_pointer)
            conflict["generation_id"] = "f" * 16
            conflict_core = {
                key: item for key, item in conflict.items()
                if key != "pointer_payload_sha256"
            }
            conflict["pointer_payload_sha256"] = baseline.canonical_sha256(
                conflict_core
            )
            with self.assertRaisesRegex(baseline.BaselineError, "conflicting generation"):
                baseline.write_latest_accepted_pointer(latest, conflict)
            self.assertEqual(latest.read_bytes(), before)

    def test_future_candidate_cannot_poison_or_wedge_latest_accepted_pointer(self) -> None:
        value = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "artifacts"
            artifact, artifact_sha256 = baseline.write_content_addressed(
                output_dir, value
            )
            pointer = baseline.build_latest_accepted_pointer(
                value, artifact, artifact_sha256, now=NOW
            )
            latest = root / "latest_accepted.json"
            baseline.write_latest_accepted_pointer(latest, pointer, now=NOW)
            normal = latest.read_bytes()

            future = deepcopy(pointer)
            future["generation_id"] = "f" * 16
            future["data_generated_at_utc"] = "2099-01-01T00:00:00+00:00"
            future["valid_until_utc"] = "2099-01-01T08:00:00+00:00"
            future_core = {
                key: item for key, item in future.items()
                if key != "pointer_payload_sha256"
            }
            future["pointer_payload_sha256"] = baseline.canonical_sha256(future_core)
            with self.assertRaisesRegex(baseline.BaselineError, "future latest-accepted"):
                baseline.write_latest_accepted_pointer(latest, future, now=NOW)
            self.assertEqual(latest.read_bytes(), normal)

            boundary = deepcopy(pointer)
            boundary["generation_id"] = "e" * 16
            boundary["data_generated_at_utc"] = "2026-08-15T01:05:00+00:00"
            boundary["valid_until_utc"] = "2026-08-15T09:05:00+00:00"
            boundary_core = {
                key: item for key, item in boundary.items()
                if key != "pointer_payload_sha256"
            }
            boundary["pointer_payload_sha256"] = baseline.canonical_sha256(boundary_core)
            self.assertTrue(
                baseline.write_latest_accepted_pointer(latest, boundary, now=NOW)
            )
            self.assertEqual(
                json.loads(latest.read_text(encoding="utf-8"))["data_generated_at_utc"],
                "2026-08-15T01:05:00+00:00",
            )

            one_second_too_far = deepcopy(boundary)
            one_second_too_far["generation_id"] = "d" * 16
            one_second_too_far["data_generated_at_utc"] = "2026-08-15T01:05:01+00:00"
            one_second_too_far["valid_until_utc"] = "2026-08-15T09:05:01+00:00"
            too_far_core = {
                key: item for key, item in one_second_too_far.items()
                if key != "pointer_payload_sha256"
            }
            one_second_too_far["pointer_payload_sha256"] = baseline.canonical_sha256(
                too_far_core
            )
            with self.assertRaisesRegex(baseline.BaselineError, "future latest-accepted"):
                baseline.write_latest_accepted_pointer(
                    latest, one_second_too_far, now=NOW
                )
            self.assertEqual(
                json.loads(latest.read_text(encoding="utf-8"))["data_generated_at_utc"],
                "2026-08-15T01:05:00+00:00",
            )

            future_baseline = fixture()
            retime(
                future_baseline,
                "2099-01-01T00:00:00+00:00",
                "2099-01-01T08:00:00+00:00",
            )
            future_artifact, future_sha256 = baseline.write_content_addressed(
                output_dir, future_baseline
            )
            with self.assertRaisesRegex(baseline.BaselineError, "too far in the future"):
                baseline.build_latest_accepted_pointer(
                    future_baseline, future_artifact, future_sha256, now=NOW
                )

            export_dir = root / "future-export"
            stderr = io.StringIO()
            with mock.patch.object(
                baseline, "_build_from_args", return_value=future_baseline
            ), redirect_stderr(stderr):
                self.assertEqual(
                    baseline.main([
                        "export", *cli_inputs(), "--output-dir", str(export_dir),
                    ]),
                    1,
                )
            self.assertIn("too far in the future", stderr.getvalue())
            self.assertFalse(export_dir.exists())

    def test_static_dark_boundary(self) -> None:
        tree = ast.parse(Path(baseline.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(
            imported.isdisjoint({"urllib", "requests", "httpx", "smtplib", "sqlite3", "subprocess"})
        )
        forbidden_calls = {"publish", "urlopen", "sendmail", "SMTP"}
        observed_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        } | {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(observed_calls.isdisjoint(forbidden_calls))


if __name__ == "__main__":
    unittest.main()
