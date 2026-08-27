#!/usr/bin/env python3
"""Enumerate every public Vavato Cars-category lot with a total check.

Vavato's public Cars category exposes a live "N lots" total, normal page
navigation, and a JSON-LD ItemList for each page. This connector uses the
public ItemList for stable title/URL identity and the visible card for asset
location. It walks every page. If Vavato's headline counter temporarily
includes hidden or just-removed lots, the connector keeps every publicly
enumerable lot only after a second identical full pass and records the exact
counter gap rather than silently publishing a partial snapshot.
"""
from __future__ import annotations

import argparse
import concurrent.futures
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
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "vavato"
SOURCE_NAME = "Vavato"
SOURCE_URL = "https://www.vavato.com/en/c/transport-logistics/cars/5196727d-c14f-48dc-a2f0-e75f50094a52"
DEFAULT_TIMEOUT = 35
DEFAULT_WORKERS = 4
MAX_PAGES = 1_000

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}
TOTAL_RE = re.compile(r"\b([0-9][0-9\s,.\u00a0]*)\s+lots\b", re.I)
PAGE_RE = re.compile(r"^Page\s+([0-9]+)$", re.I)
LOT_PATH_RE = re.compile(r"^/en/l/[^/?#]+$")
LOT_ID_RE = re.compile(r"-(A[0-9]+-[0-9]+-[0-9]+)$", re.I)
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
COUNTRY_RE = re.compile(r",\s*([A-Z]{2})(?:\s|$)")


class VavatoWatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedPage:
    total: int
    page_count: int
    rows: list[dict[str, Any]]


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def ascii_fold(value: Any) -> str:
    value = unicodedata.normalize("NFKD", clean(value))
    return "".join(char for char in value if not unicodedata.combining(char)).casefold()


def positive_int(value: Any) -> int | None:
    compact = re.sub(r"[^0-9]", "", clean(value))
    if not compact:
        return None
    parsed = int(compact)
    return parsed if parsed > 0 else None


def normalize_fuel(value: Any) -> str:
    """Only classify a fuel when its marker is explicit in the public title."""
    folded = ascii_fold(value)
    diesel = bool(re.search(r"\b(?:diesel|gazole)\b", folded))
    petrol = bool(re.search(r"\b(?:petrol|gasoline|essence)\b", folded))
    hybrid = bool(re.search(r"\b(?:hybrid|phev|hev|plug-in)\b", folded))
    electric = bool(re.search(r"\b(?:electric|electrique|bev|e-sprinter)\b", folded))
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
    return "unknown"


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, status=3, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8))
    return session


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def page_url(page: int) -> str:
    if page < 1 or page > MAX_PAGES:
        raise VavatoWatchError("Vavato page exceeds the safety limit")
    return f"{SOURCE_URL}?{urlencode((('page', str(page)),))}"


def canonical_lot_url(value: Any) -> tuple[str, str]:
    parsed = urlsplit(urljoin(SOURCE_URL, clean(value)))
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"vavato.com", "www.vavato.com"}
        or parsed.username is not None
        or parsed.password is not None
        or not LOT_PATH_RE.fullmatch(parsed.path)
    ):
        raise VavatoWatchError("Vavato item has an invalid official lot URL")
    match = LOT_ID_RE.search(parsed.path)
    if match is None:
        raise VavatoWatchError("Vavato item has no stable public lot ID")
    return f"https://www.vavato.com{parsed.path}", match.group(1).upper()


def parse_metadata(soup: BeautifulSoup, *, known_page: int) -> tuple[int, int]:
    text = clean(soup.get_text(" ", strip=True))
    total_match = TOTAL_RE.search(text)
    if total_match is None:
        raise VavatoWatchError("Vavato category total is missing")
    total = positive_int(total_match.group(1))
    pages = [known_page]
    for anchor in soup.select('nav[aria-label="Pagination"] a[href]'):
        match = PAGE_RE.fullmatch(clean(anchor.get_text(" ", strip=True)))
        if match:
            pages.append(int(match.group(1)))
    page_count = max(pages)
    if total is None or page_count < 1 or page_count > MAX_PAGES:
        raise VavatoWatchError("Vavato category metadata is invalid")
    return total, page_count


def jsonld_products(soup: BeautifulSoup) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            document = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        graph = document.get("@graph") if isinstance(document, dict) else None
        if not isinstance(graph, list):
            continue
        for node in graph:
            if not isinstance(node, dict) or node.get("@type") != "CollectionPage":
                continue
            entity = node.get("mainEntity")
            if not isinstance(entity, dict) or entity.get("@type") != "ItemList":
                continue
            entries = entity.get("itemListElement")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                product = entry.get("item") if isinstance(entry, dict) else None
                if isinstance(product, dict) and product.get("@type") == "Product":
                    products.append(product)
    if not products:
        raise VavatoWatchError("Vavato page has no public JSON-LD lot list")
    return products


def card_locations(soup: BeautifulSoup) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for card in soup.select("div.lot-card"):
        link = card.select_one('a[href*="/en/l/"]')
        if link is None:
            continue
        try:
            url, _ = canonical_lot_url(link.get("href"))
        except VavatoWatchError:
            continue
        text = clean(card.get_text(" ", strip=True))
        countries = COUNTRY_RE.findall(text)
        country = countries[-1].upper() if countries else ""
        location = ""
        if country:
            marker = f", {country}"
            before = text.rsplit(marker, 1)[0]
            location = clean(before.rsplit(" ", 5)[-1])
        result[url] = (country, location)
    return result


def parse_page(markup: str, *, observed_at: str, known_page: int = 1) -> ParsedPage:
    soup = BeautifulSoup(markup, "html.parser")
    total, page_count = parse_metadata(soup, known_page=known_page)
    locations = card_locations(soup)
    rows: list[dict[str, Any]] = []
    for product in jsonld_products(soup):
        url, lot_id = canonical_lot_url(product.get("url"))
        title = clean(product.get("name"))
        if not title:
            raise VavatoWatchError(f"Vavato lot {lot_id} has no public title")
        country, location = locations.get(url, ("", ""))
        year_match = YEAR_RE.search(title)
        rows.append({
            "id": f"{SOURCE_KEY}:{lot_id}",
            "source": SOURCE_KEY,
            "source_key": SOURCE_KEY,
            "source_name": SOURCE_NAME,
            "url": url,
            "title": title,
            "model": title,
            "country": country or "BE",
            "asset_country": country or "BE",
            "category": "vehicle",
            "category_raw": "Cars",
            "year": int(year_match.group(1)) if year_match else None,
            "mileage_km": None,
            "fuel": normalize_fuel(title),
            # The public JSON-LD exposes a number but does not label it as a
            # current bid, starting bid, or reserve. Keep the price filter
            # fail-closed instead of inventing auction semantics.
            "price_amount": None,
            "price_currency": "EUR",
            "price_eur": None,
            "price_kind": "unknown",
            "price_label": "Public lot page does not label the JSON-LD amount as an auction price.",
            "bid_visibility": "not_publicly_disclosed",
            "seller": SOURCE_NAME,
            "location": location,
            "canonical_end_utc": None,
            "sale_end_utc": None,
            "sale_event_utc": None,
            "last_seen_at": observed_at,
            "eligibility_status": "review_required",
            "eligibility_reason": "Public Vavato Cars listing; confirm vehicle condition, auction terms, fees, buyer requirements, and import eligibility before bidding.",
            "access_sale_note": "Vavato buyer registration and auction terms apply.",
            "auction_status": "active",
            "adapter_authorized": True,
            "raw_evidence_ref": f"{SOURCE_KEY}:public-cars-category",
            "evidence": "Public Vavato Cars category JSON-LD listing.",
        })
    ids = [row["id"] for row in rows]
    urls = [row["url"] for row in rows]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise VavatoWatchError("Vavato page has duplicate public lot identities")
    return ParsedPage(total, page_count, rows)


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    if timeout < 5 or workers < 1 or workers > 16:
        raise ValueError("invalid Vavato timeout/workers")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_at = now.astimezone(UTC).isoformat()
    supplied_session = session
    root_session = session or configured_session()

    def fetch_root() -> ParsedPage:
        return parse_page(
            fetch_markup(root_session, SOURCE_URL, timeout=timeout),
            observed_at=observed_at,
            known_page=1,
        )

    def collect_pages(first: ParsedPage) -> tuple[list[dict[str, Any]], int, int]:
        page_size = len(first.rows)
        expected_pages = math.ceil(first.total / page_size) if page_size else 0
        if page_size < 1 or first.page_count != expected_pages:
            raise VavatoWatchError("Vavato total/pagination metadata mismatch")

        def fetch_and_parse(page: int) -> tuple[int, list[dict[str, Any]]]:
            local_session = supplied_session or configured_session()
            try:
                markup = fetch_markup(local_session, page_url(page), timeout=timeout)
            finally:
                if supplied_session is None:
                    local_session.close()
            parsed = parse_page(markup, observed_at=observed_at, known_page=page)
            if parsed.total != first.total or parsed.page_count != first.page_count:
                raise VavatoWatchError(f"Vavato page {page} metadata changed")
            if page < expected_pages and len(parsed.rows) != page_size:
                raise VavatoWatchError(f"Vavato page {page} is short before the last page")
            if page == expected_pages and not 1 <= len(parsed.rows) <= page_size:
                raise VavatoWatchError(f"Vavato last page {page} is invalid")
            return page, parsed.rows

        pages: dict[int, list[dict[str, Any]]] = {1: first.rows}
        if expected_pages > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(fetch_and_parse, page): page
                    for page in range(2, expected_pages + 1)
                }
                for future in concurrent.futures.as_completed(futures):
                    page, parsed_rows = future.result()
                    pages[page] = parsed_rows
        rows = [row for page in range(1, expected_pages + 1) for row in pages[page]]
        ids = [row["id"] for row in rows]
        urls = [row["url"] for row in rows]
        if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
            raise VavatoWatchError("Vavato public lot identities are not unique")
        if len(rows) > first.total:
            raise VavatoWatchError("Vavato public lot rows exceed the declared total")
        return rows, page_size, expected_pages

    try:
        first = fetch_root()
        rows, page_size, expected_pages = collect_pages(first)
        final = fetch_root()
        if (
            final.total != first.total
            or final.page_count != first.page_count
            or [row["id"] for row in final.rows] != [row["id"] for row in first.rows]
        ):
            raise VavatoWatchError("Vavato category changed before final check")

        counter_gap = first.total - len(rows)
        second_pass_reconciled = False
        if counter_gap:
            repeated_rows, repeated_page_size, repeated_pages = collect_pages(final)
            if (
                repeated_page_size != page_size
                or repeated_pages != expected_pages
                or [(row["id"], row["url"]) for row in repeated_rows]
                != [(row["id"], row["url"]) for row in rows]
            ):
                raise VavatoWatchError("Vavato visible catalogue changed during counter reconciliation")
            second_pass_reconciled = True

        report = {
            "status": "ok",
            "connector_status": "ok",
            "catalogue_scope": "every publicly enumerable Vavato Cars category lot",
            "declared": first.total,
            "publicly_listed": len(rows),
            "counter_gap": counter_gap,
            "counter_gap_rechecked": second_pass_reconciled,
            "visited": len(rows),
            "normalized_rows": len(rows),
            "page_size": page_size,
            "pages": expected_pages,
            "first_page_rechecked": True,
            "stable_ids_unique": True,
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
    finally:
        if supplied_session is None:
            root_session.close()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch every public Vavato Cars category lot")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout, workers=args.workers)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "VAVATO_WATCH_PASS",
        "row_count": payload["row_count"],
        "pages": payload["source_reports"][SOURCE_KEY]["pages"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
