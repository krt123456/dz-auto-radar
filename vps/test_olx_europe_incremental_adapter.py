#!/usr/bin/env python3
"""Hostile fixture tests for the dark OLX Europe incremental adapter."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

try:
    from . import incremental_frontier as frontier
    from . import olx_europe_incremental_adapter as olx
    from . import radar_incremental_ingest as ingest
except ImportError:
    import incremental_frontier as frontier
    import olx_europe_incremental_adapter as olx
    import radar_incremental_ingest as ingest


OBSERVED_AT = "2026-08-14T17:30:00Z"
NEWEST = "2026-08-14T17:00:02Z"
TIED = "2026-08-14T17:00:01Z"
OLDER = "2026-08-14T17:00:00Z"


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def contract(
    *, max_pages: int = 8, frontier_cap: int = 300
) -> frontier.SourceContract:
    return olx.source_contract_for(
        "PL", max_pages=max_pages, frontier_cap=frontier_cap
    )


def enum(key: str, label: str) -> dict[str, str]:
    return {"key": key, "label": label}


def ad(
    native_id: object,
    created_time: object = TIED,
    *,
    title: object = " Audi  A5  S line ",
    price_value: object = 100_000,
    currency: str = "PLN",
    year: object | None = "2025",
    url: str | None = None,
    business: object = True,
    extra_params: list[dict[str, object]] | None = None,
    params_override: object | None = None,
) -> dict[str, object]:
    params: object = [
        {"key": "price", "value": {"value": price_value, "currency": currency}},
        {"key": "milage", "value": {"key": "12000", "label": "12 000 km"}},
        {"key": "petrol", "value": {"key": "diesel", "label": "Diesel"}},
        {
            "key": "transmission",
            "value": {"key": "automatic", "label": "Automatic"},
        },
    ]
    if year is not None:
        assert isinstance(params, list)
        params.append({"key": "year", "value": {"key": year, "label": year}})
    if extra_params:
        assert isinstance(params, list)
        params.extend(extra_params)
    if params_override is not None:
        params = params_override
    return {
        "id": native_id,
        "created_time": created_time,
        "title": title,
        "url": url or f"https://www.olx.pl/d/oferta/car-{native_id}.html",
        "business": business,
        "location": {"city": {"name": " Warszawa "}},
        "params": params,
    }


def descriptor(offset: int, **changes) -> olx.OlxRequestDescriptor:
    request = olx.request_descriptor_for("PL", offset=offset)
    return replace(request, **changes) if changes else request


def success(
    offset: int,
    data: object,
    *,
    request: olx.OlxRequestDescriptor | None = None,
    request_sha256: str | None = None,
    status_code: int = 200,
    payload_extra: dict[str, object] | None = None,
) -> olx.TransportSuccess:
    selected = request or descriptor(offset)
    payload = {"data": data}
    if payload_extra:
        payload.update(payload_extra)
    return olx.TransportSuccess(
        request=selected,
        request_sha256=request_sha256 or selected.sha256,
        status_code=status_code,
        payload=payload,
    )


def failure(offset: int, kind: str) -> olx.TransportFailure:
    request = descriptor(offset)
    return olx.TransportFailure(
        request=request,
        request_sha256=request.sha256,
        kind=kind,
        detail="fixture failure",
    )


def adapt(
    results,
    *,
    selected_contract: frontier.SourceContract | None = None,
    canonicalizer: olx.ProductionModelCanonicalizer | None = None,
    country_code: str = "PL",
    max_transport_pages: int = 1_000,
):
    return list(
        olx.iter_source_pages(
            results,
            country_code=country_code,
            contract=selected_contract or contract(),
            canonicalizer=canonicalizer or olx.production_canonicalizer(),
            observed_at_utc=OBSERVED_AT,
            max_transport_pages=max_transport_pages,
        )
    )


def overlapped_fixture() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    first = [ad(value, TIED) for value in range(100, 150)]
    second = [*first[-5:], ad(200, TIED), ad(99, OLDER)]
    return first, second


class OlxEuropeIncrementalAdapterTests(unittest.TestCase):
    def test_contract_request_and_seed_policy_are_sealed(self) -> None:
        selected = contract()
        self.assertEqual(selected.key, olx.PL_COUNTRY_CONTRACT.key)
        self.assertEqual(len(selected.sort_contract_sha256), 64)
        self.assertEqual(olx.PL_COUNTRY_CONTRACT.category_id, 84)
        self.assertEqual(olx.PL_COUNTRY_CONTRACT.sort_by, "created_at:desc")
        self.assertEqual(olx.PL_COUNTRY_CONTRACT.transport_overlap, 5)
        request = descriptor(45)
        self.assertEqual(request.host, "www.olx.pl")
        self.assertEqual(request.offset, 45)
        self.assertEqual(request.sort_contract_sha256, selected.sort_contract_sha256)
        self.assertEqual(request.canonicalizer_sha256, olx.PRODUCTION_CANONICALIZER_SHA256)
        self.assertEqual(len(request.sha256), 64)
        self.assertTrue(olx.FRONTIER_SEED_REQUIRED)
        self.assertGreater(olx.PL_RETAINED_MARKET_ESTIMATE, 1_000 * 50)
        self.assertEqual(len(olx.FRONTIER_SEED_POLICY_SHA256), 64)
        self.assertFalse(olx.LIVE_CANARY_ALLOWED)
        self.assertTrue(olx.LIVE_POLICY_CONSTANTS_ARE_DOCUMENTATION_ONLY)
        self.assertIn("snapshot", olx.LIVE_CANARY_BLOCKER)

        persisted = olx._sort_contract_hash("PL")
        invariant_drifts = (
            olx._sort_contract_hash("PL", api_host="olx.pl"),
            olx._sort_contract_hash("PL", category_id=85),
            olx._sort_contract_hash("PL", sort_by="id:desc"),
            olx._sort_contract_hash("PL", overlap_fingerprint="other:v2"),
            olx._sort_contract_hash("PL", page_step=46),
        )
        self.assertEqual(persisted, selected.sort_contract_sha256)
        self.assertEqual(len(set(invariant_drifts)), len(invariant_drifts))
        self.assertTrue(all(value != persisted for value in invariant_drifts))

        with self.assertRaises(frontier.ContractError):
            olx.source_contract_for("RO", max_pages=8, frontier_cap=300)
        with self.assertRaises(frontier.ContractError):
            olx.source_contract_for("PL", max_pages=8, frontier_cap=99)

    def test_production_canonicalizer_colon_keys_and_digest_drift(self) -> None:
        canonicalizer = olx.production_canonicalizer()
        expected = {
            "Audi A5": "audi:a5",
            "VW Golf": "volkswagen:golf",
            "Mercedes CLA": "mercedes-benz:cla",
            "Vauxhall Astra": "opel:astra",
            "Audi Straße": "audi:strae",
            "Unknown Mystery Car": None,
        }
        for title, result in expected.items():
            with self.subTest(title=title):
                self.assertEqual(canonicalizer.canonicalize(title), result)
        with self.assertRaises(frontier.ContractError):
            olx.ProductionModelCanonicalizer(source_sha256="0" * 64)
        with self.assertRaises(frontier.ContractError):
            olx.ProductionModelCanonicalizer(catalog_sha256="f" * 64)

    def test_request_descriptor_rejects_wrong_host_category_sort_and_digest(self) -> None:
        wrong_requests = (
            descriptor(0, host="evil.example"),
            descriptor(0, category_id=85),
            descriptor(0, sort_by="id:desc"),
            replace(descriptor(0), offset=1),
            descriptor(0, limit=49),
            descriptor(0, sort_contract_sha256="0" * 64),
            descriptor(0, canonicalizer_sha256="0" * 64),
        )
        for request in wrong_requests:
            with self.subTest(request=request), self.assertRaises(olx.OlxTransportError):
                adapt([success(0, [], request=request)])
        with self.assertRaises(olx.OlxTransportError):
            adapt([success(0, [], request_sha256="0" * 64)])

    def test_only_exact_empty_list_is_terminal_and_failures_never_alias_it(self) -> None:
        self.assertEqual(adapt([success(0, [])]), [frontier.SourcePage(1, ())])
        for payload in ({}, {"data": None}, {"data": ()}, {"data": {}}, {"data": False}):
            request = descriptor(0)
            result = olx.TransportSuccess(request, request.sha256, 200, payload)
            with self.subTest(payload=payload), self.assertRaises(olx.OlxPayloadError):
                adapt([result])
        for kind in sorted(olx.TRANSPORT_FAILURE_KINDS):
            with self.subTest(kind=kind), self.assertRaises(olx.OlxTransportError):
                adapt([failure(0, kind)])
        with self.assertRaises(olx.OlxStreamIncomplete):
            adapt([success(0, [ad(1)])])
        with self.assertRaises(olx.OlxTransportError):
            adapt([success(0, [], status_code=500)])

    def test_valid_overlap_buffers_ties_and_derives_deterministic_order(self) -> None:
        first, second = overlapped_fixture()
        pages = adapt(
            [success(0, first), success(45, second), success(90, [])]
        )
        self.assertEqual([len(page.items) for page in pages], [50, 2, 0])
        all_items = [item for page in pages for item in page.items]
        self.assertEqual(all_items[0].native_id, "olxpl_200")
        self.assertEqual(all_items[-2].native_id, "olxpl_100")
        self.assertEqual(all_items[-1].native_id, "olxpl_99")
        self.assertEqual(len({item.native_id for item in all_items}), 52)
        self.assertTrue(
            all(
                left.sort_value > right.sort_value
                for left, right in zip(all_items, all_items[1:])
            )
        )
        offer = all_items[0].offer
        assert offer is not None
        self.assertEqual(set(offer), olx.PRODUCTION_OFFER_FIELDS)
        self.assertEqual(offer["make_model"], "audi:a5")
        self.assertEqual(offer["source_listing_id"], "olxpl_200")
        self.assertEqual(offer["price_eur"], 23_500)
        self.assertEqual(offer["raw_price"], "100000 PLN")

    def test_shifted_overlap_tie_reorder_and_deletion_skip_fail_closed(self) -> None:
        first, second = overlapped_fixture()
        cases = {
            "shifted-request": success(45, second, request=descriptor(46)),
            "overlap-change": success(45, [ad(999), *second[1:]]),
            "tie-reorder": success(45, [*reversed(second[:5]), *second[5:]]),
            "deletion-skip": success(45, [*second[1:5], *second[5:]]),
        }
        for label, second_result in cases.items():
            with self.subTest(label=label), self.assertRaises(olx.OlxAdapterError):
                adapt([success(0, first), second_result, success(90, [])])
        with self.assertRaises(olx.OlxPayloadError):
            adapt([success(0, first), success(45, [])])
        with self.assertRaises(olx.OlxPayloadError):
            adapt([success(0, [ad(1)]), success(45, [ad(2)]), success(90, [])])

    def test_regression_duplicate_and_oversized_page_fail_closed(self) -> None:
        with self.assertRaises(olx.OlxPayloadError):
            adapt([success(0, [ad(1, OLDER), ad(2, TIED)]), success(45, [])])
        duplicate = [ad(1), ad(1)]
        with self.assertRaises(olx.OlxPayloadError):
            adapt([success(0, duplicate), success(45, [])])
        with self.assertRaises(olx.OlxPayloadError):
            adapt([success(0, [ad(value) for value in range(1, 52)])])

    def test_malformed_nonnumeric_oversized_and_huge_ids_fail_as_adapter_error(self) -> None:
        invalid_ids = (
            None, "", "01", "+1", "1.0", "abc", True, 0, -1,
            1 << 32, str(1 << 32), "9" * 5_000,
        )
        for invalid in invalid_ids:
            with self.subTest(value=str(invalid)[:20]), self.assertRaises(olx.OlxPayloadError):
                adapt([success(0, [ad(invalid)]), success(45, [])])

    def test_nonfinite_values_are_rejected_at_any_payload_depth(self) -> None:
        bad_values = (float("nan"), float("inf"), float("-inf"))
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(olx.OlxPayloadError):
                adapt([success(0, [ad(1, price_value=value)]), success(45, [])])
            with self.subTest(metadata=value), self.assertRaises(olx.OlxPayloadError):
                adapt([success(0, [], payload_extra={"metadata": {"nested": [value]}})])

    def test_price_and_year_filters_advance_frontier_without_offer(self) -> None:
        rows = [
            ad(6, price_value=500),
            ad(5, price_value=5_000_000),
            ad(4, year="1949"),
            ad(3, year="2040"),
            ad(2, year=None),
            ad(1),
        ]
        pages = adapt([success(0, rows), success(45, [])])
        offers = {item.native_id: item.offer for item in pages[0].items}
        for native_id in ("olxpl_6", "olxpl_5", "olxpl_4", "olxpl_3"):
            self.assertIsNone(offers[native_id])
        self.assertEqual(offers["olxpl_2"]["year"], 0)  # type: ignore[index]
        self.assertEqual(offers["olxpl_1"]["make_model"], "audi:a5")  # type: ignore[index]

    def test_unknown_model_is_filtered_but_identity_remains(self) -> None:
        pages = adapt(
            [
                success(0, [ad(2, title="Mystery Vehicle Zed"), ad(1)]),
                success(45, []),
            ]
        )
        self.assertIsNone(pages[0].items[0].offer)
        self.assertEqual(pages[0].items[0].native_id, "olxpl_2")
        self.assertEqual(pages[0].items[1].offer["make_model"], "audi:a5")  # type: ignore[index]

    def test_listing_url_must_be_canonical_offer_path_without_controls(self) -> None:
        invalid_urls = (
            "https://evil.example/d/oferta/car-1.html",
            "https://www.olx.pl/",
            "https://www.olx.pl/category/cars",
            "https://www.olx.pl/d/oferta/",
            "https://www.olx.pl/d/oferta/car-1.html#fragment",
            "https://www.olx.pl/d/oferta/car-1%0A.html",
            "https://www.olx.pl/d/oferta/car-1\n.html",
        )
        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(olx.OlxPayloadError):
                adapt([success(0, [ad(1, url=value)]), success(45, [])])

    def test_filtered_rows_commit_to_frontier_but_not_offer_table(self) -> None:
        selected = contract(max_pages=5, frontier_cap=100)
        pages = adapt(
            [
                success(
                    0,
                    [
                        ad(3, title="Mystery Vehicle Zed"),
                        ad(2, price_value=500),
                        ad(1),
                    ],
                ),
                success(45, []),
            ],
            selected_contract=selected,
        )
        with tempfile.TemporaryDirectory() as temporary:
            connection = ingest.connect(Path(temporary) / "universe.sqlite")
            try:
                receipt = ingest.ingest_incremental_run(
                    connection,
                    contract=selected,
                    allowlist=frozenset({selected.key}),
                    run_id="olx-pl-v2-fixture",
                    request_sha256=digest("olx-pl-v2-fixture"),
                    observed_at_utc=OBSERVED_AT,
                    pages=pages,
                )
                self.assertEqual(receipt["raw_item_count"], 3)
                self.assertEqual(receipt["observed_offer_count"], 1)
                self.assertEqual(receipt["inserted_offer_count"], 1)
                self.assertEqual(
                    [tuple(row) for row in connection.execute(
                        "SELECT source_listing_id, make_model FROM offers"
                    )],
                    [("olxpl_1", "audi:a5")],
                )
                self.assertEqual(
                    {row[0] for row in connection.execute(
                        "SELECT native_id FROM radar_incremental_frontier_ids"
                    )},
                    {"olxpl_1", "olxpl_2", "olxpl_3"},
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
