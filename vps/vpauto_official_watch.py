#!/usr/bin/env python3
"""Enumerate every public VPauto PRO vehicle card as one reconciled snapshot.

VPauto's public PRO landing page exposes a live "Search (N vehicles)" total.
After that page seeds the catalogue session, finite global pages at
"/pro/vehicle/list?page=..." enumerate every public offer card. Sale-filtered
pages are cookie-scoped and are therefore not used as a coverage boundary.
A physical vehicle can have several legitimate offers; stable offer identity
combines the physical vehicle ID with the canonical URL's offer token.

Only data exposed by the anonymous catalogue and public vehicle pages is
requested. Prices are deliberately left unknown when the public page does
not label their auction meaning; a hidden/account-only value must never be
presented as a bid or starting price.
"""
from __future__ import annotations

import argparse
import concurrent.futures
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
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "vpauto"
SOURCE_NAME = "VPauto"
BASE_URL = "https://vpauto.eu"
CATALOGUE_URL = f"{BASE_URL}/pro"
GLOBAL_LISTING_URL = f"{BASE_URL}/pro/vehicle/list?page=1"
DEFAULT_TIMEOUT = 30
DEFAULT_WORKERS = 8
MAX_LISTING_PAGES = 1_000
MAX_DECLARED_VEHICLES = 100_000
MAX_RECONCILIATION_ATTEMPTS = 6

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}
TOTAL_RE = re.compile(
    r"Search\s*\(\s*([0-9][0-9\s,.\u00a0]*)\s*vehicles\s*\)",
    re.I,
)
SALE_ROOT_RE = re.compile(r"^/search/sale/([A-Za-z0-9]+)$")
OFFER_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
MILEAGE_RE = re.compile(r"\b([0-9][0-9\s,.\u00a0]*)\s*km\b", re.I)
DETAIL_ID_RE = re.compile(r"""["']viewItem["']\s*:\s*["']?(\d+)""", re.I)
DETAIL_ENERGY_RE = re.compile(r"""["']energy["']\s*:\s*["']([^"']*)["']""", re.I)
DETAIL_END_RE = re.compile(
    r"""["']sale_end_date_complete["']\s*:\s*["']([^"']+)["']""", re.I
)
AWARE_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)


class VPAutoWatchError(RuntimeError):
    """The public VPauto catalogue could not be reconciled atomically."""


class VPAutoSnapshotChanged(VPAutoWatchError):
    """One global catalogue pass moved while its pages were enumerated."""


@dataclass(frozen=True)
class SaleSeed:
    sale_id: str
    listing_url: str
    sale_name: str


@dataclass(frozen=True)
class ListingPage:
    sale_id: str
    rows: list[dict[str, Any]]
    next_urls: tuple[str, ...]


@dataclass(frozen=True)
class GlobalListingPage:
    rows: tuple[dict[str, Any], ...]
    linked_pages: tuple[int, ...]


@dataclass(frozen=True)
class CataloguePass:
    pages: int
    rows: tuple[dict[str, Any], ...]
    physical_vehicle_ids: int

    @property
    def fingerprint(self) -> tuple[int, tuple[str, ...]]:
        return self.pages, tuple(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for row in self.rows
        )


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def ascii_fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", clean(value))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def positive_int(value: Any) -> int | None:
    compact = re.sub(r"[^0-9]", "", clean(value))
    if not compact:
        return None
    try:
        parsed = int(compact)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def normalize_fuel(value: Any) -> str:
    """Use only an explicit VPauto energy value; never guess from a title."""
    folded = ascii_fold(value)
    if not folded or folded in {"nc", "n c", "unknown", "non communique"}:
        return "unknown"
    diesel = bool(re.search(r"\b(?:diesel|gazole)\b", folded))
    petrol = bool(re.search(r"\b(?:essence|petrol|gasoline)\b", folded))
    hybrid = bool(re.search(r"\b(?:hybrid|hybride|phev|hev)\b", folded))
    electric = bool(re.search(r"\b(?:electric|electrique|bev)\b", folded))
    if diesel and hybrid:
        return "diesel/electric hybrid"
    if diesel:
        return "diesel"
    if petrol and hybrid:
        return "petrol/electric hybrid"
    if hybrid:
        return "hybrid"
    if electric:
        return "electric"
    if petrol:
        return "petrol"
    if re.search(r"\b(?:lpg|gpl|cng|gnv)\b", folded):
        return "gas"
    return "unknown"


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
    return session


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def normalized_listing_url(href: str) -> tuple[str, str]:
    """Return a canonical public listing URL and the sale ID it belongs to."""
    parsed = urlsplit(urljoin(BASE_URL, clean(href)))
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"vpauto.eu", "www.vpauto.eu"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise VPAutoWatchError("VPauto listing link leaves the official domain")
    root_match = SALE_ROOT_RE.fullmatch(parsed.path)
    if root_match:
        sale_id = root_match.group(1)
        return f"{BASE_URL}/search/sale/{sale_id}", sale_id
    if parsed.path != "/pro/vehicle/list":
        raise VPAutoWatchError("VPauto pagination link has an unexpected path")
    values = parse_qs(parsed.query, keep_blank_values=True)
    sale_values = values.get("sale") or []
    page_values = values.get("page") or []
    if len(sale_values) != 1 or len(page_values) != 1:
        raise VPAutoWatchError("VPauto pagination link has incomplete query values")
    sale_id = clean(sale_values[0])
    page_text = clean(page_values[0])
    if not re.fullmatch(r"[A-Za-z0-9]+", sale_id) or not page_text.isdigit():
        raise VPAutoWatchError("VPauto pagination link is malformed")
    page = int(page_text)
    if page < 1 or page > MAX_LISTING_PAGES:
        raise VPAutoWatchError("VPauto pagination page exceeds the safety limit")
    query = urlencode((("sale", sale_id), ("page", str(page))))
    return f"{BASE_URL}/pro/vehicle/list?{query}", sale_id


def normalized_vehicle_url(href: str) -> str:
    parsed = urlsplit(urljoin(BASE_URL, clean(href)))
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"vpauto.eu", "www.vpauto.eu"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/vehicle/")
        or parsed.path.count("/") < 2
    ):
        raise VPAutoWatchError("VPauto vehicle card has an invalid canonical URL")
    return f"{BASE_URL}{parsed.path}"


def offer_token_from_url(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "vpauto.eu"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(parts) != 3
        or parts[0] != "vehicle"
        or not OFFER_TOKEN_RE.fullmatch(parts[1])
        or not parts[2]
    ):
        raise VPAutoWatchError("VPauto vehicle URL has no stable public offer token")
    return parts[1]


def global_listing_url(page: int) -> str:
    if page < 1 or page > MAX_LISTING_PAGES:
        raise VPAutoWatchError("VPauto global page exceeds the safety limit")
    return f"{BASE_URL}/pro/vehicle/list?page={page}"


def global_page_number(href: str) -> int:
    parsed = urlsplit(urljoin(BASE_URL, clean(href)))
    values = parse_qs(parsed.query, keep_blank_values=True)
    page_values = values.get("page") or []
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"vpauto.eu", "www.vpauto.eu"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/pro/vehicle/list"
        or parsed.fragment
        or set(values) != {"page"}
        or len(page_values) != 1
        or not clean(page_values[0]).isdigit()
    ):
        raise VPAutoWatchError("VPauto global pagination link is malformed")
    page = int(clean(page_values[0]))
    if page < 1 or page > MAX_LISTING_PAGES:
        raise VPAutoWatchError("VPauto global pagination page exceeds the safety limit")
    return page


def parse_declared_total(markup: str) -> int:
    match = TOTAL_RE.search(markup)
    if not match:
        raise VPAutoWatchError("VPauto catalogue total is missing")
    total = positive_int(match.group(1))
    if total is None or total > MAX_DECLARED_VEHICLES:
        raise VPAutoWatchError("VPauto catalogue total is invalid")
    return total


def parse_home(markup: str) -> tuple[int, dict[str, SaleSeed]]:
    total = parse_declared_total(markup)
    soup = BeautifulSoup(markup, "html.parser")
    sales: dict[str, SaleSeed] = {}
    for anchor in soup.select('a[href^="/search/sale/"]'):
        href = clean(anchor.get("href"))
        try:
            listing_url, sale_id = normalized_listing_url(href)
        except VPAutoWatchError:
            continue
        label = clean(anchor.get_text(" ", strip=True))
        existing = sales.get(sale_id)
        # The landing page links to the same sale more than once: retain the
        # descriptive title rather than "Access the sale".
        if existing is None or len(label) > len(existing.sale_name):
            sales[sale_id] = SaleSeed(sale_id, listing_url, label or f"VPauto sale {sale_id}")
    if total and not sales:
        raise VPAutoWatchError("VPauto catalogue exposes no sale routes")
    return total, sales


def title_from_card(card: Tag) -> tuple[str, str]:
    brand_node = card.select_one(".elmt-marque h2")
    model_node = card.select_one(".elmt-modele h3")
    brand = clean(brand_node.get_text(" ", strip=True) if brand_node else "")
    model = clean(model_node.get_text(" ", strip=True) if model_node else "")
    if not model:
        model = clean(card.get("data-model") or "")
    if brand and model:
        title = model if model.casefold().startswith(brand.casefold()) else f"{brand} {model}"
    else:
        title = brand or model
    if not title:
        raise VPAutoWatchError("VPauto card has no public vehicle title")
    return title, model or title


def parse_card(
    card: Tag,
    *,
    sale: SaleSeed,
    observed_at: str,
) -> dict[str, Any]:
    vehicle_id = clean(card.get("data-vehicle-etincelle-id"))
    if not vehicle_id.isdigit():
        raise VPAutoWatchError("VPauto card has no stable public vehicle ID")
    link = card.select_one('a[href^="/vehicle/"]')
    if link is None:
        raise VPAutoWatchError(f"VPauto vehicle {vehicle_id} has no canonical card link")
    url = normalized_vehicle_url(clean(link.get("href")))
    offer_token = offer_token_from_url(url)
    title, model = title_from_card(card)
    card_text = clean(card.get_text(" ", strip=True))
    year_match = YEAR_RE.search(card_text)
    mileage_match = MILEAGE_RE.search(card_text)
    location_node = card.select_one(".elmt-ville")
    location = clean(location_node.get_text(" ", strip=True) if location_node else "")
    location = re.sub(r"^LOC\s*:\s*", "", location, flags=re.I).strip()
    return {
        "id": f"{SOURCE_KEY}:{vehicle_id}:{offer_token}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "source_vehicle_id": vehicle_id,
        "source_offer_id": offer_token,
        "url": url,
        "title": title,
        "model": model,
        "country": "FR",
        "asset_country": "FR",
        "category": "vehicle",
        "category_raw": "vehicle",
        "year": int(year_match.group(1)) if year_match else None,
        "mileage_km": positive_int(mileage_match.group(1)) if mileage_match else None,
        "fuel": "unknown",
        # Anonymous VPauto pages do not label a public auction price. Do not
        # copy unlabelled tracking values into a user-visible price filter.
        "price_amount": None,
        "price_currency": "EUR",
        "price_eur": None,
        "price_kind": "unknown",
        "price_label": "No public auction price labelled on the anonymous VPauto card.",
        "bid_visibility": "not_publicly_disclosed",
        "seller": SOURCE_NAME,
        "location": location,
        "sale_name": sale.sale_name,
        "sale_id": sale.sale_id,
        "canonical_end_utc": None,
        "sale_end_utc": None,
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public VPauto catalogue listing; confirm vehicle condition, auction "
            "terms, fees, buyer requirements, and import eligibility before bidding."
        ),
        "access_sale_note": "VPauto bidding and detailed buyer access may require a professional account.",
        "auction_status": "listed",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-catalogue:{vehicle_id}:{offer_token}",
        "evidence": "Public VPauto PRO sale listing card.",
    }


def parse_listing_page(
    markup: str,
    *,
    sale: SaleSeed,
    observed_at: str,
) -> ListingPage:
    soup = BeautifulSoup(markup, "html.parser")
    cards = soup.select("article.element[data-vehicle-etincelle-id]")
    if not cards:
        raise VPAutoWatchError(f"VPauto sale {sale.sale_id} page has no vehicle cards")
    rows = [parse_card(card, sale=sale, observed_at=observed_at) for card in cards]
    page_ids = [row["id"] for row in rows]
    if len(page_ids) != len(set(page_ids)):
        raise VPAutoWatchError(f"VPauto sale {sale.sale_id} page duplicates a vehicle ID")
    next_urls: set[str] = set()
    for anchor in soup.select("nav.pagination a[href]"):
        href = clean(anchor.get("href"))
        parsed = urlsplit(urljoin(BASE_URL, href))
        if parsed.path != "/pro/vehicle/list" or "sale=" not in parsed.query:
            continue
        url, link_sale_id = normalized_listing_url(href)
        if link_sale_id != sale.sale_id:
            raise VPAutoWatchError("VPauto pagination crossed into another sale")
        next_urls.add(url)
    return ListingPage(sale.sale_id, rows, tuple(sorted(next_urls)))


def parse_global_listing_page(
    markup: str,
    *,
    observed_at: str,
) -> GlobalListingPage:
    soup = BeautifulSoup(markup, "html.parser")
    cards = soup.select("article.element[data-vehicle-etincelle-id]")
    if not cards:
        raise VPAutoSnapshotChanged("VPauto global catalogue page has no offer cards")
    catalogue = SaleSeed("", GLOBAL_LISTING_URL, "VPauto public catalogue")
    rows = [parse_card(card, sale=catalogue, observed_at=observed_at) for card in cards]
    offer_tokens = [str(row["source_offer_id"]) for row in rows]
    if len(offer_tokens) != len(set(offer_tokens)):
        raise VPAutoSnapshotChanged("VPauto global page repeats a stable offer token")
    linked_pages = {
        global_page_number(clean(anchor.get("href")))
        for anchor in soup.select("nav.pagination a[href]")
    }
    return GlobalListingPage(tuple(rows), tuple(sorted(linked_pages)))


def global_catalogue_pass(
    session: requests.Session,
    *,
    declared: int,
    observed_at: str,
    timeout: int,
) -> CataloguePass:
    rows_by_id: dict[str, dict[str, Any]] = {}
    token_identity: dict[str, tuple[str, str]] = {}
    pending_pages = {1}
    visited_pages: set[int] = set()
    while pending_pages:
        page = min(pending_pages)
        pending_pages.remove(page)
        if page in visited_pages:
            raise VPAutoSnapshotChanged("VPauto global page was scheduled twice")
        if len(visited_pages) >= MAX_LISTING_PAGES:
            raise VPAutoSnapshotChanged("VPauto global pagination exceeds the safety limit")
        markup = fetch_markup(session, global_listing_url(page), timeout=timeout)
        page_data = parse_global_listing_page(markup, observed_at=observed_at)
        visited_pages.add(page)
        pending_pages.update(
            linked_page
            for linked_page in page_data.linked_pages
            if linked_page not in visited_pages
        )
        if len(visited_pages | pending_pages) > MAX_LISTING_PAGES:
            raise VPAutoSnapshotChanged("VPauto global pagination exceeds the safety limit")
        for row in page_data.rows:
            row_id = str(row["id"])
            offer_token = str(row["source_offer_id"])
            identity = (str(row["source_vehicle_id"]), str(row["url"]))
            if offer_token in token_identity or row_id in rows_by_id:
                raise VPAutoSnapshotChanged("VPauto global pages repeat a stable offer")
            token_identity[offer_token] = identity
            rows_by_id[row_id] = row
            if len(rows_by_id) > MAX_DECLARED_VEHICLES:
                raise VPAutoSnapshotChanged("VPauto global catalogue exceeds the safety limit")
    expected_pages = set(range(1, max(visited_pages) + 1))
    if visited_pages != expected_pages:
        raise VPAutoSnapshotChanged("VPauto global pagination graph has a page gap")
    if len(rows_by_id) < declared:
        raise VPAutoSnapshotChanged(
            f"VPauto total/offer reconciliation failed: declared={declared} unique={len(rows_by_id)}"
        )
    rows = tuple(rows_by_id[row_id] for row_id in sorted(rows_by_id))
    physical_ids = len({str(row["source_vehicle_id"]) for row in rows})
    return CataloguePass(len(visited_pages), rows, physical_ids)


def parse_detail(markup: str, *, expected_vehicle_id: str) -> tuple[str, str | None]:
    """Return explicit public fuel and a timezone-aware sale end, if exposed."""
    detail_id = DETAIL_ID_RE.search(markup)
    if detail_id and detail_id.group(1) != expected_vehicle_id:
        raise VPAutoWatchError("VPauto detail page does not match its catalogue vehicle ID")
    energy = DETAIL_ENERGY_RE.search(markup)
    fuel = normalize_fuel(html.unescape(energy.group(1))) if energy else "unknown"
    end_match = DETAIL_END_RE.search(markup)
    sale_end = clean(html.unescape(end_match.group(1))) if end_match else ""
    if sale_end and not AWARE_ISO_RE.fullmatch(sale_end):
        sale_end = ""
    return fuel, sale_end or None


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    fetch_details: bool = True,
) -> dict[str, Any]:
    if timeout < 5 or workers < 1 or workers > 24:
        raise ValueError("invalid VPauto timeout/workers")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_at = now.astimezone(UTC).isoformat()
    supplied_session = session
    root_session = session or configured_session()
    def fetch_worker_markup(url: str) -> str:
        # Detail pages are canonical URLs and do not depend on catalogue state.
        # Isolate concurrent detail requests from the global crawl's cookie.
        if supplied_session is not None:
            return fetch_markup(supplied_session, url, timeout=timeout)
        local = configured_session()
        try:
            return fetch_markup(local, url, timeout=timeout)
        finally:
            local.close()

    first_home = fetch_markup(root_session, CATALOGUE_URL, timeout=timeout)
    declared, sales = parse_home(first_home)
    if not declared:
        raise VPAutoWatchError("VPauto declared zero vehicles")
    previous: CataloguePass | None = None
    stable: CataloguePass | None = None
    transient_retries = 0
    attempts_used = 0
    last_problem = "complete global catalogue fingerprints kept changing"
    for attempts_used in range(1, MAX_RECONCILIATION_ATTEMPTS + 1):
        try:
            current = global_catalogue_pass(
                root_session,
                declared=declared,
                observed_at=observed_at,
                timeout=timeout,
            )
        except VPAutoSnapshotChanged as exc:
            transient_retries += 1
            last_problem = str(exc)
            previous = None
            continue
        if previous is not None and previous.fingerprint == current.fingerprint:
            stable = current
            break
        previous = current
    if stable is None:
        raise VPAutoWatchError(
            f"VPauto global catalogue did not stabilize after "
            f"{MAX_RECONCILIATION_ATTEMPTS} attempts: {last_problem}"
        )

    rows = list(stable.rows)
    vehicle_rows = {str(row["id"]): row for row in rows}
    catalogue_count_delta = len(rows) - declared
    count_reconciliation = (
        "exact" if catalogue_count_delta == 0 else "landing_total_lags_unique_cards"
    )
    detail_ok = 0
    detail_errors = 0
    if fetch_details:
        def fetch_detail(row: dict[str, Any]) -> tuple[str, str, str | None]:
            markup = fetch_worker_markup(row["url"])
            vehicle_id = str(row["source_vehicle_id"])
            fuel, sale_end = parse_detail(markup, expected_vehicle_id=vehicle_id)
            return row["id"], fuel, sale_end

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_detail, row): row["id"] for row in rows}
            for future in concurrent.futures.as_completed(futures):
                row_id = futures[future]
                try:
                    _row_id, fuel, sale_end = future.result()
                    if _row_id != row_id:
                        raise VPAutoWatchError("VPauto detail response has an unstable row identity")
                    row = vehicle_rows[row_id]
                    row["fuel"] = fuel
                    if sale_end:
                        row["canonical_end_utc"] = sale_end
                        row["sale_end_utc"] = sale_end
                    detail_ok += 1
                except VPAutoWatchError:
                    raise
                except (requests.RequestException, OSError, ValueError):
                    # Catalogue-card identity is complete and remains usable;
                    # an optional public detail timeout becomes unknown data,
                    # never an invented fuel, price, or deadline.
                    detail_errors += 1

    final_home = fetch_markup(root_session, CATALOGUE_URL, timeout=timeout)
    final_declared, final_sales = parse_home(final_home)

    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public offer card on the seeded VPauto PRO global catalogue",
        "declared": declared,
        "declared_rechecked": final_declared,
        "declared_total_changed_during_scan": final_declared != declared,
        "catalogue_count_delta": catalogue_count_delta,
        "count_reconciliation": count_reconciliation,
        "visited_listing_pages": stable.pages,
        "sale_routes_at_start": len(sales),
        "sale_routes_rechecked": len(final_sales),
        "normalized_rows": len(rows),
        "stable_ids_unique": True,
        "offer_identity": "vehicle_id+canonical_offer_token",
        "identity_schema_version": 2,
        "identity_migration": "physical_vehicle_id_to_vehicle_id_plus_offer_token",
        "physical_vehicle_ids": stable.physical_vehicle_ids,
        "cross_listed_offer_rows": len(rows) - stable.physical_vehicle_ids,
        "full_catalogue_rechecked": True,
        "reconciliation_attempts": attempts_used,
        "transient_snapshot_retries": transient_retries,
        "home_rechecked": True,
        "detail_pages_ok": detail_ok,
        "detail_pages_unavailable": detail_errors,
        "publication_ready": False,
    }
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": observed_at,
        "research_only": True,
        "publication_status": "review_required",
        "row_count": len(rows),
        "rows": rows,
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
    parser = argparse.ArgumentParser(description="Fetch every public VPauto PRO vehicle card")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--skip-details",
        action="store_true",
        help="Use catalogue cards only; public detail values remain unknown.",
    )
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(
        timeout=args.timeout,
        workers=args.workers,
        fetch_details=not args.skip_details,
    )
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "VPAUTO_WATCH_PASS",
        "row_count": payload["row_count"],
        "sale_routes": payload["source_reports"][SOURCE_KEY]["sale_routes_at_start"],
        "pages": payload["source_reports"][SOURCE_KEY]["visited_listing_pages"],
        "detail_pages_ok": payload["source_reports"][SOURCE_KEY]["detail_pages_ok"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
