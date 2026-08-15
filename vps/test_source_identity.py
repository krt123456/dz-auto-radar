#!/usr/bin/env python3
"""Hostile and cross-path tests for canonical Radar source identities."""

from __future__ import annotations

import unittest

try:
    from . import import_live_offers_to_universe as importer
    from . import source_identity as identity
except ImportError:
    import import_live_offers_to_universe as importer
    import source_identity as identity


class SourceIdentityTests(unittest.TestCase):
    def test_autoscout_requires_a_stable_listing_id_in_the_path(self) -> None:
        non_detail = (
            "https://www.autoscout24.com/lst/peugeot/2008?atype=C&page=2",
            "https://www.autoscout24.com/lst/peugeot/2008/123456?atype=C",
            "https://www.autoscout24.de/lst?sort=price",
            "https://www.autoscout24.fr/",
        )
        details = (
            "https://www.autoscout24.it/annunci/car-503f6455-b5a5-48af-bcfa-8a08c1dd87c7",
            "https://www.autoscout24.ch/de/d/20483026",
            "https://www.autoscout24.de/smyle/details/018f8bf5-a7e4-45c9-8dfe-91235bf72408/",
        )
        for url in non_detail:
            with self.subTest(url=url):
                self.assertTrue(identity.autoscout24_non_detail_url(url))
        for url in details:
            with self.subTest(url=url):
                self.assertFalse(identity.autoscout24_non_detail_url(url))
        self.assertFalse(
            identity.autoscout24_non_detail_url("https://cars.example/lst/peugeot")
        )

    def test_olx_aliases_and_legacy_ids_converge_on_production_identity(self) -> None:
        expected = ("olx.pl", "olxpl_1084550358")
        cases = (
            ("olx.pl", "olxpl_1084550358"),
            ("www.olx.pl", "olx.pl_1084550358"),
            ("OLX Poland Cars", "olxpl_avenger_12_gse_1084550358"),
        )
        for source, listing_id in cases:
            with self.subTest(source=source, listing_id=listing_id):
                self.assertEqual(
                    identity.canonical_source_identity(source, listing_id), expected
                )

    def test_olx_identity_rejects_ambiguous_and_hostile_ids(self) -> None:
        invalid = (
            "1084550358",
            "olxpl_0",
            "olxpl_01",
            "olxpl_model_no_api_id",
            "olxpl_model_1084550358_extra",
            "olxpl_4294967296",
            "olxpl_١٢٣",
            " olxpl_1084550358",
        )
        for listing_id in invalid:
            with self.subTest(listing_id=listing_id), self.assertRaises(
                identity.IdentityError
            ):
                identity.canonical_source_identity("olx.pl", listing_id)

    def test_polish_mirror_family_and_policy_keys_have_alias_parity(self) -> None:
        for source in (
            "olx.pl",
            "www.olx.pl",
            "OLX Poland Cars",
            "OTOMOTO",
            "MotoGratka",
        ):
            with self.subTest(source=source):
                self.assertEqual(identity.source_family(source), "pl_listing_mirrors")
        self.assertEqual(
            identity.source_identity_keys("olx.pl"),
            frozenset({"olx.pl", "www.olx.pl", "olx poland cars"}),
        )

    def test_non_olx_identity_preserves_existing_native_id_semantics(self) -> None:
        self.assertEqual(
            identity.canonical_source_identity("Source A", " native  id "),
            ("Source A", "native  id"),
        )

    def test_smart_importer_canonicalizes_source_key_id_and_raw_identity(self) -> None:
        row = {
            "source": "OLX Poland Cars",
            "listing_id": "olxpl_tiguan_15_tsi_1084550358",
            "source_url": "https://www.olx.pl/d/oferta/car-CID5-IDabc.html",
            "title": "Volkswagen Tiguan",
            "model_key": "tiguan_15_tsi",
            "price_eur": "22000",
            "country": "PL",
        }
        converted = importer.convert_row(row, "2026-08-15T10:00:00+00:00")
        assert converted is not None
        self.assertEqual(converted["source"], "olx.pl")
        self.assertEqual(converted["source_listing_id"], "olxpl_1084550358")
        self.assertEqual(converted["raw_json"]["listing_id"], "olxpl_1084550358")

    def test_smart_importer_rejects_unrecoverable_olx_identity(self) -> None:
        with self.assertRaises(identity.IdentityError):
            importer.convert_row(
                {
                    "source": "OLX Poland Cars",
                    "listing_id": "olxpl_tiguan_without_api_id",
                },
                "2026-08-15T10:00:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
