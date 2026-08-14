#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit_best_selection as selection_audit
import audit_live_convergence as live_audit


V7_ALGORITHM = "schengen-observed-peer-value-v7-live-verified"
COMPACT_FIELDS = frozenset(
    {
        "id", "m", "t", "p", "q1", "mp", "sv", "sp", "dp", "pn",
        "ps", "pc", "y", "km", "f", "c", "s", "u", "ls", "v",
    }
)
LONG_FIELDS = frozenset(
    {
        "id", "model", "title", "price", "peer_lower_quartile_eur",
        "peer_median_eur", "savings_vs_lower_quartile_eur",
        "conservative_discount_bps", "median_discount_bps", "peer_count",
        "peer_source_count", "peer_country_count", "peer_dispersion", "year",
        "mileage", "fuel", "country", "source", "url", "seller",
        "last_seen_at",
    }
)


def provisional_offer_digest(offers: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for raw in offers:
        offer = {**raw, "v": 0}
        digest.update(
            json.dumps(
                offer,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


class SameGenerationEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "car_deal_finder"
        board_dir = self.root / "mobile_site_local"
        board_dir.mkdir(parents=True)
        source_policy = self.root / "schengen_source_policy.json"
        source_policy.write_text(
            json.dumps({"schema_version": 1, "sources": {}}),
            encoding="utf-8",
        )

        timestamp = "2026-08-11T00:00:00+00:00"
        self.offer: dict[str, object] = {
            "id": "source-a:offer-1",
            "m": "clio5_tce90",
            "t": "Renault Clio TCe 90",
            "p": 10_000,
            "q1": 12_000,
            "mp": 13_000,
            "sv": 2_000,
            "sp": 16.67,
            "dp": 23.08,
            "pn": 30,
            "ps": 4,
            "pc": 3,
            "y": 2025,
            "km": 10_000,
            "f": "petrol",
            "c": "DE",
            "s": "Source A",
            "u": "https://example.test/listing/1",
            "ls": timestamp,
            "v": 1,
        }
        self.ranked_offer: dict[str, object] = {
            "id": self.offer["id"],
            "model": self.offer["m"],
            "title": self.offer["t"],
            "price": self.offer["p"],
            "peer_lower_quartile_eur": self.offer["q1"],
            "peer_median_eur": self.offer["mp"],
            "savings_vs_lower_quartile_eur": self.offer["sv"],
            "conservative_discount_bps": 1_667,
            "median_discount_bps": 2_308,
            "peer_count": self.offer["pn"],
            "peer_source_count": self.offer["ps"],
            "peer_country_count": self.offer["pc"],
            "peer_dispersion": 0.04,
            "year": self.offer["y"],
            "mileage": self.offer["km"],
            "fuel": self.offer["f"],
            "country": self.offer["c"],
            "source": self.offer["s"],
            "url": self.offer["u"],
            "seller": "dealer",
            "last_seen_at": self.offer["ls"],
        }
        snapshot_digest = hashlib.sha256(b"same-generation-v7-snapshot").hexdigest()
        offer_fields_digest = provisional_offer_digest([self.offer])
        blocked_sources: list[str] = []
        shared = {
            "schema_version": 2,
            "algorithm": V7_ALGORITHM,
            "generated_at": timestamp,
            "data_generated_at_utc": timestamp,
            "board_built_at_utc": "2026-08-11T00:01:00Z",
            "universe_unique_offers": 3,
            "observation_cutoff_utc": "2026-08-08T00:00:00+00:00",
            "max_observation_age_hours": 72,
            "snapshot_eligible_sha256": snapshot_digest,
            "offer_fields_sha256": offer_fields_digest,
            "source_policy_sha256": selection_audit.sha256_file(source_policy),
            "quarantine_manifest_sha256": selection_audit.optional_sha256_file(
                Path("/data/car_deal_sonar_export/current/quarantined_sources.json")
            ),
            "blocked_source_keys_sha256": selection_audit.canonical_json_sha256(
                blocked_sources
            ),
            "blocked_source_key_count": len(blocked_sources),
            "policy_blocked_sources": blocked_sources,
            "scanned_recent_rows": 3,
            "eligible_observed_rows": 3,
            "ranked_candidate_rows": 1,
            "saved_top_rows": 1,
            "ranking_complete": True,
            "outside_saved_better_than_cutoff": 0,
            "anomalous_low_prices_excluded": 0,
            "unsupported_economics_published": 0,
            "connected_country_count": 1,
            "connected_source_count": 1,
            "displayed_country_count": 1,
            "displayed_source_count": 1,
            "live_verified_offer_count": 1,
        }
        self.board = {
            **shared,
            "updated_utc": timestamp,
            "count": 1,
            "scope": "schengen_observed_peer_market",
            "offers": [self.offer],
        }
        (board_dir / "board.json").write_text(
            json.dumps(self.board), encoding="utf-8"
        )
        self.ranked = {
            **shared,
            "total_all": 3,
            "qualified": 1,
            "shown": 1,
            "offers": [self.ranked_offer],
        }
        (self.root / "top_offers.json").write_text(
            json.dumps(self.ranked), encoding="utf-8"
        )
        (self.root / "top400_validation.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "input_updated_at": timestamp,
                    "input_algorithm": V7_ALGORITHM,
                    "input_snapshot_sha256": snapshot_digest,
                    "input_offer_fields_sha256": offer_fields_digest,
                    "generated_at": "2026-08-11T00:02:00Z",
                    "checked": 1,
                    "counts": {"verified": 1, "dead": 0, "unknown": 0},
                    "results": [{"url": self.offer["u"], "status": "verified"}],
                }
            ),
            encoding="utf-8",
        )
        database = sqlite3.connect(self.root / "universe_offers.sqlite")
        database.execute(
            "CREATE TABLE offers (id INTEGER PRIMARY KEY, last_seen_at TEXT)"
        )
        database.executemany(
            "INSERT INTO offers(id, last_seen_at) VALUES (?, ?)",
            [(1, timestamp), (2, timestamp), (3, timestamp)],
        )
        database.commit()
        database.close()

        candidates = selection_audit.candidate_list([self.offer])
        selected = selection_audit.expected_selection(candidates, 1, 1, 1)
        candidate_digest = selection_audit.digest(candidates)
        selected_digest = selection_audit.digest(selected)
        candidate_fields_digest = selection_audit.digest_fields(candidates)
        selected_fields_digest = selection_audit.digest_fields(selected)
        generation = hashlib.sha256(
            (
                f"{selection_audit.ALGORITHM}\n"
                f"{timestamp}\n"
                f"{candidate_fields_digest}\n"
                f"{selected_fields_digest}\n"
            ).encode("utf-8")
        ).hexdigest()[:16]
        self.payload = {
            **{key: value for key, value in self.board.items() if key != "offers"},
            "selection_algorithm": selection_audit.ALGORITHM,
            "selection_candidate_sha256": candidate_digest,
            "selected_ids_sha256": selected_digest,
            "selection_candidate_fields_sha256": candidate_fields_digest,
            "selected_fields_sha256": selected_fields_digest,
            "generation_id": generation,
            "universe_unique_offers": 3,
            "qualified_universe_offers": 1,
            "published_offer_count": 1,
            "verified_live_count": 1,
            "connected_country_count": 1,
            "connected_source_count": 1,
            "selection": {
                "top_n": 1,
                "strict_global_order": True,
                "coverage_quota_substitutions": 0,
                "ranking": "observed European peer price",
                "algeria_economics_included": False,
            },
            "offers": selected,
        }
        local_report = selection_audit.audit_payload(
            root=self.root,
            payload=self.payload,
            top_n=1,
            per_country_min=1,
            per_source_min=1,
        )
        manifest = {
            "schema_version": 1,
            "generation_id": generation,
            "algorithm": selection_audit.ALGORITHM,
            "universe_unique_offers": 3,
            "candidate_ids_sha256": candidate_digest,
            "selected_ids_sha256": selected_digest,
            "candidate_fields_sha256": candidate_fields_digest,
            "selected_fields_sha256": selected_fields_digest,
            "data_generated_at_utc": timestamp,
            "snapshot_eligible_sha256": snapshot_digest,
            "offer_fields_sha256": offer_fields_digest,
        }
        self.manifest_path = Path(self.temporary.name) / "manifest.json"
        self.audit_path = Path(self.temporary.name) / "audit.json"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.audit_path.write_text(json.dumps(local_report), encoding="utf-8")
        self.generation = generation
        self.evidence = live_audit.load_sealed_generation_evidence(
            manifest_path=self.manifest_path,
            audit_path=self.audit_path,
            expected_generation=generation,
        )

    def classify(self, payload: dict[str, object]) -> live_audit.ProbeResult:
        with patch.object(
            live_audit.selection_audit,
            "decrypt_blob",
            return_value=payload,
        ):
            return live_audit.classify_blob(
                blob=b"encrypted-fixture",
                pin="not-used-by-mock",
                expected_generation=self.generation,
                root=self.root,
                top_n=1,
                per_country_min=1,
                per_source_min=1,
                sealed_evidence=self.evidence,
            )

    def test_fixture_uses_exact_v7_board_and_ranked_contract(self) -> None:
        self.assertEqual(self.board["schema_version"], 2)
        self.assertEqual(self.board["algorithm"], V7_ALGORITHM)
        self.assertEqual(
            json.loads(self.audit_path.read_text(encoding="utf-8"))["schema_version"],
            1,
        )
        self.assertEqual(set(self.offer), COMPACT_FIELDS)
        self.assertEqual(set(self.ranked_offer), LONG_FIELDS)
        self.assertEqual(
            self.offer["sv"], self.ranked_offer["savings_vs_lower_quartile_eur"]
        )
        self.assertEqual(self.offer["pn"], self.ranked_offer["peer_count"])

    def test_later_sqlite_growth_does_not_fail_same_generation_live_audit(self) -> None:
        database = sqlite3.connect(self.root / "universe_offers.sqlite")
        database.execute("INSERT INTO offers(id) VALUES (4)")
        database.commit()
        database.close()

        # The default/pre-publication path still reads current SQLite and fails.
        with self.assertRaisesRegex(AssertionError, "does not match SQLite"):
            selection_audit.audit_payload(
                root=self.root,
                payload=self.payload,
                top_n=1,
                per_country_min=1,
                per_source_min=1,
            )

        # The live path compares the remote counter to same-generation evidence.
        result = self.classify(copy.deepcopy(self.payload))
        self.assertEqual(result.state, "pass")
        self.assertEqual(result.report["universe_unique_offers"], 3)

    def test_manifest_payload_universe_mismatch_still_fails(self) -> None:
        mismatched = copy.deepcopy(self.payload)
        mismatched["universe_unique_offers"] = 2
        result = self.classify(mismatched)
        self.assertEqual(result.state, "fatal")
        self.assertEqual(result.report["result"], "SAME_GENERATION_DIVERGENCE")
        self.assertEqual(result.report["failure_class"], "audit_divergence")

    def test_manifest_and_local_audit_must_agree(self) -> None:
        audit = json.loads(self.audit_path.read_text(encoding="utf-8"))
        audit["universe_unique_offers"] = 4
        self.audit_path.write_text(json.dumps(audit), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "counters disagree"):
            live_audit.load_sealed_generation_evidence(
                manifest_path=self.manifest_path,
                audit_path=self.audit_path,
                expected_generation=self.generation,
            )

    def test_same_id_with_changed_price_is_rejected(self) -> None:
        changed = copy.deepcopy(self.payload)
        changed["offers"][0]["p"] = 9_000
        with self.assertRaisesRegex(live_audit.RemoteInvalid, "selection_field_digest_unbound"):
            self.classify(changed)

    def test_previous_contract_generation_is_deployment_pending(self) -> None:
        previous = copy.deepcopy(self.payload)
        previous.update(
            {
                "schema_version": 1,
                "algorithm": "schengen-strict-global-economics-v6-live-verified",
                "selection_algorithm": "schengen-strict-global-economics-v6-live-verified",
                "generation_id": "0123456789abcdef",
            }
        )
        result = self.classify(previous)
        self.assertEqual(result.state, "pending")
        self.assertEqual(result.report["result"], "DEPLOYMENT_PENDING")
        self.assertEqual(result.report["observed_generation"], "0123456789abcdef")

    def test_previous_contract_can_converge_within_bounded_wait(self) -> None:
        previous = copy.deepcopy(self.payload)
        previous.update(
            {
                "schema_version": 1,
                "algorithm": "schengen-strict-global-economics-v6-live-verified",
                "selection_algorithm": "schengen-strict-global-economics-v6-live-verified",
                "generation_id": "0123456789abcdef",
            }
        )
        payloads = iter((previous, copy.deepcopy(self.payload)))
        now = [0.0]

        exit_code, report = live_audit.wait_for_convergence(
            expected_generation=self.generation,
            probe=lambda _attempt, _remaining: self.classify(next(payloads)),
            deadline_sec=10,
            initial_backoff_sec=1,
            max_backoff_sec=1,
            max_network_errors=0,
            clock=lambda: now[0],
            sleeper=lambda delay: now.__setitem__(0, now[0] + delay),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["result"], "LIVE_GENERATION_AUDIT_PASS")
        self.assertEqual(report["attempts"], 2)

    def test_previous_contract_never_passes_after_bounded_deadline(self) -> None:
        previous = copy.deepcopy(self.payload)
        previous.update(
            {
                "schema_version": 1,
                "algorithm": "schengen-strict-global-economics-v6-live-verified",
                "selection_algorithm": "schengen-strict-global-economics-v6-live-verified",
                "generation_id": "0123456789abcdef",
            }
        )
        now = [0.0]

        exit_code, report = live_audit.wait_for_convergence(
            expected_generation=self.generation,
            probe=lambda _attempt, _remaining: self.classify(previous),
            deadline_sec=2,
            initial_backoff_sec=1,
            max_backoff_sec=1,
            max_network_errors=0,
            clock=lambda: now[0],
            sleeper=lambda delay: now.__setitem__(0, now[0] + delay),
        )

        self.assertEqual(exit_code, live_audit.EXIT_RETRY_EXHAUSTED)
        self.assertEqual(report["result"], "DEPLOYMENT_PENDING_TIMEOUT")
        self.assertEqual(report["attempts"], 2)
        self.assertEqual(report["observed_generation"], "0123456789abcdef")

    def test_expected_generation_with_wrong_contract_remains_fatal(self) -> None:
        invalid = copy.deepcopy(self.payload)
        invalid["schema_version"] = 1
        with self.assertRaisesRegex(live_audit.RemoteInvalid, "contract_invalid"):
            self.classify(invalid)


if __name__ == "__main__":
    unittest.main()
