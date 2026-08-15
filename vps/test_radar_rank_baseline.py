#!/usr/bin/env python3
"""Focused unit, golden, corruption, ordering and round-trip baseline tests."""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

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
            "builder_code_sha256": baseline.file_sha256(Path(baseline.builder.__file__).resolve()),
            "publisher_code_sha256": baseline.file_sha256(Path(baseline.publisher.__file__).resolve()),
            "exporter_code_sha256": baseline.file_sha256(Path(baseline.__file__).resolve()),
        }
    )
    value: dict[str, object] = {
        "contract": baseline.CONTRACT,
        "schema_version": 1,
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


class RankBaselineTests(unittest.TestCase):
    def test_golden_round_trip_and_content_address(self) -> None:
        value = fixture()
        receipt = baseline.validate_baseline(value, now=NOW)
        self.assertEqual(receipt["published_verified_count"], 50)
        encoded = baseline.artifact_bytes(value)
        decoded = baseline.loads_strict(encoded, "golden")
        self.assertEqual(decoded, value)
        # This is a golden over the complete deterministic fixture and current
        # exporter contract, not merely over a subset of fields.
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            "6e3e300c56da5163b2ed78c18b3d1ad40aca44cc7dfccadf9056a428bcae0b5f",
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
            baseline.validate_baseline(unknown, now=NOW)
        corrupt = fixture()
        corrupt["published_selection"][0]["compact_payload"]["p"] += 1
        with self.assertRaisesRegex(baseline.BaselineError, "internal payload hash"):
            baseline.validate_baseline(corrupt, now=NOW)

    def test_reordered_rows_and_bad_cutoff_reject_even_when_resigned(self) -> None:
        reordered = fixture()
        rows = reordered["published_selection"]
        rows[0], rows[1] = rows[1], rows[0]
        reordered["hashes"]["published_selection_sha256"] = baseline.canonical_sha256(rows)
        resign(reordered)
        with self.assertRaisesRegex(baseline.BaselineError, "selection row 1 contract"):
            baseline.validate_baseline(reordered, now=NOW)
        cutoff = fixture()
        cutoff["cutoffs"]["rank_50"]["public_offer_id"] = "f" * 64
        resign(cutoff)
        with self.assertRaisesRegex(baseline.BaselineError, "cutoffs"):
            baseline.validate_baseline(cutoff, now=NOW)

    def test_expiry_and_incomplete_small_horizon_fail_closed(self) -> None:
        with self.assertRaisesRegex(baseline.BaselineError, "expired"):
            baseline.validate_baseline(
                fixture(), now=datetime.fromisoformat("2026-08-15T08:00:00+00:00")
            )
        incomplete = fixture()
        incomplete["proof"]["pool_exhausted"] = False
        resign(incomplete)
        with self.assertRaisesRegex(baseline.BaselineError, "pool-exhaustion"):
            baseline.validate_baseline(incomplete, now=NOW)

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
