#!/usr/bin/env python3
"""Reconcile every public current passenger-car lot at Automotive Auctions NL.

The official Dutch overview publishes a finite set of auction cards, each with
an advertised item count and an end target.  Every current auction page then
publishes one ``data-lotid`` card per item.  This connector reads that whole
public catalogue twice, reconciles its stable identities and end times, and
only emits source-confirmed passenger-car candidates.  Price movement between
the two reads is deliberately ignored: it is live auction state, not catalogue
identity.
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
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from lxml import etree, html as lxml_html
from lxml.html import HtmlElement
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
SOURCE_KEY = "automotive-auctions-nl"
SOURCE_NAME = "Automotive Auctions"
SOURCE_URL = "https://www.automotive-auctions.nl/nl/ons-aanbod/"
SOURCE_HOSTS = frozenset({"automotive-auctions.nl", "www.automotive-auctions.nl"})
DEFAULT_TIMEOUT = 40
DEFAULT_WORKERS = 2
MAX_WORKERS = 4
MAX_AUCTIONS = 100
MAX_ITEMS_PER_AUCTION = 2_000
MAX_TOTAL_ITEMS = 50_000

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
ITEM_PATH_RE = re.compile(r"^/nl/ons-aanbod/[^/]+/[^/]+$")
ROOT_PATH_RE = re.compile(r"^/nl/ons-aanbod/[^/]+/$")
COUNT_RE = re.compile(r"\b(\d+)\s+items?\b", re.I)
AUCTION_ID_RE = re.compile(r"#([A-Za-z0-9-]+)\b")

# These are explicit non-passenger terms and known commercial-vehicle model
# families seen in the official mixed-vehicle auctions.  A title without one
# of these terms remains a passenger-car candidate; no generic auction lot is
# promoted because every emitted row comes from a named vehicle card.
WATERCRAFT_TITLE_RE = re.compile(
    r"\b(?:boot|speedboot|sportboot|sloep|jacht|watercraft|jetski|"
    r"waterscooter|albatro|chaparral)\b",
    re.I,
)
COMMERCIAL_TITLE_RE = re.compile(
    r"\b(?:bedrijfswagen|bestel(?:auto|wagen)|vrachtwagen|truck|lorry|"
    r"trekker|tractor|heftruck|graafmachine|werktuig|machine|oplegger|"
    r"aanhanger|trailer|caravan|camper|motor(?:fiets)?|scooter|quad|atv|"
    r"go[ -]?kart|junior\s+car|\b(?:transit|sprinter|crafter|vito|"
    r"transport(?:er)?|jumper|jumpy|scudo|ducato|daily|master|movano|"
    r"trafic|kangoo|berlingo|partner|doblo|dokker|combo|caddy|proace|vivaro|"
    r"primastar|nv200|canter|porter|tge|hilux|ranger|amarok|d-?max|l200|"
    r"navara|dodge\s+ram|f-?\d{3}|pickup|schoolbus|minibus|touringcar|"
    r"bus|brommobiel|piaggio\s+ape)\b)\b",
    re.I,
)


class AutomotiveAuctionsWatchError(RuntimeError):
    """The public Automotive Auctions catalogue could not be reconciled."""


@dataclass(frozen=True)
class Auction:
    auction_id: str
    url: str
    title: str
    declared_count: int
    end_utc: dt.datetime

    @property
    def fingerprint(self) -> tuple[str, str, str, int, str]:
        return (
            self.auction_id,
            self.url,
            self.title,
            self.declared_count,
            self.end_utc.isoformat(),
        )


@dataclass(frozen=True)
class Card:
    auction_id: str
    listing_id: str
    url: str
    title: str
    end_utc: dt.datetime
    current_bid: int | float | None
    starting_bid: int | float | None
    current_bid_label: str
    starting_bid_label: str
    mileage_km: int | None
    image_url: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.auction_id, self.listing_id

    @property
    def fingerprint(self) -> tuple[str, str, str, str, str, int | None]:
        # Current bid is intentionally absent: live bids can change between
        # coherent enumeration passes without changing the public lot.
        return (
            self.auction_id,
            self.listing_id,
            self.url,
            self.title,
            self.end_utc.isoformat(),
            self.mileage_km,
        )


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def fold(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", clean(value)).casefold()
        if not unicodedata.combining(character)
    )


def class_xpath_token(token: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {token} ')"


def parse_amount(value: str) -> int | float | None:
    """Parse a public Dutch price or odometer number, including visible zero."""
    compact = re.sub(r"[^0-9,.-]", "", clean(value))
    if not compact or compact.count("-") > 1 or compact.startswith("-"):
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
    elif compact.count(".") > 1:
        compact = compact.replace(".", "")
    elif "." in compact and len(compact.rsplit(".", 1)[-1]) == 3:
        # Dutch cards use a period for kilometre and whole-euro thousands.
        compact = compact.replace(".", "")
    try:
        parsed = float(compact)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def parse_nonnegative_int(value: str) -> int | None:
    parsed = parse_amount(value)
    if parsed is None or isinstance(parsed, float) and not parsed.is_integer():
        return None
    return int(parsed)


def parse_local_end(value: str) -> dt.datetime:
    raw = clean(value).replace("Z", "+00:00")
    if not raw:
        raise AutomotiveAuctionsWatchError("Automotive Auctions card has no public end target")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as error:
        raise AutomotiveAuctionsWatchError(
            f"Automotive Auctions card has invalid public end target: {raw}"
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=AMSTERDAM)
    return parsed.astimezone(UTC)


def official_url(href: str, *, item: bool) -> str:
    raw = clean(href)
    parsed = urlsplit(urljoin(SOURCE_URL, raw))
    expected_path = ITEM_PATH_RE if item else ROOT_PATH_RE
    if (
        parsed.scheme != "https"
        or parsed.hostname not in SOURCE_HOSTS
        or parsed.query
        or parsed.fragment
        or not expected_path.fullmatch(parsed.path)
    ):
        kind = "item" if item else "auction"
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions {kind} URL is invalid: {raw}")
    return parsed.geturl()


def one_text(node: HtmlElement, xpath: str, *, error: str) -> str:
    raw = node.xpath(xpath)
    raw_values = [raw] if isinstance(raw, str) else raw
    values = [clean(value) for value in raw_values if clean(value)]
    if len(values) != 1:
        raise AutomotiveAuctionsWatchError(error)
    return values[0]


def parse_root_auction(node: HtmlElement) -> Auction:
    title = one_text(node, "string(.//h2)", error="Automotive Auctions auction has no unambiguous title")
    count_text = one_text(
        node,
        "string(.//*[" + class_xpath_token("lot-count") + "])",
        error=f"Automotive Auctions auction {title} has no item count",
    )
    count_match = COUNT_RE.search(count_text)
    if count_match is None:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions auction {title} has invalid item count")
    declared_count = int(count_match.group(1))
    if declared_count > MAX_ITEMS_PER_AUCTION:
        raise AutomotiveAuctionsWatchError("Automotive Auctions auction exceeds item safety limit")
    id_text = one_text(
        node,
        "string(.//*[" + class_xpath_token("auction-id") + "])",
        error=f"Automotive Auctions auction {title} has no auction number",
    )
    id_match = AUCTION_ID_RE.search(id_text)
    if id_match is None:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions auction {title} has invalid auction number")
    hrefs = sorted({clean(value) for value in node.xpath(".//a[@href]/@href") if clean(value).startswith("/nl/ons-aanbod/") and clean(value).endswith("/")})
    if len(hrefs) != 1:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions auction {id_match.group(1)} has no unambiguous URL")
    targets = [clean(value) for value in node.xpath(".//*[@data-target]/@data-target") if clean(value)]
    if len(targets) != 1:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions auction {id_match.group(1)} has no unambiguous end target")
    return Auction(
        auction_id=id_match.group(1),
        url=official_url(hrefs[0], item=False),
        title=title,
        declared_count=declared_count,
        end_utc=parse_local_end(targets[0]),
    )


def parse_root_page(markup: str) -> list[Auction]:
    try:
        tree = lxml_html.fromstring(markup)
    except (etree.ParserError, ValueError) as error:
        raise AutomotiveAuctionsWatchError("Automotive Auctions overview markup is invalid") from error
    nodes = tree.xpath("//div[" + class_xpath_token("auction") + " and " + class_xpath_token("set") + "]")
    auctions = [parse_root_auction(node) for node in nodes]
    ids = [auction.auction_id for auction in auctions]
    urls = [auction.url for auction in auctions]
    if not auctions or len(auctions) > MAX_AUCTIONS or len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise AutomotiveAuctionsWatchError("Automotive Auctions overview has invalid auction membership")
    if sum(auction.declared_count for auction in auctions) > MAX_TOTAL_ITEMS:
        raise AutomotiveAuctionsWatchError("Automotive Auctions overview exceeds total item safety limit")
    return sorted(auctions, key=lambda auction: auction.auction_id)


def listing_value(content: HtmlElement, label: str) -> str:
    desired = fold(label).rstrip(":")
    matches: list[str] = []
    for listing in content.xpath(".//div[" + class_xpath_token("listing") + "]"):
        labels = [fold(value).rstrip(":") for value in listing.xpath("./span[1]/text()") if clean(value)]
        if labels != [desired]:
            continue
        values = [clean(value) for value in listing.xpath(".//span[" + class_xpath_token("val") + "]/text()") if clean(value)]
        if len(values) != 1:
            raise AutomotiveAuctionsWatchError(f"Automotive Auctions listing has invalid {label} value")
        matches.append(values[0])
    if len(matches) != 1:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions listing has no unambiguous {label} value")
    return matches[0]


def optional_listing_value(content: HtmlElement, label: str) -> str:
    desired = fold(label).rstrip(":")
    matches: list[str] = []
    for listing in content.xpath(".//div[" + class_xpath_token("listing") + "]"):
        labels = [fold(value).rstrip(":") for value in listing.xpath("./span[1]/text()") if clean(value)]
        if labels != [desired]:
            continue
        values = [clean(value) for value in listing.xpath(".//span[" + class_xpath_token("val") + "]/text()") if clean(value)]
        if len(values) != 1:
            raise AutomotiveAuctionsWatchError(f"Automotive Auctions listing has invalid {label} value")
        matches.append(values[0])
    if len(matches) > 1:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions listing has ambiguous {label} value")
    return matches[0] if matches else ""


def parse_item_card(node: HtmlElement, auction: Auction) -> Card:
    content_nodes = node.xpath(".//div[" + class_xpath_token("auction-content") + "]")
    if len(content_nodes) != 1:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions auction {auction.auction_id} item has invalid content")
    content = content_nodes[0]
    listing_id = clean(content.get("data-lotid"))
    if not listing_id:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions auction {auction.auction_id} item has no lot ID")
    title = one_text(content, "string(./h2)", error=f"Automotive Auctions lot {listing_id} has no title")
    hrefs = sorted({clean(value) for value in node.xpath(".//a[@href]/@href") if ITEM_PATH_RE.fullmatch(urlsplit(clean(value)).path)})
    if len(hrefs) != 1:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions lot {listing_id} has no unambiguous detail URL")
    url = official_url(hrefs[0], item=True)
    auction_path = urlsplit(auction.url).path
    if not urlsplit(url).path.startswith(auction_path):
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions lot {listing_id} is outside its auction URL")
    targets = [clean(value) for value in content.xpath(".//*[@data-target]/@data-target") if clean(value)]
    if len(targets) != 1:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions lot {listing_id} has no unambiguous end target")
    current_label = listing_value(content, "Huidig bod")
    starting_label = listing_value(content, "Startbod")
    mileage_label = optional_listing_value(content, "Afgelezen tellerstand")
    images = [clean(value) for value in node.xpath(".//img/@src") if clean(value)]
    image_url = images[0] if len(images) == 1 else ""
    return Card(
        auction_id=auction.auction_id,
        listing_id=listing_id,
        url=url,
        title=title,
        end_utc=parse_local_end(targets[0]),
        current_bid=parse_amount(current_label),
        starting_bid=parse_amount(starting_label),
        current_bid_label=current_label,
        starting_bid_label=starting_label,
        mileage_km=parse_nonnegative_int(mileage_label) if mileage_label else None,
        image_url=image_url,
    )


def parse_auction_page(markup: str, auction: Auction) -> list[Card]:
    try:
        tree = lxml_html.fromstring(markup)
    except (etree.ParserError, ValueError) as error:
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions auction {auction.auction_id} markup is invalid") from error
    nodes = tree.xpath("//div[" + class_xpath_token("auction-tiles-item") + "]")
    cards = [parse_item_card(node, auction) for node in nodes]
    identities = [card.identity for card in cards]
    urls = [card.url for card in cards]
    if len(cards) != auction.declared_count:
        raise AutomotiveAuctionsWatchError(
            f"Automotive Auctions auction {auction.auction_id} count mismatch: {len(cards)} != {auction.declared_count}"
        )
    if len(identities) != len(set(identities)) or len(urls) != len(set(urls)):
        raise AutomotiveAuctionsWatchError(f"Automotive Auctions auction {auction.auction_id} has duplicate lots")
    return sorted(cards, key=lambda card: card.identity)


def configured_session(*, workers: int = DEFAULT_WORKERS) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=workers, pool_maxsize=workers))
    return session


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def fetch_catalogue(
    *,
    session: requests.Session,
    now: dt.datetime,
    timeout: int,
    workers: int,
) -> tuple[list[Auction], list[Auction], list[Card]]:
    all_auctions = parse_root_page(fetch_markup(session, SOURCE_URL, timeout=timeout))
    active_auctions = [auction for auction in all_auctions if auction.end_utc > now]
    if sum(auction.declared_count for auction in active_auctions) > MAX_TOTAL_ITEMS:
        raise AutomotiveAuctionsWatchError("Automotive Auctions active catalogue exceeds item safety limit")

    def fetch_one(auction: Auction) -> tuple[Auction, list[Card]]:
        local_session = configured_session(workers=workers)
        try:
            return auction, parse_auction_page(fetch_markup(local_session, auction.url, timeout=timeout), auction)
        finally:
            local_session.close()

    pages: dict[str, list[Card]] = {}
    if workers == 1:
        for auction in active_auctions:
            pages[auction.auction_id] = parse_auction_page(
                fetch_markup(session, auction.url, timeout=timeout), auction
            )
    elif active_auctions:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_one, auction): auction.auction_id for auction in active_auctions}
            for future in concurrent.futures.as_completed(futures):
                auction, cards = future.result()
                pages[auction.auction_id] = cards

    cards = [card for auction in active_auctions for card in pages[auction.auction_id]]
    identities = [card.identity for card in cards]
    urls = [card.url for card in cards]
    declared_total = sum(auction.declared_count for auction in active_auctions)
    if len(cards) != declared_total or len(identities) != len(set(identities)) or len(urls) != len(set(urls)):
        raise AutomotiveAuctionsWatchError("Automotive Auctions active catalogue reconciliation failed")
    return all_auctions, active_auctions, sorted(cards, key=lambda card: card.identity)


def passenger_exclusion_reason(card: Card, auction: Auction) -> str:
    title = fold(f"{auction.title} {card.title}")
    if WATERCRAFT_TITLE_RE.search(title):
        return "watercraft"
    if COMMERCIAL_TITLE_RE.search(title):
        return "commercial_or_non_passenger_vehicle"
    return ""


def infer_fuel(title: str) -> str:
    value = fold(title)
    if re.search(r"\b(?:plug[ -]?in\s+hybrid|hybrid)\b", value):
        return "hybrid"
    if re.search(r"\b(?:electric|elektrisch|ev)\b", value):
        return "electric"
    if re.search(r"\b(?:benzine|petrol|gasoline)\b", value):
        return "gasoline"
    if re.search(r"\bdiesel\b", value):
        return "diesel"
    return "unknown"


def normalize_card(card: Card, auction: Auction, *, observed_at: str) -> dict[str, Any]:
    if card.current_bid is not None and card.current_bid > 0:
        price = card.current_bid
        price_kind = "current_bid"
    elif card.starting_bid is not None and card.starting_bid > 0:
        price = card.starting_bid
        price_kind = "starting_bid"
    else:
        price = None
        price_kind = "unknown"
    year_match = YEAR_RE.search(card.title)
    return {
        "id": f"{SOURCE_KEY}:{card.auction_id}:{card.listing_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": card.url,
        "title": card.title,
        "model": card.title,
        "country": "NL",
        "asset_country": "NL",
        "category": "car",
        "category_raw": "Automotive Auctions public vehicle-auction card",
        "year": int(year_match.group(1)) if year_match else None,
        "mileage": card.mileage_km,
        "mileage_km": card.mileage_km,
        "fuel": infer_fuel(card.title),
        "seller": SOURCE_NAME,
        "image_url": card.image_url,
        "price_amount": price,
        "price_currency": "EUR" if price is not None else "",
        "price_eur": price,
        "price_kind": price_kind,
        "price_label": f"Huidig bod: {card.current_bid_label}; Startbod: {card.starting_bid_label}",
        "bid_visibility": "public Automotive Auctions auction card",
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": "Official Automotive Auctions current public auction listing",
        "auction_status": "active",
        "canonical_end_utc": card.end_utc.isoformat(),
        "sale_end_utc": card.end_utc.isoformat(),
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public Automotive Auctions current listing; confirm condition, fees, documents, "
            "collection, registration, and export requirements before bidding."
        ),
        "access_sale_note": "Auction participation and purchase may require a registered buyer account.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:auction:{card.auction_id}:lot:{card.listing_id}",
        "evidence": "Public Automotive Auctions overview and auction-card data.",
    }


def assert_coherent(
    first_auctions: list[Auction],
    first_active: list[Auction],
    first_cards: list[Card],
    second_auctions: list[Auction],
    second_active: list[Auction],
    second_cards: list[Card],
) -> None:
    first_roots = {auction.auction_id: auction for auction in first_auctions}
    second_roots = {auction.auction_id: auction for auction in second_auctions}
    first_active_map = {auction.auction_id: auction for auction in first_active}
    second_active_map = {auction.auction_id: auction for auction in second_active}
    if first_active_map.keys() != second_active_map.keys():
        raise AutomotiveAuctionsWatchError("Automotive Auctions active auction membership changed between passes")
    # An archived card may disappear from the historical tail without changing
    # the current catalogue.  Current membership and stable fields must match.
    if any(first_active_map[key].fingerprint != second_active_map[key].fingerprint for key in first_active_map):
        raise AutomotiveAuctionsWatchError("Automotive Auctions active auction facts changed between passes")
    first_cards_map = {card.identity: card for card in first_cards}
    second_cards_map = {card.identity: card for card in second_cards}
    if first_cards_map.keys() != second_cards_map.keys():
        raise AutomotiveAuctionsWatchError("Automotive Auctions lot IDs changed between passes")
    if any(first_cards_map[key].fingerprint != second_cards_map[key].fingerprint for key in first_cards_map):
        raise AutomotiveAuctionsWatchError("Automotive Auctions lot facts changed between passes")
    # Keep a check that the canonical current-root objects were not replaced by
    # a malformed duplicate in either pass.  It deliberately ignores expired
    # historical cards, which are not part of the current source contract.
    if not set(first_active_map).issubset(first_roots) or not set(second_active_map).issubset(second_roots):
        raise AutomotiveAuctionsWatchError("Automotive Auctions active-root reconciliation failed")


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    if timeout < 5 or workers < 1 or workers > MAX_WORKERS:
        raise ValueError("invalid Automotive Auctions timeout/workers")
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session(workers=workers)
    try:
        first_auctions, first_active, first_cards = fetch_catalogue(
            session=active_session, now=current, timeout=timeout, workers=workers
        )
        second_auctions, second_active, second_cards = fetch_catalogue(
            session=active_session, now=current, timeout=timeout, workers=workers
        )
    finally:
        if supplied_session is None:
            active_session.close()

    assert_coherent(
        first_auctions, first_active, first_cards,
        second_auctions, second_active, second_cards,
    )
    auctions_by_id = {auction.auction_id: auction for auction in second_active}
    exclusions: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    current_cards = 0
    for card in second_cards:
        if card.end_utc <= current:
            exclusions["ended_item"] += 1
            continue
        current_cards += 1
        reason = passenger_exclusion_reason(card, auctions_by_id[card.auction_id])
        if reason:
            exclusions[reason] += 1
            continue
        rows.append(normalize_card(card, auctions_by_id[card.auction_id], observed_at=observed_at))

    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": (
            "every current public Automotive Auctions overview auction and every declared "
            "public lot card; explicit commercial vehicles and watercraft excluded"
        ),
        "overview_auctions": len(second_auctions),
        "expired_overview_auctions": len(second_auctions) - len(second_active),
        "active_auctions": len(second_active),
        "declared": sum(auction.declared_count for auction in second_active),
        "visited": len(second_cards),
        "current_cards": current_cards,
        "passenger_cars": len(rows),
        "source_excluded": dict(sorted(exclusions.items())),
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
    parser = argparse.ArgumentParser(description="Fetch every current public Automotive Auctions NL passenger-car listing")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout, workers=args.workers)
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "AUTOMOTIVE_AUCTIONS_NL_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": report["declared"],
        "active_auctions": report["active_auctions"],
        "seconds": round(time.monotonic() - started, 1),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
