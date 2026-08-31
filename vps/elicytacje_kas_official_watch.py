#!/usr/bin/env python3
"""Fail-closed connector for the public eLicytacje KAS catalogue.

The portal exposes its own anonymous JSON search endpoint.  This connector
discovers the official movable-property and vehicle dictionary identifiers,
enumerates both the whole catalogue and the official ``Pojazdy`` category,
and verifies total, membership, and stable content in two complete passes.

Only current/future vehicle metadata is emitted.  Pictures, attachments,
personal data, login, bidding, and other authenticated routes are not used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from fx_rates import fetch_ecb_units_per_eur, to_eur

from auction_raw_evidence import RawEvidenceCapture, atomic_write_json


UTC = dt.timezone.utc
SOURCE_KEY = "elicytacje-kas"
SOURCE_NAME = "eLicytacje Krajowej Administracji Skarbowej"
SOURCE_COUNTRY = "PL"
SOURCE_HOST = "elicytacje.mf.gov.pl"
BASE_URL = f"https://{SOURCE_HOST}"
CATALOGUE_URL = BASE_URL + "/auction/"
CONFIG_URL = CATALOGUE_URL + "assets/config/config.json"
ROOT_TRANSLATION_URL = CATALOGUE_URL + "assets/i18n/pl.json"
TERMS_TRANSLATION_URL = CATALOGUE_URL + "assets/i18n/terms/pl.json"
EXPECTED_API_BASE = CATALOGUE_URL.rstrip("/") + "/api"
SEARCH_PATH = "/auction/api/v1/announcement/external"
SEARCH_URL = BASE_URL + SEARCH_PATH
SECTIONS_URL = EXPECTED_API_BASE + "/v1/dict/sections"
CATEGORIES_URL = EXPECTED_API_BASE + "/v1/dict/categories"
DETAIL_PREFIX = CATALOGUE_URL + "announcements/"
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "pl,en;q=0.7",
    "User-Agent": "SonarDeals-eLicytacje-public-metadata/1.0",
}
ACTIVE_STATES = {"WAITING", "INPROGRESS"}
TERMINAL_STATES = {"UNSOLD", "SOLD", "CLOSED", "VOID", "CANCELED"}
KNOWN_STATES = ACTIVE_STATES | TERMINAL_STATES
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_uuid(value: Any, *, label: str) -> str:
    candidate = clean(value).lower()
    if not UUID_RE.fullmatch(candidate):
        raise ValueError(f"{label} is not a canonical UUID: {candidate!r}")
    parsed = uuid.UUID(candidate)
    if str(parsed) != candidate:
        raise ValueError(f"{label} is not canonical: {candidate!r}")
    return candidate


def require_official_url(value: Any, *, path: str | None = None) -> str:
    candidate = clean(value)
    parsed = urlparse(candidate)
    if (
        parsed.scheme != "https"
        or parsed.hostname != SOURCE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or (path is not None and parsed.path != path)
    ):
        raise ValueError(f"non-official eLicytacje URL: {candidate!r}")
    return candidate


def response_json(response: requests.Response, *, label: str) -> Any:
    response.raise_for_status()
    require_official_url(response.url)
    content_type = clean(response.headers.get("content-type")).lower()
    if "json" not in content_type:
        raise ValueError(f"{label} did not return JSON")
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} returned invalid JSON") from exc


def fetch_json(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    capture: RawEvidenceCapture | None = None,
    capture_name: str,
) -> Any:
    require_official_url(url)
    kwargs = {"headers": HEADERS, "timeout": timeout, "allow_redirects": True}
    response = (
        capture.get(session, capture_name, url, **kwargs)
        if capture
        else session.get(url, **kwargs)
    )
    return response_json(response, label=capture_name)


def discover_contract(
    session: requests.Session,
    *,
    timeout: int,
    capture: RawEvidenceCapture | None = None,
) -> dict[str, Any]:
    config = fetch_json(
        session,
        CONFIG_URL,
        timeout=timeout,
        capture=capture,
        capture_name="config",
    )
    if not isinstance(config, dict) or clean(config.get("apiUrlAuction")) != EXPECTED_API_BASE:
        raise ValueError("official config does not pin the expected auction API")

    sections = fetch_json(
        session,
        SECTIONS_URL,
        timeout=timeout,
        capture=capture,
        capture_name="sections",
    )
    categories = fetch_json(
        session,
        CATEGORIES_URL,
        timeout=timeout,
        capture=capture,
        capture_name="categories",
    )
    root_translation = fetch_json(
        session,
        ROOT_TRANSLATION_URL,
        timeout=timeout,
        capture=capture,
        capture_name="translation-root-pl",
    )
    terms_translation = fetch_json(
        session,
        TERMS_TRANSLATION_URL,
        timeout=timeout,
        capture=capture,
        capture_name="translation-terms-pl",
    )
    if not isinstance(sections, list) or not isinstance(categories, list):
        raise ValueError("official section/category dictionaries are malformed")
    movable = [item for item in sections if clean(item.get("name")) == "Ruchomości"]
    if len(movable) != 1:
        raise ValueError("official Ruchomości section is missing or ambiguous")
    movable_id = canonical_uuid(movable[0].get("id"), label="Ruchomości section ID")
    vehicles = [
        item
        for item in categories
        if clean(item.get("name")) == "Pojazdy"
        and clean(item.get("sectionId")).lower() == movable_id
    ]
    if len(vehicles) != 1:
        raise ValueError("official Pojazdy category is missing or ambiguous")
    vehicle_id = canonical_uuid(vehicles[0].get("id"), label="Pojazdy category ID")

    if not isinstance(root_translation, dict):
        raise ValueError("root Polish translation is malformed")
    reuse_clause = clean(root_translation.get("clause"))
    required_reuse_phrases = (
        "nie wymaga zgody Ministra Finansów",
        "niezależnie od celu i sposobu korzystania",
        "Creative Commons Uznanie Autorstwa 3.0 Polska",
    )
    if not all(phrase in reuse_clause for phrase in required_reuse_phrases):
        raise ValueError("official reuse/CC BY clause is missing or changed")

    try:
        first_rule = clean(terms_translation["termsOfUse"]["list"][0])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("official public-access term is missing") from exc
    if not all(
        phrase in first_rule
        for phrase in (
            "Dostęp do Portalu eLicytacje KAS jest darmowy",
            "nie wymaga od gościa Portalu eLicytacje KAS uwierzytelniania się",
        )
    ):
        raise ValueError("official anonymous-access term is missing or changed")

    return {
        "api_base": EXPECTED_API_BASE,
        "movable_section_id": movable_id,
        "vehicle_category_id": vehicle_id,
        "reuse_clause": reuse_clause,
        "reuse_clause_sha256": canonical_sha256(reuse_clause),
        "public_access_term": first_rule,
        "public_access_term_sha256": canonical_sha256(first_rule),
    }


def normalize_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("search row is not an object")
    uid = canonical_uuid(item.get("uuid"), label="announcement UUID")
    state = clean(item.get("electronicStateCode")).upper()
    if state not in KNOWN_STATES:
        raise ValueError(f"unknown announcement state for {uid}: {state!r}")
    title = clean(item.get("title"))
    if not title:
        raise ValueError(f"announcement {uid} has no title")
    type_codes = item.get("announcementTypeCodes")
    if not isinstance(type_codes, list) or not type_codes:
        raise ValueError(f"announcement {uid} has no type-code proof")
    normalized_codes = sorted({clean(code).upper() for code in type_codes if clean(code)})
    if not normalized_codes:
        raise ValueError(f"announcement {uid} has empty type codes")
    return {
        "uuid": uid,
        "title": title,
        "picture_id": clean(item.get("pictureId")) or None,
        "starting_price": item.get("startingPrice"),
        "final_price": item.get("finalPrice"),
        "highest_bid": item.get("highestBid"),
        "publication_date_time": clean(item.get("publicationDateTime")) or None,
        "sale_begin_date_time": clean(item.get("saleBeginDateTime")) or None,
        "sale_end_date_time": clean(item.get("saleEndDateTime")) or None,
        "cancellation_date_time": clean(item.get("cancellationDateTime")) or None,
        "void_date_time": clean(item.get("voidDateTime")) or None,
        "deposit_due_date": clean(item.get("depositDueDate")) or None,
        "announcement_type_codes": normalized_codes,
        "state": state,
        "location": clean(item.get("localization")),
    }


def stable_item_view(item: dict[str, Any]) -> dict[str, Any]:
    """Fields expected not to change merely because another bidder bids."""

    return {
        key: item[key]
        for key in (
            "uuid",
            "title",
            "picture_id",
            "starting_price",
            "publication_date_time",
            "sale_begin_date_time",
            "sale_end_date_time",
            "cancellation_date_time",
            "void_date_time",
            "deposit_due_date",
            "announcement_type_codes",
            "state",
            "location",
        )
    }


def fetch_search_page(
    session: requests.Session,
    *,
    body: dict[str, Any],
    page: int,
    page_size: int,
    timeout: int,
    capture: RawEvidenceCapture | None,
    capture_name: str,
) -> dict[str, Any]:
    params = {
        "page": page,
        "size": page_size,
        "sort": "publicationDateTime,desc",
    }
    kwargs = {
        "headers": {**HEADERS, "Content-Type": "application/json"},
        "params": params,
        "json": body,
        "timeout": timeout,
        "allow_redirects": True,
    }
    response = (
        capture.post(session, capture_name, SEARCH_URL, **kwargs)
        if capture
        else session.post(SEARCH_URL, **kwargs)
    )
    require_official_url(response.url, path=SEARCH_PATH)
    payload = response_json(response, label=capture_name)
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        raise ValueError("search response envelope is malformed")
    total = payload.get("totalElements")
    pages = payload.get("totalPages")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or isinstance(pages, bool)
        or not isinstance(pages, int)
        or pages < 0
    ):
        raise ValueError("search response totals are malformed")
    return {"total": total, "pages": pages, "items": payload["content"]}


def enumerate_search(
    session: requests.Session,
    *,
    body: dict[str, Any],
    label: str,
    pass_number: int,
    page_size: int,
    max_rows: int,
    timeout: int,
    request_delay: float,
    capture: RawEvidenceCapture | None,
) -> dict[str, Any]:
    first = fetch_search_page(
        session,
        body=body,
        page=0,
        page_size=page_size,
        timeout=timeout,
        capture=capture,
        capture_name=f"pass-{pass_number}-{label}-page-0000",
    )
    total, pages = first["total"], first["pages"]
    expected_pages = math.ceil(total / page_size) if total else 0
    if total > max_rows:
        raise ValueError(f"{label} total exceeds the configured row guard")
    if pages != expected_pages:
        raise ValueError(f"{label} totalPages does not reconcile with totalElements")
    raw_items = list(first["items"])
    for page in range(1, pages):
        if request_delay:
            time.sleep(request_delay)
        snapshot = fetch_search_page(
            session,
            body=body,
            page=page,
            page_size=page_size,
            timeout=timeout,
            capture=capture,
            capture_name=f"pass-{pass_number}-{label}-page-{page:04d}",
        )
        if snapshot["total"] != total or snapshot["pages"] != pages:
            raise ValueError(f"{label} advertised boundary changed during enumeration")
        raw_items.extend(snapshot["items"])
    normalized = [normalize_item(item) for item in raw_items]
    ids = [item["uuid"] for item in normalized]
    if len(normalized) != total or len(set(ids)) != total:
        raise ValueError(f"{label} advertised/visited/unique accounting failed")
    ordered = sorted(normalized, key=lambda item: item["uuid"])
    stable = [stable_item_view(item) for item in ordered]
    return {
        "advertised_count": total,
        "visited_count": len(normalized),
        "unique_id_count": len(set(ids)),
        "page_count": pages,
        "membership_sha256": canonical_sha256(sorted(ids)),
        "stable_content_sha256": canonical_sha256(stable),
        "full_content_sha256": canonical_sha256(ordered),
        "items": ordered,
    }


def pass_public_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in value if key != "items"}


def assert_two_pass_equal(first: dict[str, Any], second: dict[str, Any], *, label: str) -> None:
    keys = (
        "advertised_count",
        "visited_count",
        "unique_id_count",
        "page_count",
        "membership_sha256",
        "stable_content_sha256",
    )
    mismatches = [key for key in keys if first.get(key) != second.get(key)]
    if mismatches:
        raise ValueError(f"{label} two-pass drift: {', '.join(mismatches)}")


def make_row(
    item: dict[str, Any], *, observed_at: dt.datetime, raw_evidence_ref: str = "", fx_rate: float | None = None
) -> dict[str, Any]:
    uid = item["uuid"]
    source_url = DETAIL_PREFIX + uid
    current_bid = item["highest_bid"]
    price_amount = current_bid if isinstance(current_bid, (int, float)) and current_bid > 0 else item["starting_price"]
    price_kind = "current_bid" if price_amount == current_bid and current_bid is not None else "starting_bid"
    year_matches = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", item["title"])
    year = int(year_matches[-1]) if year_matches else None
    return {
        "id": f"{SOURCE_KEY}:{uid}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "country": SOURCE_COUNTRY,
        "title": item["title"],
        "url": source_url,
        "source_url": source_url,
        "currency": "PLN",
        "price": price_amount,
        "price_kind": price_kind,
        "price_currency": "PLN",
        "price_amount": price_amount,
        "price_label": "Aktualna oferta" if price_kind == "current_bid" else "Cena wywołania",
        "bid_visibility": "public_current_bid" if price_kind == "current_bid" else "not_present_in_public_list",
        "current_bid": current_bid,
        "final_price": item["final_price"],
        "category": "vehicle",
        "category_raw": "Pojazdy",
        "year": year,
        "seller": "Krajowa Administracja Skarbowa",
        "location": item["location"],
        "auction_start": item["sale_begin_date_time"],
        "auction_end": item["sale_end_date_time"],
        "sale_event_utc": item["sale_begin_date_time"],
        "sale_end_utc": item["sale_end_date_time"],
        "canonical_end_utc": item["sale_end_date_time"],
        "publication_date": item["publication_date_time"],
        "deposit_due_date": item["deposit_due_date"],
        "status": item["state"],
        "announcement_type_codes": item["announcement_type_codes"],
        "last_seen_at": observed_at.isoformat(),
        "observed_at_utc": observed_at.isoformat(),
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public current/future vehicle metadata is verified; Algerian importability "
            "and bidder participation requirements still require per-lot review."
        ),
        "access_sale_note": (
            "Public viewing needs no account; bidding/participation requires the portal's "
            "authentication, profile and sale-specific conditions."
        ),
        "raw_evidence_ref": raw_evidence_ref,
        "provisional_import_classification": "current_or_future_vehicle",
        "classification_reason": "Official Pojazdy category and WAITING/INPROGRESS state",
        "publication_attribution": SOURCE_NAME,
        "publication_license": "Creative Commons Attribution 3.0 Poland",
        "publication_license_url": "https://creativecommons.org/licenses/by/3.0/pl/",
    }


def build_watch(
    session: requests.Session | None = None,
    *,
    timeout: int = 30,
    page_size: int = 2000,
    max_rows: int = 10_000,
    request_delay: float = 0.0,
    raw_root: Path | None = None,
    fx_rates: dict[str, tuple[float, str]] | None = None,
) -> dict[str, Any]:
    if page_size < 1 or page_size > max_rows or max_rows < 1:
        raise ValueError("invalid page-size/max-rows guard")
    if fx_rates is not None and "PLN" in fx_rates:
        fx_rate, fx_date = fx_rates["PLN"]
    else:
        fx_rate, fx_date = fetch_ecb_units_per_eur("PLN")
    if request_delay < 0 or request_delay > 10:
        raise ValueError("invalid request delay")
    session = session or requests.Session()
    captured_at = dt.datetime.now(UTC)
    capture = RawEvidenceCapture(raw_root, SOURCE_KEY, captured_at) if raw_root else None
    contract = discover_contract(session, timeout=timeout, capture=capture)
    vehicle_filter = {
        "sectionId": contract["movable_section_id"],
        "categoryId": contract["vehicle_category_id"],
    }

    snapshots: dict[str, dict[str, Any]] = {}
    for pass_number in (1, 2):
        snapshots[f"global_{pass_number}"] = enumerate_search(
            session,
            body={},
            label="global",
            pass_number=pass_number,
            page_size=page_size,
            max_rows=max_rows,
            timeout=timeout,
            request_delay=request_delay,
            capture=capture,
        )
        snapshots[f"vehicle_{pass_number}"] = enumerate_search(
            session,
            body=vehicle_filter,
            label="vehicle",
            pass_number=pass_number,
            page_size=page_size,
            max_rows=max_rows,
            timeout=timeout,
            request_delay=request_delay,
            capture=capture,
        )

    assert_two_pass_equal(snapshots["global_1"], snapshots["global_2"], label="global")
    assert_two_pass_equal(snapshots["vehicle_1"], snapshots["vehicle_2"], label="vehicle")
    global_items = snapshots["global_2"]["items"]
    vehicle_items = snapshots["vehicle_2"]["items"]
    global_ids = {item["uuid"] for item in global_items}
    vehicle_ids = {item["uuid"] for item in vehicle_items}
    if not vehicle_ids.issubset(global_ids):
        raise ValueError("official vehicle membership is not a subset of the global catalogue")
    if global_ids and len(vehicle_ids) >= len(global_ids):
        raise ValueError("category filter acknowledgement cannot be proven as a proper subset")

    classification_counts = {
        "non_vehicle": len(global_ids - vehicle_ids),
        "vehicle": len(vehicle_ids),
    }
    lifecycle_counts = dict(sorted(Counter(item["state"] for item in vehicle_items).items()))
    eligible_items = [item for item in vehicle_items if item["state"] in ACTIVE_STATES]
    evidence_ref = capture.reference if capture else ""
    rows = sorted(
        (
            make_row(
                item,
                observed_at=captured_at,
                raw_evidence_ref=evidence_ref,
                fx_rate=fx_rate,
            )
            for item in eligible_items
        ),
        key=lambda row: row["id"],
    )
    report = {
        "status": "ok",
        "access": "public_anonymous",
        "catalogue_url": CATALOGUE_URL,
        "search_api_url": SEARCH_URL,
        "dictionary_urls": [SECTIONS_URL, CATEGORIES_URL],
        "official_filter": vehicle_filter,
        "publication_status": "accepted",
        "publication_scope": "public list metadata only; no pictures, attachments or personal data mirrored",
        "publication_basis": (
            "The official portal states that use of service content, regardless of purpose "
            "or manner, needs no Minister of Finance consent; copyright-marked content is "
            "offered under CC BY 3.0 Poland. Metadata output includes attribution and source links."
        ),
        "reuse_evidence": {
            "url": ROOT_TRANSLATION_URL,
            "clause_sha256": contract["reuse_clause_sha256"],
            "cc_license": "Creative Commons Uznanie Autorstwa 3.0 Polska",
            "attribution": SOURCE_NAME,
        },
        "public_access_evidence": {
            "url": TERMS_TRANSLATION_URL,
            "term_sha256": contract["public_access_term_sha256"],
        },
        "enumeration_reconciliation": {
            "global": {
                "pass_1": pass_public_summary(snapshots["global_1"]),
                "pass_2": pass_public_summary(snapshots["global_2"]),
                "snapshot_verified": True,
            },
            "vehicle": {
                "pass_1": pass_public_summary(snapshots["vehicle_1"]),
                "pass_2": pass_public_summary(snapshots["vehicle_2"]),
                "snapshot_verified": True,
            },
        },
        "global_total": len(global_ids),
        "classified_rows": len(global_ids),
        "classification_counts": classification_counts,
        "vehicle_lifecycle_counts": lifecycle_counts,
        "current_or_future_vehicle_rows": len(rows),
        "normalized_rows": len(rows),
        "eligibility_counts": {"current_or_future_vehicle": len(rows)},
        "invariants": {
            "official_config_api_pinned": True,
            "official_dictionary_filter_pinned": True,
            "global_two_pass_membership_and_stable_content": True,
            "vehicle_two_pass_membership_and_stable_content": True,
            "advertised_visited_unique_equal": True,
            "vehicle_is_proper_global_subset": True,
            "classified_equals_global_total": True,
            "no_authenticated_routes": True,
            "no_images_or_attachments_mirrored": True,
        },
    }
    payload = {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": captured_at.isoformat(),
        "research_only": False,
        "publication_status": "accepted",
        "publication_attribution": SOURCE_NAME,
        "row_count": len(rows),
        "rows": rows,
        "source_reports": {SOURCE_KEY: report},
    }
    validate_watch(payload)
    if capture:
        capture.finish(report)
        payload["raw_evidence_manifest"] = capture.reference
    return payload


def validate_watch(payload: Any) -> None:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("lane") != "official_auction_watch"
        or payload.get("research_only") is not False
        or payload.get("publication_status") != "accepted"
        or not isinstance(payload.get("rows"), list)
        or payload.get("row_count") != len(payload["rows"])
    ):
        raise ValueError("invalid eLicytacje output envelope")
    report = payload.get("source_reports", {}).get(SOURCE_KEY)
    if not isinstance(report, dict) or report.get("status") != "ok":
        raise ValueError("eLicytacje source report is missing")
    global_total = report.get("global_total")
    counts = report.get("classification_counts")
    if (
        isinstance(global_total, bool)
        or not isinstance(global_total, int)
        or not isinstance(counts, dict)
        or sum(counts.values()) != global_total
        or report.get("classified_rows") != global_total
    ):
        raise ValueError("global classification accounting does not balance")
    reconciliation = report.get("enumeration_reconciliation")
    if not isinstance(reconciliation, dict):
        raise ValueError("enumeration reconciliation is missing")
    for label in ("global", "vehicle"):
        group = reconciliation.get(label)
        if not isinstance(group, dict) or group.get("snapshot_verified") is not True:
            raise ValueError(f"{label} snapshot proof is missing")
        first, second = group.get("pass_1"), group.get("pass_2")
        if not isinstance(first, dict) or not isinstance(second, dict):
            raise ValueError(f"{label} pass proof is missing")
        for snapshot in (first, second):
            total = snapshot.get("advertised_count")
            if (
                snapshot.get("visited_count") != total
                or snapshot.get("unique_id_count") != total
                or not SHA256_RE.fullmatch(clean(snapshot.get("membership_sha256")))
                or not SHA256_RE.fullmatch(clean(snapshot.get("stable_content_sha256")))
            ):
                raise ValueError(f"{label} pass accounting is invalid")
        assert_two_pass_equal(first, second, label=label)
    ids: set[str] = set()
    urls: set[str] = set()
    for row in payload["rows"]:
        if not isinstance(row, dict):
            raise ValueError("non-object normalized row")
        row_id = clean(row.get("id"))
        source_url = require_official_url(row.get("source_url"))
        if (
            not row_id.startswith(SOURCE_KEY + ":")
            or row.get("source_key") != SOURCE_KEY
            or row.get("provisional_import_classification") != "current_or_future_vehicle"
            or row.get("status") not in ACTIVE_STATES
            or row.get("price_kind") not in {"starting_bid", "current_bid"}
            or row.get("price_currency") != "PLN"
            or not isinstance(row.get("price_amount"), (int, float))
            or row.get("price_amount") <= 0
            or row.get("eligibility_status") != "review_required"
            or not clean(row.get("eligibility_reason"))
            or not clean(row.get("last_seen_at"))
            or not clean(row.get("canonical_end_utc"))
            or row_id in ids
            or source_url in urls
        ):
            raise ValueError("invalid or duplicate normalized vehicle row")
        ids.add(row_id)
        urls.add(source_url)
    if (
        report.get("normalized_rows") != len(ids)
        or report.get("current_or_future_vehicle_rows") != len(ids)
        or report.get("eligibility_counts") != {"current_or_future_vehicle": len(ids)}
    ):
        raise ValueError("normalized eligibility accounting does not balance")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--page-size", type=int, default=2000)
    parser.add_argument("--max-rows", type=int, default=10_000)
    parser.add_argument("--request-delay", type=float, default=0.0)
    args = parser.parse_args()
    payload = build_watch(
        timeout=args.timeout,
        page_size=args.page_size,
        max_rows=args.max_rows,
        request_delay=args.request_delay,
        raw_root=args.raw_root,
    )
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(
        json.dumps(
            {
                "source": SOURCE_KEY,
                "global_total": report["global_total"],
                "vehicle_total": report["classification_counts"]["vehicle"],
                "eligible_vehicle_rows": payload["row_count"],
                "publication_status": report["publication_status"],
                "output": str(args.out),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
