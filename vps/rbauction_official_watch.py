#!/usr/bin/env python3
"""Enumerate every public Schengen automobile auction at Ritchie Bros.

The official automobile catalogue is server-rendered and embeds a Next.js
``data.results`` payload.  It declares an exact total and exposes finite
``?from=`` pages of sixty cards.  This connector walks every page twice and
requires the complete public item-ID set to remain stable before writing a
snapshot.  It keeps only Schengen-located lots with an explicit auction
format; fixed-price and make-offer cards are counted but never labelled as an
auction.
"""
from __future__ import annotations

import argparse
import datetime as dt
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
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "rbauction-eu"
SOURCE_NAME = "Ritchie Bros. Europe"
CATALOGUE_URL = "https://www.rbauction.com/cp/automobile"
PAGE_SIZE = 60
# Public catalogue growth must not be silently capped below the scale target.
# This remains an operational guard against a malformed counter, not a 200k
# publication ceiling; the broad-watch architecture will shard before a single
# artifact approaches its host-size limit.
MAX_CATALOGUE_ROWS = 1_000_000
DEFAULT_TIMEOUT = 35
SCHENGEN_COUNTRIES = frozenset({
    "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IS", "IT", "LI", "LT", "LU", "LV",
    "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
})
AUCTION_FORMATS = frozenset({"live auction", "online auction", "sealed bid"})

COUNTRY_CODES = {
    "AT": "AT", "AUT": "AT", "AUSTRIA": "AT",
    "BE": "BE", "BEL": "BE", "BELGIUM": "BE",
    "BG": "BG", "BGR": "BG", "BULGARIA": "BG",
    "CH": "CH", "CHE": "CH", "SWITZERLAND": "CH",
    "CZ": "CZ", "CZE": "CZ", "CZECH REPUBLIC": "CZ",
    "DE": "DE", "DEU": "DE", "GERMANY": "DE",
    "DK": "DK", "DNK": "DK", "DENMARK": "DK",
    "EE": "EE", "EST": "EE", "ESTONIA": "EE",
    "ES": "ES", "ESP": "ES", "SPAIN": "ES",
    "FI": "FI", "FIN": "FI", "FINLAND": "FI",
    "FR": "FR", "FRA": "FR", "FRANCE": "FR",
    "GR": "GR", "GRC": "GR", "GREECE": "GR",
    "HR": "HR", "HRV": "HR", "CROATIA": "HR",
    "HU": "HU", "HUN": "HU", "HUNGARY": "HU",
    "IS": "IS", "ISL": "IS", "ICELAND": "IS",
    "IT": "IT", "ITA": "IT", "ITALY": "IT",
    "LI": "LI", "LIE": "LI", "LIECHTENSTEIN": "LI",
    "LT": "LT", "LTU": "LT", "LITHUANIA": "LT",
    "LU": "LU", "LUX": "LU", "LUXEMBOURG": "LU",
    "LV": "LV", "LVA": "LV", "LATVIA": "LV",
    "MT": "MT", "MLT": "MT", "MALTA": "MT",
    "NL": "NL", "NLD": "NL", "NETHERLANDS": "NL",
    "NO": "NO", "NOR": "NO", "NORWAY": "NO",
    "PL": "PL", "POL": "PL", "POLAND": "PL",
    "PT": "PT", "PRT": "PT", "PORTUGAL": "PT",
    "RO": "RO", "ROU": "RO", "ROMANIA": "RO",
    "SE": "SE", "SWE": "SE", "SWEDEN": "SE",
    "SI": "SI", "SVN": "SI", "SLOVENIA": "SI",
    "SK": "SK", "SVK": "SK", "SLOVAKIA": "SK",
    "US": "US", "USA": "US", "UNITED STATES": "US",
    "CA": "CA", "CAN": "CA", "CANADA": "CA",
    "MX": "MX", "MEX": "MX", "MEXICO": "MX",
    "AU": "AU", "AUS": "AU", "AUSTRALIA": "AU",
    "AE": "AE", "ARE": "AE", "UNITED ARAB EMIRATES": "AE",
    "JP": "JP", "JPN": "JP", "JAPAN": "JP",
}

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}


class RitchieBrosWatchError(RuntimeError):
    """The public Ritchie Bros automobile catalogue could not be reconciled."""


@dataclass(frozen=True)
class CataloguePass:
    total: int
    pages: int
    all_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    rejected_counts: dict[str, int]

    @property
    def fingerprint(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        return self.total, self.all_ids, tuple(str(row["id"]) for row in self.rows)


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def ascii_fold(value: Any) -> str:
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


def positive_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, str):
        compact = re.sub(r"[^0-9,.-]", "", value)
        if "," in compact and "." in compact:
            compact = compact.replace(".", "").replace(",", ".")
        elif "," in compact:
            compact = compact.replace(",", ".")
        value = compact
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return int(result) if result.is_integer() else result


def country_code(value: Any) -> str:
    return COUNTRY_CODES.get(ascii_fold(value).upper(), "")


def epoch_millis(value: Any, *, field: str) -> dt.datetime:
    amount = positive_number(value)
    if amount is None or amount < 946_684_800_000:
        raise RitchieBrosWatchError(f"Ritchie Bros listing has invalid {field}")
    try:
        return dt.datetime.fromtimestamp(float(amount) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise RitchieBrosWatchError(f"Ritchie Bros listing has invalid {field}") from exc


def slugify(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_fold(value)).strip("-")
    return slug or "automobile"


def normalize_fuel(value: Any) -> str:
    folded = ascii_fold(value)
    diesel = bool(re.search(r"\b(?:diesel|gazole)\b", folded))
    petrol = bool(re.search(r"\b(?:petrol|gasoline|essence|benzine|bensin)\b", folded))
    hybrid = "hybrid" in folded
    electric = bool(re.search(r"\b(?:electric|bev|ev)\b", folded))
    if diesel and hybrid:
        return "diesel/electric hybrid"
    if petrol and hybrid:
        return "petrol/electric hybrid"
    if hybrid:
        return "hybrid"
    if electric:
        return "electric"
    if diesel:
        return "diesel"
    if petrol:
        return "petrol"
    if re.search(r"\b(?:lpg|gpl|cng|lng)\b", folded):
        return "gas"
    return "unknown"


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.45,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def parse_catalogue_results(markup: str) -> tuple[int, list[dict[str, Any]]]:
    soup = BeautifulSoup(markup, "html.parser")
    node = soup.find("script", id="__NEXT_DATA__")
    if node is None or not node.string:
        raise RitchieBrosWatchError("Ritchie Bros catalogue has no public Next data")
    try:
        payload = json.loads(node.string)
        results = payload["props"]["pageProps"]["data"]["results"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RitchieBrosWatchError("Ritchie Bros catalogue data has an invalid shape") from exc
    if not isinstance(results, dict):
        raise RitchieBrosWatchError("Ritchie Bros catalogue results are not an object")
    total = nonnegative_integer(results.get("totalAmount"))
    returned = nonnegative_integer(results.get("returnedAmount"))
    records = results.get("records")
    if (
        total is None
        or returned is None
        or total > MAX_CATALOGUE_ROWS
        or not isinstance(records, list)
        or not all(isinstance(record, dict) for record in records)
        or returned != len(records)
    ):
        raise RitchieBrosWatchError("Ritchie Bros catalogue pagination metadata is invalid")
    return total, records


def fetch_page(
    session: requests.Session,
    *,
    offset: int,
    timeout: int,
) -> tuple[int, list[dict[str, Any]]]:
    response = session.get(
        CATALOGUE_URL,
        params={"from": offset},
        headers=HEADERS,
        timeout=timeout,
    )
    try:
        response.raise_for_status()
        total, records = parse_catalogue_results(response.text)
    finally:
        response.close()
    expected = max(0, min(PAGE_SIZE, total - offset))
    if len(records) != expected:
        raise RitchieBrosWatchError(
            f"Ritchie Bros page {offset} declared total {total} but returned "
            f"{len(records)}, expected {expected}"
        )
    return total, records


def record_id(record: dict[str, Any]) -> str:
    native_id = clean(record.get("itemNumber"))
    if not native_id.isdigit():
        raise RitchieBrosWatchError("Ritchie Bros catalogue card has no stable item number")
    return native_id


def row_from_record(
    record: dict[str, Any],
    *,
    observed_at: str,
    now: dt.datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    native_id = record_id(record)
    asset_country = country_code(record.get("locationCountry") or record.get("eventCountry"))
    if not asset_country:
        return None, "unknown_asset_country"
    if asset_country not in SCHENGEN_COUNTRIES:
        return None, "non_schengen_asset"
    buying_format = clean(record.get("buyingFormat") or record.get("buyingFormatFacetLabel"))
    if buying_format.casefold() not in AUCTION_FORMATS:
        return None, "non_auction_format"

    title = clean(record.get("assetDescription"))
    if not title:
        manufacturer = clean(record.get("manufacturerLocalized") or record.get("rawManufacturerName"))
        model = clean(record.get("modelLocalized") or record.get("rawModelName"))
        title = clean(f"{manufacturer} {model}")
    if not title:
        raise RitchieBrosWatchError(f"Ritchie Bros item {native_id} has no public title")
    end = epoch_millis(record.get("biddingEndTime"), field="bidding end time")
    if end <= now:
        return None, "already_ended"
    event_value = record.get("eventEndDateTime")
    event = epoch_millis(event_value, field="event end time") if event_value not in (None, "") else None
    currency = clean(record.get("priceCurrency") or "EUR").upper()
    starting_bid = positive_number(record.get("startPrice"))
    if starting_bid is None:
        price_kind = "unknown"
        price_label = "No public Ritchie Bros starting bid is displayed."
        bid_visibility = "not_publicly_disclosed"
    else:
        price_kind = "starting_bid"
        price_label = "Public Ritchie Bros starting bid."
        bid_visibility = "public_starting_bid"
    mileage = positive_number(record.get("usageKilometers"))
    features = clean(record.get("features"))
    fuel = normalize_fuel(f"{title} {features}")
    status_text = clean(record.get("listingStatus")).casefold()
    auction_status = "active" if status_text == "open" else "upcoming"
    location = clean(record.get("locationCity") or record.get("locationName") or record.get("itemSiteName"))
    sale_name = clean(record.get("eventAdvertisedName") or record.get("saleEventName"))
    asset_type = clean(record.get("assetTypeLocalized") or record.get("categoryLocalized") or "Automobile")
    model = clean(record.get("modelLocalized") or record.get("rawModelName") or title)
    return {
        "id": f"{SOURCE_KEY}:{native_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": f"https://www.rbauction.com/pdp/{slugify(title)}/{native_id}",
        "title": title,
        "model": model,
        "country": asset_country,
        "asset_country": asset_country,
        "category": "vehicle",
        "category_raw": asset_type,
        "year": nonnegative_integer(record.get("manufactureYear")),
        "mileage_km": int(mileage) if mileage is not None else None,
        "fuel": fuel,
        "price_amount": starting_bid,
        "price_currency": currency,
        "price_eur": starting_bid if currency == "EUR" else None,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": bid_visibility,
        "seller": SOURCE_NAME,
        "location": location,
        "sale_name": sale_name,
        "sale_id": clean(record.get("saleEventID") or record.get("eventPrimarySiteId")),
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": event.isoformat() if event else None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public Ritchie Bros auction card; verify bidder registration, vehicle "
            "condition, fees, documents, collection and import terms before bidding."
        ),
        "access_sale_note": "Ritchie Bros bidder registration and lot-specific auction terms apply.",
        "auction_status": auction_status,
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-catalogue:{native_id}",
        "evidence": f"Public Ritchie Bros {buying_format} catalogue card.",
    }, None


def catalogue_pass(
    session: requests.Session,
    *,
    timeout: int,
    observed_at: str,
    now: dt.datetime,
) -> CataloguePass:
    total, first_records = fetch_page(session, offset=0, timeout=timeout)
    all_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    pages = 0

    def consume(records: list[dict[str, Any]]) -> None:
        nonlocal pages
        pages += 1
        for record in records:
            native_id = record_id(record)
            if native_id in all_ids:
                raise RitchieBrosWatchError("Ritchie Bros catalogue repeats a stable item number")
            all_ids.add(native_id)
            row, reason = row_from_record(record, observed_at=observed_at, now=now)
            if row is not None:
                rows.append(row)
            elif reason:
                rejected[reason] = rejected.get(reason, 0) + 1

    consume(first_records)
    for offset in range(PAGE_SIZE, total, PAGE_SIZE):
        page_total, records = fetch_page(session, offset=offset, timeout=timeout)
        if page_total != total:
            raise RitchieBrosWatchError("Ritchie Bros catalogue total changed during enumeration")
        consume(records)
    if len(all_ids) != total:
        raise RitchieBrosWatchError(
            f"Ritchie Bros catalogue ID reconciliation failed: declared={total} unique={len(all_ids)}"
        )
    return CataloguePass(
        total=total,
        pages=pages,
        all_ids=tuple(sorted(all_ids)),
        rows=tuple(sorted(rows, key=lambda row: str(row["id"]))),
        rejected_counts=dict(sorted(rejected.items())),
    )


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    if timeout < 5:
        raise ValueError("Ritchie Bros timeout must be at least five seconds")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    observed_at = now.isoformat()
    owned_session = session is None
    active_session = session or configured_session()
    try:
        first = catalogue_pass(
            active_session, timeout=timeout, observed_at=observed_at, now=now
        )
        second = catalogue_pass(
            active_session, timeout=timeout, observed_at=observed_at, now=now
        )
    finally:
        if owned_session:
            active_session.close()
    if first.fingerprint != second.fingerprint:
        raise RitchieBrosWatchError("Ritchie Bros final reconciliation changed")
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public automobile card reachable from the Ritchie Bros catalogue",
        "catalogue_total": first.total,
        "listing_pages": first.pages,
        "stable_ids_unique": True,
        "full_catalogue_rechecked": True,
        "schengen_auction_rows": len(first.rows),
        "rejected_counts": first.rejected_counts,
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
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch every public Schengen Ritchie Bros automobile auction"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "RBAUCTION_WATCH_PASS",
        "row_count": payload["row_count"],
        "catalogue_total": report["catalogue_total"],
        "pages": report["listing_pages"],
        "seconds": round(time.monotonic() - started, 1),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
