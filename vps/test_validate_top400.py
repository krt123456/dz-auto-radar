import json
import tempfile
import threading
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

    def test_404_and_410_protection_redirects_are_unknown(self):
        final_url = (
            "https://cars.example/communfo/antiaspiration/default/getCaptcha"
        )
        for status in (404, 410):
            with self.subTest(status=status):
                result = self.check(FakeResponse(status, url=final_url))
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "protection_redirect")

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

    def test_http_200_protection_redirect_paths_are_unknown(self):
        html = "<html><body>ordinary response content</body></html>" * 20
        paths = (
            "/communfo/antiaspiration/default/getCaptcha",
            "/security/CAPTCHA",
            "/communfo/ANTIASPIRATION/default/check",
        )
        for path in paths:
            with self.subTest(path=path):
                result = self.check(
                    FakeResponse(200, html, url=f"https://cars.example{path}")
                )
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "protection_redirect")

    def test_http_200_ordinary_changed_paths_remain_fail_closed_unknown(self):
        html = "<html><body>ordinary vehicle details</body></html>" * 20
        for path in ("/search", "/"):
            with self.subTest(path=path):
                result = self.check(
                    FakeResponse(200, html, url=f"https://cars.example{path}")
                )
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(
                    result["reason"], "http_200_listing_identity_unproven"
                )

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

    def classify(self, final_url, http_status=200):
        return validator.classify_browser_page(
            self.offer,
            http_status=http_status,
            final_url=final_url,
            page_title="Toyota Corolla Hybrid",
            body_text=self.body,
        )

    def test_ordinary_404_and_410_are_dead(self):
        for status in (404, 410):
            with self.subTest(status=status):
                result = self.classify(self.offer["url"], http_status=status)
                self.assertEqual(result["status"], "dead")
                self.assertEqual(result["reason"], f"browser_http_{status}")

    def test_404_and_410_protection_redirects_are_unknown(self):
        final_url = (
            "https://cars.example/communfo/antiaspiration/default/getCaptcha"
        )
        for status in (404, 410):
            with self.subTest(status=status):
                result = self.classify(final_url, http_status=status)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "browser_protection_redirect")

    def test_cross_host_redirect_never_verifies_matching_content(self):
        result = self.classify("https://search.example/listing/123")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "browser_cross_host_redirect")

    def test_protection_redirect_paths_are_unknown(self):
        paths = (
            "/communfo/antiaspiration/default/getCaptcha",
            "/security/CAPTCHA",
            "/communfo/ANTIASPIRATION/default/check",
        )
        for path in paths:
            with self.subTest(path=path):
                result = self.classify(f"https://cars.example{path}")
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "browser_protection_redirect")

    def test_same_host_ordinary_changed_paths_never_verify_matching_content(self):
        for path in ("/search", "/"):
            with self.subTest(path=path):
                result = self.classify(f"https://cars.example{path}")
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "browser_detail_path_changed")

    def test_same_detail_path_with_matching_identity_verifies(self):
        result = self.classify("https://www.cars.example/listing/123")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["reason"], "browser_rendered_detail_identity")

    def test_autoscout_search_results_never_verify_matching_identity(self):
        url = "https://www.autoscout24.com/lst/toyota/corolla?atype=C&page=2"
        result = validator.classify_browser_page(
            {"url": url, "title": "Toyota Corolla Hybrid"},
            http_status=200,
            final_url=url,
            page_title="Toyota Corolla Hybrid offers",
            body_text=self.body,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "browser_autoscout24_non_detail_url")

    def test_autoscout_individual_detail_can_verify_matching_identity(self):
        url = (
            "https://www.autoscout24.it/annunci/toyota-corolla-hybrid-"
            "503f6455-b5a5-48af-bcfa-8a08c1dd87c7"
        )
        result = validator.classify_browser_page(
            {"url": url, "title": "Toyota Corolla Hybrid"},
            http_status=200,
            final_url=url,
            page_title="Toyota Corolla Hybrid",
            body_text=self.body,
        )
        self.assertEqual(result["status"], "verified")


class BrowserEligibilityTests(unittest.TestCase):
    @staticmethod
    def result(**overrides):
        result = {
            "board_rank": 1,
            "status": "unknown",
            "url": "https://www.paruvendu.fr/a/voiture-occasion/123",
            "final_url": (
                "https://paruvendu.fr/communfo/antiaspiration/default/getCaptcha"
            ),
            "reason": "protection_redirect",
        }
        result.update(overrides)
        return result

    def test_paruvendu_direct_protection_redirect_is_not_browser_eligible(self):
        item = self.result()
        self.assertFalse(validator.browser_eligible(item))
        self.assertEqual(validator.select_browser_target_ranks([item], 0), [])

    def test_other_paruvendu_unknowns_remain_browser_eligible(self):
        for reason in ("http_429", "cloudflare_challenge", "browser_protection_redirect"):
            with self.subTest(reason=reason):
                self.assertTrue(validator.browser_eligible(self.result(reason=reason)))

    def test_other_source_protection_redirect_remains_browser_eligible(self):
        self.assertTrue(
            validator.browser_eligible(
                self.result(
                    url="https://cars.example/listing/123",
                    final_url="https://cars.example/security/captcha",
                )
            )
        )

    def test_both_normalized_hosts_must_be_exactly_paruvendu(self):
        cases = (
            {
                "url": "https://www.paruvendu.fr/a/voiture-occasion/123",
                "final_url": "https://captcha.example/security/captcha",
            },
            {
                "url": "https://cars.example/listing/123",
                "final_url": "https://www.paruvendu.fr/security/captcha",
            },
            {
                "url": "https://autos.paruvendu.fr/listing/123",
                "final_url": "https://autos.paruvendu.fr/security/captcha",
            },
        )
        for overrides in cases:
            with self.subTest(**overrides):
                self.assertTrue(validator.browser_eligible(self.result(**overrides)))

    def test_verified_and_dead_results_remain_ineligible(self):
        for status in ("verified", "dead"):
            with self.subTest(status=status):
                self.assertFalse(
                    validator.browser_eligible(
                        self.result(status=status, reason="http_200")
                    )
                )

    def test_autoscout_search_results_are_not_browser_targets(self):
        item = self.result(
            url="https://www.autoscout24.com/lst/peugeot/2008?atype=C&page=2",
            final_url="https://www.autoscout24.com/lst/peugeot/2008?atype=C&page=2",
            reason="http_200_listing_identity_unproven",
        )
        self.assertFalse(validator.browser_eligible(item))
        self.assertEqual(validator.select_browser_target_ranks([item], 0), [])


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
                "verified_target": 1,
            },
        }

    def test_checkpoint_rejects_verified_autoscout_search_result(self):
        normalized = validator.normalize_offers(
            [{
                "u": "https://www.autoscout24.com/lst/peugeot/2008?atype=C",
                "t": "Peugeot 2008",
                "s": "AutoScout24",
            }],
            limit=0,
            id_index={},
        )
        store = validator.CheckpointStore(
            Path("unused-checkpoint.json"), identity={}, normalized=normalized
        )
        result = {
            **normalized[0],
            "status": "verified",
            "http_status": 200,
            "final_url": normalized[0]["url"],
            "reason": "browser_rendered_detail_identity",
        }
        with self.assertRaisesRegex(
            validator.CheckpointError, "non-detail AutoScout URL"
        ):
            store._validated_result(result, browser=False, direct_by_rank={})

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

    def test_complete_checkpoint_may_stop_when_verified_target_is_reached(self):
        direct = [self.classification(offer) for offer in self.normalized]
        targets = validator.select_browser_target_ranks(direct, 2)
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
                stage="complete",
                run_started_at=validator.utc_now(),
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=targets,
                browser_by_rank={1: browser_one},
            )
            resumed = store.load()
            self.assertEqual(resumed["stage"], "complete")
            self.assertEqual(set(resumed["browser_by_rank"]), {1})

    def test_exact_complete_checkpoint_can_be_removed_after_resume_expiry(self):
        direct = [self.classification(offer) for offer in self.normalized]
        targets = validator.select_browser_target_ranks(direct, 2)
        browser_one = {
            **direct[0],
            "direct_reason": direct[0]["reason"],
            "status": "verified",
            "http_status": 200,
            "reason": "browser_rendered_detail_identity",
        }
        expired = (datetime.now(UTC) - timedelta(hours=7)).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        complete = {
            "stage": "complete",
            "run_started_at": expired,
            "direct_by_rank": {item["board_rank"]: item for item in direct},
            "browser_target_ranks": targets,
            "browser_by_rank": {1: browser_one},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "remove.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            expected_checksum = store.save(**complete)
            store.remove_completed(
                expected_checkpoint_sha256=expected_checksum,
            )
            self.assertFalse(path.exists())

            resume_path = root / "resume.json"
            resume_store = validator.CheckpointStore(
                resume_path, identity=self.identity, normalized=self.normalized
            )
            resume_store.save(**complete)
            with self.assertRaises(validator.CheckpointError):
                resume_store.load()
            self.assertFalse(resume_path.exists())
            self.assertEqual(len(list(root.glob("resume.json.*.quarantine"))), 1)

    def test_expired_browser_checkpoint_accepts_only_bounded_explicit_grace(self):
        direct = [self.classification(offer) for offer in self.normalized]
        expired = (datetime.now(UTC) - timedelta(hours=7)).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "browser.json"
            store = validator.CheckpointStore(
                path,
                identity=self.identity,
                normalized=self.normalized,
                resume_grace_sec=2 * 60 * 60,
            )
            store.save(
                stage="browser",
                run_started_at=expired,
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=validator.select_browser_target_ranks(direct, 2),
                browser_by_rank={},
            )
            resumed = store.load()
            self.assertEqual(resumed["run_started_at"], expired)

            direct_path = root / "direct.json"
            direct_store = validator.CheckpointStore(
                direct_path,
                identity=self.identity,
                normalized=self.normalized,
                resume_grace_sec=2 * 60 * 60,
            )
            direct_store.save(
                stage="direct",
                run_started_at=expired,
                direct_by_rank={1: direct[0]},
                browser_target_ranks=[],
                browser_by_rank={},
            )
            with self.assertRaises(validator.CheckpointError):
                direct_store.load()

        with self.assertRaises(ValueError):
            validator.CheckpointStore(
                Path("unused.json"),
                identity=self.identity,
                normalized=self.normalized,
                resume_grace_sec=validator.MAX_CHECKPOINT_RESUME_GRACE_SEC + 1,
            )

    def test_compatibility_pins_allow_only_validator_source_hash_migration(self):
        direct = [self.classification(offer) for offer in self.normalized]
        old_identity = json.loads(json.dumps(self.identity))
        old_identity["validator"] = {"source_sha256": "c" * 64}
        current_identity = json.loads(json.dumps(old_identity))
        current_identity["validator"]["source_sha256"] = "d" * 64
        old_identity_sha = validator.sha256_bytes(
            validator.canonical_json_bytes(old_identity)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            old_store = validator.CheckpointStore(
                path, identity=old_identity, normalized=self.normalized
            )
            checkpoint_sha = old_store.save(
                stage="browser",
                run_started_at=validator.utc_now(),
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=validator.select_browser_target_ranks(direct, 2),
                browser_by_rank={},
            )
            rescue_store = validator.CheckpointStore(
                path,
                identity=current_identity,
                normalized=self.normalized,
                compatible_identity_sha256=old_identity_sha,
                compatible_checkpoint_sha256=checkpoint_sha,
            )
            self.assertEqual(rescue_store.load()["stage"], "browser")

            wrong_path = Path(directory) / "wrong.json"
            wrong_store = validator.CheckpointStore(
                wrong_path,
                identity=old_identity,
                normalized=self.normalized,
            )
            wrong_store.save(
                stage="browser",
                run_started_at=validator.utc_now(),
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=validator.select_browser_target_ranks(direct, 2),
                browser_by_rank={},
            )
            rejected = validator.CheckpointStore(
                wrong_path,
                identity=current_identity,
                normalized=self.normalized,
                compatible_identity_sha256=old_identity_sha,
                compatible_checkpoint_sha256="e" * 64,
            )
            with self.assertRaises(validator.CheckpointError):
                rejected.load()

    def test_completed_removal_rejects_stale_digest_and_wrong_identity(self):
        direct = [self.classification(offer) for offer in self.normalized]
        targets = validator.select_browser_target_ranks(direct, 2)
        browser_one = {
            **direct[0],
            "direct_reason": direct[0]["reason"],
            "status": "verified",
            "http_status": 200,
            "reason": "browser_rendered_detail_identity",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            stale_checksum = store.save(
                stage="complete",
                run_started_at=validator.utc_now(),
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=targets,
                browser_by_rank={1: browser_one},
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["checkpointed_at"] = "2026-08-14T19:00:00Z"
            unsigned = {
                key: value for key, value in payload.items()
                if key != "checkpoint_sha256"
            }
            current_checksum = validator.sha256_bytes(
                validator.canonical_json_bytes(unsigned)
            )
            payload["checkpoint_sha256"] = current_checksum
            validator.atomic_json_write(path, payload)

            with self.assertRaises(validator.CheckpointError):
                store.remove_completed(
                    expected_checkpoint_sha256=stale_checksum,
                )
            self.assertTrue(path.exists())

            wrong_identity = json.loads(json.dumps(self.identity))
            wrong_identity["input"]["full_content_sha256"] = "c" * 64
            wrong_store = validator.CheckpointStore(
                path, identity=wrong_identity, normalized=self.normalized
            )
            with self.assertRaises(validator.CheckpointError):
                wrong_store.remove_completed(
                    expected_checkpoint_sha256=current_checksum,
                )
            self.assertTrue(path.exists())

            store.remove_completed(
                expected_checkpoint_sha256=current_checksum,
            )
            self.assertFalse(path.exists())

    def test_completed_removal_preserves_tampered_or_non_complete_checkpoint(self):
        direct = {1: self.classification(self.normalized[0])}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            direct_checksum = store.save(
                stage="direct",
                run_started_at=validator.utc_now(),
                direct_by_rank=direct,
                browser_target_ranks=[],
                browser_by_rank={},
            )
            with self.assertRaises(validator.CheckpointError):
                store.remove_completed(
                    expected_checkpoint_sha256=direct_checksum,
                )
            self.assertTrue(path.exists())

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["stage"] = "complete"
            validator.atomic_json_write(path, payload)
            with self.assertRaises(validator.CheckpointError):
                store.remove_completed(
                    expected_checkpoint_sha256=direct_checksum,
                )
            self.assertTrue(path.exists())

    def test_completed_removal_serializes_a_competing_store_save(self):
        direct = [self.classification(offer) for offer in self.normalized]
        targets = validator.select_browser_target_ranks(direct, 2)
        browser_one = {
            **direct[0],
            "direct_reason": direct[0]["reason"],
            "status": "verified",
            "http_status": 200,
            "reason": "browser_rendered_detail_identity",
        }
        validated = threading.Event()
        release_removal = threading.Event()

        class PausingStore(validator.CheckpointStore):
            def _validate_for_removal(self, payload, *, expected_checkpoint_sha256):
                state = super()._validate_for_removal(
                    payload,
                    expected_checkpoint_sha256=expected_checkpoint_sha256,
                )
                validated.set()
                if not release_removal.wait(2):
                    raise RuntimeError("test timed out waiting to release removal")
                return state

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            remover = PausingStore(
                path, identity=self.identity, normalized=self.normalized
            )
            writer = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            expected_checksum = remover.save(
                stage="complete",
                run_started_at=validator.utc_now(),
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=targets,
                browser_by_rank={1: browser_one},
            )
            errors = []
            writer_started = threading.Event()
            writer_finished = threading.Event()

            def remove_checkpoint():
                try:
                    remover.remove_completed(
                        expected_checkpoint_sha256=expected_checksum,
                    )
                except BaseException as exc:
                    errors.append(exc)

            def replace_checkpoint():
                writer_started.set()
                try:
                    writer.save(
                        stage="direct",
                        run_started_at=validator.utc_now(),
                        direct_by_rank={1: direct[0]},
                        browser_target_ranks=[],
                        browser_by_rank={},
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    writer_finished.set()

            remove_thread = threading.Thread(target=remove_checkpoint)
            remove_thread.start()
            self.assertTrue(validated.wait(1))
            writer_thread = threading.Thread(target=replace_checkpoint)
            writer_thread.start()
            self.assertTrue(writer_started.wait(1))
            self.assertFalse(writer_finished.wait(0.1))
            release_removal.set()
            remove_thread.join(2)
            writer_thread.join(2)

            self.assertFalse(remove_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(path.exists())
            self.assertEqual(writer.load()["stage"], "direct")

    def test_completed_removal_preserves_future_dated_checkpoint(self):
        direct = [self.classification(offer) for offer in self.normalized]
        targets = validator.select_browser_target_ranks(direct, 2)
        browser_one = {
            **direct[0],
            "direct_reason": direct[0]["reason"],
            "status": "verified",
            "http_status": 200,
            "reason": "browser_rendered_detail_identity",
        }
        future = (datetime.now(UTC) + timedelta(minutes=10)).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            expected_checksum = store.save(
                stage="complete",
                run_started_at=future,
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=targets,
                browser_by_rank={1: browser_one},
            )
            with self.assertRaises(validator.CheckpointError):
                store.remove_completed(
                    expected_checkpoint_sha256=expected_checksum,
                )
            self.assertTrue(path.exists())

    def test_completed_removal_rejects_resigned_invalid_frontier(self):
        direct = [self.classification(offer) for offer in self.normalized]
        targets = validator.select_browser_target_ranks(direct, 2)
        browser_two = {
            **direct[1],
            "direct_reason": direct[1]["reason"],
            "status": "verified",
            "http_status": 200,
            "reason": "browser_rendered_detail_identity",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized
            )
            expected_checksum = store.save(
                stage="complete",
                run_started_at=validator.utc_now(),
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=targets,
                browser_by_rank={2: browser_two},
            )
            with self.assertRaises(validator.CheckpointError):
                store.remove_completed(
                    expected_checkpoint_sha256=expected_checksum,
                )
            self.assertTrue(path.exists())

    def test_target_frontier_rejects_unattempted_higher_rank(self):
        results = [
            {"board_rank": 1, "url": "https://cars.example/1", "status": "unknown"},
            {"board_rank": 2, "url": "https://cars.example/2", "status": "verified"},
            {"board_rank": 3, "url": "https://cars.example/3", "status": "unknown"},
        ]
        self.assertFalse(
            validator.target_finalization_ready(
                results=results,
                verified_target=1,
                browser_target_ranks=[1, 2, 3],
                browser_attempted_ranks={2},
            )
        )

    def test_target_frontier_allows_only_lower_priority_pending_tail(self):
        results = [
            {"board_rank": 1, "url": "https://cars.example/1", "status": "verified"},
            {
                "board_rank": 2,
                "url": "https://cars.example/2",
                "status": "unknown",
                "direct_reason": "http_429",
            },
            {"board_rank": 3, "url": "https://cars.example/3", "status": "unknown"},
        ]
        self.assertTrue(
            validator.target_finalization_ready(
                results=results,
                verified_target=1,
                browser_target_ranks=[1, 2, 3],
                browser_attempted_ranks={1, 2},
            )
        )

    def test_non_http_unknown_is_not_a_browser_frontier_hole(self):
        results = [
            {"board_rank": 1, "url": "", "status": "unknown"},
            {"board_rank": 2, "url": "https://cars.example/2", "status": "verified"},
        ]
        self.assertTrue(
            validator.target_finalization_ready(
                results=results,
                verified_target=1,
                browser_target_ranks=[2],
                browser_attempted_ranks={2},
            )
        )

    def test_complete_checkpoint_rejects_verified_rank_below_pending_hole(self):
        direct = [self.classification(offer) for offer in self.normalized]
        targets = validator.select_browser_target_ranks(direct, 2)
        browser_two = {
            **direct[1],
            "direct_reason": direct[1]["reason"],
            "status": "verified",
            "http_status": 200,
            "reason": "browser_rendered_detail_identity",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "checkpoint.json"
            store = validator.CheckpointStore(
                path, identity=self.identity, normalized=self.normalized,
            )
            store.save(
                stage="complete",
                run_started_at=validator.utc_now(),
                direct_by_rank={item["board_rank"]: item for item in direct},
                browser_target_ranks=targets,
                browser_by_rank={2: browser_two},
            )
            with self.assertRaises(validator.CheckpointError):
                store.load()
            self.assertFalse(path.exists())

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
