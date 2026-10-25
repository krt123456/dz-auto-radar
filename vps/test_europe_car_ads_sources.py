#!/usr/bin/env python3
"""Fail-closed contract tests for the European car-ads sources lane."""

from __future__ import annotations

import copy
from pathlib import Path
import unittest

try:
    from . import europe_car_ads_sources as eas
except ImportError:
    import europe_car_ads_sources as eas


def probe_record(**overrides: object) -> dict:
    record = {
        "url": "https://example.test/",
        "host": "example.test",
        "site_key": "example",
        "http_status": 200,
        "final_url": "https://example.test/",
        "title": "Example Motors",
        "body_bytes": 4096,
        "challenge": False,
        "error": "",
        "checked_at_utc": "2026-10-25T00:00:00+00:00",
    }
    record.update(overrides)
    return record


def synthetic_manifest() -> dict:
    return {
        "registry_digest": {
            "sources": [
                {"key": "agorastore", "name": "AgoraStore", "domains": ["agorastore.fr"]},
                {"key": "klaravik", "name": "Klaravik", "domains": ["klaravik.se"]},
            {"key": "troostwijk", "name": "Troostwijk", "domains": ["troostwijk.com"]},
            ]
        },
        "source_reports": [{"source": "agorastore", "row_count": 5}],
    }


def synthetic_probe() -> dict:
    return {
        "checked_at_utc": "2026-10-25T00:00:00+00:00",
        "results": [
            probe_record(
                url="https://www.autohero.com/",
                host="autohero.com",
                site_key="autohero-com",
                final_url="https://www.autohero.com/",
                title="AutoHero — Gebrauchtwagen",
            ),
            probe_record(
                url="https://www.mobile.de/",
                host="mobile.de",
                site_key="mobile-de",
                http_status=403,
                title="Zugriff verweigert",
                challenge=True,
                error="http_403",
            ),
            probe_record(
                url="https://www.agorastore.fr/",
                host="agorastore.fr",
                site_key="agorastore-fr",
                title="",
                body_bytes=12492,
            ),
            probe_record(
                url="https://www.troostwijk.com/",
                host="troostwijk.com",
                site_key="troostwijk-com",
                http_status=0,
                error="connection_failure",
                title="",
                body_bytes=0,
            ),
            probe_record(
                url="https://www.bilasol.is/",
                host="bilasol.is",
                site_key="bilasol-is",
                http_status=0,
                error="dns_failure",
                title="",
                body_bytes=0,
            ),
        ],
    }


class DocumentEvaluationTests(unittest.TestCase):
    def test_strong_incapsula_shell_flags_a_challenge(self) -> None:
        title, challenge = eas.evaluate_document(
            '<html><head><title>Site</title></head><body>'
            '<script src="/_Incapsula_Resource"></script></body></html>'
        )
        self.assertEqual(title, "Site")
        self.assertTrue(challenge)

    def test_js_cookie_redirect_stub_flags_a_challenge(self) -> None:
        _, challenge = eas.evaluate_document(
            "<html lang=\"en\"><head></head><body><script>"
            "window.location.href='/redirect_4BNNTERZ3KUSLKBUBCR3N4R2MMONHBCI3HPGVQ5B4N32QDLU5UUQ====/';"
            "</script><noscript>This website requires JS enabled and cookies"
            "</noscript></body></html>"
        )
        self.assertTrue(challenge)

    def test_weak_akamai_marker_only_fires_on_a_short_stub(self) -> None:
        stub = "<html><head></head><body>Reference #18c12f. Akamai reference: a1b2</body></html>"
        self.assertTrue(eas.evaluate_document(stub)[1])
        substantial = "<html><head><title>Real auction house</title></head><body>" \
            + ("listings and bid history mention Akamai reference 18c12f. " * 200) \
            + "</body></html>"
        title, challenge = eas.evaluate_document(substantial)
        self.assertEqual(title, "Real auction house")
        self.assertFalse(challenge)

    def test_real_title_lands_in_evidence(self) -> None:
        title, challenge = eas.evaluate_document(
            "<html><head><title>Coches Segunda Mano en Andorra</title></head></html>"
        )
        self.assertEqual(title, "Coches Segunda Mano en Andorra")
        self.assertFalse(challenge)


class ClassifyProbeTests(unittest.TestCase):
    def test_reachable_document_with_title_is_verified(self) -> None:
        status, reason = eas.classify_probe(probe_record())
        self.assertEqual((status, reason), ("verified", "reachable"))

    def test_challenged_document_is_walled_not_verified(self) -> None:
        status, reason = eas.classify_probe(probe_record(challenge=True))
        self.assertEqual((status, reason), ("walled", "gated"))

    def test_gated_status_codes_are_walled(self) -> None:
        for code in sorted(eas.GATED_STATUSES):
            status, reason = eas.classify_probe(
                probe_record(http_status=code, error=f"http_{code}")
            )
            self.assertEqual((status, reason), ("walled", "gated"))

    def test_registry_waf_status_468_is_walled(self) -> None:
        self.assertIn(468, eas.GATED_STATUSES)

    def test_title_less_stub_is_excluded_as_empty(self) -> None:
        status, reason = eas.classify_probe(
            probe_record(title="", body_bytes=212, challenge=True)
        )
        self.assertEqual((status, reason), ("walled", "gated"))
        status, reason = eas.classify_probe(
            probe_record(title="", body_bytes=212, challenge=False)
        )
        self.assertIsNone(status)
        self.assertEqual(reason, "empty_response")

    def test_title_less_but_substantial_document_stays_verified(self) -> None:
        status, reason = eas.classify_probe(
            probe_record(title="", body_bytes=eas.MIN_DOCUMENT_BYTES + 1)
        )
        self.assertEqual((status, reason), ("verified", "reachable"))

    def test_dns_failure_is_excluded_not_published(self) -> None:
        status, reason = eas.classify_probe(
            probe_record(http_status=0, error="dns_failure", title="", body_bytes=0)
        )
        self.assertIsNone(status)
        self.assertEqual(reason, "dns_failure")

    def test_missing_homepage_is_excluded(self) -> None:
        status, reason = eas.classify_probe(probe_record(http_status=404, error="http_404"))
        self.assertIsNone(status)
        self.assertEqual(reason, "http_error")

    def test_transport_failure_is_unreachable_with_its_reason(self) -> None:
        for error in ("timeout", "connection_failure"):
            status, reason = eas.classify_probe(
                probe_record(http_status=0, error=error, title="", body_bytes=0)
            )
            self.assertEqual(status, "unreachable")
            self.assertEqual(reason, error)

    def test_non_https_final_url_is_never_verified(self) -> None:
        status, reason = eas.classify_probe(probe_record(final_url="http://example.test/"))
        self.assertIsNone(status)
        self.assertEqual(reason, "connection_failure")


class BuildArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.artifact = eas.build_artifact(
            synthetic_probe(),
            synthetic_manifest(),
            generated_at="2026-10-25T00:00:00+00:00",
        )

    def test_build_passes_validation(self) -> None:
        summary = eas.validate_artifact(self.artifact)
        self.assertEqual(summary["countries"], 52)
        self.assertGreater(summary["sites"], 0)

    def test_country_set_is_complete_even_without_sites(self) -> None:
        codes = [row["code"] for row in self.artifact["countries"]]
        self.assertEqual(codes, sorted(eas.COUNTRIES))
        self.assertEqual(len(codes), 52)

    def test_unmapped_registry_key_is_reported(self) -> None:
        self.assertEqual(
            self.artifact["contract"]["unmapped_registry_keys"], ["klaravik"]
        )

    def test_candidate_probe_failure_is_excluded_without_registry_backing(self) -> None:
        bilasol = next(
            item for item in self.artifact["excluded"] if item["host"] == "bilasol.is"
        )
        self.assertEqual(bilasol["reason"], "dns_failure")
        self.assertEqual(bilasol["countries"], ["IS"])
        self.assertEqual(self.artifact["sites"]["autohero-com"]["status"], "verified")

    def test_unprobed_candidate_fails_closed_as_not_probed(self) -> None:
        willhaben = next(
            item for item in self.artifact["excluded"] if item["host"] == "willhaben.at"
        )
        self.assertEqual(willhaben["reason"], "not_probed")

    def test_registry_platform_survives_a_failed_probe_as_unreachable(self) -> None:
        entry = self.artifact["sites"].get("troostwijk-com")
        self.assertIsNotNone(entry, "registry host must still be published")
        self.assertEqual(entry["status"], "unreachable")
        self.assertEqual(entry["evidence"]["probe_reason"], "connection_failure")
        self.assertEqual(entry["evidence"]["http_status"], 0)

    def test_title_less_registry_platform_is_verified_from_body_size(self) -> None:
        entry = self.artifact["sites"]["agorastore-fr"]
        self.assertEqual(entry["status"], "verified")
        self.assertEqual(entry["evidence"]["http_status"], 200)
        self.assertFalse(entry["evidence"]["challenge"])

    def test_every_site_carries_probe_evidence(self) -> None:
        for key, entry in self.artifact["sites"].items():
            evidence = entry["evidence"]
            self.assertGreaterEqual(evidence["http_status"], 0, key)
            self.assertTrue(evidence["checked_at_utc"], key)
            self.assertIsInstance(evidence["challenge"], bool, key)
            if entry["status"] == "unreachable":
                self.assertIn(evidence["probe_reason"], eas.EXCLUDE_REASONS, key)

    def test_summary_counts_match_the_site_table(self) -> None:
        summary = self.artifact["summary"]
        statuses: dict[str, int] = {status: 0 for status in eas.STATUSES}
        for entry in self.artifact["sites"].values():
            statuses[entry["status"]] += 1
        self.assertEqual(summary["by_status"], statuses)
        self.assertEqual(summary["sites"], len(self.artifact["sites"]))
        self.assertEqual(summary["excluded"], len(self.artifact["excluded"]))


class ValidateArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.artifact = eas.build_artifact(
            synthetic_probe(),
            synthetic_manifest(),
            generated_at="2026-10-25T00:00:00+00:00",
        )

    def mutate(self, path: tuple, value: object) -> dict:
        clone = copy.deepcopy(self.artifact)
        target = clone
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
        return clone

    def assert_rejected(self, artifact: dict) -> None:
        with self.assertRaises(ValueError):
            eas.validate_artifact(artifact)

    def test_verified_status_requires_reachable_evidence(self) -> None:
        key = next(
            k for k, v in self.artifact["sites"].items() if v["status"] == "verified"
        )
        clone = copy.deepcopy(self.artifact)
        clone["sites"][key]["evidence"]["http_status"] = 403
        self.assert_rejected(clone)

    def test_verified_status_with_challenge_flag_is_rejected(self) -> None:
        key = next(
            k for k, v in self.artifact["sites"].items() if v["status"] == "verified"
        )
        clone = copy.deepcopy(self.artifact)
        clone["sites"][key]["evidence"]["challenge"] = True
        self.assert_rejected(clone)

    def test_walled_status_without_gated_evidence_is_rejected(self) -> None:
        key = next(
            k for k, v in self.artifact["sites"].items() if v["status"] == "walled"
        )
        clone = copy.deepcopy(self.artifact)
        clone["sites"][key]["evidence"]["http_status"] = 200
        clone["sites"][key]["evidence"]["challenge"] = False
        self.assert_rejected(clone)

    def test_unreachable_status_requires_a_probe_reason(self) -> None:
        key = next(
            k
            for k, v in self.artifact["sites"].items()
            if v["status"] == "unreachable"
        )
        clone = copy.deepcopy(self.artifact)
        del clone["sites"][key]["evidence"]["probe_reason"]
        self.assert_rejected(clone)

    def test_missing_evidence_is_rejected(self) -> None:
        key = next(iter(self.artifact["sites"]))
        clone = copy.deepcopy(self.artifact)
        del clone["sites"][key]["evidence"]
        self.assert_rejected(clone)

    def test_count_mismatch_is_rejected(self) -> None:
        clone = self.mutate(("contract", "site_count"), 1)
        self.assert_rejected(clone)

    def test_country_set_shrink_is_rejected(self) -> None:
        clone = copy.deepcopy(self.artifact)
        clone["countries"] = [row for row in clone["countries"] if row["code"] != "MT"]
        clone["contract"]["country_count"] = 51
        clone["summary"]["countries"] = 51
        self.assert_rejected(clone)

    def test_unsorted_country_codes_are_rejected(self) -> None:
        clone = copy.deepcopy(self.artifact)
        clone["countries"][0], clone["countries"][1] = (
            clone["countries"][1],
            clone["countries"][0],
        )
        self.assert_rejected(clone)

    def test_country_site_keys_must_match_site_countries(self) -> None:
        clone = copy.deepcopy(self.artifact)
        row = next(row for row in clone["countries"] if row["code"] == "FR")
        row["site_keys"] = []
        self.assert_rejected(clone)

    def test_unknown_status_is_rejected(self) -> None:
        key = next(iter(self.artifact["sites"]))
        clone = copy.deepcopy(self.artifact)
        clone["sites"][key]["status"] = "pending"
        self.assert_rejected(clone)

    def test_http_url_is_rejected(self) -> None:
        key = next(iter(self.artifact["sites"]))
        clone = copy.deepcopy(self.artifact)
        clone["sites"][key]["url"] = "http://example.test/"
        self.assert_rejected(clone)

    def test_excluded_entry_without_reason_is_rejected(self) -> None:
        clone = copy.deepcopy(self.artifact)
        if clone["excluded"]:
            clone["excluded"][0]["reason"] = "unknown"
            self.assert_rejected(clone)

    def test_by_status_mismatch_is_rejected(self) -> None:
        clone = copy.deepcopy(self.artifact)
        clone["summary"]["by_status"]["verified"] += 1
        self.assert_rejected(clone)


class ProbePlumbingTests(unittest.TestCase):
    def test_normalize_host_requires_https_and_strips_www(self) -> None:
        self.assertEqual(eas.normalize_host("https://WWW.Example.com/path"), "example.com")
        with self.assertRaises(ValueError):
            eas.normalize_host("http://example.com/")
        with self.assertRaises(ValueError):
            eas.normalize_host("https://example.com:443/")

    def test_site_key_is_stable_and_unique_per_host(self) -> None:
        self.assertEqual(eas.site_key_for_host("autohero.com"), "autohero-com")
        self.assertEqual(
            eas.site_key_for_host(eas.normalize_host("https://www.autohero.com/")),
            "autohero-com",
        )

    def test_every_candidate_is_https_and_carries_a_valid_market(self) -> None:
        for country, entries in eas.CANDIDATES.items():
            self.assertIn(country, eas.COUNTRIES, country)
            for key, name, url, kind in entries:
                self.assertTrue(key and name)
                self.assertTrue(url.startswith("https://"), url)
                self.assertIn(kind, eas.KINDS, url)
                eas.normalize_host(url)

    def test_registry_market_mapping_covers_the_expected_shape(self) -> None:
        for key, market in eas.AUCTION_REGISTRY_COUNTRIES.items():
            self.assertIn(market, eas.COUNTRIES, key)

    def test_probe_never_raises_on_an_unroutable_url(self) -> None:
        record = eas.probe_url("https://nonexistent-tld-sources.invalid/", timeout=5)
        self.assertIsInstance(record, dict)
        self.assertEqual(record["url"], "https://nonexistent-tld-sources.invalid/")
        self.assertIn("checked_at_utc", record)


class LandingSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (Path(__file__).resolve().parents[1] / "index.html").read_text(
            encoding="utf-8"
        )
        cls.dashboard = (Path(__file__).parent / "dashboard_index.html").read_text(
            encoding="utf-8"
        )

    def test_dashboard_copy_stays_in_sync_with_the_landing_page(self) -> None:
        self.assertEqual(self.source, self.dashboard)

    def test_public_sources_section_exists_before_the_hidden_app(self) -> None:
        landing = self.source.split('<div class="wrap hidden" id="app">', 1)[0]
        self.assertIn('id="europeSources"', landing)
        self.assertIn('id="euGrid"', landing)
        self.assertIn('id="euSearch"', landing)
        self.assertIn("مصادر السيارات في أوروبا", landing)

    def test_sources_file_is_linked_from_the_public_landing(self) -> None:
        self.assertIn('<a href="europe_car_ads_sources.json">', self.source)

    def test_loader_is_wired_and_fails_closed(self) -> None:
        self.assertIn("loadEuropeSources();", self.source)
        self.assertIn("validateEuropeSources(", self.source)
        self.assertIn("function renderEuropeSources(", self.source)
        self.assertIn('fetch("europe_car_ads_sources.json",{cache:"no-cache"})', self.source)
        self.assertIn("throw contractFailure()", self.source)

    def test_static_landing_text_publishes_no_volatile_counts(self) -> None:
        import html as html_module
        import re

        landing_match = re.search(
            r'<div\s+id="lock">(.*?)<div\s+class="wrap hidden"\s+id="app">',
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(landing_match)
        text = " ".join(
            html_module.unescape(re.sub(r"<[^>]+>", " ", landing_match.group(1))).split()
        )
        self.assertIsNone(re.search(r"[0-9\u0660-\u0669]", text))

    def test_status_labels_stay_arabic_and_truthful(self) -> None:
        for phrase in ("موثّق", "محمي بحاجز", "خارج نطاق الرصد"):
            self.assertIn(phrase, self.source)


if __name__ == "__main__":
    unittest.main()
