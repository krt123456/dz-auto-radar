import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
