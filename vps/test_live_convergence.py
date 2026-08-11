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


class SameGenerationEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "car_deal_finder"
        board_dir = self.root / "mobile_site_local"
        board_dir.mkdir(parents=True)

        offer = {
            "id": "offer-1",
            "m": "clio",
            "t": "Car 1",
            "p": 10_000,
            "pr": 8_000,
            "ep": 7_000,
            "roi": 50,
            "er": 45,
            "y": 2025,
            "km": 10_000,
            "f": "petrol",
            "c": "DE",
            "s": "Source A",
            "u": "https://example.test/listing/1",
            "cr": 90,
            "v": 1,
            "a": 0,
            "e": 0,
        }
        (board_dir / "board.json").write_text(
            json.dumps({
                "data_generated_at_utc": "2026-08-11T00:00:00Z",
                "offers": [offer],
            }),
            encoding="utf-8",
        )
        (self.root / "top_offers.json").write_text(
            json.dumps(
                {
                    "total_all": 1,
                    "qualified": 1,
                    "qualified_non_estimated": 1,
                    "non_estimated_partition_complete": True,
                    "shown": 1,
                    "offers": [
                        {
                            "id": "offer-1", "model": "clio", "title": "Car 1",
                            "price": 10_000, "profit": 8_000,
                            "effective_profit": 7_000, "roi": 50,
                            "effective_roi": 45, "year": 2025, "mileage": 10_000,
                            "country": "DE", "source": "Source A",
                            "url": "https://example.test/listing/1",
                            "credibility": 90, "estimated": False,
                            "eligible": True, "auction": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        database = sqlite3.connect(self.root / "universe_offers.sqlite")
        database.execute("CREATE TABLE offers (id INTEGER PRIMARY KEY)")
        database.executemany(
            "INSERT INTO offers(id) VALUES (?)",
            [(1,), (2,), (3,)],
        )
        database.commit()
        database.close()

        candidates = selection_audit.candidate_list([offer])
        selected = selection_audit.expected_selection(candidates, 1, 1, 1)
        candidate_digest = selection_audit.digest(candidates)
        selected_digest = selection_audit.digest(selected)
        candidate_fields_digest = selection_audit.digest_fields(candidates)
        selected_fields_digest = selection_audit.digest_fields(selected)
        generation = hashlib.sha256(
            (
                f"{selection_audit.ALGORITHM}\n"
                f"2026-08-11T00:00:00Z\n"
                f"{candidate_fields_digest}\n"
                f"{selected_fields_digest}\n"
            ).encode("utf-8")
        ).hexdigest()[:16]
        self.payload = {
            "selection_algorithm": selection_audit.ALGORITHM,
            "selection_candidate_sha256": candidate_digest,
            "selected_ids_sha256": selected_digest,
            "selection_candidate_fields_sha256": candidate_fields_digest,
            "selected_fields_sha256": selected_fields_digest,
            "data_generated_at_utc": "2026-08-11T00:00:00Z",
            "generation_id": generation,
            "universe_unique_offers": 3,
            "connected_country_count": 1,
            "connected_source_count": 1,
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
            "data_generated_at_utc": "2026-08-11T00:00:00Z",
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


if __name__ == "__main__":
    unittest.main()
