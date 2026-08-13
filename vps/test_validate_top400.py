import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import requests

import validate_top400 as validator


class FakeResponse:
    def __init__(self, status_code, text="", url="https://cars.example/listing/1"):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size=16_384):
        raw = self.text.encode("utf-8")
        for offset in range(0, len(raw), chunk_size):
            yield raw[offset : offset + chunk_size]

    def close(self):
        self.closed = True


def request_returning(response):
    return Mock(return_value=response)


class CheckUrlTests(unittest.TestCase):
    def check(self, response):
        return validator.check_url(
            "https://cars.example/listing/1",
            timeout_sec=1,
            request_get=request_returning(response),
        )

    def test_404_and_410_are_dead(self):
        for status in (404, 410):
            with self.subTest(status=status):
                result = self.check(FakeResponse(status))
                self.assertEqual(result["status"], "dead")
                self.assertEqual(result["reason"], f"http_{status}")

    def test_403_and_429_are_unknown(self):
        for status in (403, 429):
            with self.subTest(status=status):
                result = self.check(FakeResponse(status, "blocked"))
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], f"http_{status}")

    def test_http_200_protection_page_is_unknown(self):
        html = "<html><title>Just a moment...</title><body>cf-chl-bypass</body></html>" * 10
        result = self.check(FakeResponse(200, html))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "cloudflare_challenge")

    def test_http_200_expired_marker_is_dead(self):
        html = "<html><body>This listing has been removed.</body></html>" * 10
        result = self.check(FakeResponse(200, html))
        self.assertEqual(result["status"], "dead")
        self.assertTrue(result["reason"].startswith("dead_marker:"))

    def test_normal_http_200_listing_is_fail_closed_unknown(self):
        html = "<html><body><h1>2024 Toyota Yaris Hybrid</h1>" + ("vehicle details " * 30) + "</body></html>"
        result = self.check(FakeResponse(200, html))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "http_200_listing_identity_unproven")

    def test_http_200_structured_sold_out_is_dead(self):
        html = (
            '<html><body><script type="application/ld+json">'
            '{"@type": "Product", "offers": {"availability": "https://schema.org/SoldOut"}}'
            "</script>" + ("vehicle details " * 30) + "</body></html>"
        )
        result = self.check(FakeResponse(200, html))
        self.assertEqual(result["status"], "dead")
        self.assertEqual(result["reason"], "structured:authoritative_soldout")

    def test_http_200_structured_expired_price_valid_until_is_dead(self):
        html = (
            '<html><body><script type="application/ld+json">'
            '{"@type": "Product", "offers": {"priceValidUntil": "2020-01-01T00:00:00Z"}}'
            "</script>" + ("vehicle details " * 30) + "</body></html>"
        )
        result = self.check(FakeResponse(200, html))
        self.assertEqual(result["status"], "dead")
        self.assertEqual(result["reason"], "structured:expired_at_or_before_observation")

    def test_http_200_structured_in_stock_never_verifies_without_identity(self):
        html = (
            '<html><body><script type="application/ld+json">'
            '{"@type": "Product", "offers": {"availability": "https://schema.org/InStock"}}'
            "</script>" + ("vehicle details " * 30) + "</body></html>"
        )
        result = self.check(FakeResponse(200, html))
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["reason"].startswith("structured:"))

    def test_network_failure_and_invalid_url_are_unknown(self):
        requester = Mock(side_effect=requests.Timeout("timeout"))
        result = validator.check_url(
            "https://cars.example/listing/1", timeout_sec=1, request_get=requester
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "request_error:Timeout")
        self.assertEqual(validator.check_url("")["status"], "unknown")


class BoardValidationTests(unittest.TestCase):
    def test_validates_actual_board_order_and_preserves_unknown(self):
        offers = [
            {"u": "https://cars.example/1", "s": "First", "t": "Top deal", "c": "DE"},
            {"u": "https://cars.example/2", "s": "Second", "t": "Blocked", "c": "FR"},
            {"u": "https://cars.example/3", "s": "Third", "t": "Not selected", "c": "IT"},
        ]

        def checker(url, _timeout):
            if url.endswith("/1"):
                return {"status": "dead", "http_status": 410, "final_url": url, "reason": "http_410"}
            return {"status": "unknown", "http_status": 429, "final_url": url, "reason": "http_429"}

        results = validator.verify_offers(
            offers,
            limit=2,
            workers=2,
            timeout_sec=1,
            id_index={"https://cars.example/1": "listing-1", "https://cars.example/2": "listing-2"},
            checker=checker,
        )

        self.assertEqual([item["board_rank"] for item in results], [1, 2])
        self.assertEqual([item["source"] for item in results], ["First", "Second"])
        self.assertEqual([item["status"] for item in results], ["dead", "unknown"])
        self.assertEqual(results[1]["listing_id"], "listing-2")
        self.assertEqual(len(offers), 3, "validation must not remove unknown or dead input offers")

        report = validator.build_report(
            input_path=Path("board.json"),
            input_payload={"updated_utc": "2026-07-14T12:00:00Z"},
            results=results,
            requested_limit=2,
        )
        self.assertEqual(report["counts"], {"verified": 0, "dead": 1, "unknown": 1})
        self.assertEqual(report["dead_listing_ids"], ["listing-1"])
        self.assertEqual(report["unknown_listing_ids"], ["listing-2"])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "validation.json"
            validator.atomic_json_write(output, report)
            saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["results"][1]["status"], "unknown")


class BrowserPageTests(unittest.TestCase):
    def setUp(self):
        self.offer = {
            "url": "https://cars.example/listing/123",
            "title": "Toyota Corolla Hybrid",
        }
        self.body = "Toyota Corolla Hybrid vehicle details " * 20

    def classify(self, final_url):
        return validator.classify_browser_page(
            self.offer,
            http_status=200,
            final_url=final_url,
            page_title="Toyota Corolla Hybrid",
            body_text=self.body,
        )

    def test_cross_host_redirect_never_verifies_matching_content(self):
        result = self.classify("https://search.example/listing/123")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "browser_cross_host_redirect")

    def test_same_host_changed_path_never_verifies_matching_content(self):
        result = self.classify("https://cars.example/search")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "browser_detail_path_changed")

    def test_same_detail_path_with_matching_identity_verifies(self):
        result = self.classify("https://www.cars.example/listing/123")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["reason"], "browser_rendered_detail_identity")


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.offers = [
            {"u": f"https://cars.example/{rank}", "t": f"Car {rank}", "s": "Source"}
            for rank in range(1, 5)
        ]
        self.normalized = validator.normalize_offers(
            self.offers, limit=0, id_index={}
        )
        self.identity = {
            "contract": validator.CHECKPOINT_CONTRACT,
            "input": {"full_content_sha256": "a" * 64},
            "validator": {"source_sha256": "b" * 64},
            "config": {
                "browser_fallback": True,
                "browser_limit": 2,
                "checkpoint_max_age_sec": 21_600,
            },
        }

    @staticmethod
    def classification(offer):
        rank = offer["board_rank"]
        return {
            **offer,
            "status": "dead" if rank == 3 else "unknown",
            "http_status": 410 if rank == 3 else 429,
            "final_url": offer["url"],
            "reason": "http_410" if rank == 3 else "http_429",
        }

    def test_interrupted_resume_matches_uninterrupted_without_duplicate_ranks(self):
        uninterrupted = [self.classification(offer) for offer in self.normalized]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            partial = {item["board_rank"]: item for item in uninterrupted[:2]}
            started = validator.utc_now()
            store.save(
                stage="direct",
                run_started_at=started,
                direct_by_rank=partial,
                browser_target_ranks=[],
                browser_by_rank={},
            )
            resumed = store.load()
            calls = []

            def checker(url, _timeout):
                calls.append(url)
                rank = int(url.rsplit("/", 1)[1])
                return {
                    key: value
                    for key, value in self.classification(self.normalized[rank - 1]).items()
                    if key not in self.normalized[rank - 1]
                }

            results = validator.verify_offers(
                self.offers,
                limit=0,
                workers=2,
                timeout_sec=1,
                existing_results=resumed["direct_by_rank"],
                checker=checker,
            )
            self.assertEqual(results, uninterrupted)
            self.assertEqual(set(calls), {"https://cars.example/3", "https://cars.example/4"})
            self.assertEqual([item["board_rank"] for item in results], [1, 2, 3, 4])
            self.assertEqual(len({item["board_rank"] for item in results}), len(results))

            report_args = {
                "input_path": Path("board.json"),
                "input_payload": {"updated_utc": "2026-08-13T08:00:00Z"},
                "requested_limit": 0,
                "generated_at": started,
            }
            uninterrupted_report = validator.build_report(
                results=uninterrupted, **report_args
            )
            resumed_report = validator.build_report(results=results, **report_args)
            uninterrupted_path = Path(directory) / "uninterrupted.json"
            resumed_path = Path(directory) / "resumed.json"
            validator.atomic_json_write(uninterrupted_path, uninterrupted_report)
            validator.atomic_json_write(resumed_path, resumed_report)
            self.assertEqual(resumed_path.read_bytes(), uninterrupted_path.read_bytes())

    def test_identity_binds_raw_input_and_id_index_hashes(self):
        args = validator.argparse.Namespace(
            input=Path("board.json"),
            id_index=Path("top_offers.json"),
            limit=0,
            workers=2,
            timeout_sec=1,
            browser_fallback=False,
            browser_limit=0,
            browser_workers=1,
            browser_timeout_sec=1,
            checkpoint_batch_size=2,
            checkpoint_interval_sec=1.0,
            checkpoint_max_age_sec=60,
        )
        identity = validator.build_checkpoint_identity(
            args=args,
            normalized=self.normalized,
            input_updated_at="2026-08-13T08:00:00Z",
            input_file_sha256="a" * 64,
            id_index_file_sha256="b" * 64,
        )
        self.assertEqual(identity["input"]["file_sha256"], "a" * 64)
        self.assertEqual(identity["input"]["id_index_file_sha256"], "b" * 64)
        changed = validator.build_checkpoint_identity(
            args=args,
            normalized=self.normalized,
            input_updated_at="2026-08-13T08:00:00Z",
            input_file_sha256="c" * 64,
            id_index_file_sha256="b" * 64,
        )
        self.assertNotEqual(
            validator.sha256_bytes(validator.canonical_json_bytes(identity)),
            validator.sha256_bytes(validator.canonical_json_bytes(changed)),
        )

    def test_browser_targets_are_frozen_and_completed_override_is_validated(self):
        direct = [self.classification(offer) for offer in self.normalized]
        targets = validator.select_browser_target_ranks(direct, 2)
        self.assertEqual(targets, [1, 2])
        browser_one = {
            **direct[0],
            "direct_reason": direct[0]["reason"],
            "status": "verified",
            "http_status": 200,
            "reason": "browser_rendered_detail_identity",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = validator.CheckpointStore(
                Path(directory) / "checkpoint.json",
                identity=self.identity,
                normalized=self.normalized,
            )
            store.save(
                stage="browser",
                run_started_at=validator.utc_now(),
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=targets,
                browser_by_rank={1: browser_one},
            )
            resumed = store.load()
            self.assertEqual(resumed["browser_target_ranks"], [1, 2])
            self.assertEqual(resumed["browser_by_rank"][1], browser_one)

    def test_corrupt_mismatched_and_expired_checkpoints_are_quarantined(self):
        direct = {1: self.classification(self.normalized[0])}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "checkpoint.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            store.save(
                stage="direct",
                run_started_at=validator.utc_now(),
                direct_by_rank=direct,
                browser_target_ranks=[],
                browser_by_rank={},
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["direct_results"][0]["board_rank"] = 2
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(validator.CheckpointError):
                store.load()
            self.assertFalse(path.exists())
            self.assertEqual(len(list(root.glob("*.quarantine"))), 1)

            expired = (datetime.now(UTC) - timedelta(hours=7)).replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z")
            store.save(
                stage="direct",
                run_started_at=expired,
                direct_by_rank=direct,
                browser_target_ranks=[],
                browser_by_rank={},
            )
            with self.assertRaises(validator.CheckpointError):
                store.load()
            self.assertFalse(path.exists())
            self.assertEqual(len(list(root.glob("*.quarantine"))), 2)

    def test_duplicate_json_keys_fail_closed_and_are_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "checkpoint.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            path.write_text(
                '{"stage":"direct","stage":"browser"}', encoding="utf-8"
            )

            with self.assertRaises(validator.CheckpointError):
                store.load()

            self.assertFalse(path.exists())
            self.assertEqual(len(list(root.glob("*.quarantine"))), 1)


if __name__ == "__main__":
    unittest.main()
