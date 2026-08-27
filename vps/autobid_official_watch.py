#!/usr/bin/env python3
"""Complete research capture for Autobid's anonymous public vehicle catalogue.

The public page exposes a finite ID snapshot and uses the site's own public
``/api/backend`` endpoint to render those results.  This connector processes
every ID in that snapshot in bounded chunks.  It never logs in, never bypasses
an access control, and deliberately does not request or persist ``price.current``:
the anonymous UI labels the prevailing bid as confidential.

The resulting artifact remains research-only until reuse/publication rights and
the verified-dealer participation path are accepted for production.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import requests

from auction_raw_evidence import RawEvidenceCapture


UTC = dt.timezone.utc
SOURCE_KEY = "autobid"
SOURCE_NAME = "Autobid.de"
SOURCE_COUNTRY = "DE"
SEARCH_URL = "https://autobid.de/en/search-results?sortingType=auctionStartDate-ASCENDING"
API_URL = "https://autobid.de/api/backend"
ITEM_URL = "https://autobid.de/en/item/{slug}"
PAGE_SIZE = 30
MAX_CATALOGUE_IDS = 20_000
HEADERS = {
    "User-Agent": "DZ-Auto-Radar/1.0 (+official public-auction monitor)",
    "Accept-Language": "en-US,en;q=0.9",
}
SORT_FIELDS = [
    {"name": "auctionStartDate", "order": "ASCENDING"},
    {"name": "catalogNumber", "order": "ASCENDING"},
    {"name": "auctionNumber", "order": "ASCENDING"},
]

# Only fields needed for classification are requested.  In particular,
# price.current, bids, user flags, seller contacts, VIN and images are absent.
CATALOGUE_QUERY = """
query cars($pageSize: Int!, $pageNumber: Int!, $lang: String!, $sortBy: [SortingField!], $ids: [Int] = []) {
  items(params: {
    includeNotVisibleInList: false,
    publicationStatus: [PUBLISHED],
    stages: [BEFORE_AUCTION, IN_AUCTION],
    pageSize: $pageSize,
    pageNumber: $pageNumber,
    lang: $lang,
    sortBy: $sortBy,
    ids: $ids,
    equipmentIdWhitelist: [17, 21, 68, 70]
  }) {
    itemPageCount
    items {
      id auctionId auctionNumber name stage state
      price { start }
      category { id name }
      equipments auctionStartDate
      manufacturer { name }
      catalogNumber slug taxInformation
      additionalInformation {
        itemLocationCode
        itemLocationCountry { isoCode }
      }
    }
  }
}
"""

_NUXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_DIESEL_RE = re.compile(r"\b(?:diesel|gazole|gasoleo|tdi|cdi|dci|hdi|bluehdi)\b", re.I)
_PETROL_RE = re.compile(r"\b(?:petrol|gasoline|benzin|benzine|essence|tsi|tfsi)\b", re.I)
_ELECTRIC_RE = re.compile(r"\b(?:electric|elektro|bev|battery electric)\b", re.I)
_HYBRID_RE = re.compile(
    r"\b(?:hybrid|phev|hev|plug[ -]?in|e-tech|e-tense|mhev)\b",
    re.I,
)
_NON_CAR_RE = re.compile(
    r"\b(?:motorcycle|motorbike|scooter|moped|quad|tractor|trailer|semi-trailer|"
    r"forklift|excavator|spare parts?|engine only)\b",
    re.I,
)
_MATERIAL_DAMAGE_RE = re.compile(
    r"\b(?:salvage|accident(?:ed)?|damaged|not running|non-runner|engine damage|"
    r"fire damage|flood damage)\b",
    re.I,
)


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    text = re.sub(r"[^0-9]", "", clean(value))
    if not text:
        return None
    try:
        result = int(text)
    except ValueError:
        return None
    return result if result >= 0 else None


def _three_year_cutoff(today: dt.date) -> dt.date:
    try:
        return today.replace(year=today.year - 3)
    except ValueError:
        return today.replace(year=today.year - 3, day=28)


def registration_details(value: Any, *, today: dt.date) -> dict[str, Any]:
    """Classify a full-date, month, or year registration against the rolling cutoff."""
    raw = clean(value)
    if not raw:
        return {"display": None, "year": None, "age_status": "unknown", "precision": "missing"}
    parsed: dt.date | None = None
    precision = "invalid"
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(raw, date_format).date()
            precision = "day"
            break
        except ValueError:
            continue
    year: int | None = None
    month: int | None = None
    if parsed is not None:
        year, month = parsed.year, parsed.month
        first, last = parsed, parsed
        display = parsed.isoformat()
    else:
        month_match = re.fullmatch(r"(0?[1-9]|1[0-2])[./-]((?:19|20)\d{2})", raw)
        iso_month_match = re.fullmatch(r"((?:19|20)\d{2})-(0[1-9]|1[0-2])", raw)
        year_match = re.fullmatch(r"((?:19|20)\d{2})", raw)
        if month_match:
            month, year = int(month_match.group(1)), int(month_match.group(2))
            precision = "month"
        elif iso_month_match:
            year, month = int(iso_month_match.group(1)), int(iso_month_match.group(2))
            precision = "month"
        elif year_match:
            year = int(year_match.group(1))
            precision = "year"
        if precision == "month" and year is not None and month is not None:
            first = dt.date(year, month, 1)
            last = dt.date(year, month, calendar.monthrange(year, month)[1])
            display = f"{year:04d}-{month:02d}"
        elif precision == "year" and year is not None:
            first, last = dt.date(year, 1, 1), dt.date(year, 12, 31)
            display = f"{year:04d}"
        else:
            return {"display": raw, "year": None, "age_status": "unknown", "precision": "invalid"}
    if first > today:
        age_status = "unknown"
    else:
        cutoff = _three_year_cutoff(today)
        if last < cutoff:
            age_status = "outside_window"
        elif first >= cutoff:
            age_status = "within_window"
        else:
            age_status = "boundary_needs_exact_day"
    return {
        "display": display,
        "year": year,
        "age_status": age_status,
        "precision": precision,
    }


def normalize_fuel(raw_value: Any, title: Any) -> tuple[str, str]:
    raw = clean(raw_value).casefold()
    text = clean(title)
    hybrid = bool(_HYBRID_RE.search(f"{raw} {text}"))
    diesel = bool(_DIESEL_RE.search(f"{raw} {text}"))
    petrol = bool(_PETROL_RE.search(f"{raw} {text}"))
    electric = bool(_ELECTRIC_RE.search(f"{raw} {text}"))
    if diesel:
        return ("diesel/electric hybrid" if hybrid or electric else "diesel", "incompatible")
    if petrol:
        return ("petrol/electric hybrid" if hybrid or electric else "petrol", "compatible")
    if electric and not hybrid:
        return "electric", "compatible"
    if hybrid:
        return "hybrid (combustion fuel unknown)", "unknown"
    if raw in {"electric", "elektro"}:
        return "electric", "compatible"
    if raw in {"petrol", "gasoline", "benzin", "benzine"}:
        return "petrol", "compatible"
    if raw in {"diesel", "gazole"}:
        return "diesel", "incompatible"
    if raw in {"gas", "lpg", "cng", "other"}:
        return raw, "unknown"
    return raw or "unknown", "unknown"


def provisional_classification(
    *, title: str, age_status: str, fuel_status: str, registration: str | None, fuel: str
) -> tuple[str, str, str]:
    if _NON_CAR_RE.search(title):
        return "not_eligible", "not_eligible", "The catalogue title identifies a non-car asset."
    if _MATERIAL_DAMAGE_RE.search(title):
        return "not_eligible", "not_eligible", "The catalogue title contains a material damage or non-running marker."
    if age_status == "outside_window":
        return "not_eligible", "not_eligible", f"First registration {registration or 'unknown'} is outside the rolling three-year window."
    if fuel_status == "incompatible":
        return "not_eligible", "not_eligible", f"Published fuel classification ({fuel}) is not accepted by the configured Algeria import policy."
    if age_status == "within_window" and fuel_status == "compatible":
        return (
            "review_required",
            "preliminarily_eligible",
            "Age and fuel pass the preliminary screen; verify exact documents, condition, dealer admission, fees, and export before purchase.",
        )
    missing = []
    if age_status == "boundary_needs_exact_day":
        missing.append("exact registration day at the three-year boundary")
    elif age_status != "within_window":
        missing.append("reliable first-registration date")
    if fuel_status != "compatible":
        missing.append("accepted fuel subtype")
    return (
        "review_required",
        "review_required",
        "Public catalogue requires " + ", ".join(missing or ["document and condition verification"]) + "; dealer admission, fees, and export also remain unverified.",
    )


def _equipment(item: dict[str, Any], equipment_id: int) -> str:
    equipments = item.get("equipments")
    if not isinstance(equipments, dict):
        return ""
    candidate = equipments.get(f"eq{equipment_id}")
    if not isinstance(candidate, dict):
        candidate = next(
            (
                value for value in equipments.values()
                if isinstance(value, dict) and value.get("id") == equipment_id
            ),
            None,
        )
    if not isinstance(candidate, dict):
        return ""
    return clean(candidate.get("value") or candidate.get("rawValue"))


def parse_utc(value: Any) -> dt.datetime | None:
    raw = clean(value)
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def extract_catalogue_ids(markup: str, *, max_ids: int = MAX_CATALOGUE_IDS) -> list[int]:
    match = _NUXT_DATA_RE.search(markup)
    if not match:
        raise ValueError("Autobid Nuxt payload is missing")
    try:
        flat = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("Autobid Nuxt payload is invalid JSON") from exc
    if not isinstance(flat, list):
        raise ValueError("Autobid Nuxt payload is not a flattened list")
    candidates: list[list[int]] = []
    for node in flat:
        if not isinstance(node, dict) or not {"ids", "pageSize", "pageNumber", "sortBy"}.issubset(node):
            continue
        ref = node.get("ids")
        if not isinstance(ref, int) or ref < 0 or ref >= len(flat) or not isinstance(flat[ref], list):
            continue
        values: list[int] = []
        for value_ref in flat[ref]:
            if isinstance(value_ref, int) and 0 <= value_ref < len(flat):
                value = flat[value_ref]
                if isinstance(value, int) and 0 < value <= 2_147_483_647:
                    values.append(value)
        if values:
            candidates.append(values)
    if not candidates:
        raise ValueError("Autobid public result IDs are missing")
    selected = max(candidates, key=len)
    unique = list(dict.fromkeys(selected))
    if len(unique) > max_ids:
        raise ValueError(f"Autobid ID count {len(unique)} exceeds safety limit {max_ids}")
    return unique


def _request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    name: str,
    timeout: int,
    capture: RawEvidenceCapture | None,
    **kwargs: Any,
) -> requests.Response:
    last_error: requests.RequestException | None = None
    sender = session.get if method == "GET" else session.post
    for attempt in range(3):
        try:
            response = sender(url, timeout=timeout, **kwargs)
            if capture:
                record_name = name if attempt == 0 else f"{name}-retry{attempt + 1}"
                capture.record(record_name, method, response)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.4 * (2 ** attempt))
    assert last_error is not None
    raise last_error


def _decode_api_page(response: requests.Response) -> tuple[list[dict[str, Any]], int]:
    try:
        decoded = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Autobid API did not return JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Autobid API envelope is not an object")
    if decoded.get("errors"):
        raise ValueError(f"Autobid API returned errors: {str(decoded['errors'])[:300]}")
    data = decoded.get("data")
    envelope = data.get("items") if isinstance(data, dict) else None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("items"), list):
        raise ValueError("Autobid API item schema changed")
    try:
        page_count = int(envelope.get("itemPageCount"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Autobid API page count is missing") from exc
    if page_count < 0:
        raise ValueError("Autobid API page count is negative")
    rows = [row for row in envelope["items"] if isinstance(row, dict)]
    if len(rows) != len(envelope["items"]):
        raise ValueError("Autobid API returned a non-object item")
    return rows, page_count


def fetch_catalogue(
    session: requests.Session,
    *,
    timeout: int = 30,
    page_size: int = PAGE_SIZE,
    max_ids: int = MAX_CATALOGUE_IDS,
    request_delay: float = 0.1,
    capture: RawEvidenceCapture | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not 1 <= page_size <= PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {PAGE_SIZE}")
    if request_delay < 0:
        raise ValueError("request_delay cannot be negative")
    seed = _request(
        session,
        "GET",
        SEARCH_URL,
        name="search-results",
        timeout=timeout,
        capture=capture,
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
    )
    ids = extract_catalogue_ids(seed.text, max_ids=max_ids)
    id_set = set(ids)
    unique: dict[int, dict[str, Any]] = {}
    duplicate_ids = 0
    chunks = [ids[index:index + page_size] for index in range(0, len(ids), page_size)]
    for chunk_index, chunk in enumerate(chunks):
        payload = {
            "queryApi": "auctions",
            "queryMethod": "POST",
            "queryUrl": "/api/v1/query",
            "query": CATALOGUE_QUERY,
            "variables": {
                "pageSize": page_size,
                "pageNumber": 0,
                "lang": "en",
                "sortBy": SORT_FIELDS,
                "ids": chunk,
            },
        }
        response = _request(
            session,
            "POST",
            API_URL,
            name=f"catalogue-c{chunk_index:04d}",
            timeout=timeout,
            capture=capture,
            json=payload,
            headers={
                **HEADERS,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": SEARCH_URL,
            },
        )
        rows, page_count = _decode_api_page(response)
        if page_count not in {0, 1}:
            raise ValueError(
                f"Autobid chunk {chunk_index} unexpectedly spans {page_count} pages"
            )
        chunk_set = set(chunk)
        for row in rows:
            row_id = row.get("id")
            if not isinstance(row_id, int) or row_id not in chunk_set or row_id not in id_set:
                raise ValueError("Autobid API returned an item outside the requested ID chunk")
            if row_id in unique:
                duplicate_ids += 1
            else:
                unique[row_id] = row
        if request_delay and chunk_index + 1 < len(chunks):
            time.sleep(request_delay)
    digest = hashlib.sha256(
        ",".join(str(value) for value in ids).encode("ascii")
    ).hexdigest()
    return list(unique.values()), {
        "seed_id_count": len(ids),
        "seed_ids_sha256": digest,
        "api_chunks": len(chunks),
        "page_size": page_size,
        "public_items": len(unique),
        "not_public_or_not_accessible": len(ids) - len(unique),
        "duplicate_ids": duplicate_ids,
    }


def item_to_row(
    item: dict[str, Any], *, now: dt.datetime, raw_evidence_ref: str = ""
) -> dict[str, Any] | None:
    listing_id = item.get("id")
    slug = clean(item.get("slug")).strip("/")
    title = clean(item.get("name"))
    if not isinstance(listing_id, int) or listing_id <= 0 or not slug or not title:
        return None
    if not re.fullmatch(r"[a-z0-9-]+", slug) or not slug.endswith(f"-{listing_id}"):
        return None
    registration = registration_details(_equipment(item, 21), today=now.date())
    fuel, fuel_status = normalize_fuel(_equipment(item, 17), title)
    status, provisional, reason = provisional_classification(
        title=title,
        age_status=registration["age_status"],
        fuel_status=fuel_status,
        registration=registration["display"],
        fuel=fuel,
    )
    price = _positive_number(
        item.get("price", {}).get("start") if isinstance(item.get("price"), dict) else None
    )
    event = parse_utc(item.get("auctionStartDate"))
    additional = item.get("additionalInformation")
    additional = additional if isinstance(additional, dict) else {}
    location_country = additional.get("itemLocationCountry")
    location_country = location_country if isinstance(location_country, dict) else {}
    asset_country = clean(location_country.get("isoCode")).upper()
    if not re.fullmatch(r"[A-Z]{2}", asset_country):
        asset_country = SOURCE_COUNTRY
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    stage = clean(item.get("stage")).upper()
    auction_status = "upcoming" if stage == "BEFORE_AUCTION" else "active" if stage == "IN_AUCTION" else "unknown"
    return {
        "id": f"{SOURCE_KEY}:{listing_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        # This identifies a row emitted by the installed source-specific
        # connector.  It permits broad monitored display while it remains
        # review-required; it never promotes the row into the strict lane.
        "adapter_authorized": True,
        "source_name": SOURCE_NAME,
        "source_country": SOURCE_COUNTRY,
        "country": asset_country,
        "asset_country": asset_country,
        "url": ITEM_URL.format(slug=slug),
        "title": title,
        "model": title,
        "manufacturer": clean(
            item.get("manufacturer", {}).get("name")
            if isinstance(item.get("manufacturer"), dict) else ""
        ),
        "category": clean(category.get("name")) or "vehicle",
        "category_raw": clean(category.get("name")),
        "year": registration["year"],
        "registration_date": registration["display"],
        "registration_precision": registration["precision"],
        "age_classification": registration["age_status"],
        "mileage_km": _nonnegative_int(_equipment(item, 68)),
        "fuel": fuel,
        "fuel_raw": _equipment(item, 17),
        "fuel_classification": fuel_status,
        "transmission": _equipment(item, 70),
        "price_amount": price,
        "price_currency": "EUR",
        "price_eur": price,
        "price_kind": "starting_bid" if price is not None else "unknown",
        "price_label": "public starting price; anonymous prevailing bid is confidential",
        "bid_visibility": "prevailing_bid_confidential",
        "canonical_end_utc": None,
        "sale_end_utc": None,
        "sale_event_utc": event.isoformat() if event else None,
        "last_seen_at": now.isoformat(),
        "eligibility_status": status,
        "provisional_import_classification": provisional,
        "eligibility_reason": reason,
        "access_sale_note": (
            "Verified motor-dealer account required. Confirm non-EU admission, lot condition, documents, fees, tax, payment, transport, and export directly with Autobid."
        ),
        "auction_status": auction_status,
        "official_stage": stage,
        "official_state": clean(item.get("state")),
        "auction_number": item.get("auctionNumber"),
        "catalog_number": item.get("catalogNumber"),
        "location": clean(additional.get("itemLocationCode")),
        "tax_information": clean(item.get("taxInformation")),
        "damage": "",
        "documents": "",
        "description": "Anonymous public Autobid catalogue summary; prevailing bid and full due diligence require verified access.",
        "raw_evidence_ref": raw_evidence_ref,
        "evidence": (
            "Autobid public search ID snapshot and public catalogue fields; price.current is neither requested nor retained because the anonymous UI marks the prevailing bid confidential."
        ),
    }


def validate_payload(payload: dict[str, Any]) -> None:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("lane") != "official_auction_watch"
        or payload.get("research_only") is not True
        or not isinstance(payload.get("rows"), list)
        or payload.get("row_count") != len(payload["rows"])
    ):
        raise ValueError("Autobid output envelope is invalid")
    report = payload.get("source_reports", {}).get(SOURCE_KEY)
    if not isinstance(report, dict) or report.get("publication_ready") is not False:
        raise ValueError("Autobid research publication gate is missing")
    if report.get("current_bid_requested") is not False or report.get("current_bid_retained") is not False:
        raise ValueError("Autobid confidential-bid guard is missing")
    if report.get("seed_id_count") != report.get("public_items", 0) + report.get("not_public_or_not_accessible", 0):
        raise ValueError("Autobid seed/public accounting does not balance")
    if report.get("normalized_rows") != len(payload["rows"]):
        raise ValueError("Autobid normalized row accounting does not balance")
    ids: set[str] = set()
    urls: set[str] = set()
    forbidden = {"current", "current_bid", "current_bid_eur", "bids", "minimum_next_bid"}
    classifications = Counter()
    for row in payload["rows"]:
        if not isinstance(row, dict) or forbidden.intersection(row):
            raise ValueError("Autobid row contains a forbidden confidential-bid field")
        row_id, url = clean(row.get("id")), clean(row.get("url"))
        if (
            not row_id.startswith(f"{SOURCE_KEY}:")
            or not url.startswith("https://autobid.de/en/item/")
            or row.get("source_key") != SOURCE_KEY
            or row.get("adapter_authorized") is not True
            or row_id in ids
            or url in urls
        ):
            raise ValueError("Autobid row identity is invalid or duplicated")
        ids.add(row_id)
        urls.add(url)
        if row.get("bid_visibility") != "prevailing_bid_confidential":
            raise ValueError("Autobid bid visibility is not confidential")
        price_kind = row.get("price_kind")
        if price_kind not in {"starting_bid", "unknown"}:
            raise ValueError("Autobid price semantics are invalid")
        if price_kind == "starting_bid" and _positive_number(row.get("price_eur")) is None:
            raise ValueError("Autobid starting bid is not positive")
        provisional = row.get("provisional_import_classification")
        if provisional not in {"preliminarily_eligible", "review_required", "not_eligible"}:
            raise ValueError("Autobid provisional classification is invalid")
        expected_status = "not_eligible" if provisional == "not_eligible" else "review_required"
        if row.get("eligibility_status") != expected_status:
            raise ValueError("Autobid eligibility status conflicts with its classification")
        classifications[provisional] += 1
    if dict(sorted(classifications.items())) != report.get("eligibility_counts"):
        raise ValueError("Autobid classification accounting does not balance")


def build_watch(
    session: requests.Session | None = None,
    *,
    now: dt.datetime | None = None,
    timeout: int = 30,
    page_size: int = PAGE_SIZE,
    max_ids: int = MAX_CATALOGUE_IDS,
    request_delay: float = 0.1,
    raw_root: Path | None = None,
) -> dict[str, Any]:
    session = session or requests.Session()
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    capture = RawEvidenceCapture(raw_root, SOURCE_KEY, now) if raw_root else None
    items, fetch_report = fetch_catalogue(
        session,
        timeout=timeout,
        page_size=page_size,
        max_ids=max_ids,
        request_delay=request_delay,
        capture=capture,
    )
    rows: list[dict[str, Any]] = []
    rejected_identity = 0
    for item in items:
        row = item_to_row(
            item,
            now=now,
            raw_evidence_ref=capture.reference if capture else "",
        )
        if row is None:
            rejected_identity += 1
        else:
            rows.append(row)
    rows.sort(key=lambda row: (row.get("sale_event_utc") or "9999", row["id"]))
    eligibility = Counter(row["provisional_import_classification"] for row in rows)
    fuel_counts = Counter(row["fuel"] for row in rows)
    age_counts = Counter(row["age_classification"] for row in rows)
    source_report = {
        "status": "ok",
        "connector_status": "ok",
        "publication_ready": False,
        "publication_gate": "pending written reuse authorization or counsel-approved basis",
        "catalogue_scope": "anonymous PUBLISHED listings in BEFORE_AUCTION or IN_AUCTION stage",
        **fetch_report,
        "normalized_rows": len(rows),
        "vehicle_rows": len(rows),
        "rejected_bad_identity": rejected_identity,
        "eligibility_counts": dict(sorted(eligibility.items())),
        "fuel_counts": dict(sorted(fuel_counts.items())),
        "age_counts": dict(sorted(age_counts.items())),
        "price_semantics": "public starting price only; prevailing/current bid is confidential and not requested",
        "current_bid_requested": False,
        "current_bid_retained": False,
        "access": "verified motor dealers only",
        "raw_capture_id": capture.capture_id if capture else None,
    }
    payload = {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": now.isoformat(),
        "row_count": len(rows),
        "rows": rows,
        "source_reports": {SOURCE_KEY: source_report},
        "source_key": SOURCE_KEY,
        "source_url": SEARCH_URL,
        "research_only": True,
        "price_semantics": source_report["price_semantics"],
    }
    validate_payload(payload)
    if capture:
        capture.finish(source_report)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture every anonymously public Autobid catalogue result for restricted research"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    parser.add_argument("--max-ids", type=int, default=MAX_CATALOGUE_IDS)
    parser.add_argument("--request-delay", type=float, default=0.1)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=Path("/var/lib/sonardeals-radar/raw-auctions"),
    )
    args = parser.parse_args()
    payload = build_watch(
        timeout=args.timeout,
        page_size=args.page_size,
        max_ids=args.max_ids,
        request_delay=args.request_delay,
        raw_root=args.raw_root,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(args.out)
    print(json.dumps({
        "source": SOURCE_KEY,
        "research_rows": payload["row_count"],
        "classification": payload["source_reports"][SOURCE_KEY]["eligibility_counts"],
        "output": str(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
