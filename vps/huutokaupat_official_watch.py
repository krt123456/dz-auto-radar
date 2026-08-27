#!/usr/bin/env python3
"""Enumerate every public Huutokaupat vehicle-and-accessories category card.

The public category page states a finite Finnish total ("N ilmoitusta") and
exposes all page numbers through normal ?sivu=N navigation. The connector
checks every page and only writes when unique public auction IDs equal that
declared total. Fuel, bid count, and price come only from visible card fields.
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
SOURCE_KEY = "huutokaupat"
SOURCE_NAME = "Huutokaupat.com"
SOURCE_URL = "https://huutokaupat.com/osasto/ajoneuvot-ja-tarvikkeet"
DEFAULT_TIMEOUT = 35
DEFAULT_WORKERS = 4
MAX_PAGES = 1_000

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
}
TOTAL_RE = re.compile(
    r"([0-9][0-9\s,.\u00a0]*)\s+ilmoitusta\s*,\s*sivu\s+([0-9]+)",
    re.I,
)
PAGE_COUNT_RE = re.compile(r'aria-label="Sivu\s+[0-9]+\s*/\s*([0-9]+)"', re.I)
CARD_ID_RE = re.compile(r"^entry-card-([0-9]+)$")
LOT_PATH_RE = re.compile(r"^/kohde/([0-9]+)(?:/[^/?#]+)?$")
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
MILEAGE_RE = re.compile(r"\b([0-9][0-9\s,.\u00a0]*)\s*km\b", re.I)
PRICE_RE = re.compile(r"(?<![0-9])([0-9][0-9\s,.\u00a0]*)\s*(?:\u20ac|eur)", re.I)
BID_RE = re.compile(r"\b([0-9][0-9\s,.\u00a0]*)\s+tarjousta\b", re.I)
LOCATION_RE = re.compile(r",\s*(?:19[7-9]\d|20[0-2]\d)\s*,\s*(.+)$")


class HuutokaupatWatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedPage:
    total: int
    current_page: int
    page_count: int
    rows: list[dict[str, Any]]


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def ascii_fold(value: Any) -> str:
    value = unicodedata.normalize("NFKD", clean(value))
    return "".join(char for char in value if not unicodedata.combining(char)).casefold()


def positive_number(value: Any) -> int | float | None:
    compact = re.sub(r"[^0-9,.-]", "", clean(value))
    if not compact:
        return None
    if "," in compact and "." in compact:
        compact = compact.replace(".", "").replace(",", ".") if compact.rfind(",") > compact.rfind(".") else compact.replace(",", "")
    elif "," in compact:
        tail = compact.rsplit(",", 1)[-1]
        compact = compact.replace(",", ".") if len(tail) <= 2 else compact.replace(",", "")
    try:
        parsed = float(compact)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def normalize_fuel(value: Any) -> str:
    folded = ascii_fold(value)
    if not folded:
        return "unknown"
    diesel = bool(re.search(r"\b(?:diesel|kaasuoljy)\b", folded))
    petrol = bool(re.search(r"\b(?:bensiini|petrol|gasoline|essence)\b", folded))
    hybrid = bool(re.search(r"\b(?:hybridi|hybrid|plug-in)\b", folded))
    electric = bool(re.search(r"\b(?:sahko|electric|electrique)\b", folded))
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
    if re.search(r"\b(?:kaasu|lpg|cng)\b", folded):
        return "gas"
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
        raise HuutokaupatWatchError("Huutokaupat page exceeds the safety limit")
    return f"{SOURCE_URL}?{urlencode((('sivu', str(page)),))}"


def parse_metadata(markup: str) -> tuple[int, int, int]:
    text = clean(BeautifulSoup(markup, "html.parser").get_text(" ", strip=True))
    total_match = TOTAL_RE.search(text)
    page_match = PAGE_COUNT_RE.search(markup)
    if total_match is None or page_match is None:
        raise HuutokaupatWatchError("Huutokaupat category metadata is missing")
    total = positive_number(total_match.group(1))
    current_page = int(total_match.group(2))
    page_count = int(page_match.group(1))
    if (
        total is None or int(total) != total or current_page < 1
        or page_count < current_page or page_count > MAX_PAGES
    ):
        raise HuutokaupatWatchError("Huutokaupat category metadata is invalid")
    return int(total), current_page, page_count


def canonical_lot_url(href: str, *, expected_id: str) -> str:
    parsed = urlsplit(urljoin(SOURCE_URL, clean(href)))
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"huutokaupat.com", "www.huutokaupat.com"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HuutokaupatWatchError("Huutokaupat card leaves the official domain")
    match = LOT_PATH_RE.fullmatch(parsed.path)
    if match is None or match.group(1) != expected_id:
        raise HuutokaupatWatchError("Huutokaupat card has an invalid lot URL")
    return f"https://huutokaupat.com{parsed.path}"


def parse_card(card: Tag, *, observed_at: str) -> dict[str, Any]:
    id_match = CARD_ID_RE.fullmatch(clean(card.get("data-test")))
    if id_match is None:
        raise HuutokaupatWatchError("Huutokaupat card has no stable lot ID")
    lot_id = id_match.group(1)
    link = card.select_one(f'a[data-test="entry-card-link-{lot_id}"]') or card.select_one('a[href^="/kohde/"]')
    if link is None:
        raise HuutokaupatWatchError(f"Huutokaupat lot {lot_id} has no card link")
    url = canonical_lot_url(clean(link.get("href")), expected_id=lot_id)
    title = clean(link.get_text(" ", strip=True))
    if not title:
        raise HuutokaupatWatchError(f"Huutokaupat lot {lot_id} has no title")
    card_text = clean(card.get_text(" ", strip=True))
    year_match = YEAR_RE.search(title) or YEAR_RE.search(card_text)
    mileage_match = MILEAGE_RE.search(card_text)
    price_match = PRICE_RE.search(card_text)
    bid_match = BID_RE.search(card_text)
    location_match = LOCATION_RE.search(title)
    price = positive_number(price_match.group(1)) if price_match else None
    return {
        "id": f"{SOURCE_KEY}:{lot_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": url,
        "title": title,
        "model": title,
        "country": "FI",
        "asset_country": "FI",
        "category": "vehicle_related",
        "category_raw": "Ajoneuvot ja tarvikkeet",
        "year": int(year_match.group(1)) if year_match else None,
        "mileage_km": positive_number(mileage_match.group(1)) if mileage_match else None,
        "fuel": normalize_fuel(card_text),
        "price_amount": price,
        "price_currency": "EUR",
        "price_eur": price,
        "price_kind": "current_bid" if price is not None else "unknown",
        "price_label": "public current auction amount" if price is not None else "price not shown",
        "bid_visibility": "catalogue summary",
        "bid_count": int(positive_number(bid_match.group(1))) if bid_match else None,
        "seller": SOURCE_NAME,
        "location": clean(location_match.group(1)) if location_match else "",
        "canonical_end_utc": None,
        "sale_end_utc": None,
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": "Public Huutokaupat listing; confirm lot type, condition, terms, fees, buyer requirements, and import eligibility before bidding.",
        "access_sale_note": "Huutokaupat auction participation requires the platform's buyer process.",
        "auction_status": "ended" if "paattynyt" in ascii_fold(card_text) else "active",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-category",
        "evidence": "Public Huutokaupat vehicle-and-accessories category card.",
    }


def parse_page(markup: str, *, observed_at: str) -> ParsedPage:
    total, current_page, page_count = parse_metadata(markup)
    soup = BeautifulSoup(markup, "html.parser")
    cards = soup.select('article[data-test^="entry-card-"]')
    rows = [parse_card(card, observed_at=observed_at) for card in cards]
    ids = [row["id"] for row in rows]
    if not rows or len(ids) != len(set(ids)):
        raise HuutokaupatWatchError("Huutokaupat page has missing or duplicate cards")
    return ParsedPage(total, current_page, page_count, rows)


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    if timeout < 5 or workers < 1 or workers > 16:
        raise ValueError("invalid Huutokaupat timeout/workers")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_at = now.astimezone(UTC).isoformat()
    supplied_session = session
    root_session = session or configured_session()
    first_markup = fetch_markup(root_session, SOURCE_URL, timeout=timeout)
    first = parse_page(first_markup, observed_at=observed_at)
    page_size = len(first.rows)
    expected_pages = math.ceil(first.total / page_size)
    if first.page_count != expected_pages or first.current_page != 1:
        raise HuutokaupatWatchError("Huutokaupat total/pagination metadata mismatch")

    def fetch_and_parse(page: int) -> tuple[int, list[dict[str, Any]]]:
        local_session = supplied_session or configured_session()
        try:
            markup = fetch_markup(local_session, page_url(page), timeout=timeout)
        finally:
            if supplied_session is None:
                local_session.close()
        parsed = parse_page(markup, observed_at=observed_at)
        expected_rows = page_size if page < expected_pages else first.total - page_size * (page - 1)
        if (
            parsed.total != first.total or parsed.page_count != first.page_count
            or parsed.current_page != page or len(parsed.rows) != expected_rows
        ):
            raise HuutokaupatWatchError(f"Huutokaupat page {page} failed reconciliation")
        return page, parsed.rows

    pages: dict[int, list[dict[str, Any]]] = {1: first.rows}
    if expected_pages > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_and_parse, page): page for page in range(2, expected_pages + 1)}
            for future in concurrent.futures.as_completed(futures):
                page, rows = future.result()
                pages[page] = rows
    rows = [row for page in range(1, expected_pages + 1) for row in pages[page]]
    ids = [row["id"] for row in rows]
    urls = [row["url"] for row in rows]
    if len(rows) != first.total or len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise HuutokaupatWatchError("Huutokaupat total/ID reconciliation failed")
    final = parse_page(fetch_markup(root_session, SOURCE_URL, timeout=timeout), observed_at=observed_at)
    if final.total != first.total or final.page_count != first.page_count or [row["id"] for row in final.rows] != [row["id"] for row in first.rows]:
        raise HuutokaupatWatchError("Huutokaupat category changed before final check")
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public card in the vehicle-and-accessories category",
        "declared": first.total,
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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch every public Huutokaupat category card")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout, workers=args.workers)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "HUUTOKAUPAT_WATCH_PASS",
        "row_count": payload["row_count"],
        "pages": payload["source_reports"][SOURCE_KEY]["pages"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
