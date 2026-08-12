#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import stat
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import seal_validation_report as sealer


class SealValidationReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.board_path = self.root / "board.json"
        self.validation_path = self.root / "validation.json"
        self.board = {
            "schema_version": 2,
            "algorithm": sealer.ALGORITHM,
            "data_generated_at_utc": "2026-08-11T20:00:00+00:00",
            "snapshot_eligible_sha256": "a" * 64,
            "offers": [
                {
                    "u": "https://one.example/listing/1?lang=de",
                    "id": "one",
                    "nested": {"z": 2, "a": 1},
                },
                {
                    "id": "two",
                    "url": "https://two.example/listing/2",
                    "price": 12_345,
                },
            ],
        }
        self.validation = {
            "schema_version": 1,
            "input_updated_at": self.board["data_generated_at_utc"],
            "generated_at": "2026-08-11T20:01:00Z",
            "checked": 2,
            "counts": {"verified": 1, "dead": 0, "unknown": 1},
            "results": [
                {"url": "https://two.example/listing/2", "status": "unknown"},
                {
                    "url": "https://one.example/listing/1?lang=de",
                    "status": "verified",
                    "http_status": 200,
                },
            ],
        }
        self.write_inputs()

    def write_inputs(self) -> None:
        self.board_path.write_text(json.dumps(self.board), encoding="utf-8")
        self.validation_path.write_text(json.dumps(self.validation), encoding="utf-8")

    def assert_rejected_without_rewrite(self) -> None:
        before = self.validation_path.read_bytes()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                sealer.main(
                    [
                        "--board",
                        str(self.board_path),
                        "--validation",
                        str(self.validation_path),
                    ]
                ),
                2,
            )
        self.assertIn("VALIDATION_SEAL_FAILED", stderr.getvalue())
        self.assertEqual(self.validation_path.read_bytes(), before)

    def test_happy_path_seals_in_place_and_preserves_results(self) -> None:
        original_results = deepcopy(self.validation["results"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = sealer.main(
                [
                    "--board",
                    str(self.board_path),
                    "--validation",
                    str(self.validation_path),
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "VALIDATION_SEAL_OK")

        sealed = json.loads(self.validation_path.read_text(encoding="utf-8"))
        expected_digest = hashlib.sha256()
        for raw_offer in self.board["offers"]:
            offer = {**raw_offer, "v": 0} if "v" in raw_offer else raw_offer
            expected_digest.update(json.dumps(
                offer,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"))
            expected_digest.update(b"\n")
        self.assertEqual(sealed["results"], original_results)
        self.assertEqual(sealed["input_algorithm"], sealer.ALGORITHM)
        self.assertEqual(sealed["input_snapshot_sha256"], "a" * 64)
        self.assertEqual(
            sealed["input_offer_fields_sha256"],
            expected_digest.hexdigest(),
        )
        self.assertEqual(stat.S_IMODE(self.validation_path.stat().st_mode), 0o600)

    def test_retry_normalizes_existing_verification_verdicts(self) -> None:
        self.board["offers"][0]["v"] = 1
        self.board["offers"][1]["v"] = -1
        self.write_inputs()

        self.assertEqual(
            sealer.main(
                [
                    "--board",
                    str(self.board_path),
                    "--validation",
                    str(self.validation_path),
                ]
            ),
            0,
        )
        sealed = json.loads(self.validation_path.read_text(encoding="utf-8"))
        expected = hashlib.sha256()
        for raw_offer in self.board["offers"]:
            offer = {**raw_offer, "v": 0}
            expected.update(
                json.dumps(
                    offer,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            expected.update(b"\n")
        self.assertEqual(sealed["input_offer_fields_sha256"], expected.hexdigest())

    def test_capability_check_needs_no_files(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(sealer.main(["--capability-check"]), 0)
        self.assertEqual(stdout.getvalue().strip(), sealer.READY_MARKER)

    def test_timestamp_mismatch_fails_closed(self) -> None:
        self.validation["input_updated_at"] = "2026-08-11T19:59:59+00:00"
        self.write_inputs()
        self.assert_rejected_without_rewrite()

    def test_invalid_snapshot_fails_closed(self) -> None:
        self.board["snapshot_eligible_sha256"] = "not-a-sha256"
        self.write_inputs()
        self.assert_rejected_without_rewrite()

    def test_board_and_validation_schema_fail_closed(self) -> None:
        for target in ("board", "validation"):
            with self.subTest(target=target):
                self.board["schema_version"] = 2
                self.validation["schema_version"] = 1
                if target == "board":
                    self.board["schema_version"] = 1
                else:
                    self.validation["schema_version"] = 2
                self.write_inputs()
                self.assert_rejected_without_rewrite()

    def test_missing_result_url_fails_closed(self) -> None:
        self.validation["results"] = self.validation["results"][:1]
        self.validation["checked"] = 1
        self.write_inputs()
        self.assert_rejected_without_rewrite()

    def test_extra_result_url_fails_closed(self) -> None:
        self.validation["results"].append(
            {"url": "https://extra.example/listing/3", "status": "unknown"}
        )
        self.validation["checked"] = 3
        self.write_inputs()
        self.assert_rejected_without_rewrite()

    def test_duplicate_validation_result_url_fails_closed(self) -> None:
        self.validation["results"][1]["url"] = self.validation["results"][0]["url"]
        self.write_inputs()
        self.assert_rejected_without_rewrite()

    def test_duplicate_or_unsafe_board_url_fails_closed(self) -> None:
        cases = (
            "https://one.example/listing/1?lang=de",
            "http://two.example/listing/2",
            "https://user:password@two.example/listing/2",
        )
        for value in cases:
            with self.subTest(value=value):
                self.board["offers"][1]["url"] = value
                self.write_inputs()
                self.assert_rejected_without_rewrite()


if __name__ == "__main__":
    unittest.main()
