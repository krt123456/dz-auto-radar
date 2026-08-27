#!/usr/bin/env python3
"""Broad Copart watch for the official Schengen country sites.

Copart currently operates first-party country platforms in Germany, Spain and
Finland.  The public vehicle-finder API is server-side only (the sites block a
GitHub Pages browser origin), so this connector is intended for the hourly VPS
refresh.  It keeps vehicle condition and bidder restrictions explicit:

* German and Spanish lots are never promoted as eligible because bidding is
  aimed at verified automotive businesses and much of the stock is salvage.
* Finnish lots may be bought by identified individuals, but remain review
  required unless the lot-specific condition, documents and export path are
  checked.  Dismantler, old, diesel and materially damaged lots are marked not
  eligible.
* Estimated retail value is never presented as the auction price.  A price is
  a current bid only when Copart's dynamic public bid field is positive; a
  public Buy-It-Now amount is labelled as a guide price.

The API exposes an auction event time, not a per-lot guaranteed closing time.
It is therefore retained as ``sale_event_utc`` and is deliberately not copied
to ``canonical_end_utc``.

Catalogue completeness is fail-closed.  The first ``totalElements`` value is
frozen, every page must have its exact expected cardinality, lot IDs must be
valid and unique, and a second complete pass must reproduce the same lot-ID
fingerprint.  Any upward or downward total drift rejects that source snapshot.
Publication remains pending even after the technical enumeration passes.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
LANE = "official_auction_watch"
SCHEMA_VERSION = 1
SEARCH_PATH = "/public/lots/vehicle-finder-search-results"
DEFAULT_MAX_FALLBACK_AGE = dt.timedelta(hours=8)
MAX_CATALOGUE_ROWS = 12_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


@dataclasses.dataclass(frozen=True)
class CopartSource:
    key: str
    name: str
    country: str
    base_url: str
    vehicle_queries: tuple[str, ...]
    access_note: str


SOURCES: tuple[CopartSource, ...] = (
    CopartSource(
        "copart-de",
        "Copart Germany",
        "DE",
        "https://www.copart.de",
        ("vehicle_type_code:V", "vehicle_type_code:K"),
        "Copart Germany requires verified trade registration and paid membership; export and lot documents must be checked.",
    ),
    CopartSource(
        "copart-es",
        "Copart Spain",
        "ES",
        "https://www.copart.es",
        ("vehicle_type_code:V", "vehicle_type_code:K"),
        "Copart Spain is intended for verified automotive professionals; fees, condition, documents and export must be checked.",
    ),
    CopartSource(
        "copart-fi",
        "Copart Finland",
        "FI",
        "https://www.copart.fi",
        ("vehicle_type_code:V", "vehicle_type_code:VN", "vehicle_type_code:K"),
        "Copart Finland accepts identified individuals for many lots, but dismantler lots and export/document requirements remain lot-specific.",
    ),
)

SOURCE_BY_KEY = {source.key: source for source in SOURCES}

FATAL_DAMAGE_RE = re.compile(
    r"\b(?:all over|biohazard|burn|engine breakdown|frame damage|flood|"
    r"hail|mechanical|missing/altered vin|partial/incomplete repair|"
    r"rollover|structural|total loss|to be dismantled|scrap|parts only|"
    r"front end|rear end|side|animal|corrosion|hull|"
    r"siniestro|desguace|purettavaksi|purku)\b",
    re.I,
)
BENIGN_DAMAGE_RE = re.compile(
    r"\b(?:normal wear|minor dents(?:/| and )scratches|used vehicle)\b",
    re.I,
)
DIESEL_RE = re.compile(r"\b(?:diesel|hybrid diesel)\b", re.I)
ACCEPTED_FUEL_RE = re.compile(
    r"\b(?:petrol|gasoline|electric|hybrid gasoline|hybrid engine)\b", re.I
)


def clean(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\ufffd", " ")
    return " ".join(text.split())


def number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def integer(value: Any) -> int | None:
    result = number(value)
    return int(round(result)) if result is not None else None


def parse_iso(value: Any) -> dt.datetime | None:
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


def epoch_millis(value: Any) -> str | None:
    amount = number(value)
    if amount is None:
        return None
    try:
        parsed = dt.datetime.fromtimestamp(amount / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return parsed.isoformat()


def normalize_fuel(value: Any) -> str:
    raw = clean(value).casefold()
    if "hybrid diesel" in raw:
        return "diesel/electric hybrid"
    if "hybrid gasoline" in raw or "hybrid petrol" in raw:
        return "petrol/electric hybrid"
    if "hybrid" in raw:
        return "hybrid"
    if "electric" in raw:
        return "electric"
    if "diesel" in raw:
        return "diesel"
    if any(token in raw for token in ("petrol", "gasoline", "benzin")):
        return "petrol"
    if "gas" in raw or "lpg" in raw:
        return "gas"
    return raw


def build_payload(source: CopartSource, page: int, page_size: int) -> dict[str, Any]:
    return {
        "query": ["*"],
        "filter": {"VEHT": list(source.vehicle_queries)},
        "sort": [
            "lot_status_code asc",
            "auction_date_utc asc",
            "auction_assignment_number asc",
        ],
        "page": page,
        "size": page_size,
        "start": page * page_size,
        "watchListOnly": False,
        "freeFormSearch": False,
        "hideImages": False,
        "defaultSort": False,
        "specificRowProvided": False,
        "displayName": "",
        "searchName": "",
        "backUrl": "",
        "includeTagByField": {"VEHT": "{!tag=VEHT}"},
        "rawParams": {},
    }


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    return session


def request_page(
    session: Any,
    source: CopartSource,
    *,
    page: int,
    page_size: int,
    timeout: float,
) -> tuple[int, list[dict[str, Any]]]:
    url = source.base_url + SEARCH_PATH
    response = session.post(
        url,
        json=build_payload(source, page, page_size),
        headers={**HEADERS, "Referer": source.base_url + "/vehicleFinder"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("returnCode") != 1:
        raise RuntimeError(
            f"Copart API error for {source.key}: {clean(payload.get('returnCodeDesc'))}"
        )
    results = ((payload.get("data") or {}).get("results") or {})
    total = results.get("totalElements")
    content = results.get("content")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise RuntimeError(f"Copart totalElements drift for {source.key}")
    if total > MAX_CATALOGUE_ROWS:
        raise RuntimeError(f"Copart catalogue guard exceeded for {source.key}: {total}")
    if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
        raise RuntimeError(f"Copart content schema drift for {source.key}")
    return total, content


def _lot_id(item: dict[str, Any], source: CopartSource, *, page: int) -> str:
    lot = clean(item.get("lotNumberStr") or item.get("ln"))
    if not re.fullmatch(r"\d{6,12}", lot):
        raise RuntimeError(
            f"Copart invalid lot ID for {source.key} on page {page}: {lot!r}"
        )
    return lot


def _lot_id_fingerprint(lot_ids: Iterable[str]) -> str:
    canonical = "\n".join(sorted(lot_ids)).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _page_fingerprint(lot_ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(lot_ids).encode("ascii")).hexdigest()


def _catalogue_pass(
    session: Any,
    source: CopartSource,
    *,
    timeout: float,
    page_size: int,
    max_pages: int,
    expected_total: int | None,
    pass_name: str,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """Run one complete, cardinality-checked pass over a frozen catalogue."""

    total, first_items = request_page(
        session, source, page=0, page_size=page_size, timeout=timeout
    )
    # ``latest_total`` is intentionally assigned only once.  The first response
    # freezes the invariant; later values are compared and never adopted.
    latest_total = total
    frozen_total = latest_total if expected_total is None else expected_total
    if total != frozen_total:
        direction = "increased" if total > frozen_total else "decreased"
        raise RuntimeError(
            f"Copart totalElements {direction} during {pass_name} for "
            f"{source.key}: frozen={frozen_total} observed={total} page=0"
        )
    expected_pages = max(1, math.ceil(frozen_total / page_size))
    if expected_pages > max_pages:
        raise RuntimeError(
            f"Copart catalogue {frozen_total} exceeds configured page capacity "
            f"for {source.key}: expected_pages={expected_pages} max_pages={max_pages}"
        )

    unique: dict[str, dict[str, Any]] = {}
    page_row_counts: list[int] = []
    page_fingerprints: list[str] = []
    for page in range(expected_pages):
        if page == 0:
            page_total, items = total, first_items
        else:
            page_total, items = request_page(
                session, source, page=page, page_size=page_size, timeout=timeout
            )
        if page_total != frozen_total:
            direction = "increased" if page_total > frozen_total else "decreased"
            raise RuntimeError(
                f"Copart totalElements {direction} during {pass_name} for "
                f"{source.key}: frozen={frozen_total} observed={page_total} page={page}"
            )
        expected_rows = (
            page_size
            if page < expected_pages - 1
            else frozen_total - page * page_size
        )
        if len(items) != expected_rows:
            raise RuntimeError(
                f"Copart page cardinality drift during {pass_name} for "
                f"{source.key}: page={page} expected={expected_rows} "
                f"observed={len(items)}"
            )
        page_lot_ids: list[str] = []
        for item in items:
            lot = _lot_id(item, source, page=page)
            if lot in unique:
                raise RuntimeError(
                    f"Copart duplicate lot ID during {pass_name} for "
                    f"{source.key}: lot={lot} page={page}"
                )
            unique[lot] = item
            page_lot_ids.append(lot)
        page_row_counts.append(len(items))
        page_fingerprints.append(_page_fingerprint(page_lot_ids))
        if page + 1 < expected_pages:
            time.sleep(0.05)
    if len(unique) != frozen_total:
        raise RuntimeError(
            f"Copart unique lot accounting failed during {pass_name} for "
            f"{source.key}: frozen={frozen_total} unique={len(unique)}"
        )
    return list(unique.values()), frozen_total, {
        "pass": pass_name,
        "pages_visited": expected_pages,
        "page_row_counts": page_row_counts,
        "page_lot_id_fingerprints": page_fingerprints,
        "unique_lot_ids": len(unique),
        "lot_ids_sha256": _lot_id_fingerprint(unique),
    }


def crawl_source_with_evidence(
    source: CopartSource,
    *,
    timeout: float,
    page_size: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    if page_size < 1 or page_size > 100:
        raise ValueError("Copart public API page size must be between 1 and 100")
    session = configured_session()
    first_items, frozen_total, first_evidence = _catalogue_pass(
        session,
        source,
        timeout=timeout,
        page_size=page_size,
        max_pages=max_pages,
        expected_total=None,
        pass_name="enumeration",
    )
    verification_items, verified_total, verification_evidence = _catalogue_pass(
        session,
        source,
        timeout=timeout,
        page_size=page_size,
        max_pages=max_pages,
        expected_total=frozen_total,
        pass_name="verification",
    )
    if verified_total != frozen_total:
        raise RuntimeError(
            f"Copart frozen total verification failed for {source.key}: "
            f"enumeration={frozen_total} verification={verified_total}"
        )
    if first_evidence["lot_ids_sha256"] != verification_evidence["lot_ids_sha256"]:
        raise RuntimeError(
            f"Copart lot ID fingerprint drift for {source.key}: "
            f"enumeration={first_evidence['lot_ids_sha256']} "
            f"verification={verification_evidence['lot_ids_sha256']}"
        )
    if len(first_items) != len(verification_items) or len(verification_items) != frozen_total:
        raise RuntimeError(f"Copart two-pass lot accounting failed for {source.key}")
    return verification_items, frozen_total, {
        "total_elements_frozen": frozen_total,
        "page_size": page_size,
        "expected_pages": max(1, math.ceil(frozen_total / page_size)),
        "enumeration": first_evidence,
        "verification": verification_evidence,
        "verification_passed": True,
        "lot_ids_sha256": verification_evidence["lot_ids_sha256"],
    }


def crawl_source(
    source: CopartSource,
    *,
    timeout: float,
    page_size: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int]:
    """Backward-compatible wrapper around the two-pass invariant crawl."""

    items, total, _ = crawl_source_with_evidence(
        source, timeout=timeout, page_size=page_size, max_pages=max_pages
    )
    return items, total


def public_price(item: dict[str, Any]) -> tuple[float | None, str, str, str]:
    dynamic = item.get("dynamicLotDetails") or {}
    bid = next(
        (
            candidate
            for candidate in (
                number(dynamic.get("finalCurrentBidPrice")),
                number(dynamic.get("currentBid")),
                number(item.get("fcb")),
                number(item.get("currb")),
                number(item.get("hb")),
            )
            if candidate is not None
        ),
        None,
    )
    if bid is not None:
        return bid, "current_bid", "current bid", "public"
    buy_now = next(
        (
            candidate
            for candidate in (
                number(dynamic.get("finalBuyItNowPrice")),
                number(dynamic.get("buyTodayBid")),
                number(item.get("fbp")),
                number(item.get("bnp")),
            )
            if candidate is not None
        ),
        None,
    )
    if buy_now is not None:
        return buy_now, "guide_price", "Buy It Now", "public"
    return None, "unknown", "price not publicly visible", "hidden"


def condition_text(item: dict[str, Any]) -> str:
    return " | ".join(
        bit
        for bit in (
            clean(item.get("dd")),
            clean(item.get("sdd")),
            clean(item.get("st")),
            clean(item.get("sttd")),
            clean(item.get("crd")),
        )
        if bit
    )


def classify_eligibility(
    source: CopartSource,
    item: dict[str, Any],
    *,
    now: dt.datetime,
) -> tuple[str, str]:
    condition = condition_text(item)
    fuel_text = clean(item.get("ft") or item.get("egn_typ") or item.get("egn"))
    year = integer(item.get("lcy"))
    dismantler = bool(item.get("df")) or "dismantl" in condition.casefold()

    if source.country in {"DE", "ES"}:
        return (
            "not_eligible",
            "Professional Copart membership is required and the lot may be damaged or salvage; it is not confirmed eligible for an Algerian private buyer.",
        )
    if dismantler or FATAL_DAMAGE_RE.search(condition):
        return "not_eligible", "The lot is dismantler/salvage or carries a material damage marker."
    if DIESEL_RE.search(fuel_text):
        return "not_eligible", "Diesel or diesel-hybrid fuel is outside the requested import filter."
    if year is None or year < now.year - 3:
        return "not_eligible", "The model year is outside the three-year candidate window."
    if not ACCEPTED_FUEL_RE.search(fuel_text):
        return "not_eligible", "Fuel type is not positively confirmed as petrol, electric or petrol-hybrid."
    if condition and not BENIGN_DAMAGE_RE.search(condition):
        return "not_eligible", "The public condition is not positively limited to ordinary wear."
    return (
        "review_required",
        "Finnish individual bidding is possible after identification, but exact first registration, documents, condition and export must be verified for this lot.",
    )


def item_to_row(
    source: CopartSource,
    item: dict[str, Any],
    *,
    now: dt.datetime,
) -> dict[str, Any] | None:
    dynamic = item.get("dynamicLotDetails") or {}
    if dynamic.get("lotSold") is True:
        return None
    lot = clean(item.get("lotNumberStr") or item.get("ln"))
    if not re.fullmatch(r"\d{6,12}", lot):
        return None
    make = clean(item.get("mkn"))
    model = clean(item.get("lm"))
    title = clean(item.get("ld")) or " ".join(bit for bit in (make, model) if bit)
    if not title:
        return None
    condition = condition_text(item)
    documents = clean(item.get("sttd"))
    location = clean(item.get("yn"))
    sale_name = clean(item.get("syn"))
    hints = [bit for bit in (condition, documents, location, sale_name) if bit]
    if hints:
        title = title + " | " + " | ".join(dict.fromkeys(hints))
    price, price_kind, price_label, visibility = public_price(item)
    status, reason = classify_eligibility(source, item, now=now)
    observed = now.astimezone(UTC).isoformat()
    event = epoch_millis(item.get("adu") or item.get("sdu") or item.get("ad"))
    sale_status = clean(
        dynamic.get("saleStatus")
        or dynamic.get("saleStatusCode")
        or item.get("lss")
    )
    no_reserve = sale_status.upper() in {"PURE_SALE", "NO_RESERVE", "NO RESERVE"}
    reserve_met = dynamic.get("sellerReserveMet")
    fuel = normalize_fuel(item.get("ft") or item.get("egn_typ") or item.get("egn"))
    currency = clean(item.get("cuc") or "EUR").upper()
    price_eur = price if currency == "EUR" else None
    return {
        "id": f"{source.key}-{lot}",
        "source": source.key,
        "source_key": source.key,
        "url": f"{source.base_url}/lot/{lot}",
        "title": title,
        "model": " ".join(bit for bit in (make, model) if bit),
        "country": source.country,
        "year": integer(item.get("lcy")),
        "mileage": integer(item.get("orr")),
        "fuel": fuel,
        "seller": " · ".join(bit for bit in (source.name, location) if bit),
        "price_eur": price_eur,
        "price_kind": price_kind,
        "price_currency": currency,
        "price_amount": price,
        "price_label": price_label,
        "bid_visibility": visibility,
        "registration_date": "",
        "canonical_end_utc": None,
        "sale_end_utc": None,
        "sale_event_utc": event,
        # ``adu`` is the official scheduled auction session time.  The public
        # site counts down to it (for example 6D 10H 35min), but it is not a
        # per-lot guaranteed closing timestamp, so it remains an event rather
        # than being mislabeled as ``canonical_end_utc``.
        "sale_terms": "No Reserve" if no_reserve else sale_status,
        "no_reserve": no_reserve,
        "reserve_met": reserve_met if isinstance(reserve_met, bool) else None,
        "auction_status": clean(dynamic.get("lotAuctionStatus") or item.get("las") or item.get("lotStatusCode")),
        "damage": condition,
        "documents": documents,
        "first_seen_at": observed,
        "last_seen_at": observed,
        "eligibility_status": status,
        "eligibility_reason": reason,
        "access_sale_note": source.access_note,
    }


def fetch_source(
    source: CopartSource,
    *,
    now: dt.datetime,
    timeout: float,
    page_size: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items, catalogue_total, crawl_evidence = crawl_source_with_evidence(
        source, timeout=timeout, page_size=page_size, max_pages=max_pages
    )
    unique: dict[str, dict[str, Any]] = {}
    excluded = 0
    for item in items:
        row = item_to_row(source, item, now=now)
        if row is None:
            excluded += 1
            continue
        unique[row["id"]] = row
    rows = sorted(unique.values(), key=lambda row: row["id"])
    report = {
        "source_key": source.key,
        "status": "ok",
        "observed_at_utc": now.astimezone(UTC).isoformat(),
        "catalogue_total": catalogue_total,
        "catalogue_total_frozen": crawl_evidence["total_elements_frozen"],
        "fetched_unique": len(items),
        "enumeration_verified": crawl_evidence["verification_passed"],
        "pagination_evidence": crawl_evidence,
        "accepted_vehicle_rows": len(rows),
        "excluded_invalid_or_sold": excluded,
        "current_bid_rows": sum(row["price_kind"] == "current_bid" for row in rows),
        "guide_price_rows": sum(row["price_kind"] == "guide_price" for row in rows),
        "unknown_price_rows": sum(row["price_kind"] == "unknown" for row in rows),
        "review_required_rows": sum(row["eligibility_status"] == "review_required" for row in rows),
        "not_eligible_rows": sum(row["eligibility_status"] == "not_eligible" for row in rows),
        "publication_status": "pending",
        "publication_ready": False,
    }
    return rows, report


def valid_fallback_rows(
    output: Path,
    source_key: str,
    *,
    now: dt.datetime,
    max_age: dt.timedelta = DEFAULT_MAX_FALLBACK_AGE,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("lane") != LANE:
        return []
    generated = parse_iso(payload.get("generated_at_utc"))
    if generated is None or generated > now + dt.timedelta(minutes=5) or now - generated > max_age:
        return []
    result: list[dict[str, Any]] = []
    for row in payload.get("rows") or []:
        if not isinstance(row, dict) or row.get("source_key") != source_key:
            continue
        last_seen = parse_iso(row.get("last_seen_at"))
        if last_seen is None or last_seen > now + dt.timedelta(minutes=5) or now - last_seen > max_age:
            return []
        result.append(row)
    return result


def build_watch(
    *,
    output: Path,
    now: dt.datetime,
    timeout: float,
    page_size: int,
    max_pages: int,
    workers: int,
) -> dict[str, Any]:
    all_rows: list[dict[str, Any]] = []
    reports: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                fetch_source,
                source,
                now=now,
                timeout=timeout,
                page_size=page_size,
                max_pages=max_pages,
            ): source
            for source in SOURCES
        }
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            try:
                rows, report = future.result()
            except Exception as exc:  # source isolation + bounded LKG
                fallback = valid_fallback_rows(output, source.key, now=now)
                all_rows.extend(fallback)
                reports[source.key] = {
                    "source_key": source.key,
                    "status": "partial" if fallback else "error",
                    "observed_at_utc": now.astimezone(UTC).isoformat(),
                    "accepted_vehicle_rows": len(fallback),
                    "fallback_used": bool(fallback),
                    "enumeration_verified": False,
                    "publication_status": "pending",
                    "publication_ready": False,
                    "connector_error": f"{type(exc).__name__}: {clean(exc)}"[:500],
                }
            else:
                all_rows.extend(rows)
                reports[source.key] = report
    deduped = {row["id"]: row for row in all_rows}
    rows = sorted(deduped.values(), key=lambda row: (row["source_key"], row["id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "lane": LANE,
        "generated_at_utc": now.astimezone(UTC).isoformat(),
        "research_only": True,
        "publication_status": "pending",
        "publication_ready": False,
        "row_count": len(rows),
        "rows": rows,
        "source_reports": reports,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        temporary.write_text(data, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch official Copart Schengen auction inventory")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=120)
    parser.add_argument("--workers", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    now = dt.datetime.now(UTC)
    payload = build_watch(
        output=args.out,
        now=now,
        timeout=args.timeout,
        page_size=args.page_size,
        max_pages=args.max_pages,
        workers=args.workers,
    )
    atomic_write_json(args.out, payload)
    summary = ", ".join(
        f"{key}={report.get('accepted_vehicle_rows', 0)}:{report.get('status')}"
        for key, report in sorted(payload["source_reports"].items())
    )
    print(f"COPART_SCHENGEN_WATCH_PASS rows={payload['row_count']} {summary}", flush=True)


if __name__ == "__main__":
    main()
