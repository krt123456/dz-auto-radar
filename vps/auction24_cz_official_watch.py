#!/usr/bin/env python3
"""Reconcile Auction24.cz's public current passenger-car category.

The official ``/en/category/car`` page exposes a JSON-LD ``ItemList`` total
and ordinary numbered HTML pages.  This collector walks that finite public
category twice, requires the same stable card identities and lifecycle labels
on both reads, and emits only current passenger-car listings.  Sold cards and
explicit commercial/recreational vehicles remain counted in the coverage
report but never enter the car watch.
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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from lxml import etree, html as lxml_html
from lxml.html import HtmlElement
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "auction24-cz"
SOURCE_NAME = "Auction24"
SOURCE_URL = "https://auction24.cz/en/category/car"
PAGE_SIZE = 24
MAX_PAGES = 100
DEFAULT_TIMEOUT = 30
DEFAULT_WORKERS = 4

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}
ITEM_HREF_RE = re.compile(r"^/en/item/([A-Za-z0-9]+)$")
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
MILEAGE_RE = re.compile(r"\b([0-9][0-9\s,\.\u00a0]*)\s*km\b", re.I)
PRICE_RE = re.compile(r"([0-9][0-9\s,.\u00a0]*)\s*€")

# The official Cars category currently contains a small number of commercial
# vehicles.  These title terms identify a non-passenger vehicle without
# guessing about ordinary passenger-car body styles such as estate or SUV.
NON_PASSENGER_TITLE_RE = re.compile(
    r"\b(?:motorbike|motorcycle|scooter|quad|atv|utv|boat|jetski|"
    r"caravan|camper|motorhome|truck|lorry|lkw|van|pickup|double\s+cab|"
    r"transit|transporter|trafic|vito|jumpy|proace|hilux|"
    r"berlingo|dokker|partner(?!\s+tepee\b)|caravelle|dodge\s+ram|"
    r"caddy(?!\s+life\b)|volkswagen\s+t1)\b",
    re.I,
)
ENDED_STATUS_RE = re.compile(r"\b(?:sold|ended|withdrawn|cancelled)\b", re.I)
COUNTRY_BY_LABEL = {
    "ceska republika": "CZ",
    "czech republic": "CZ",
    "czechia": "CZ",
}


class Auction24CzWatchError(RuntimeError):
    """Auction24.cz's public Cars category could not be reconciled."""


@dataclass(frozen=True)
class Card:
    listing_id: str
    url: str
    title: str
    meta: str
    price_label: str
    status_label: str
    country: str

    @property
    def fingerprint(self) -> tuple[str, str, str, str, str]:
        """Fields that must not change while the finite category is read."""
        return (self.listing_id, self.title, self.meta, self.status_label, self.country)


@dataclass(frozen=True)
class ParsedPage:
    total: int
    cards: list[Card]


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def fold(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", clean(value)).casefold()
        if not unicodedata.combining(character)
    )


def positive_number(value: str) -> int | float | None:
    compact = re.sub(r"[^0-9,.-]", "", value or "")
    if not compact:
        return None
    if "," in compact and "." in compact:
        compact = (
            compact.replace(".", "").replace(",", ".")
            if compact.rfind(",") > compact.rfind(".")
            else compact.replace(",", "")
        )
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


def country_from_card(card: HtmlElement) -> str:
    labels = card.xpath(
        ".//*[@aria-label and contains(concat(' ', normalize-space(@class), ' '), ' flag-badge ')]/@aria-label"
    )
    # A handful of otherwise complete public cards currently omit the optional
    # flag decoration.  The category belongs to the Czech source and its
    # detail page likewise omits a separate location for those cards, so use
    # the source's canonical CZ country only for that explicit missing-field
    # case.  A present but unknown/multiple flag remains a hard failure.
    if not labels:
        return "CZ"
    if len(labels) != 1:
        raise Auction24CzWatchError("Auction24.cz card has no unambiguous public country flag")
    country = COUNTRY_BY_LABEL.get(fold(labels[0]))
    if country is None:
        raise Auction24CzWatchError(f"Auction24.cz card has an unsupported country flag: {clean(labels[0])}")
    return country


def category_total(tree: HtmlElement) -> int:
    totals: list[int] = []
    for script in tree.xpath("//script[@type='application/ld+json']"):
        try:
            payload = json.loads(script.text or "")
        except json.JSONDecodeError as error:
            raise Auction24CzWatchError("Auction24.cz JSON-LD is invalid") from error
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if not isinstance(value, dict) or value.get("@type") != "ItemList":
                continue
            total = positive_number(clean(value.get("numberOfItems")))
            if total is None or int(total) != total:
                raise Auction24CzWatchError("Auction24.cz Cars total is invalid")
            totals.append(int(total))
    if len(totals) != 1:
        raise Auction24CzWatchError("Auction24.cz Cars total is missing or ambiguous")
    return totals[0]


def parse_card(node: HtmlElement) -> Card:
    href = clean(node.get("href"))
    match = ITEM_HREF_RE.fullmatch(href)
    if match is None:
        raise Auction24CzWatchError("Auction24.cz card has an invalid public item URL")
    title = clean(node.xpath("string(.//p[contains(concat(' ', normalize-space(@class), ' '), ' title ')])"))
    if not title:
        raise Auction24CzWatchError(f"Auction24.cz card {match.group(1)} has no title")
    meta = clean(node.xpath("string(.//p[contains(concat(' ', normalize-space(@class), ' '), ' title-meta ')])"))
    price_label = clean(node.xpath("string(.//div[contains(concat(' ', normalize-space(@class), ' '), ' price-row ')])"))
    status_label = clean(node.xpath("string(.//div[contains(concat(' ', normalize-space(@class), ' '), ' status-overlay ')])"))
    if not status_label:
        raise Auction24CzWatchError(f"Auction24.cz card {match.group(1)} has no lifecycle label")
    return Card(
        listing_id=match.group(1),
        url=f"https://auction24.cz{href}",
        title=title,
        meta=meta,
        price_label=price_label,
        status_label=status_label,
        country=country_from_card(node),
    )


def parse_page(markup: str) -> ParsedPage:
    try:
        tree = lxml_html.fromstring(markup)
    except (etree.ParserError, ValueError) as error:
        raise Auction24CzWatchError("Auction24.cz category markup is invalid") from error
    total = category_total(tree)
    nodes = tree.xpath(
        "//a[contains(concat(' ', normalize-space(@class), ' '), ' card ') and starts-with(@href, '/en/item/')]"
    )
    cards = [parse_card(node) for node in nodes]
    ids = [card.listing_id for card in cards]
    if not cards or len(ids) != len(set(ids)):
        raise Auction24CzWatchError("Auction24.cz category page has missing or duplicate cards")
    return ParsedPage(total=total, cards=cards)


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
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8))
    return session


def page_url(page: int) -> str:
    if page < 1:
        raise ValueError("page must be positive")
    return SOURCE_URL if page == 1 else f"{SOURCE_URL}?page={page}"


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def fetch_catalogue(
    *,
    session: requests.Session,
    timeout: int,
    workers: int,
) -> tuple[int, list[Card]]:
    first = parse_page(fetch_markup(session, page_url(1), timeout=timeout))
    if first.total > PAGE_SIZE * MAX_PAGES:
        raise Auction24CzWatchError("Auction24.cz Cars category exceeds the page safety limit")
    expected_pages = math.ceil(first.total / PAGE_SIZE)
    if len(first.cards) != min(PAGE_SIZE, first.total):
        raise Auction24CzWatchError("Auction24.cz first category page cardinality is invalid")

    pages: dict[int, list[Card]] = {1: first.cards}

    def fetch_page(page: int) -> tuple[int, ParsedPage]:
        local_session = configured_session()
        try:
            return page, parse_page(fetch_markup(local_session, page_url(page), timeout=timeout))
        finally:
            local_session.close()

    if expected_pages > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_page, page): page for page in range(2, expected_pages + 1)}
            for future in concurrent.futures.as_completed(futures):
                page, parsed = future.result()
                if parsed.total != first.total:
                    raise Auction24CzWatchError("Auction24.cz Cars category changed during pagination")
                expected_size = PAGE_SIZE if page < expected_pages else first.total - PAGE_SIZE * (page - 1)
                if len(parsed.cards) != expected_size:
                    raise Auction24CzWatchError(
                        f"Auction24.cz category page {page} cardinality is invalid: {len(parsed.cards)}"
                    )
                pages[page] = parsed.cards

    cards = [card for page in range(1, expected_pages + 1) for card in pages[page]]
    ids = [card.listing_id for card in cards]
    urls = [card.url for card in cards]
    if len(cards) != first.total or len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise Auction24CzWatchError("Auction24.cz total/ID reconciliation failed")
    return expected_pages, cards


def is_current(card: Card) -> bool:
    return ENDED_STATUS_RE.search(card.status_label) is None


def passenger_exclusion_reason(card: Card) -> str:
    if NON_PASSENGER_TITLE_RE.search(fold(card.title)):
        return "non_passenger_title"
    return ""


def normalize_card(card: Card, *, observed_at: str) -> dict[str, Any]:
    summary_text = f"{card.title} {card.meta}"
    year_match = YEAR_RE.search(summary_text)
    mileage_match = MILEAGE_RE.search(summary_text)
    price_match = PRICE_RE.search(card.price_label)
    price = positive_number(price_match.group(1)) if price_match else None
    return {
        "id": f"{SOURCE_KEY}:{card.listing_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": card.url,
        "title": card.title,
        "model": card.title,
        "country": card.country,
        "asset_country": card.country,
        "category": "car",
        "category_raw": "Auction24.cz public Cars category",
        "year": int(year_match.group(1)) if year_match else None,
        "mileage_km": positive_number(mileage_match.group(1)) if mileage_match else None,
        "fuel": "unknown",
        "price_amount": price,
        "price_currency": "EUR",
        "price_eur": price,
        "price_kind": "guide_price" if price is not None else "unknown",
        "price_label": card.price_label or "price not shown in public category card",
        "bid_visibility": "public Cars category card",
        "canonical_end_utc": None,
        "sale_end_utc": None,
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public Auction24.cz current Cars-category listing; confirm condition, price, "
            "auction terms, documents, fees, collection, and export before bidding."
        ),
        "access_sale_note": "Auction participation and purchase may require a registered buyer account.",
        "auction_status": "active",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-cars-category",
        "evidence": "Public Auction24.cz Cars category card.",
    }


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    if timeout < 5 or workers < 1 or workers > 16:
        raise ValueError("invalid Auction24.cz timeout/workers")
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_at = current.astimezone(UTC).isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        pages, first_cards = fetch_catalogue(session=active_session, timeout=timeout, workers=workers)
        _, second_cards = fetch_catalogue(session=active_session, timeout=timeout, workers=workers)
    finally:
        if supplied_session is None:
            active_session.close()

    first_by_id = {card.listing_id: card for card in first_cards}
    second_by_id = {card.listing_id: card for card in second_cards}
    if first_by_id.keys() != second_by_id.keys():
        raise Auction24CzWatchError("Auction24.cz Cars category IDs changed between reconciliation passes")
    changed = [
        listing_id
        for listing_id in sorted(first_by_id)
        if first_by_id[listing_id].fingerprint != second_by_id[listing_id].fingerprint
    ]
    if changed:
        raise Auction24CzWatchError("Auction24.cz Cars category lifecycle changed between reconciliation passes")

    exclusions: Counter[str] = Counter()
    current_cards: list[Card] = []
    rows: list[dict[str, Any]] = []
    for card in second_cards:
        if not is_current(card):
            exclusions["inactive_status"] += 1
            continue
        current_cards.append(card)
        reason = passenger_exclusion_reason(card)
        if reason:
            exclusions[reason] += 1
            continue
        rows.append(normalize_card(card, observed_at=observed_at))

    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public card in Auction24.cz's Cars category; sold and explicit non-passenger cards excluded",
        "declared": len(second_cards),
        "visited": len(second_cards),
        "normalized_rows": len(rows),
        "active_cards": len(current_cards),
        "passenger_cars": len(rows),
        "source_excluded": dict(sorted(exclusions.items())),
        "page_size": PAGE_SIZE,
        "pages": pages,
        "two_pass_verified": True,
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
    parser = argparse.ArgumentParser(description="Fetch every current public Auction24.cz passenger-car listing")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout, workers=args.workers)
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "AUCTION24_CZ_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": report["declared"],
        "active_cards": report["active_cards"],
        "seconds": round(time.monotonic() - started, 1),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
