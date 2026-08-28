#!/usr/bin/env python3
"""Reconcile AURENA's complete public ``Fahrzeuge`` auction category.

The public AURENA web application uses one documented-by-the-page package
endpoint to render its vehicle filter.  The response supplies an exact
``elementCount``, stable lot IDs, native EUR bid data and canonical end times.
This connector requests every offset in that public vehicle category twice.
It fails closed when the declared count, pagination, category identity or lot
fingerprint changes, so it cannot silently publish an incomplete Austrian
auction catalogue.
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
SOURCE_KEY = "aurena"
SOURCE_NAME = "AURENA"
ROOT_URL = "https://www.aurena.at"
PACKAGE_URL = (
    "https://webplatform-facade.cluster.prod.aurena.services/"
    "api/v1/package/2485524364"
)
VEHICLE_CATEGORY_ID = 5
PAGE_SIZE = 96
DEFAULT_TIMEOUT = 35

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    "Origin": ROOT_URL,
    "Referer": f"{ROOT_URL}/auktionen?c={VEHICLE_CATEGORY_ID}",
}
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
MILEAGE_RE = re.compile(
    r"\b([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{1,7})\s*(?:km|kilometer)\b", re.I
)
TAG_RE = re.compile(r"<[^>]+>")


class AurenaWatchError(RuntimeError):
    """AURENA's public vehicle category could not be reconciled safely."""


@dataclass(frozen=True)
class ParsedPage:
    offset: int
    element_count: int
    category_ids: frozenset[int]
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Catalogue:
    element_count: int
    rows: tuple[dict[str, Any], ...]

    @property
    def fingerprint(self) -> tuple[int, tuple[str, ...]]:
        return self.element_count, tuple(sorted(str(row["id"]) for row in self.rows))


def clean(value: Any) -> str:
    text = str(value or "")
    # AURENA's transfer-state uses compact custom entities in text blocks.
    text = text.replace("&l;", "<").replace("&g;", ">").replace("&a;", "&")
    text = text.replace("&q;", '"')
    text = TAG_RE.sub(" ", text)
    return " ".join(html.unescape(text).split())


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


def category_request(offset: int) -> dict[str, Any]:
    return {
        "offset": offset,
        "limit": PAGE_SIZE,
        "languageCode": "de_DE",
        "filter": {
            "auctions": [],
            "provinces": [],
            "brands": [],
            "categories": [[VEHICLE_CATEGORY_ID]],
            "bidCount": None,
        },
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
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


def vehicle_category_ids(value: Any) -> frozenset[int] | None:
    if not isinstance(value, dict):
        return None
    categories = value.get("categories")
    if not isinstance(categories, list) or not categories:
        return None
    ids: set[int] = set()
    has_vehicle_root = False
    for category in categories:
        if not isinstance(category, dict):
            return None
        path = category.get("path")
        if not isinstance(path, list) or not path:
            return None
        for node in path:
            if not isinstance(node, dict) or not isinstance(node.get("id"), int):
                return None
            ids.add(node["id"])
            has_vehicle_root = has_vehicle_root or node["id"] == VEHICLE_CATEGORY_ID
        subcategories = category.get("subcategories")
        if not isinstance(subcategories, list):
            return None
        for node in subcategories:
            if not isinstance(node, dict) or not isinstance(node.get("id"), int):
                return None
            ids.add(node["id"])
    return frozenset(ids) if has_vehicle_root and VEHICLE_CATEGORY_ID in ids else None


def canonical_end(value: Any) -> dt.datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AurenaWatchError(f"AURENA vehicle has an invalid end timestamp: {value!r}")
    if value <= 0:
        raise AurenaWatchError(f"AURENA vehicle has a non-positive end timestamp: {value!r}")
    try:
        return dt.datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise AurenaWatchError(f"AURENA vehicle has an invalid end timestamp: {value!r}") from exc


def normalize_fuel(text: str) -> str:
    folded = text.casefold()
    if "hybrid" in folded:
        return "hybrid"
    if "plug-in" in folded or "plugin" in folded:
        return "hybrid"
    if "diesel" in folded:
        return "diesel"
    if "benzin" in folded or "super" in folded or "ottokraftstoff" in folded:
        return "petrol"
    if "elektro" in folded or "electric" in folded or "strom" in folded:
        return "electric"
    if "lpg" in folded or "autogas" in folded:
        return "lpg"
    if "cng" in folded or "erdgas" in folded:
        return "cng"
    return "unknown"


def localized_lot_text(item: dict[str, Any], key: str) -> str:
    language_data = item.get("ld")
    if not isinstance(language_data, dict):
        return ""
    translations = language_data.get(key)
    if not isinstance(translations, dict):
        return ""
    return clean(translations.get("de_DE") or translations.get("en_US") or "")


def item_to_row(
    item: Any, *, observed_at: str, now: dt.datetime, category_ids: frozenset[int] | None = None
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise AurenaWatchError("AURENA package includes a non-object vehicle lot")
    lot_id = item.get("lid")
    auction_id = item.get("aid")
    if isinstance(lot_id, bool) or isinstance(auction_id, bool):
        raise AurenaWatchError("AURENA vehicle has an invalid stable identity")
    if not isinstance(lot_id, int) or not isinstance(auction_id, int) or lot_id <= 0 or auction_id <= 0:
        raise AurenaWatchError("AURENA vehicle has no stable lot or auction identity")
    allowed_categories = category_ids or frozenset({VEHICLE_CATEGORY_ID})
    if item.get("cat") not in allowed_categories:
        raise AurenaWatchError(f"AURENA category response contains non-vehicle lot {lot_id}")
    title = localized_lot_text(item, "ti")
    description = localized_lot_text(item, "de")
    if not title:
        raise AurenaWatchError(f"AURENA vehicle lot {lot_id} has no public title")
    vehicle_text = f"{title} {description}"
    year_match = YEAR_RE.search(vehicle_text)
    mileage_match = MILEAGE_RE.search(vehicle_text)
    year = int(year_match.group(1)) if year_match else None
    mileage = positive_number(mileage_match.group(1).replace(" ", "").replace(".", "")) if mileage_match else None
    end = canonical_end(item.get("et"))
    if end <= now:
        raise AurenaWatchError(
            f"AURENA lot {lot_id} is already ended while returned in the current vehicle category"
        )

    current_bid = positive_number((item.get("hib") or {}).get("val") if isinstance(item.get("hib"), dict) else None)
    starting_bid = positive_number(item.get("sp"))
    if current_bid is not None:
        price_amount = current_bid
        price_kind = "current_bid"
        price_label = "public current bid from AURENA"
    elif starting_bid is not None:
        price_amount = starting_bid
        price_kind = "starting_bid"
        price_label = "public starting bid from AURENA"
    else:
        price_amount = None
        price_kind = "unknown"
        price_label = "AURENA vehicle card does not state a public bid"

    images = item.get("im")
    image_url = clean(images[0]) if isinstance(images, list) and images else ""
    bid_count = item.get("bc")
    if isinstance(bid_count, bool) or not isinstance(bid_count, int) or bid_count < 0:
        bid_count = None
    url = f"{ROOT_URL}/auktion/{auction_id}/lot/{lot_id}"
    return {
        "id": f"{SOURCE_KEY}:{auction_id}:{lot_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": url,
        "title": title,
        "model": title,
        "country": "AT",
        "asset_country": "AT",
        "category": "vehicle",
        "category_raw": "Fahrzeuge",
        "year": year,
        "mileage": mileage,
        "mileage_km": mileage,
        "fuel": normalize_fuel(vehicle_text),
        "seller": SOURCE_NAME,
        "image_url": image_url or None,
        "price_amount": price_amount,
        "price_currency": "EUR" if price_amount is not None else "",
        "price_eur": price_amount,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public AURENA vehicle-category package",
        "bid_count": bid_count,
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": "Official AURENA auction lot; inspect the linked lot for terms and fees.",
        "auction_status": "active",
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official AURENA vehicle listing. Confirm vehicle condition, auction terms, "
            "fees, buyer requirements, and Algerian import eligibility before bidding."
        ),
        "access_sale_note": "Open the official AURENA lot to inspect all bidding and pickup terms.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-vehicle-package:{auction_id}:{lot_id}",
        "evidence": "Official AURENA public Fahrzeuge category package.",
    }


def fetch_page(
    session: requests.Session, *, offset: int, observed_at: str, now: dt.datetime, timeout: int
) -> ParsedPage:
    response = session.post(PACKAGE_URL, json=category_request(offset), timeout=timeout)
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise AurenaWatchError("AURENA vehicle package is not a JSON object")
    count = payload.get("elementCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise AurenaWatchError("AURENA vehicle package has an invalid element count")
    if payload.get("offset") != offset or payload.get("limit") != PAGE_SIZE:
        raise AurenaWatchError("AURENA vehicle package pagination does not match the requested offset")
    category_ids = vehicle_category_ids(payload.get("filter"))
    if category_ids is None:
        raise AurenaWatchError("AURENA package does not confirm the Fahrzeuge category")
    items = payload.get("items")
    if not isinstance(items, list):
        raise AurenaWatchError("AURENA vehicle package has no list of lots")
    expected_size = max(0, min(PAGE_SIZE, count - offset))
    if len(items) != expected_size:
        raise AurenaWatchError(
            "AURENA vehicle package page is incomplete "
            f"at offset {offset} ({len(items)} != {expected_size})"
        )
    rows = tuple(
        item_to_row(item, observed_at=observed_at, now=now, category_ids=category_ids)
        for item in items
    )
    return ParsedPage(
        offset=offset,
        element_count=count,
        category_ids=category_ids,
        rows=rows,
    )


def enumerate_catalogue(
    session: requests.Session, *, observed_at: str, now: dt.datetime, timeout: int
) -> Catalogue:
    first = fetch_page(session, offset=0, observed_at=observed_at, now=now, timeout=timeout)
    pages = [first]
    for offset in range(PAGE_SIZE, first.element_count, PAGE_SIZE):
        page = fetch_page(session, offset=offset, observed_at=observed_at, now=now, timeout=timeout)
        if page.element_count != first.element_count or page.category_ids != first.category_ids:
            raise AurenaWatchError("AURENA vehicle count changed while pages were enumerated")
        pages.append(page)
    rows = tuple(row for page in pages for row in page.rows)
    ids = [str(row["id"]) for row in rows]
    urls = [str(row["url"]) for row in rows]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise AurenaWatchError("AURENA vehicle category contains duplicate stable identities")
    if len(rows) != first.element_count:
        raise AurenaWatchError(
            "AURENA declared vehicle count does not equal every enumerated public lot "
            f"({first.element_count} != {len(rows)})"
        )
    return Catalogue(element_count=first.element_count, rows=rows)


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
            raise AurenaWatchError("AURENA vehicle category changed during final reconciliation")
        page_count = math.ceil(first.element_count / PAGE_SIZE) if first.element_count else 1
        report = {
            "status": "ok",
            "connector_status": "ok",
            "catalogue_scope": "every public current AURENA Fahrzeuge category lot",
            "declared": first.element_count,
            "publicly_listed": len(first.rows),
            "visited": len(first.rows),
            "normalized_rows": len(first.rows),
            "pages": page_count,
            "page_size": PAGE_SIZE,
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
    parser = argparse.ArgumentParser(description="Fetch every public AURENA Fahrzeuge category lot")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "AURENA_WATCH_PASS",
        "row_count": payload["row_count"],
        "pages": payload["source_reports"][SOURCE_KEY]["pages"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
