#!/usr/bin/env python3
"""Reconcile every public active KVD Cars bidding auction in Sweden.

KVD's public auction endpoint supplies the authoritative total and accepts a
maximum page size of fifty.  A publication is emitted only after two complete,
stable enumerations of that total; fixed-price and already closed listings are
reported but never represented as bidding auctions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from fx_rates import fetch_ecb_units_per_eur, to_eur
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "kvdcars"
SOURCE_NAME = "KVD Cars"
API_URL = "https://api.kvd.se/v1/auction/search"
PAGE_SIZE = 50
DEFAULT_TIMEOUT = 35
ACTIVE_STATES = frozenset({"OPEN", "PREPARING", "SCHEDULING"})

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Origin": "https://www.kvd.se",
    "Referer": "https://www.kvd.se/",
}


class KvdCarsWatchError(RuntimeError):
    """The public KVD Cars auction catalogue could not be reconciled safely."""


@dataclass(frozen=True)
class Catalogue:
    api_total: int
    api_bidding_total: int
    closed_or_inactive_bidding_total: int
    fixed_price_total: int
    all_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def fingerprint(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        return self.api_total, self.all_ids, tuple(str(row["id"]) for row in self.rows)


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


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
        elif compact.count(".") > 1:
            compact = compact.replace(".", "")
        value = compact
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return int(result) if result.is_integer() else result


def timestamp(value: Any, *, field: str, allow_empty: bool = True) -> dt.datetime | None:
    text = clean(value)
    if not text:
        if allow_empty:
            return None
        raise KvdCarsWatchError(f"KVD Cars listing has no {field}")
    try:
        result = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KvdCarsWatchError(f"KVD Cars listing has invalid {field}: {text!r}") from exc
    if result.tzinfo is None:
        raise KvdCarsWatchError(f"KVD Cars listing has timezone-free {field}: {text!r}")
    return result.astimezone(UTC)


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


def fetch_page(session: requests.Session, *, offset: int, timeout: int) -> tuple[int, list[dict[str, Any]]]:
    response = session.get(
        API_URL,
        params={"limit": PAGE_SIZE, "offset": offset},
        timeout=timeout,
    )
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise KvdCarsWatchError(f"KVD Cars page {offset} is not an object")
    total = nonnegative_integer(payload.get("total"))
    auctions = payload.get("auctions")
    if total is None or not isinstance(auctions, list) or not all(isinstance(row, dict) for row in auctions):
        raise KvdCarsWatchError(f"KVD Cars page {offset} has invalid pagination metadata")
    expected = max(0, min(PAGE_SIZE, total - offset))
    if len(auctions) != expected:
        raise KvdCarsWatchError(
            f"KVD Cars page {offset} declared total {total} but returned {len(auctions)}, expected {expected}"
        )
    return total, auctions


def fuel_name(properties: dict[str, Any]) -> str:
    raw_fuels = properties.get("fuels")
    values: list[str] = []
    if isinstance(raw_fuels, list):
        for raw in raw_fuels:
            if isinstance(raw, dict):
                values.append(clean(raw.get("fuelCode")))
            else:
                values.append(clean(raw))
    folded = " ".join(values).casefold()
    if "hybrid" in folded:
        return "hybrid"
    if "diesel" in folded:
        return "diesel"
    if "petrol" in folded or "gasoline" in folded or "bensin" in folded:
        return "petrol"
    if "electric" in folded or "el" == folded:
        return "electric"
    if "gas" in folded or "lpg" in folded:
        return "lpg"
    return "unknown"


def mileage_km(properties: dict[str, Any]) -> int | None:
    raw = positive_number(properties.get("odometerReading"))
    if raw is None:
        return None
    unit = clean(properties.get("odometerUnit")).casefold()
    kilometres = raw * 10 if unit in {"mil", "swedish mil"} else raw
    return int(kilometres) if float(kilometres).is_integer() else None


def row_to_watch(auction: dict[str, Any], *, observed_at: str, now: dt.datetime, fx_rate: float | None = None) -> dict[str, Any]:
    native_id = clean(auction.get("id"))
    url = clean(auction.get("auctionUrl"))
    if not native_id or not native_id.isdigit() or not url.startswith("https://www.kvd.se/"):
        raise KvdCarsWatchError("KVD Cars auction has no canonical public identity")
    process_object = auction.get("processObject")
    if not isinstance(process_object, dict):
        raise KvdCarsWatchError(f"KVD Cars auction {native_id} has no vehicle object")
    properties = process_object.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    title = clean(properties.get("title") or process_object.get("title"))
    if not title:
        raise KvdCarsWatchError(f"KVD Cars auction {native_id} has no vehicle title")
    auction_state = clean(auction.get("state")).upper()
    if auction_state not in ACTIVE_STATES:
        raise KvdCarsWatchError(f"KVD Cars auction {native_id} is not active")
    if auction.get("closedAt") not in (None, ""):
        raise KvdCarsWatchError(f"KVD Cars auction {native_id} is already closed")

    active = auction.get("activeAuction")
    if not isinstance(active, dict):
        active = {}
    end = timestamp(active.get("preliminaryCloseAt"), field="preliminary close time")
    if end is not None and end <= now:
        raise KvdCarsWatchError(f"KVD Cars auction {native_id} has a past public close time")
    countdown_start = timestamp(auction.get("countdownStartAt"), field="countdown start")
    if countdown_start is not None and countdown_start <= now:
        countdown_start = None

    highest_bid = active.get("highestBid")
    if not isinstance(highest_bid, dict):
        highest_bid = {}
    current_bid = positive_number(highest_bid.get("amount"))
    starting_bid = positive_number(auction.get("startBid"))
    guide_price = positive_number(auction.get("preliminaryPrice"))
    if current_bid is not None:
        price, price_kind, price_label = current_bid, "current_bid", "Public KVD Cars highest bid"
    elif starting_bid is not None:
        price, price_kind, price_label = starting_bid, "starting_bid", "Public KVD Cars starting bid"
    elif guide_price is not None:
        price, price_kind, price_label = guide_price, "guide_price", "Public KVD Cars preliminary price"
    else:
        price, price_kind, price_label = None, "unknown", "KVD Cars has not published a bid or guide price"
    bids = active.get("bids")
    bid_count = len(bids) if isinstance(bids, list) else None
    reserve_reached = active.get("reservationPriceReached")

    vehicle_type = clean(process_object.get("vehicleType") or process_object.get("objectType"))
    location = ""
    location_info = process_object.get("locationInfo")
    if isinstance(location_info, dict):
        facility = location_info.get("facility")
        if isinstance(facility, dict):
            location = clean(facility.get("city") or facility.get("name"))
    year = nonnegative_integer(properties.get("modelYear"))
    terms: list[str] = ["Official KVD Cars public bidding auction"]
    if auction.get("isReserved") is True:
        terms.append("KVD marks this auction as reserved")
    if guide_price is not None:
        terms.append(f"Preliminary price: {guide_price} {clean(auction.get('currency') or 'SEK')}")
    exportable = properties.get("exportable")
    if isinstance(exportable, bool):
        terms.append(f"Exportable: {'yes' if exportable else 'no'}")
    status = "active" if auction_state == "OPEN" else "upcoming"
    return {
        "id": f"{SOURCE_KEY}:{native_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": url,
        "title": title,
        "model": clean(properties.get("modelName") or process_object.get("modelName") or title),
        "country": "SE",
        "asset_country": "SE",
        "category": "vehicle",
        "category_raw": vehicle_type or "KVD Cars vehicle auction",
        "year": year,
        "mileage": mileage_km(properties),
        "mileage_km": mileage_km(properties),
        "fuel": fuel_name(properties),
        "seller": SOURCE_NAME,
        "location": location,
        "image_url": clean(auction.get("previewImage")) or None,
        "price_amount": to_eur(price, fx_rate) if (price is not None and fx_rate) else price,
        "price_currency": "EUR" if (price is not None and fx_rate) else clean(auction.get("currency") or "SEK").upper(),
        "price_eur": to_eur(price, fx_rate) if (price is not None and fx_rate) else None,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public KVD Cars auction API",
        "bid_count": bid_count,
        "minimum_next_bid": None,
        "reserve_met": reserve_reached if isinstance(reserve_reached, bool) else None,
        "no_reserve": None,
        "sale_terms": "; ".join(terms),
        "auction_status": status,
        "canonical_end_utc": end.isoformat() if end else None,
        "sale_end_utc": end.isoformat() if end else None,
        "sale_event_utc": countdown_start.isoformat() if countdown_start else None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official KVD Cars bidding auction. Confirm vehicle condition, fees, buyer requirements, "
            "and Algerian import eligibility before bidding."
        ),
        "access_sale_note": "Open the official KVD Cars auction to inspect bidding, delivery, and export terms.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-auction-api:{native_id}",
        "evidence": "Official KVD Cars public auction API listing.",
    }


def enumerate_catalogue(
    session: requests.Session, *, observed_at: str, now: dt.datetime, timeout: int, fx_rate: float | None = None
) -> Catalogue:
    total, first_page = fetch_page(session, offset=0, timeout=timeout)
    pages = [first_page]
    for offset in range(PAGE_SIZE, total, PAGE_SIZE):
        page_total, auctions = fetch_page(session, offset=offset, timeout=timeout)
        if page_total != total:
            raise KvdCarsWatchError(
                f"KVD Cars page {offset} changed the declared total from {total} to {page_total}"
            )
        pages.append(auctions)
    auctions = [auction for page in pages for auction in page]
    if len(auctions) != total:
        raise KvdCarsWatchError(f"KVD Cars returned {len(auctions)} auctions for declared total {total}")
    all_ids: list[str] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    rows: list[dict[str, Any]] = []
    bidding_total = 0
    closed_or_inactive_bidding_total = 0
    fixed_price_total = 0
    for auction in auctions:
        native_id = clean(auction.get("id"))
        url = clean(auction.get("auctionUrl"))
        if not native_id or not native_id.isdigit() or not url.startswith("https://www.kvd.se/"):
            raise KvdCarsWatchError("KVD Cars page contains an auction without canonical identity")
        if native_id in seen_ids or url in seen_urls:
            raise KvdCarsWatchError("KVD Cars pagination repeats a stable auction identity")
        seen_ids.add(native_id)
        seen_urls.add(url)
        all_ids.append(native_id)
        auction_type = clean(auction.get("auctionType")).upper()
        if auction_type != "BIDDING":
            fixed_price_total += 1
            continue
        bidding_total += 1
        active = clean(auction.get("state")).upper() in ACTIVE_STATES and auction.get("closedAt") in (None, "")
        if not active:
            closed_or_inactive_bidding_total += 1
            continue
        rows.append(row_to_watch(auction, observed_at=observed_at, now=now, fx_rate=fx_rate))
    row_ids = [str(row["id"]) for row in rows]
    if len(row_ids) != len(set(row_ids)):
        raise KvdCarsWatchError("KVD Cars active bidding results repeat a stable auction identity")
    return Catalogue(
        api_total=total,
        api_bidding_total=bidding_total,
        closed_or_inactive_bidding_total=closed_or_inactive_bidding_total,
        fixed_price_total=fixed_price_total,
        all_ids=tuple(all_ids),
        rows=tuple(rows),
    )


def build_watch(
    *, session: requests.Session | None = None, now: dt.datetime | None = None, timeout: int = DEFAULT_TIMEOUT,
    fx_rates: dict[str, tuple[float, str]] | None = None
) -> dict[str, Any]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        if fx_rates is not None and "SEK" in fx_rates:
            fx_rate, fx_date = fx_rates["SEK"]
        else:
            fx_rate, fx_date = fetch_ecb_units_per_eur("SEK")
        first = enumerate_catalogue(active_session, observed_at=observed_at, now=current, timeout=timeout, fx_rate=fx_rate)
        second = enumerate_catalogue(active_session, observed_at=observed_at, now=current, timeout=timeout, fx_rate=fx_rate)
    finally:
        if supplied_session is None:
            active_session.close()
    if first.fingerprint != second.fingerprint:
        raise KvdCarsWatchError("KVD Cars catalogue changed during final reconciliation")
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public current or upcoming KVD Cars bidding vehicle auction",
        "api_catalogue_total": first.api_total,
        "api_bidding_total": first.api_bidding_total,
        "current_or_future_rows": len(first.rows),
        "closed_or_inactive_bidding_rows": first.closed_or_inactive_bidding_total,
        "fixed_price_rows_excluded": first.fixed_price_total,
        "pages": math.ceil(first.api_total / PAGE_SIZE) if first.api_total else 0,
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
    parser = argparse.ArgumentParser(description="Fetch every public KVD Cars bidding auction")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "KVDCARS_WATCH_PASS",
        "row_count": payload["row_count"],
        "api_catalogue_total": payload["source_reports"][SOURCE_KEY]["api_catalogue_total"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
