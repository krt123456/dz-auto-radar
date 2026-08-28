#!/usr/bin/env python3
"""Reconcile every open Auctionmaster passenger-car lot.

Auctionmaster exposes a dedicated public ``Passenger cars`` category (ID 10),
whose category endpoint declares the current open-card count.  The listing
endpoint itself has inconsistent ``totalElements`` values on later pages, so
the collector treats the dedicated category count as authoritative, walks each
normal public page, and rechecks the complete snapshot before publication.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "auctionmaster"
SOURCE_NAME = "Auctionmaster"
ROOT_URL = "https://auctionmaster.com"
LIST_URL = f"{ROOT_URL}/rest/en/v2/kavels"
CATEGORY_URL = f"{ROOT_URL}/rest/en/categorieen/10"
PASSENGER_CAR_CATEGORY_ID = "10"
PASSENGER_CAR_CATEGORY_NAME = "Passenger cars"
PAGE_SIZE = 100
DEFAULT_TIMEOUT = 35

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
MILEAGE_RE = re.compile(r"\b([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{1,7})\s*km\b", re.I)


class AuctionmasterWatchError(RuntimeError):
    """Auctionmaster's public vehicle category could not be reconciled safely."""


@dataclass(frozen=True)
class ParsedPage:
    page: int
    reported_total: int | None
    reported_total_pages: int | None
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Catalogue:
    total: int
    total_pages: int
    metadata_mismatch_pages: tuple[int, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def fingerprint(self) -> tuple[int, int, tuple[str, ...]]:
        return self.total, self.total_pages, tuple(str(row["id"]) for row in self.rows)


def clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def positive_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number) if number.is_integer() else number


def parse_utc(value: Any) -> dt.datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuctionmasterWatchError(f"Auctionmaster has an invalid timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        raise AuctionmasterWatchError(f"Auctionmaster timestamp has no UTC offset: {text!r}")
    return parsed.astimezone(UTC)


def normalize_fuel(text: str) -> str:
    folded = text.casefold()
    if "hybrid" in folded:
        return "hybrid"
    if "diesel" in folded:
        return "diesel"
    if "petrol" in folded or "gasoline" in folded or "benzine" in folded:
        return "petrol"
    if "electric" in folded or "elektrisch" in folded or "ev " in folded:
        return "electric"
    if "lpg" in folded:
        return "lpg"
    if "cng" in folded:
        return "cng"
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
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


def list_params(page: int) -> dict[str, str | int]:
    return {
        "page": page,
        "size": PAGE_SIZE,
        "status": "open",
        "categorieIds": PASSENGER_CAR_CATEGORY_ID,
    }


def nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def fetch_declared_total(session: requests.Session, *, timeout: int) -> int:
    response = session.get(CATEGORY_URL, timeout=timeout)
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    if not isinstance(payload, dict) or not isinstance(payload.get("categorieen"), list):
        raise AuctionmasterWatchError("Auctionmaster public category counter is invalid")
    for category in payload["categorieen"]:
        if not isinstance(category, dict) or clean(category.get("categorieId")) != PASSENGER_CAR_CATEGORY_ID:
            continue
        details = category.get("categorieDetails")
        if (
            not isinstance(details, dict)
            or clean(details.get("id")) != PASSENGER_CAR_CATEGORY_ID
            or clean(details.get("naam")) != PASSENGER_CAR_CATEGORY_NAME
            or clean(details.get("parent_id")) != "1"
        ):
            raise AuctionmasterWatchError(
                "Auctionmaster category 10 is no longer the passenger-car category"
            )
        total = nonnegative_integer(category.get("open"))
        if total is None:
            raise AuctionmasterWatchError(
                "Auctionmaster passenger-car category has an invalid declared count"
            )
        return total
    raise AuctionmasterWatchError("Auctionmaster public passenger-car category is absent")


def row_from_lot(lot: Any, *, observed_at: str, now: dt.datetime) -> dict[str, Any]:
    if not isinstance(lot, dict):
        raise AuctionmasterWatchError("Auctionmaster category response contains a non-object lot")
    listing_id = lot.get("id")
    auction = lot.get("veiling")
    category = lot.get("categorie")
    if not isinstance(listing_id, int) or listing_id <= 0 or not isinstance(auction, dict):
        raise AuctionmasterWatchError("Auctionmaster lot has no stable identity")
    auction_id = auction.get("id")
    lot_number = clean(lot.get("volgNummer"))
    if not isinstance(auction_id, int) or auction_id <= 0 or not lot_number:
        raise AuctionmasterWatchError(f"Auctionmaster lot {listing_id} has no public auction route")
    if (
        not isinstance(category, dict)
        or clean(category.get("id")) != PASSENGER_CAR_CATEGORY_ID
        or clean(category.get("naam")) != PASSENGER_CAR_CATEGORY_NAME
        or clean(category.get("parentTrackingKey")) != "Cars and other transport"
    ):
        raise AuctionmasterWatchError(
            f"Auctionmaster lot {listing_id} is outside the passenger-car category"
        )
    country = clean(auction.get("land")).upper()
    if country != "NL":
        raise AuctionmasterWatchError(
            f"Auctionmaster vehicle lot {listing_id} has unexpected auction country {country!r}"
        )
    title = clean(lot.get("naam"))
    if not title:
        raise AuctionmasterWatchError(f"Auctionmaster lot {listing_id} has no public title")
    end = parse_utc(lot.get("sluitingsDatumISO"))
    if end is None or end <= now:
        raise AuctionmasterWatchError(
            f"Auctionmaster lot {listing_id} is not current despite the open-category response"
        )
    start = parse_utc(auction.get("openingsDatumISO"))
    highest_bid = positive_number(lot.get("hoogsteBod"))
    opening_bid = positive_number(lot.get("openingsBod"))
    if highest_bid is not None:
        price_amount = highest_bid
        price_kind = "current_bid"
        price_label = "public current bid from Auctionmaster"
    elif opening_bid is not None:
        price_amount = opening_bid
        price_kind = "starting_bid"
        price_label = "public starting bid from Auctionmaster"
    else:
        price_amount = None
        price_kind = "unknown"
        price_label = "Auctionmaster card does not state a public bid"
    year_match = YEAR_RE.search(title)
    mileage_match = MILEAGE_RE.search(title)
    year = int(year_match.group(1)) if year_match else None
    mileage = positive_number(
        mileage_match.group(1).replace(" ", "").replace(".", "")
    ) if mileage_match else None
    bid_count = lot.get("aantalBiedingen")
    if isinstance(bid_count, bool) or not isinstance(bid_count, int) or bid_count < 0:
        bid_count = None
    auction_name = clean(auction.get("naam"))
    url = f"{ROOT_URL}/en/veilingen/{auction_id}/kavels/{lot_number}"
    return {
        "id": f"{SOURCE_KEY}:{auction_id}:{listing_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": url,
        "title": title,
        "model": title,
        "country": "NL",
        "asset_country": "NL",
        "category": "car",
        "category_raw": "Auctionmaster Passenger cars category 10",
        "year": year,
        "mileage": mileage,
        "mileage_km": mileage,
        "fuel": normalize_fuel(title),
        "seller": SOURCE_NAME,
        "price_amount": price_amount,
        "price_currency": "EUR" if price_amount is not None else "",
        "price_eur": price_amount,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public Auctionmaster passenger-car category listing",
        "bid_count": bid_count,
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": auction_name or "Official Auctionmaster auction lot",
        "auction_status": "upcoming" if start and start > now else "active",
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": start.isoformat() if start else None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official Auctionmaster vehicle listing. Confirm vehicle condition, auction terms, "
            "fees, buyer requirements, and Algerian import eligibility before bidding."
        ),
        "access_sale_note": "Open the official Auctionmaster lot to inspect all bidding and pickup terms.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-category:{auction_id}:{listing_id}",
        "evidence": "Official Auctionmaster public Passenger cars category listing.",
    }


def fetch_page(
    session: requests.Session,
    *,
    page: int,
    expected_total: int,
    observed_at: str,
    now: dt.datetime,
    timeout: int,
) -> ParsedPage:
    response = session.get(LIST_URL, params=list_params(page), timeout=timeout)
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise AuctionmasterWatchError("Auctionmaster vehicle category is not a JSON object")
    if payload.get("number") != page:
        raise AuctionmasterWatchError("Auctionmaster response page number does not match the request")
    content = payload.get("content")
    if not isinstance(content, list):
        raise AuctionmasterWatchError("Auctionmaster vehicle category has no lot list")
    expected_size = expected_total - ((page - 1) * PAGE_SIZE)
    expected_size = max(0, min(PAGE_SIZE, expected_size))
    if len(content) != expected_size:
        raise AuctionmasterWatchError(
            f"Auctionmaster page {page} is incomplete ({len(content)} != {expected_size})"
        )
    rows = tuple(row_from_lot(lot, observed_at=observed_at, now=now) for lot in content)
    return ParsedPage(
        page=page,
        reported_total=nonnegative_integer(payload.get("totalElements")),
        reported_total_pages=nonnegative_integer(payload.get("totalPages")),
        rows=rows,
    )


def enumerate_catalogue(
    session: requests.Session, *, observed_at: str, now: dt.datetime, timeout: int
) -> Catalogue:
    declared_total = fetch_declared_total(session, timeout=timeout)
    total_pages = math.ceil(declared_total / PAGE_SIZE)
    pages: list[ParsedPage] = []
    for page_number in range(1, total_pages + 1):
        page = fetch_page(
            session,
            page=page_number,
            expected_total=declared_total,
            observed_at=observed_at,
            now=now,
            timeout=timeout,
        )
        pages.append(page)
    rows = tuple(row for page in pages for row in page.rows)
    ids = [str(row["id"]) for row in rows]
    urls = [str(row["url"]) for row in rows]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise AuctionmasterWatchError("Auctionmaster category contains duplicate stable identities")
    if len(rows) != declared_total:
        raise AuctionmasterWatchError(
            "Auctionmaster declared vehicle total does not equal every enumerated public lot "
            f"({declared_total} != {len(rows)})"
        )
    metadata_mismatch_pages = tuple(
        page.page for page in pages
        if page.reported_total != declared_total or page.reported_total_pages != total_pages
    )
    return Catalogue(
        total=declared_total,
        total_pages=total_pages,
        metadata_mismatch_pages=metadata_mismatch_pages,
        rows=rows,
    )


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        first = enumerate_catalogue(
            active_session, observed_at=observed_at, now=current, timeout=timeout
        )
        second = enumerate_catalogue(
            active_session, observed_at=observed_at, now=current, timeout=timeout
        )
        if second.fingerprint != first.fingerprint:
            raise AuctionmasterWatchError("Auctionmaster category changed during final reconciliation")
        report = {
            "status": "ok",
            "connector_status": "ok",
            "catalogue_scope": "every public open Auctionmaster Passenger cars category lot",
            "declared": first.total,
            "publicly_listed": len(first.rows),
            "visited": len(first.rows),
            "normalized_rows": len(first.rows),
            "pages": first.total_pages,
            "page_size": PAGE_SIZE,
            "page_metadata_mismatch_pages": list(first.metadata_mismatch_pages),
            "page_metadata_is_not_authoritative": True,
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
    finally:
        if supplied_session is None:
            active_session.close()


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
        description="Fetch every public Auctionmaster Cars and other transport lot"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "AUCTIONMASTER_WATCH_PASS",
        "row_count": payload["row_count"],
        "pages": payload["source_reports"][SOURCE_KEY]["pages"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
