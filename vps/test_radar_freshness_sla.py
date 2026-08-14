#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from . import radar_freshness_sla as sla
except ImportError:
    import radar_freshness_sla as sla


class RadarFreshnessSlaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        self.algorithm = "schengen-observed-peer-value-v7-live-verified"
        self.data_at = self.now - timedelta(hours=3)
        self.validation_at = self.now - timedelta(hours=2)
        self.publication_at = self.now - timedelta(hours=1)
        self.hashes = {
            "candidate_ids": "1" * 64,
            "selected_ids": "2" * 64,
            "candidate_fields": "3" * 64,
            "selected_fields": "4" * 64,
            "snapshot": "5" * 64,
            "offer_fields": "6" * 64,
        }
        self.refresh_evidence()

    @staticmethod
    def timestamp(value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    def refresh_evidence(self) -> None:
        data = self.timestamp(self.data_at)
        validation_time = self.timestamp(self.validation_at)
        publication_time = self.timestamp(self.publication_at)
        generation = hashlib.sha256(
            (
                f"{self.algorithm}\n{data}\n"
                f"{self.hashes['candidate_fields']}\n"
                f"{self.hashes['selected_fields']}\n"
            ).encode("utf-8")
        ).hexdigest()[:16]
        self.payload = {
            "schema_version": 2,
            "algorithm": self.algorithm,
            "selection_algorithm": self.algorithm,
            "generation_id": generation,
            "data_generated_at_utc": data,
            "universe_last_seen_at": data,
            "published_at_utc": publication_time,
            "published_offer_count": 10_000,
            "verified_live_count": 10_000,
            "count": 10_000,
            "offers": [0] * 10_000,
            "selection_candidate_sha256": self.hashes["candidate_ids"],
            "selected_ids_sha256": self.hashes["selected_ids"],
            "selection_candidate_fields_sha256": self.hashes["candidate_fields"],
            "selected_fields_sha256": self.hashes["selected_fields"],
            "snapshot_eligible_sha256": self.hashes["snapshot"],
            "offer_fields_sha256": self.hashes["offer_fields"],
        }
        self.validation = {
            "schema_version": 1,
            "input_updated_at": data,
            "generated_at": validation_time,
            "input_algorithm": self.algorithm,
            "input_snapshot_sha256": self.hashes["snapshot"],
            "input_offer_fields_sha256": self.hashes["offer_fields"],
            "checked": 60_000,
            "ranked_pool_count": 60_000,
            "direct_attempted_count": 60_000,
            "verified_target": 10_000,
            "target_reached": True,
            "browser_frontier_complete": True,
            "full_input_coverage": True,
            "counts": {"verified": 10_000, "dead": 1_000, "unknown": 49_000},
        }
        shared = {
            "schema_version": 1,
            "algorithm": self.algorithm,
            "generation_id": generation,
            "data_generated_at_utc": data,
            "published_offer_count": 10_000,
            "verified_live_count": 10_000,
            "candidate_ids_sha256": self.hashes["candidate_ids"],
            "selected_ids_sha256": self.hashes["selected_ids"],
            "candidate_fields_sha256": self.hashes["candidate_fields"],
            "selected_fields_sha256": self.hashes["selected_fields"],
            "snapshot_eligible_sha256": self.hashes["snapshot"],
        }
        self.publication = {**shared, "prepared_at": publication_time}
        self.convergence = {
            **shared,
            "result": "LIVE_GENERATION_AUDIT_PASS",
            "expected_generation": generation,
            "observed_generation": generation,
            "universe_unique_offers": 60_000,
            "qualified_universe_offers": 10_000,
            "full_ranked_input_offers": 60_000,
            "ranking_qualified_offers": 37_897,
            "ranking_saved_offers": 60_000,
            "ranking_saved_observed_offers": 60_000,
            "connected_country_count": 28,
            "connected_source_count": 103,
            "attempts": 1,
            "network_errors": 0,
            "deadline_sec": 600.0,
            "elapsed_sec": 2.5,
            **{field: True for field in sla.CONVERGENCE_TRUE_FIELDS},
            **{field: 0 for field in sla.CONVERGENCE_ZERO_FIELDS},
        }

    def write_evidence(self) -> list[str]:
        arguments: list[str] = []
        for label, value in (
            ("payload", self.payload),
            ("validation", self.validation),
            ("publication", self.publication),
            ("convergence", self.convergence),
        ):
            path = self.root / f"{label}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            arguments.extend((f"--{label}", str(path)))
        arguments.extend(("--allow-test-now", "--now", self.timestamp(self.now)))
        return arguments

    def run_cli(self) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = sla.main(self.write_evidence())
        return code, json.loads(stdout.getvalue())

    def test_healthy_report_distinguishes_all_three_ages(self) -> None:
        code, report = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "RADAR_FRESHNESS_SLA_HEALTHY")
        self.assertEqual(report["generation_id"], self.payload["generation_id"])
        self.assertEqual(report["published_offer_count"], 10_000)
        self.assertEqual(report["ages"]["data_observed"]["age_hours"], 3.0)
        self.assertEqual(report["ages"]["validation"]["age_hours"], 2.0)
        self.assertEqual(report["ages"]["publication"]["age_hours"], 1.0)
        self.assertTrue(all(report["checks"].values()))

    def test_every_status_boundary_is_exact(self) -> None:
        cases = (
            (timedelta(hours=4) - timedelta(seconds=1), "healthy", 0),
            (timedelta(hours=4), "warn", 0),
            (timedelta(hours=5) - timedelta(seconds=1), "warn", 0),
            (timedelta(hours=5), "fallback", sla.EXIT_FALLBACK),
            (timedelta(hours=6) - timedelta(seconds=1), "fallback", sla.EXIT_FALLBACK),
            (timedelta(hours=6), "breach", sla.EXIT_BREACH),
        )
        for age, expected_status, expected_code in cases:
            with self.subTest(age=age):
                self.data_at = self.now - age
                self.validation_at = self.data_at + timedelta(minutes=10)
                self.publication_at = self.validation_at + timedelta(minutes=10)
                self.refresh_evidence()
                code, report = self.run_cli()
                self.assertEqual(code, expected_code)
                self.assertEqual(report["status"], expected_status)
                self.assertEqual(
                    report["ages"]["data_observed"]["status"], expected_status
                )

    def test_generation_mismatch_is_invalid(self) -> None:
        self.publication["generation_id"] = "f" * 16
        code, report = self.run_cli()
        self.assertEqual(code, sla.EXIT_INVALID)
        self.assertEqual(report["status"], "invalid")
        self.assertIn("generation does not match", report["error"])

    def test_unbound_generation_is_invalid(self) -> None:
        replacement = deepcopy(self.payload)
        replacement["generation_id"] = "a" * 16
        self.payload = replacement
        self.publication["generation_id"] = "a" * 16
        self.convergence["generation_id"] = "a" * 16
        self.convergence["expected_generation"] = "a" * 16
        self.convergence["observed_generation"] = "a" * 16
        code, report = self.run_cli()
        self.assertEqual(code, sla.EXIT_INVALID)
        self.assertIn("generation_id is not bound", report["error"])

    def test_hash_mismatch_is_invalid(self) -> None:
        self.convergence["selected_fields_sha256"] = "a" * 64
        code, report = self.run_cli()
        self.assertEqual(code, sla.EXIT_INVALID)
        self.assertIn("hashes do not converge", report["error"])

    def test_validation_binding_mismatch_is_invalid(self) -> None:
        self.validation["input_offer_fields_sha256"] = "a" * 64
        code, report = self.run_cli()
        self.assertEqual(code, sla.EXIT_INVALID)
        self.assertIn("validation offer hash", report["error"])

    def test_any_present_public_count_must_be_exactly_ten_thousand(self) -> None:
        for artifact, field in (
            ("payload", "count"),
            ("publication", "published_offer_count"),
            ("convergence", "verified_live_count"),
        ):
            with self.subTest(artifact=artifact, field=field):
                self.refresh_evidence()
                getattr(self, artifact)[field] = 9_999
                code, report = self.run_cli()
                self.assertEqual(code, sla.EXIT_INVALID)
                self.assertIn("must equal 10000", report["error"])

    def test_validation_may_legitimately_overshoot_verified_target(self) -> None:
        self.validation["counts"] = {
            "verified": 10_045,
            "dead": 1_000,
            "unknown": 48_955,
        }
        code, report = self.run_cli()
        self.assertEqual(code, sla.EXIT_OK)
        self.assertEqual(report["status"], "healthy")

    def test_required_proof_fields_fail_closed_when_missing(self) -> None:
        mutants = (
            ("payload", ("offers",)),
            ("payload", ("count",)),
            ("payload", ("published_offer_count",)),
            ("payload", ("verified_live_count",)),
            ("publication", ("published_offer_count",)),
            ("publication", ("verified_live_count",)),
            ("validation", ("checked",)),
            ("validation", ("counts",)),
            ("validation", ("counts", "verified")),
            ("validation", ("verified_target",)),
            ("validation", ("target_reached",)),
            ("validation", ("browser_frontier_complete",)),
            ("validation", ("full_input_coverage",)),
            ("validation", ("ranked_pool_count",)),
            ("validation", ("direct_attempted_count",)),
            ("validation", ("input_algorithm",)),
            ("validation", ("input_snapshot_sha256",)),
            ("validation", ("input_offer_fields_sha256",)),
            ("convergence", ("result",)),
            ("convergence", ("schema_version",)),
            ("convergence", ("expected_generation",)),
            ("convergence", ("observed_generation",)),
            ("convergence", ("published_offer_count",)),
            ("convergence", ("verified_live_count",)),
            ("convergence", ("attempts",)),
            ("convergence", ("deadline_sec",)),
            ("convergence", (sla.CONVERGENCE_TRUE_FIELDS[0],)),
            ("convergence", (sla.CONVERGENCE_ZERO_FIELDS[0],)),
        )
        for artifact, path in mutants:
            with self.subTest(artifact=artifact, path=".".join(path)):
                self.refresh_evidence()
                owner = getattr(self, artifact)
                for key in path[:-1]:
                    owner = owner[key]
                del owner[path[-1]]
                code, report = self.run_cli()
                self.assertEqual(code, sla.EXIT_INVALID)
                self.assertEqual(report["status"], "invalid")

    def test_every_convergence_pass_invariant_is_mandatory(self) -> None:
        for field in (*sla.CONVERGENCE_TRUE_FIELDS, *sla.CONVERGENCE_ZERO_FIELDS):
            with self.subTest(field=field):
                self.refresh_evidence()
                del self.convergence[field]
                code, report = self.run_cli()
                self.assertEqual(code, sla.EXIT_INVALID)
                self.assertIn(f"convergence.{field}", report["error"])

    def test_now_override_requires_explicit_test_only_gate(self) -> None:
        arguments = self.write_evidence()
        arguments.remove("--allow-test-now")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                sla.parse_args(arguments)
        self.assertEqual(failure.exception.code, 2)

    def test_nonpassing_convergence_is_invalid(self) -> None:
        self.convergence["result"] = "DEPLOYMENT_PENDING_TIMEOUT"
        code, report = self.run_cli()
        self.assertEqual(code, sla.EXIT_INVALID)
        self.assertIn("LIVE_GENERATION_AUDIT_PASS", report["error"])

    def test_future_or_out_of_order_timestamps_are_invalid(self) -> None:
        self.validation["generated_at"] = self.timestamp(self.now + timedelta(seconds=1))
        code, report = self.run_cli()
        self.assertEqual(code, sla.EXIT_INVALID)
        self.assertIn("timestamps violate", report["error"])

    def test_malformed_and_duplicate_key_json_are_invalid(self) -> None:
        arguments = self.write_evidence()
        payload_path = Path(arguments[1])
        for malformed in ('{"schema_version":', '{"schema_version":2,"schema_version":2}'):
            with self.subTest(malformed=malformed):
                payload_path.write_text(malformed, encoding="utf-8")
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = sla.main(arguments)
                report = json.loads(stdout.getvalue())
                self.assertEqual(code, sla.EXIT_INVALID)
                self.assertEqual(report["result"], "RADAR_FRESHNESS_SLA_INVALID")
                self.assertTrue(report["error"])


if __name__ == "__main__":
    unittest.main()
