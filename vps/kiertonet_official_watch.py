#!/usr/bin/env python3
"""Reconcile every public current Kiertonet vehicle auction in Finland."""
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
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "kiertonet"
SOURCE_NAME = "Kiertonet"
ROOT_URL = "https://kiertonet.fi"
VEHICLES_URL = f"{ROOT_URL}/ajoneuvot-ja-peravaunut"
FILTER_URL = f"{ROOT_URL}/filter-auctions"
PAGE_SIZE = 30
DEFAULT_TIMEOUT = 35
HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": VEHICLES_URL,
}
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


class KiertonetWatchError(RuntimeError):
    """Kiertonet's public vehicle-auction category could not be reconciled."""


@dataclass(frozen=True)
class Catalogue:
    category_csv: str
    declared_total: int
    fixed_price_total: int
    all_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def fingerprint(self) -> tuple[str, int, tuple[str, ...], tuple[str, ...]]:
        return (
            self.category_csv,
            self.declared_total,
            tuple(sorted(self.all_ids)),
            tuple(sorted(str(row["id"]) for row in self.rows)),
        )


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


def truthy(value: Any) -> bool:
    return value is True or value in (1, "1", "true", "True")


def timestamp(value: Any, *, field: str) -> dt.datetime:
    text = clean(value)
    if not text:
        raise KiertonetWatchError(f"Kiertonet auction has no {field}")
    try:
        result = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KiertonetWatchError(f"Kiertonet auction has invalid {field}: {text!r}") from exc
    if result.tzinfo is None:
        raise KiertonetWatchError(f"Kiertonet auction has timezone-free {field}: {text!r}")
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


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, timeout=timeout)
    try:
        response.raise_for_status()
        markup = response.text
    finally:
        response.close()
    if not markup:
        raise KiertonetWatchError(f"Kiertonet returned an empty category page: {url}")
    return markup


def vehicle_category_csv(markup: str) -> str:
    soup = BeautifulSoup(markup, "html.parser")
    component: Tag | None = soup.find("auctions-list")
    raw = component.attrs.get(":fixed_params") if component is not None else None
    try:
        payload = json.loads(html.unescape(str(raw or "")))
    except (TypeError, ValueError) as exc:
        raise KiertonetWatchError("Kiertonet vehicle page has no usable category scope") from exc
    category_csv = clean(payload.get("kategoria") if isinstance(payload, dict) else "")
    ids = category_csv.split(",")
    if not ids or any(not item.isdigit() for item in ids) or len(ids) != len(set(ids)):
        raise KiertonetWatchError("Kiertonet vehicle page has invalid category identities")
    return ",".join(ids)


def fetch_page(
    session: requests.Session, *, category_csv: str, page: int, timeout: int
) -> tuple[int, int, list[dict[str, Any]]]:
    response = session.get(
        FILTER_URL,
        params={
            "per_page": PAGE_SIZE,
            "page": page,
            "piilota_paattyneet": 1,
            "kategoria": category_csv,
            "jarjestys": "paattyvat",
        },
        timeout=timeout,
    )
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise KiertonetWatchError(f"Kiertonet page {page} is not an object")
    rows = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows) or not isinstance(meta, dict):
        raise KiertonetWatchError(f"Kiertonet page {page} has invalid pagination fields")
    total = nonnegative_integer(meta.get("total"))
    last_page = nonnegative_integer(meta.get("last_page"))
    current_page = nonnegative_integer(meta.get("current_page"))
    per_page = nonnegative_integer(meta.get("per_page"))
    if total is None or last_page is None or current_page != page or per_page != PAGE_SIZE:
        raise KiertonetWatchError(f"Kiertonet page {page} has invalid pagination metadata")
    expected_last = math.ceil(total / PAGE_SIZE) if total else 1
    if last_page != expected_last:
        raise KiertonetWatchError(
            f"Kiertonet page {page} declares total {total} but last page {last_page}, expected {expected_last}"
        )
    expected_rows = max(0, min(PAGE_SIZE, total - (page - 1) * PAGE_SIZE))
    if len(rows) != expected_rows:
        raise KiertonetWatchError(
            f"Kiertonet page {page} declared total {total} but returned {len(rows)}, expected {expected_rows}"
        )
    return total, last_page, rows


def normalize_fuel(text: str) -> str:
    folded = text.casefold()
    if "hybrid" in folded:
        return "hybrid"
    if "diesel" in folded:
        return "diesel"
    if "bensiini" in folded or "petrol" in folded or "gasoline" in folded:
        return "petrol"
    if "sähkö" in folded or "electric" in folded:
        return "electric"
    if "kaasu" in folded or "lpg" in folded:
        return "lpg"
    return "unknown"


def row_to_watch(
    item: dict[str, Any], *, category_ids: frozenset[str], observed_at: str, now: dt.datetime
) -> dict[str, Any]:
    native_id = clean(item.get("id"))
    url = clean(item.get("fullUrl"))
    title = clean(item.get("full_title") or item.get("title"))
    category_id = clean(item.get("category_id"))
    if not native_id.isdigit() or not url.startswith(f"{ROOT_URL}/huutokaupat/") or not title:
        raise KiertonetWatchError("Kiertonet vehicle card has no canonical public identity")
    if category_id not in category_ids:
        raise KiertonetWatchError(f"Kiertonet auction {native_id} escaped the declared vehicle categories")
    if truthy(item.get("hasEnded")):
        raise KiertonetWatchError(f"Kiertonet auction {native_id} is ended despite the active filter")
    end = timestamp(item.get("ends_at"), field="end time")
    if end <= now:
        raise KiertonetWatchError(f"Kiertonet auction {native_id} has a past end time")
    highest_bid = positive_number(item.get("highest_bid"))
    starting_price = positive_number(item.get("starting_price"))
    if highest_bid is not None:
        price, price_kind, price_label = highest_bid, "current_bid", "Public Kiertonet highest bid"
    elif starting_price is not None:
        price, price_kind, price_label = starting_price, "starting_bid", "Public Kiertonet starting price"
    else:
        price, price_kind, price_label = None, "unknown", "Kiertonet card has no positive bid or starting price"
    year_match = YEAR_RE.search(title)
    terms = ["Official Kiertonet public vehicle auction"]
    if truthy(item.get("is_sold_to_highest_bidder")):
        terms.append("Kiertonet marks this lot as sold to the highest bidder")
    if truthy(item.get("is_bankruptcy_estate_auction")):
        terms.append("Kiertonet marks this as a bankruptcy-estate auction")
    return {
        "id": f"{SOURCE_KEY}:{native_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": url,
        "title": title,
        "model": title,
        "country": "FI",
        "asset_country": "FI",
        "category": "vehicle",
        "category_raw": f"Kiertonet vehicle category {category_id}",
        "year": int(year_match.group(1)) if year_match else None,
        "mileage": None,
        "mileage_km": None,
        "fuel": normalize_fuel(title),
        "seller": clean(item.get("seller_name")) or SOURCE_NAME,
        "location": clean(item.get("city")),
        "image_url": clean(item.get("medium_image_url") or item.get("thumbnail_image_url")) or None,
        "price_amount": price,
        "price_currency": "EUR",
        "price_eur": price,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public Kiertonet auction card",
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": "; ".join(terms),
        "auction_status": "active",
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official Kiertonet vehicle auction. Confirm vehicle condition, fees, buyer requirements, "
            "and Algerian import eligibility before bidding."
        ),
        "access_sale_note": "Open the official Kiertonet lot to inspect bidding, pickup, and export terms.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-vehicle-filter:{native_id}",
        "evidence": "Official Kiertonet public vehicle-auction card.",
    }


def enumerate_catalogue(
    session: requests.Session, *, observed_at: str, now: dt.datetime, timeout: int
) -> Catalogue:
    category_csv = vehicle_category_csv(fetch_markup(session, VEHICLES_URL, timeout=timeout))
    category_ids = frozenset(category_csv.split(","))
    total, last_page, first_page = fetch_page(
        session, category_csv=category_csv, page=1, timeout=timeout
    )
    pages = [first_page]
    for page in range(2, last_page + 1):
        page_total, page_last, rows = fetch_page(
            session, category_csv=category_csv, page=page, timeout=timeout
        )
        if page_total != total or page_last != last_page:
            raise KiertonetWatchError(f"Kiertonet page {page} changed the declared vehicle catalogue")
        pages.append(rows)
    items = [item for page in pages for item in page]
    if len(items) != total:
        raise KiertonetWatchError(f"Kiertonet returned {len(items)} cards for declared total {total}")
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    rows: list[dict[str, Any]] = []
    fixed_price_total = 0
    for item in items:
        native_id = clean(item.get("id"))
        url = clean(item.get("fullUrl"))
        if not native_id.isdigit() or not url.startswith(f"{ROOT_URL}/huutokaupat/"):
            raise KiertonetWatchError("Kiertonet page contains a card without canonical identity")
        if native_id in seen_ids or url in seen_urls:
            raise KiertonetWatchError("Kiertonet pagination repeats a stable auction identity")
        seen_ids.add(native_id)
        seen_urls.add(url)
        if truthy(item.get("is_buy_now")):
            fixed_price_total += 1
            continue
        rows.append(row_to_watch(item, category_ids=category_ids, observed_at=observed_at, now=now))
    return Catalogue(
        category_csv=category_csv,
        declared_total=total,
        fixed_price_total=fixed_price_total,
        all_ids=tuple(seen_ids),
        rows=tuple(rows),
    )


def build_watch(
    *, session: requests.Session | None = None, now: dt.datetime | None = None, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        first = enumerate_catalogue(active_session, observed_at=observed_at, now=current, timeout=timeout)
        second = enumerate_catalogue(active_session, observed_at=observed_at, now=current, timeout=timeout)
    finally:
        if supplied_session is None:
            active_session.close()
    if first.fingerprint != second.fingerprint:
        raise KiertonetWatchError("Kiertonet vehicle catalogue changed during final reconciliation")
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public current Kiertonet vehicle auction in the official vehicle category tree",
        "vehicle_category_ids": first.category_csv.split(","),
        "declared": first.declared_total,
        "publicly_listed": first.declared_total,
        "normalized_active": len(first.rows),
        "fixed_price_rows_excluded": first.fixed_price_total,
        "pages": math.ceil(first.declared_total / PAGE_SIZE) if first.declared_total else 0,
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
    parser = argparse.ArgumentParser(description="Fetch every public Kiertonet vehicle auction")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "KIERTONET_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": payload["source_reports"][SOURCE_KEY]["declared"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
