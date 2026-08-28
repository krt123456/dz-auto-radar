#!/usr/bin/env python3
"""Reconcile every currently public vehicle card from CarAukce.cz.

CarAukce exposes its active and scheduled vehicle catalogue as a numbered,
server-rendered public index.  The heading declares the total number of
vehicles and the pagination exposes every page.  This connector reads every
page twice, requires the announced total to match the unique stable listing
IDs, and retains the first pass only when the second pass has the same
catalogue fingerprint.  A transient change therefore fails closed instead of
publishing a partial Czech auction snapshot.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
CZECHIA = ZoneInfo("Europe/Prague")
SOURCE_KEY = "caraukce"
SOURCE_NAME = "CarAukce"
ROOT_URL = "https://www.caraukce.cz"
CATALOGUE_URL = f"{ROOT_URL}/vozidla?ajax=1"
DEFAULT_TIMEOUT = 35

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "cs,en;q=0.8",
}
COUNT_RE = re.compile(r"vehicle-results__count[^>]*>\s*([0-9\s]+)\s*<", re.I)
LISTING_ID_RE = re.compile(r"^/item/(\d+)$")
END_RE = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
MILEAGE_RE = re.compile(r"\b([0-9\s]{1,16})\s*km\b", re.I)


class CarAukceWatchError(RuntimeError):
    """The public CarAukce catalogue was incomplete or internally inconsistent."""


@dataclass(frozen=True)
class ParsedPage:
    page: int
    announced_total: int
    pages: tuple[int, ...]
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Catalogue:
    announced_total: int
    pages: tuple[int, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def fingerprint(self) -> tuple[int, tuple[int, ...], tuple[str, ...]]:
        return (
            self.announced_total,
            self.pages,
            tuple(str(row["id"]) for row in self.rows),
        )


def clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def positive_integer(value: Any) -> int | None:
    digits = re.sub(r"[^0-9]", "", clean(value))
    if not digits:
        return None
    parsed = int(digits)
    return parsed if parsed > 0 else None


def listing_url(page: int) -> str:
    return f"{CATALOGUE_URL}&strana={page}"


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


def fetch_markup(session: requests.Session, page: int, *, timeout: int) -> str:
    response = session.get(listing_url(page), timeout=timeout)
    try:
        response.raise_for_status()
        markup = response.text
    finally:
        response.close()
    if not markup or "vehicle-results__count" not in markup:
        raise CarAukceWatchError(f"CarAukce page {page} is not the public vehicle catalogue")
    return markup


def parse_announced_total(markup: str) -> int:
    match = COUNT_RE.search(markup)
    if match is None:
        raise CarAukceWatchError("CarAukce catalogue does not declare its vehicle count")
    total = positive_integer(match.group(1))
    if total is None:
        raise CarAukceWatchError("CarAukce catalogue declares an invalid vehicle count")
    return total


def parse_pages(soup: BeautifulSoup) -> tuple[int, ...]:
    numbers = {1}
    for link in soup.select('a[href*="strana="]'):
        href = str(link.get("href") or "")
        query = parse_qs(urlparse(urljoin(ROOT_URL, href)).query)
        for value in query.get("strana", ()):
            if value.isdigit() and int(value) >= 1:
                numbers.add(int(value))
    pages = tuple(sorted(numbers))
    if pages != tuple(range(1, pages[-1] + 1)):
        raise CarAukceWatchError("CarAukce pagination has a missing page number")
    return pages


def normalize_fuel(values: list[str]) -> str:
    text = " ".join(values).casefold()
    if "hybrid" in text:
        return "hybrid"
    if "benz" in text:
        return "petrol"
    if "nafta" in text or "diesel" in text:
        return "diesel"
    if "elekt" in text:
        return "electric"
    if "lpg" in text:
        return "lpg"
    if "cng" in text:
        return "cng"
    return "unknown"


def parse_end(value: str) -> dt.datetime:
    match = END_RE.search(clean(value))
    if match is None:
        raise CarAukceWatchError(f"CarAukce card has no valid end time: {value!r}")
    try:
        local = dt.datetime.strptime(match.group(1), "%d.%m.%Y %H:%M")
    except ValueError as exc:
        raise CarAukceWatchError(f"CarAukce card has an invalid end time: {value!r}") from exc
    return local.replace(tzinfo=CZECHIA).astimezone(UTC)


def text_or_empty(node: Tag | None) -> str:
    return clean(node.get_text(" ", strip=True) if node is not None else "")


def card_to_row(card: Tag, *, observed_at: str, now: dt.datetime) -> dict[str, Any]:
    detail = card.select_one('a[href^="/item/"]')
    href = str(detail.get("href") or "") if detail is not None else ""
    matched = LISTING_ID_RE.fullmatch(href)
    if matched is None:
        raise CarAukceWatchError("CarAukce card has no stable /item/ listing URL")
    listing_id = matched.group(1)
    title = text_or_empty(card.select_one("h3.vehicle-card__title"))
    if not title:
        title = clean(detail.get("title") if detail is not None else "")
    if not title:
        raise CarAukceWatchError(f"CarAukce listing {listing_id} has no title")

    parameters = [text_or_empty(item) for item in card.select("ul.vehicle-card__params li")]
    year_match = YEAR_RE.search(" ".join(parameters))
    mileage_match = None
    for parameter in parameters:
        candidate = MILEAGE_RE.search(parameter)
        if candidate is not None:
            mileage_match = candidate
            break
    year = int(year_match.group(1)) if year_match else None
    mileage = positive_integer(mileage_match.group(1)) if mileage_match else None
    fuel = normalize_fuel(parameters)

    price_box = card.select_one(".vehicle-card__price")
    price_title = text_or_empty(
        price_box.select_one(".vehicle-card__price-title") if price_box is not None else None
    )
    price_value = text_or_empty(price_box.find("strong") if price_box is not None else None)
    price_amount = positive_integer(price_value)
    price_text = f"{price_title} {price_value}".casefold()
    if price_amount is None:
        price_kind = "unknown"
        price_currency = ""
        price_label = "CarAukce card does not state a public bid or guide price"
    elif "vyvol" in price_text or "počáte" in price_text:
        price_kind = "starting_bid"
        price_currency = "CZK"
        price_label = f"CarAukce {price_title}"
    elif "orienta" in price_text or "odhad" in price_text:
        price_kind = "guide_price"
        price_currency = "CZK"
        price_label = f"CarAukce {price_title}"
    else:
        raise CarAukceWatchError(
            f"CarAukce listing {listing_id} exposes an unlabelled price: {price_title!r}"
        )

    end = parse_end(text_or_empty(card.select_one(".vehicle-card__auction")))
    if end <= now:
        raise CarAukceWatchError(
            f"CarAukce listing {listing_id} is already ended while still in the active catalogue"
        )
    flag = text_or_empty(card.select_one(".vehicle-card__flag"))
    image = card.select_one("img[src]")
    image_url = urljoin(ROOT_URL, str(image.get("src") or "")) if image else ""
    canonical_url = urljoin(ROOT_URL, href)
    return {
        "id": f"{SOURCE_KEY}:{listing_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": canonical_url,
        "title": title,
        "model": title,
        "country": "CZ",
        "asset_country": "CZ",
        "category": "vehicle",
        "category_raw": flag or "vehicle",
        "year": year,
        "mileage": mileage,
        "mileage_km": mileage,
        "fuel": fuel,
        "seller": SOURCE_NAME,
        "image_url": image_url or None,
        "price_amount": price_amount,
        "price_currency": price_currency,
        "price_eur": None,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public CarAukce catalogue card",
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": flag or "auction format shown on official CarAukce card",
        "auction_status": "active",
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official CarAukce vehicle listing. Confirm vehicle condition, auction "
            "terms, fees, buyer requirements, and Algerian import eligibility before bidding."
        ),
        "access_sale_note": "Open the official CarAukce listing to inspect all auction terms and documents.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-catalogue:{listing_id}",
        "evidence": "Official CarAukce public active-vehicle catalogue card.",
    }


def parse_page(
    markup: str, *, page: int, observed_at: str, now: dt.datetime
) -> ParsedPage:
    soup = BeautifulSoup(markup, "html.parser")
    cards = soup.select("div.vehicle-card")
    if not cards:
        raise CarAukceWatchError(f"CarAukce page {page} contains no vehicle cards")
    rows = tuple(card_to_row(card, observed_at=observed_at, now=now) for card in cards)
    ids = [str(row["id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise CarAukceWatchError(f"CarAukce page {page} repeats a stable listing ID")
    return ParsedPage(
        page=page,
        announced_total=parse_announced_total(markup),
        pages=parse_pages(soup),
        rows=rows,
    )


def enumerate_catalogue(
    session: requests.Session, *, observed_at: str, now: dt.datetime, timeout: int
) -> Catalogue:
    first = parse_page(
        fetch_markup(session, 1, timeout=timeout),
        page=1,
        observed_at=observed_at,
        now=now,
    )
    parsed_pages: list[ParsedPage] = [first]
    for page in first.pages[1:]:
        parsed = parse_page(
            fetch_markup(session, page, timeout=timeout),
            page=page,
            observed_at=observed_at,
            now=now,
        )
        if parsed.announced_total != first.announced_total or parsed.pages != first.pages:
            raise CarAukceWatchError("CarAukce catalogue changed while its pages were enumerated")
        parsed_pages.append(parsed)
    rows = tuple(row for parsed in parsed_pages for row in parsed.rows)
    ids = [str(row["id"]) for row in rows]
    urls = [str(row["url"]) for row in rows]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise CarAukceWatchError("CarAukce full catalogue has duplicate stable identities")
    if len(rows) != first.announced_total:
        raise CarAukceWatchError(
            "CarAukce announced vehicle total does not equal every enumerated public card "
            f"({first.announced_total} != {len(rows)})"
        )
    return Catalogue(
        announced_total=first.announced_total,
        pages=first.pages,
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
            raise CarAukceWatchError("CarAukce catalogue changed during final reconciliation")
        report = {
            "status": "ok",
            "connector_status": "ok",
            "catalogue_scope": "every public current or scheduled CarAukce vehicle card",
            "declared": first.announced_total,
            "publicly_listed": len(first.rows),
            "visited": len(first.rows),
            "normalized_rows": len(first.rows),
            "pages": len(first.pages),
            "page_numbers": list(first.pages),
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
    parser = argparse.ArgumentParser(description="Fetch every public CarAukce vehicle card")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "CARAUKCE_WATCH_PASS",
        "row_count": payload["row_count"],
        "pages": payload["source_reports"][SOURCE_KEY]["pages"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
