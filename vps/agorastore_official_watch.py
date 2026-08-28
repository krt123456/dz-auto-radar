#!/usr/bin/env python3
"""Reconcile Agorastore's public passenger-car catalogue through its public API."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "agorastore"
SOURCE_NAME = "Agorastore"
SOURCE_URL = "https://www.agorastore.fr/fr/ventes-occasions/equipement/vehicules-et-transport/voiture/mBPf3OT0nWtcEx_rLBmQk"
API_URL = "https://api.auctelia.com/searchable-items/searches"
CAR_CATEGORY_ID = "mBPf3OT0nWtcEx_rLBmQk"
PAGE_SIZE = 100
MAX_ROWS = 50_000
DEFAULT_TIMEOUT = 35
SCHENGEN_COUNTRIES = frozenset({
    "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IS", "IT", "LI", "LT", "LU", "LV",
    "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
})
HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
REGISTRATION_DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](19\d{2}|20\d{2})\b")
MILEAGE_RE = re.compile(r"\b([0-9][0-9 .\u00a0]{2,})\s*(?:km|kms|kilom(?:e|??)tres?)\b", re.I)
NON_PASSENGER_TYPE_RE = re.compile(
    r"\b(?:pick[ -]?up|fourgon(?:nette)?|camionnette|minibus|autocar|autobus|"
    r"camion|truck|tracteur|tractor)\b",
    re.I,
)
NON_PASSENGER_TITLE_RE = re.compile(
    r"\b(?:pick[ -]?up|fourgon(?:nette)?|camionnette|minibus|autocar|autobus|"
    r"camion|truck|tracteur|tractor|utilitaire|v[?e]hicule(?:s)?\s+commercial(?:e)?|"
    r"v[?e]hicule\s+de\s+services|soci[?e]t[?e]|affaire)\b",
    re.I,
)


class AgorastoreWatchError(RuntimeError):
    """The public Agorastore passenger-car catalogue could not be reconciled."""


@dataclass(frozen=True)
class ApiPage:
    total: int
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Catalogue:
    declared_total: int
    pages: int
    raw_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    excluded_non_passenger_ids: tuple[str, ...]

    @property
    def fingerprint(self) -> tuple[int, tuple[str, ...]]:
        return self.declared_total, tuple(sorted(self.raw_ids))


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def folded(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", clean(value))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def positive_money_from_cents(value: Any) -> int | float | None:
    cents = nonnegative_integer(value)
    if cents is None or cents <= 0:
        return None
    amount = cents / 100
    return int(amount) if amount.is_integer() else amount


def text_without_markup(value: Any) -> str:
    value = html.unescape(str(value or ""))
    return clean(re.sub(r"<[^>]+>", " ", value))


def field_value(description: Any, *labels: str) -> str:
    markup = str(description or "")
    for label in labels:
        pattern = re.compile(
            rf"<h[1-6][^>]*>\s*{re.escape(label)}\s*</h[1-6]>\s*<p[^>]*>(.*?)</p>",
            re.I | re.S,
        )
        match = pattern.search(markup)
        if match:
            value = text_without_markup(match.group(1))
            if value:
                return value
    return ""


def pick_translation(item: dict[str, Any]) -> dict[str, Any]:
    translations = item.get("translations")
    if not isinstance(translations, list):
        raise AgorastoreWatchError("Agorastore item has no public translations")
    candidates = [translation for translation in translations if isinstance(translation, dict)]
    for language in ("fr", "en", "nl", "de", "pl"):
        for translation in candidates:
            if clean(translation.get("language")).casefold() == language and clean(translation.get("name")):
                return translation
    for translation in candidates:
        if clean(translation.get("name")):
            return translation
    raise AgorastoreWatchError("Agorastore item has no public title")


def item_id(item: dict[str, Any]) -> str:
    value = clean(item.get("itemShortId"))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AgorastoreWatchError("Agorastore item has no stable public ID")
    return value


def item_has_public_car_category(item: dict[str, Any]) -> bool:
    categories = item.get("categories")
    if not isinstance(categories, list):
        return False
    for category in categories:
        if not isinstance(category, dict):
            continue
        if clean(category.get("categoryShortId")) == CAR_CATEGORY_ID and clean(category.get("code")).casefold() == "cars":
            return True
    return False


def parse_end(value: Any) -> dt.datetime:
    raw = clean(value)
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AgorastoreWatchError(f"Agorastore item has invalid auction end: {value!r}") from exc
    if parsed.tzinfo is None:
        raise AgorastoreWatchError("Agorastore item has a timezone-naive auction end")
    return parsed.astimezone(UTC)


def parse_registration_date(value: str) -> tuple[int | None, str]:
    match = REGISTRATION_DATE_RE.search(value)
    if match is None:
        year_match = YEAR_RE.search(value)
        return (int(year_match.group(1)), "") if year_match else (None, "")
    day, month, year = (int(match.group(index)) for index in range(1, 4))
    try:
        parsed = dt.date(year, month, day)
    except ValueError:
        return year, ""
    return parsed.year, parsed.isoformat()


def parse_mileage(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        return None
    number = int(digits)
    return number if 0 <= number <= 2_000_000 else None


def normalize_fuel(value: str) -> str:
    text = folded(value)
    petrol = bool(re.search(r"\b(?:essence|petrol|gasoline|benzine)\b", text))
    diesel = bool(re.search(r"\b(?:gazole|diesel)\b", text))
    hybrid = bool(re.search(r"\b(?:hybrid|hybride|phev|hev|plug[ -]?in)\b", text))
    electric = bool(re.search(r"\b(?:electric|electrique|ev)\b", text))
    if hybrid:
        if diesel:
            return "diesel/electric hybrid"
        if petrol:
            return "petrol/electric hybrid"
        return "hybrid"
    if diesel:
        return "diesel"
    if petrol:
        return "petrol"
    if electric:
        return "electric"
    if re.search(r"\b(?:gpl|lpg|gnv|cng)\b", text):
        return "gas"
    return "unknown"


def is_explicit_non_passenger(title: str, vehicle_type: str) -> bool:
    return bool(NON_PASSENGER_TYPE_RE.search(vehicle_type) or NON_PASSENGER_TITLE_RE.search(title))


def row_from_item(item: dict[str, Any], *, observed_at: str, now: dt.datetime) -> dict[str, Any] | None:
    native_id = item_id(item)
    if not item_has_public_car_category(item):
        raise AgorastoreWatchError(f"Agorastore item {native_id} is outside the public cars category")
    translation = pick_translation(item)
    title = clean(translation.get("name"))
    description = str(translation.get("description") or "")
    vehicle_type = field_value(description, "Type de v?hicule", "Type de vehicule", "Vehicle Type")
    if is_explicit_non_passenger(title, vehicle_type):
        return None

    end = parse_end(item.get("auctionEndDate"))
    if end <= now:
        raise AgorastoreWatchError(f"Agorastore public active item {native_id} is already ended")
    indexed_meta = item.get("indexedMeta") if isinstance(item.get("indexedMeta"), dict) else {}
    info = indexed_meta.get("info") if isinstance(indexed_meta.get("info"), dict) else {}
    country = clean(info.get("country")).upper()
    if country not in SCHENGEN_COUNTRIES:
        raise AgorastoreWatchError(
            f"Agorastore cars-category item {native_id} has non-Schengen country {country!r}"
        )
    sale_information = indexed_meta.get("saleInformation") if isinstance(indexed_meta.get("saleInformation"), dict) else {}
    prices = sale_information.get("pricesCents") if isinstance(sale_information.get("pricesCents"), dict) else {}
    bid_count = nonnegative_integer(sale_information.get("numberOfBids"))
    current_price = positive_money_from_cents(prices.get("current"))
    start_price = positive_money_from_cents(prices.get("start"))
    if current_price is not None and (bid_count or 0) > 0:
        price, price_kind, price_label = current_price, "current_bid", "Public current bid"
    elif start_price is not None:
        price, price_kind, price_label = start_price, "starting_bid", "Public starting bid"
    elif current_price is not None:
        price, price_kind, price_label = current_price, "starting_bid", "Public displayed starting price"
    else:
        price, price_kind, price_label = None, "unknown", "Price is not shown in the public catalogue"

    registration_text = field_value(
        description,
        "Date de mise en circulation",
        "Date d'immatriculation",
        "Date of registration",
        "Registration date",
    )
    year, registration_date = parse_registration_date(registration_text)
    if year is None:
        year_text = field_value(description, "Ann?e", "Annee", "Year")
        year, _ = parse_registration_date(year_text or title)
    mileage_text = field_value(description, "Kilom?trage", "Kilometrage", "Mileage", "Odometer")
    if mileage_text:
        mileage = parse_mileage(mileage_text)
    else:
        mileage_match = MILEAGE_RE.search(text_without_markup(description)) or MILEAGE_RE.search(title)
        mileage = parse_mileage(mileage_match.group(1)) if mileage_match else None
    fuel_text = field_value(description, "?nergie", "Energie", "Energy", "Fuel")
    seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
    localities = item.get("localities") if isinstance(item.get("localities"), list) else []
    has_reserve = sale_information.get("hasReservePrice")
    reserve_met = sale_information.get("reservePriceReached")
    return {
        "id": f"{SOURCE_KEY}:{native_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": f"https://www.agorastore.fr/fr/ventes-occasions/voiture/{native_id}",
        "title": title,
        "model": title,
        "country": country,
        "asset_country": country,
        "category": "car",
        "category_raw": "Agorastore public cars category",
        "year": year,
        "registration_date": registration_date,
        "mileage": mileage,
        "mileage_km": mileage,
        "fuel": normalize_fuel(fuel_text or f"{title} {text_without_markup(description)}"),
        "seller": clean(seller.get("organisationName")) or SOURCE_NAME,
        "location": ", ".join(clean(locality) for locality in localities if clean(locality)),
        "image_url": None,
        "price_amount": price,
        "price_currency": clean(prices.get("currency")).upper() or "EUR",
        "price_eur": price,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "Public Agorastore cars-category catalogue",
        "bid_count": bid_count,
        "reserve_met": reserve_met if isinstance(reserve_met, bool) else None,
        "no_reserve": (not has_reserve) if isinstance(has_reserve, bool) else None,
        "sale_terms": "Official Agorastore public car auction; inspect the source item for fees, condition, pickup, and buyer terms.",
        "auction_status": clean(item.get("status")).casefold() or "active",
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": "Official public Agorastore car listing; confirm condition, fees, buyer requirements, and import eligibility before bidding.",
        "access_sale_note": "Open the official Agorastore listing to inspect bidding, pickup, and export terms.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-cars-category:{native_id}",
        "evidence": "Official Agorastore public cars-category API result.",
    }


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.45,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))
    session.headers.update(HEADERS)
    return session


def fetch_page(session: requests.Session, *, offset: int, page_size: int, timeout: int) -> ApiPage:
    body = {
        "from": offset,
        "size": page_size,
        "terms_filters": {
            "categories": [CAR_CATEGORY_ID],
            "statuses": ["OPEN", "VALIDATED"],
            "visible": True,
            "privateSale": False,
            "unpublished": False,
        },
        "sort": [{"auctionEndDate": "asc"}, {"referenceNumber": "asc"}],
    }
    response = session.post(API_URL, json=body, timeout=timeout)
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise AgorastoreWatchError("Agorastore cars API returned a non-object response")
    total = nonnegative_integer(payload.get("total"))
    items = payload.get("results")
    count = nonnegative_integer(payload.get("count"))
    if total is None or not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise AgorastoreWatchError("Agorastore cars API response has no valid total/results")
    if count is not None and count != len(items):
        raise AgorastoreWatchError("Agorastore cars API count does not match returned items")
    return ApiPage(total=total, items=tuple(items))


def enumerate_catalogue(
    session: requests.Session, *, observed_at: str, now: dt.datetime, timeout: int, page_size: int = PAGE_SIZE
) -> Catalogue:
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    first = fetch_page(session, offset=0, page_size=page_size, timeout=timeout)
    if first.total > MAX_ROWS:
        raise AgorastoreWatchError("Agorastore cars catalogue exceeds the configured safety limit")
    pages = math.ceil(first.total / page_size) if first.total else 0
    raw_items = list(first.items)
    if len(first.items) != min(first.total, page_size):
        raise AgorastoreWatchError("Agorastore cars first-page cardinality is invalid")
    for page in range(2, pages + 1):
        current = fetch_page(session, offset=(page - 1) * page_size, page_size=page_size, timeout=timeout)
        if current.total != first.total:
            raise AgorastoreWatchError("Agorastore cars catalogue changed during pagination")
        expected = min(page_size, first.total - len(raw_items))
        if len(current.items) != expected:
            raise AgorastoreWatchError(
                f"Agorastore cars page {page} returned {len(current.items)} items, expected {expected}"
            )
        raw_items.extend(current.items)
    if len(raw_items) != first.total:
        raise AgorastoreWatchError("Agorastore cars API total does not reconcile to all pages")

    raw_ids: list[str] = []
    rows: list[dict[str, Any]] = []
    excluded: list[str] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for item in raw_items:
        native_id = item_id(item)
        if native_id in seen_ids:
            raise AgorastoreWatchError("Agorastore cars pagination repeats an item ID")
        seen_ids.add(native_id)
        raw_ids.append(native_id)
        row = row_from_item(item, observed_at=observed_at, now=now)
        if row is None:
            excluded.append(native_id)
            continue
        if row["url"] in seen_urls:
            raise AgorastoreWatchError("Agorastore cars pagination repeats a canonical item URL")
        seen_urls.add(row["url"])
        rows.append(row)
    return Catalogue(
        declared_total=first.total,
        pages=pages,
        raw_ids=tuple(raw_ids),
        rows=tuple(rows),
        excluded_non_passenger_ids=tuple(excluded),
    )


def build_watch(
    *, session: requests.Session | None = None, now: dt.datetime | None = None, timeout: int = DEFAULT_TIMEOUT,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        first = enumerate_catalogue(
            active_session, observed_at=observed_at, now=current, timeout=timeout, page_size=page_size
        )
        second = enumerate_catalogue(
            active_session, observed_at=observed_at, now=current, timeout=timeout, page_size=page_size
        )
    finally:
        if supplied_session is None:
            active_session.close()
    if first.fingerprint != second.fingerprint:
        raise AgorastoreWatchError("Agorastore cars catalogue changed during final reconciliation")
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every current public Agorastore cars-category item, excluding explicit non-passenger vehicles",
        "car_category_id": CAR_CATEGORY_ID,
        "declared": first.declared_total,
        "visited": len(first.raw_ids),
        "passenger_cars": len(first.rows),
        "excluded_explicit_non_passenger": len(first.excluded_non_passenger_ids),
        "pages": first.pages,
        "full_catalogue_rechecked": True,
        "stable_ids_unique": True,
        "publication_ready": False,
    }
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": observed_at,
        "research_only": True,
        "publication_status": "review_required",
        "row_count": len(first.rows),
        "rows": list(first.rows),
        "source_reports": {SOURCE_KEY: report},
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch every public active Agorastore passenger-car listing")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "AGORASTORE_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": report["declared"],
        "excluded": report["excluded_explicit_non_passenger"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
