#!/usr/bin/env python3
"""Reconcile Bilauppboð Iceland's complete public passenger-car catalogue.

The public index contains mixed vehicle types.  Two finite index sweeps must
agree on every listing ID and stable card fact; each stable listing's official
detail page is then read before a row can be classified as a passenger car.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from lxml import etree, html as lxml_html
from lxml.html import HtmlElement
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UTC = dt.timezone.utc
REYKJAVIK = ZoneInfo("Atlantic/Reykjavik")
SOURCE_KEY = "bilauppbod"
SOURCE_NAME = "Bilauppboð"
SOURCE_URL = "https://www.bilauppbod.is/"
DEFAULT_TIMEOUT = 35
DEFAULT_WORKERS = 6
MAX_PAGES = 100
HEADERS = {"User-Agent": "SonarDeals-Auction-Monitor/1.0", "Accept-Language": "is-IS,is;q=0.9,en;q=0.7"}
ITEM_HREF_RE = re.compile(r"^/auction/view/(\d+)$")
PAGE_HREF_RE = re.compile(r"^/\?page=(\d+)$")
DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")

# Explicit models/classes only.  Door count and manufacturer evidence are
# required as an additional source-side passenger-car check.
NON_PASSENGER_RE = re.compile(
    r"\b(?:weinsberg|knaus|hymer|adria|caravan|camper|motorhome|sudwind|trailer|kerru|"
    r"sprinter|transit|transporter|trafic|vito|caddy|ducato|boxer|jumper|master|movano|canter|daily|"
    r"tundra|hilux|d[\s-]?max|navara|l200|amarok|ranger|f[\s-]?150|dodge\s+ram|\bram\s+(?:1500|2500|3500)\b|"
    r"traxter|can[\s-]?am|lynx|ski[\s-]?doo|snowmobile|motorcycle|motorbike|moped|scooter|quad|atv|utv|"
    r"tractor|excavator|forklift|boat|jetski|berlingo|partner|doblo|combo|proace|kangoo|commercial|truck|lorry|bus|minibus)\b",
    re.I,
)


class BilauppbodWatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Card:
    listing_id: str
    url: str
    title: str
    price_label: str
    price_amount: int | None
    end_utc: dt.datetime
    registration_date: str
    mileage_km: int | None
    seller: str
    image_url: str | None

    @property
    def fingerprint(self) -> tuple[str, str, str, str, str, int | None]:
        return (self.listing_id, self.url, self.title, self.end_utc.isoformat(), self.registration_date, self.mileage_km)


@dataclass(frozen=True)
class Detail:
    listing_id: str
    manufacturer: str
    doors: str
    fuel_raw: str
    mileage_km: int | None
    seller: str


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def fold(value: Any) -> str:
    text = clean(value).translate(str.maketrans({"ð": "d", "Ð": "d", "þ": "th", "Þ": "th", "æ": "ae", "Æ": "ae", "ö": "o", "Ö": "o"}))
    return "".join(ch for ch in unicodedata.normalize("NFKD", text).casefold() if not unicodedata.combining(ch))


def digits(value: Any) -> int | None:
    numeric = re.sub(r"[^0-9]", "", clean(value))
    return int(numeric) if numeric else None


def parse_end(date_label: str, time_label: str) -> dt.datetime:
    date_match, time_match = DATE_RE.fullmatch(clean(date_label)), TIME_RE.fullmatch(clean(time_label))
    if date_match is None or time_match is None:
        raise BilauppbodWatchError("Bilauppboð card has invalid public closing time")
    day, month, year = (int(v) for v in date_match.groups())
    hour, minute = (int(v) for v in time_match.groups())
    try:
        return dt.datetime(year, month, day, hour, minute, tzinfo=REYKJAVIK).astimezone(UTC)
    except ValueError as error:
        raise BilauppbodWatchError("Bilauppboð card has impossible public closing time") from error


def parse_card(node: HtmlElement) -> Card:
    href = clean(node.get("href"))
    match = ITEM_HREF_RE.fullmatch(href)
    if match is None:
        raise BilauppbodWatchError("Bilauppboð card has invalid item URL")
    title = clean(node.xpath("string(.//div[contains(concat(' ',normalize-space(@class),' '),' auctionTitle ')])"))
    if not title:
        raise BilauppbodWatchError(f"Bilauppboð card {match.group(1)} has no title")
    end_values = [clean(v) for v in node.xpath(".//span[contains(concat(' ',normalize-space(@class),' '),' aucEndDtTxt ')]/text()") if clean(v)]
    dates = [v for v in end_values if DATE_RE.fullmatch(v)]
    times = [v for v in end_values if TIME_RE.fullmatch(v)]
    if len(dates) != 1 or len(times) != 1:
        raise BilauppbodWatchError(f"Bilauppboð card {match.group(1)} has ambiguous closing time")
    registration_date, mileage_km = "", None
    for prop in node.xpath(".//div[contains(concat(' ',normalize-space(@class),' '),' aucProp ')]"):
        label = fold(prop.xpath("string(.//img/@alt)"))
        value = clean(prop.xpath("string(.//span[contains(concat(' ',normalize-space(@class),' '),' aucPropTxt ')])"))
        if "fyrsti skraningard" in label:
            registration_date = value
        elif "akstur" in label:
            mileage_km = digits(value)
    image = clean(node.xpath("string(.//img[@src][1]/@src)"))
    price_label = clean(node.xpath("string(.//div[contains(concat(' ',normalize-space(@class),' '),' priceTxt ')])"))
    return Card(
        listing_id=match.group(1), url=urljoin(SOURCE_URL, href), title=title,
        price_label=price_label or "current bid not shown on public index card", price_amount=digits(price_label),
        end_utc=parse_end(dates[0], times[0]), registration_date=registration_date, mileage_km=mileage_km,
        seller=clean(node.xpath("string(.//span[contains(concat(' ',normalize-space(@class),' '),' seljandiTxt ')])")),
        image_url=urljoin(SOURCE_URL, image) if image else None,
    )


def parse_page(markup: bytes | str) -> tuple[int, list[Card]]:
    try:
        tree = lxml_html.fromstring(markup)
    except (etree.ParserError, ValueError) as error:
        raise BilauppbodWatchError("Bilauppboð public index markup is invalid") from error
    nodes = tree.xpath("//a[contains(concat(' ',normalize-space(@class),' '),' carCardLink ') and starts-with(@href,'/auction/view/')]")
    if not nodes:
        raise BilauppbodWatchError("Bilauppboð public index contains no auction cards")
    cards = [parse_card(node) for node in nodes]
    if len({card.listing_id for card in cards}) != len(cards):
        raise BilauppbodWatchError("Bilauppboð index page has duplicate listing IDs")
    pages = [1]
    for href in tree.xpath("//a[@href]/@href"):
        match = PAGE_HREF_RE.fullmatch(clean(href))
        if match is not None:
            pages.append(int(match.group(1)))
    page_count = max(pages)
    if page_count > MAX_PAGES:
        raise BilauppbodWatchError("Bilauppboð index exceeds page safety limit")
    return page_count, cards


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, connect=3, read=3, status=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=12))
    return session


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> bytes:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.content


def page_url(page: int) -> str:
    if page < 1:
        raise ValueError("page must be positive")
    return SOURCE_URL if page == 1 else f"{SOURCE_URL}?page={page}"


def fetch_catalogue(*, session: requests.Session, timeout: int) -> tuple[int, list[Card]]:
    page_count, first_cards = parse_page(fetch_markup(session, page_url(1), timeout=timeout))
    page_size, pages = len(first_cards), {1: first_cards}
    for page in range(2, page_count + 1):
        reported_pages, cards = parse_page(fetch_markup(session, page_url(page), timeout=timeout))
        if reported_pages != page_count:
            raise BilauppbodWatchError("Bilauppboð pagination changed during traversal")
        if page < page_count and len(cards) != page_size:
            raise BilauppbodWatchError("Bilauppboð full index page has unexpected card count")
        if page == page_count and not 1 <= len(cards) <= page_size:
            raise BilauppbodWatchError("Bilauppboð final index page has unexpected card count")
        pages[page] = cards
    cards = [card for page in range(1, page_count + 1) for card in pages[page]]
    if len({card.listing_id for card in cards}) != len(cards) or len({card.url for card in cards}) != len(cards):
        raise BilauppbodWatchError("Bilauppboð catalogue-wide ID/URL reconciliation failed")
    return page_count, cards


def parse_detail(markup: bytes | str, listing_id: str) -> Detail:
    try:
        tree = lxml_html.fromstring(markup)
    except (etree.ParserError, ValueError) as error:
        raise BilauppbodWatchError(f"Bilauppboð detail {listing_id} markup is invalid") from error
    fields: dict[str, str] = {}
    for row in tree.xpath("//tr[td[1] and td[2]]"):
        label, value = fold(row.xpath("string(./td[1])")), clean(row.xpath("string(./td[2])"))
        if label and value:
            if label in fields and fields[label] != value:
                raise BilauppbodWatchError(f"Bilauppboð detail {listing_id} has conflicting fields")
            fields[label] = value
    def field(prefix: str) -> str:
        values = [value for label, value in fields.items() if label.startswith(prefix)]
        if len(values) > 1:
            raise BilauppbodWatchError(f"Bilauppboð detail {listing_id} has ambiguous {prefix} fields")
        return values[0] if values else ""
    return Detail(listing_id, field("framleidandi"), field("dyr"), field("velargerd"), digits(field("akstur")), field("seljandi"))


def fetch_details(cards: list[Card], *, session: requests.Session, timeout: int, workers: int) -> dict[str, Detail]:
    def fetch_one(active: requests.Session, card: Card) -> Detail:
        return parse_detail(fetch_markup(active, card.url, timeout=timeout), card.listing_id)
    if workers == 1:
        details = [fetch_one(session, card) for card in cards]
    else:
        def threaded(card: Card) -> Detail:
            local = configured_session()
            try:
                return fetch_one(local, card)
            finally:
                local.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            details = list(executor.map(threaded, cards))
    by_id = {detail.listing_id: detail for detail in details}
    if len(by_id) != len(cards) or set(by_id) != {card.listing_id for card in cards}:
        raise BilauppbodWatchError("Bilauppboð detail-page reconciliation failed")
    return by_id


def normalize_fuel(value: str) -> str:
    value = fold(value)
    if "hybrid" in value or ("rafmagn" in value and ("bensin" in value or "disel" in value)):
        return "hybrid"
    if "bensin" in value:
        return "petrol"
    if "disel" in value:
        return "diesel"
    if "rafmagn" in value:
        return "electric"
    return "unknown"


def passenger_exclusion_reason(card: Card, detail: Detail) -> str:
    if NON_PASSENGER_RE.search(fold(card.title)):
        return "explicit_non_passenger_title"
    if clean(detail.doors) not in {"2", "3", "4", "5"}:
        return "door_count_not_passenger"
    manufacturer = fold(detail.manufacturer)
    if not manufacturer or manufacturer in {"ekki skrad", "unknown", "n/a"}:
        return "missing_passenger_manufacturer"
    return ""


def normalize_card(card: Card, detail: Detail, *, observed_at: str) -> dict[str, Any]:
    mileage = detail.mileage_km if detail.mileage_km is not None else card.mileage_km
    year_match = YEAR_RE.search(card.registration_date) or YEAR_RE.search(card.title)
    return {
        "id": f"{SOURCE_KEY}:{card.listing_id}", "source": SOURCE_KEY, "source_key": SOURCE_KEY, "source_name": SOURCE_NAME,
        "url": card.url, "title": card.title, "model": card.title, "country": "IS", "asset_country": "IS", "category": "car",
        "category_raw": "Bilauppboð public current vehicle-auction index; passenger-car detail evidence",
        "year": int(year_match.group(1)) if year_match else None, "mileage": mileage, "mileage_km": mileage, "fuel": normalize_fuel(detail.fuel_raw),
        "seller": detail.seller or card.seller or SOURCE_NAME, "image_url": card.image_url,
        "price_amount": card.price_amount, "price_currency": "ISK" if card.price_amount is not None else "", "price_eur": None,
        "price_kind": "current_bid" if card.price_amount is not None else "unknown", "price_label": card.price_label,
        "bid_visibility": "public Bilauppboð current-auction card", "reserve_met": None, "no_reserve": None,
        "sale_terms": "Official Bilauppboð current auction listing", "auction_status": "active",
        "canonical_end_utc": card.end_utc.isoformat(), "sale_end_utc": card.end_utc.isoformat(), "sale_event_utc": None,
        "first_seen_at": observed_at, "last_seen_at": observed_at, "eligibility_status": "review_required",
        "eligibility_reason": "Public Bilauppboð passenger-car auction listing; confirm condition, price, fees, documents, collection, and export before bidding.",
        "access_sale_note": "Auction participation and purchase may require a registered buyer account.", "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-current-vehicle-index:{card.listing_id}",
        "evidence": "Public Bilauppboð index card and official listing detail page.",
    }


def build_watch(*, session: requests.Session | None = None, now: dt.datetime | None = None, timeout: int = DEFAULT_TIMEOUT, workers: int = DEFAULT_WORKERS) -> dict[str, Any]:
    if timeout < 5 or not 1 <= workers <= 12:
        raise ValueError("invalid Bilauppboð timeout/workers")
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_at = current.astimezone(UTC).isoformat()
    supplied, active = session, session or configured_session()
    try:
        first_pages, first_cards = fetch_catalogue(session=active, timeout=timeout)
        second_pages, second_cards = fetch_catalogue(session=active, timeout=timeout)
        first_by_id, second_by_id = ({card.listing_id: card for card in first_cards}, {card.listing_id: card for card in second_cards})
        if first_pages != second_pages or first_by_id.keys() != second_by_id.keys():
            raise BilauppbodWatchError("Bilauppboð catalogue changed between reconciliation passes")
        if any(first_by_id[key].fingerprint != second_by_id[key].fingerprint for key in first_by_id):
            raise BilauppbodWatchError("Bilauppboð listing lifecycle changed between reconciliation passes")
        details = fetch_details(second_cards, session=active, timeout=timeout, workers=workers)
    finally:
        if supplied is None:
            active.close()
    exclusions: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for card in second_cards:
        detail = details[card.listing_id]
        reason = passenger_exclusion_reason(card, detail)
        if reason:
            exclusions[reason] += 1
        else:
            rows.append(normalize_card(card, detail, observed_at=observed_at))
    report = {
        "status": "ok", "connector_status": "ok",
        "catalogue_scope": "every card across Bilauppboð public current-auction pages; only detail-confirmed passenger cars emitted",
        "declared": len(second_cards), "visited": len(second_cards), "detail_visited": len(details), "normalized_rows": len(rows), "passenger_cars": len(rows),
        "source_excluded": dict(sorted(exclusions.items())), "pages": second_pages, "two_pass_verified": True, "stable_ids_unique": True, "publication_ready": False,
    }
    return {"schema_version": 1, "lane": "official_auction_watch", "generated_at_utc": observed_at, "research_only": True,
            "publication_status": "review_required", "row_count": len(rows), "rows": rows, "source_reports": {SOURCE_KEY: report}}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch every public Bilauppboð passenger-car auction")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout, workers=args.workers)
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({"result": "BILAUPPBOD_WATCH_PASS", "row_count": payload["row_count"], "declared": report["declared"], "pages": report["pages"], "seconds": round(time.monotonic() - started, 1)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
