#!/usr/bin/env python3
"""Reconcile Veacom's current public passenger-car auction listings.

Veacom's public upcoming-auction page is a single complete index which mixes
cars with machinery, commercial vehicles, motorcycles, trailers, and other
assets.  Each card has a stable public vehicle-detail URL and each detail page
publishes road-vehicle specification fields.  This collector walks the full
index twice, requires a stable membership/content fingerprint, reads every
detail page, and emits only the rows whose official fields identify a
passenger car.  A failed card, changed index, duplicate ID, or ambiguous
classification fails the complete source run rather than silently publishing a
partial result.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import html
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from fx_rates import fetch_ecb_units_per_eur, to_eur
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "veacom"
SOURCE_NAME = "Veacom"
ROOT_URL = "https://www.veacom.cz"
CATALOGUE_URL = f"{ROOT_URL}/cs/homepage/upcoming-auction"
DEFAULT_TIMEOUT = 35
DEFAULT_WORKERS = 4
PASSENGER_VEHICLE_MASS_LIMIT_KG = 3500

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "cs,en;q=0.8",
}
DETAIL_PATH_RE = re.compile(r"^/cs/vehicle/detail/(?P<id>[1-9][0-9]*)$")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
NUMBER_RE = re.compile(r"\b([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{1,8})\b")
MILEAGE_RE = re.compile(r"\b([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{1,8})\s*km\b", re.I)
MOTOUR_RE = re.compile(r"\b(?:mth|motohodin(?:a|y|ách)?|hours?)\b", re.I)
SEAT_RE = re.compile(r"\b([0-9]{1,2})\b")

# These terms describe items that Veacom exposes in the same public index but
# which cannot be passenger cars.  The detail-field checks below are also
# required, so a title-only match never promotes a row by itself.
NON_CAR_TEXT_RE = re.compile(
    r"\b(?:motocykl|motorka|moped|sk[úu]tr|scooter|quad|atv|utv|buggy|"
    r"traktor|tractor|bagr|r[yý]padlo|excavator|naklada[cč]|buldozer|"
    r"forklift|vysokozdvi|jeřáb|j[eř]rab|kombajn|seka[cč]|mower|"
    r"v[yý]tah|zved[aá]k|př[ií]v[eě]s|přívěs|náv[eě]s|naves|trailer|"
    r"karavan|obytn[ýa]|loď|člun|boat|autobus|\bbus\b|\blkw\b|"
    r"truck|nákladn|nakladn|valn[ií]k|skl[aá]p[eě][cč]|cisterna|"
    r"hasi[cč]|pož[aá]r|polici[ei]|ambulance|sanit|z[aá]chran|"
    r"speci[aá]ln[ií]|taha[cč])\b",
    re.I,
)
COMMERCIAL_MODEL_RE = re.compile(
    r"\b(?:transit|crafter|boxer|jumper|ducato|daily|master|movano|"
    r"sprinter|citan|vito|caddy|berlingo|partner|dobl[oó]|talento|"
    r"proace|canter|dyna|amarok|hilux|navara|ranger|pick[ -]?up|"
    r"transporter|multivan|caravelle|trafic|kangoo|\blt\s*[0-9]+|"
    r"musso|\bl[ -]?200\b)\b",
    re.I,
)
HEAVY_TRUCK_MODEL_RE = re.compile(
    r"\b(?:tatra\b|iveco\s+(?:eurocargo|trakker|stralis|cargo)|avia\b|"
    r"man\s+(?:l2000|le\s*\d|tgs|tgm|tgx|\d{1,2}\.\d{3})|"
    r"volvo\s+(?:fmx|fh|fe|fl)\b|(?:mercedes(?:[- ]benz)?|mb)\s+"
    r"(?:\d{3,4}|axor|atego|actros)\b|renault\s+(?:midlum|premium|kerax)\b|"
    r"daf\s+(?:lf|cf|xf)\b|nissan\s+cabstar|(?:škoda|skoda)\s+st\s*180\b)",
    re.I,
)
PASSENGER_MAKE_RE = re.compile(
    r"\b(?:mercedes(?:[- ]benz)?|land rover|range rover|alfa romeo|"
    r"volkswagen|renault|peugeot|citro[eë]n|opel|vauxhall|toyota|"
    r"bmw|audi|ford|nissan|hyundai|kia|honda|mazda|fiat|skoda|škoda|"
    r"seat|volvo|mitsubishi|suzuki|dacia|chevrolet|jeep|porsche|"
    r"lexus|subaru|jaguar|chrysler|dodge|tesla|ssangyong|isuzu|"
    r"daihatsu|infiniti|genesis|cupra|smart|chery|geely|haval|byd|"
    r"mg|ds|lada|saab|rover|vw)\b",
    re.I,
)


class VeacomWatchError(RuntimeError):
    """The public Veacom catalogue could not be reconciled safely."""


@dataclass(frozen=True)
class Listing:
    listing_id: str
    url: str
    title: str
    image_url: str | None
    price_amount: int | None
    price_currency: str
    price_amount_eur: int | float | None
    summary_year: int | None
    summary_mileage_km: int | None
    summary_fuel: str
    event_start: dt.datetime | None
    event_end: dt.datetime

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.listing_id,
            self.url,
            self.title,
            self.price_amount,
            self.price_currency,
            self.price_amount_eur,
            self.summary_year,
            self.summary_mileage_km,
            self.summary_fuel,
            self.event_start.isoformat() if self.event_start else None,
            self.event_end.isoformat(),
        )


@dataclass(frozen=True)
class Catalogue:
    listings: tuple[Listing, ...]

    @property
    def fingerprint(self) -> tuple[tuple[Any, ...], ...]:
        return tuple(sorted(listing.fingerprint for listing in self.listings))


def clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    digits = re.sub(r"[^0-9]", "", clean(value))
    if not digits:
        return None
    number = int(digits)
    return number if number > 0 else None


def parse_year(value: Any, *, now: dt.datetime) -> int | None:
    match = YEAR_RE.search(clean(value))
    if match is None:
        return None
    year = int(match.group(1))
    return year if 1950 <= year <= now.year + 1 else None


def parse_mileage_km(value: Any) -> int | None:
    text = clean(value)
    if MOTOUR_RE.search(text):
        return None
    match = MILEAGE_RE.search(text)
    if match is None:
        return None
    number = int(re.sub(r"[^0-9]", "", match.group(1)))
    return number if 0 <= number <= 2_000_000 else None


def parse_weight_kg(value: Any) -> int | None:
    text = clean(value)
    if not text or not re.search(r"\bkg\b", text, re.I):
        return None
    return positive_integer(text)


def normalize_fuel(value: Any) -> str:
    text = clean(value).casefold()
    if "hybrid" in text:
        return "hybrid"
    if "nafta" in text or "diesel" in text:
        return "diesel"
    if "benz" in text or "petrol" in text:
        return "petrol"
    if "elekt" in text or "electric" in text:
        return "electric"
    if "lpg" in text or "autogas" in text:
        return "lpg"
    if "cng" in text or "zemn" in text:
        return "cng"
    return "unknown"


def parse_iso_datetime(value: Any, *, field_name: str) -> dt.datetime:
    text = clean(value)
    if not text:
        raise VeacomWatchError(f"Veacom event has no {field_name}")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VeacomWatchError(f"Veacom event has an invalid {field_name}: {text!r}") from exc
    if parsed.tzinfo is None:
        raise VeacomWatchError(f"Veacom event {field_name} has no timezone")
    return parsed.astimezone(UTC)


def parse_public_json_ld(raw: str) -> Any:
    """Parse Veacom's JSON-LD, including its public string-concatenation quirk."""
    # The page currently serializes its canonical event URL as
    # ``".../cs"+"/homepage/upcoming-auction"``.  Concatenating adjacent JSON
    # string literals restores valid JSON without inferring any data endpoint.
    normalized = re.sub(r'"\s*\+\s*"', "", raw)
    try:
        return json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise VeacomWatchError("Veacom page has invalid public JSON-LD") from exc


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


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, timeout=timeout)
    try:
        response.raise_for_status()
        markup = response.text
    finally:
        response.close()
    if not markup:
        raise VeacomWatchError(f"Veacom returned an empty public page: {url}")
    return markup


def event_payloads(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        payloads = [value]
        graph = value.get("@graph")
        if isinstance(graph, list):
            payloads.extend(item for item in graph if isinstance(item, dict))
        return payloads
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def parse_event_bounds(soup: BeautifulSoup, *, now: dt.datetime) -> tuple[dt.datetime | None, dt.datetime]:
    events: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.get_text(strip=True)
        if not raw:
            continue
        parsed = parse_public_json_ld(raw)
        events.extend(
            item for item in event_payloads(parsed)
            if clean(item.get("@type")).casefold() == "event"
        )
    if len(events) != 1:
        raise VeacomWatchError(
            f"Veacom public catalogue must expose exactly one active event ({len(events)})"
        )
    event = events[0]
    event_end = parse_iso_datetime(event.get("endDate"), field_name="endDate")
    if event_end <= now:
        raise VeacomWatchError("Veacom public event is already ended")
    start_raw = event.get("startDate")
    event_start = parse_iso_datetime(start_raw, field_name="startDate") if clean(start_raw) else None
    if event_start and event_start > event_end:
        raise VeacomWatchError("Veacom event starts after it ends")
    return event_start, event_end


def card_summary_values(card: Tag) -> dict[str, str]:
    values: dict[str, str] = {}
    for label in card.select("span.label-ar"):
        label_text = clean(label.get_text(" ", strip=True))
        parent = label.parent
        raw = clean(parent.get_text(" ", strip=True) if parent is not None else "")
        if raw.casefold().startswith(label_text.casefold()):
            raw = clean(raw[len(label_text):])
        if label_text:
            values[label_text.casefold()] = raw
    return values


def card_listing(
    card: Tag,
    *,
    event_start: dt.datetime | None,
    event_end: dt.datetime,
    now: dt.datetime,
    fx_rate: float | None = None,
) -> Listing:
    detail_ids: set[str] = set()
    detail_url = ""
    for link in card.select("a[href]"):
        absolute = urljoin(ROOT_URL, str(link.get("href") or ""))
        match = DETAIL_PATH_RE.fullmatch(urlparse(absolute).path)
        if match is not None:
            detail_ids.add(match.group("id"))
            detail_url = absolute
    if len(detail_ids) != 1 or not detail_url:
        raise VeacomWatchError("Veacom public card has no single stable vehicle-detail URL")
    listing_id = next(iter(detail_ids))
    title = clean(card.select_one("span.name-ar").get_text(" ", strip=True) if card.select_one("span.name-ar") else "")
    if not title:
        raise VeacomWatchError(f"Veacom listing {listing_id} has no public title")
    summary = card_summary_values(card)
    price_box = card.select_one("div.price-ar")
    price_text = clean(price_box.get_text(" ", strip=True) if price_box is not None else "")
    price_amount = positive_integer(price_text)
    price_currency = "CZK" if re.search(r"\bCZK\b", price_text, re.I) else ""
    if price_amount is not None and price_currency != "CZK":
        raise VeacomWatchError(f"Veacom listing {listing_id} has a public price without CZK currency")
    image = card.select_one("img[src]")
    image_url = urljoin(ROOT_URL, str(image.get("src") or "")) if image is not None else ""
    summary_year = parse_year(
        summary.get("první registrace") or summary.get("rok výroby"), now=now
    )
    return Listing(
        listing_id=listing_id,
        url=detail_url,
        title=title,
        image_url=image_url or None,
        price_amount=price_amount,
        price_currency=price_currency,
        price_amount_eur=to_eur(price_amount, fx_rate) if (price_amount is not None and fx_rate) else None,
        summary_year=summary_year,
        summary_mileage_km=parse_mileage_km(summary.get("najeto")),
        summary_fuel=normalize_fuel(summary.get("palivo")),
        event_start=event_start,
        event_end=event_end,
    )


def parse_catalogue(markup: str, *, now: dt.datetime, fx_rate: float | None = None) -> Catalogue:
    soup = BeautifulSoup(markup, "html.parser")
    event_start, event_end = parse_event_bounds(soup, now=now)
    cards = soup.select("div.auction-row")
    if not cards:
        raise VeacomWatchError("Veacom public upcoming-auction page has no item cards")
    listings = tuple(
        card_listing(card, event_start=event_start, event_end=event_end, now=now, fx_rate=fx_rate)
        for card in cards
    )
    ids = [listing.listing_id for listing in listings]
    urls = [listing.url for listing in listings]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise VeacomWatchError("Veacom public catalogue repeats a stable listing identity")
    return Catalogue(listings=listings)


def enumerate_catalogue(session: requests.Session, *, now: dt.datetime, timeout: int, fx_rate: float | None = None) -> Catalogue:
    return parse_catalogue(fetch_markup(session, CATALOGUE_URL, timeout=timeout), now=now, fx_rate=fx_rate)


def detail_values(soup: BeautifulSoup) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in soup.select("div.justify-row"):
        cells = row.find_all("div", recursive=False)
        if len(cells) < 2 or "bold-500" not in (cells[0].get("class") or []):
            continue
        label = clean(cells[0].get_text(" ", strip=True)).casefold()
        value = clean(cells[1].get_text(" ", strip=True))
        if label and value and label not in values:
            values[label] = value
    return values


def product_metadata(soup: BeautifulSoup) -> tuple[str, str | None, str | None]:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.get_text(strip=True)
        if not raw:
            continue
        try:
            payload = parse_public_json_ld(raw)
        except VeacomWatchError:
            continue
        for item in event_payloads(payload):
            if clean(item.get("@type")).casefold() != "product":
                continue
            name = clean(item.get("name"))
            image = clean(item.get("image"))
            offers = item.get("offers")
            offer_url = clean(offers.get("url")) if isinstance(offers, dict) else ""
            return name, image or None, offer_url or None
    return "", None, None


def titles_match(listing_title: str, detail_title: str) -> bool:
    left = clean(listing_title).casefold().replace("…", "")
    right = clean(detail_title).casefold()
    return bool(left and right and (left == right or left in right or right in left))


def parse_seats(value: Any) -> int | None:
    match = SEAT_RE.search(clean(value))
    if match is None:
        return None
    count = int(match.group(1))
    return count if 1 <= count <= 99 else None


def passenger_car_reason(
    *, title: str, fields: dict[str, str], now: dt.datetime
) -> str | None:
    detail_text = " ".join((title, fields.get("stav", ""), fields.get("závady", "")))
    if NON_CAR_TEXT_RE.search(detail_text):
        return "explicit_non_car_text"
    if HEAVY_TRUCK_MODEL_RE.search(title):
        return "heavy_truck_model"
    if COMMERCIAL_MODEL_RE.search(title):
        return "commercial_model"
    seats = parse_seats(fields.get("počet sedadel"))
    if seats is None or seats < 2 or seats > 7:
        return "passenger_seat_count"
    vehicle_mass = parse_weight_kg(fields.get("nejvyšší povolená hmotnost"))
    if vehicle_mass is not None and vehicle_mass > PASSENGER_VEHICLE_MASS_LIMIT_KG:
        return "heavy_vehicle_mass"
    mileage_text = fields.get("najeto", "")
    if MOTOUR_RE.search(mileage_text):
        return "motohours"
    mileage = parse_mileage_km(mileage_text)
    registration_year = parse_year(fields.get("první registrace"), now=now)
    known_make = PASSENGER_MAKE_RE.search(title) is not None
    if mileage is None and not known_make:
        return "no_road_mileage_or_known_make"
    if registration_year is None and not known_make:
        return "no_road_registration_or_known_make"
    if seats == 2 and not known_make:
        return "two_seats_without_passenger_make"
    return None


def detail_to_row(
    markup: str, *, listing: Listing, observed_at: str, now: dt.datetime
) -> tuple[dict[str, Any] | None, str | None]:
    soup = BeautifulSoup(markup, "html.parser")
    fields = detail_values(soup)
    detail_title = fields.get("jméno")
    if not detail_title:
        raise VeacomWatchError(f"Veacom detail {listing.listing_id} has no official name field")
    if not titles_match(listing.title, detail_title):
        raise VeacomWatchError(
            f"Veacom detail {listing.listing_id} title does not match its public catalogue card"
        )
    product_title, product_image, product_url = product_metadata(soup)
    if product_title and not titles_match(detail_title, product_title):
        raise VeacomWatchError(f"Veacom detail {listing.listing_id} JSON-LD title disagrees with fields")
    if product_url and urlparse(product_url).path != urlparse(listing.url).path:
        raise VeacomWatchError(f"Veacom detail {listing.listing_id} JSON-LD URL disagrees with card URL")
    reason = passenger_car_reason(title=detail_title, fields=fields, now=now)
    if reason is not None:
        return None, reason
    registration_year = parse_year(fields.get("první registrace"), now=now)
    mileage = parse_mileage_km(fields.get("najeto"))
    fuel = normalize_fuel(fields.get("palivo"))
    image_url = product_image or listing.image_url
    return {
        "id": f"{SOURCE_KEY}:{listing.listing_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": listing.url,
        "title": detail_title,
        "model": detail_title,
        "country": "CZ",
        "asset_country": "CZ",
        "category": "car",
        "category_raw": "Veacom public road-passenger-car detail fields",
        "year": registration_year or listing.summary_year,
        "mileage": mileage or listing.summary_mileage_km,
        "mileage_km": mileage or listing.summary_mileage_km,
        "fuel": fuel if fuel != "unknown" else listing.summary_fuel,
        "seller": SOURCE_NAME,
        "image_url": image_url,
        "price_amount": listing.price_amount_eur if listing.price_amount_eur is not None else listing.price_amount,
        "price_currency": "EUR" if listing.price_amount_eur is not None else listing.price_currency,
        "price_eur": listing.price_amount_eur,
        "price_kind": "starting_bid" if listing.price_amount is not None else "unknown",
        "price_label": (
            "Veacom public starting price" if listing.price_amount is not None
            else "Veacom public card has no visible starting price"
        ),
        "bid_visibility": "public Veacom upcoming-auction card",
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": "Official Veacom auction lot; inspect the linked lot for all terms and fees.",
        "auction_status": "upcoming" if listing.event_start and listing.event_start > now else "active",
        "canonical_end_utc": listing.event_end.isoformat(),
        "sale_end_utc": listing.event_end.isoformat(),
        "sale_event_utc": listing.event_start.isoformat() if listing.event_start else None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official Veacom passenger-car listing. Confirm vehicle condition, auction terms, "
            "fees, buyer requirements, and Algerian import eligibility before bidding."
        ),
        "access_sale_note": "Open the official Veacom listing to inspect all auction and pickup terms.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-upcoming-auction:{listing.listing_id}",
        "evidence": "Official Veacom public upcoming-auction card and road-vehicle detail fields.",
    }, None


def fetch_detail_for_listing(
    session: requests.Session, *, listing: Listing, observed_at: str, now: dt.datetime, timeout: int
) -> tuple[dict[str, Any] | None, str | None]:
    return detail_to_row(
        fetch_markup(session, listing.url, timeout=timeout),
        listing=listing,
        observed_at=observed_at,
        now=now,
    )


def classify_all_listings(
    listings: tuple[Listing, ...],
    *,
    supplied_session: requests.Session | None,
    observed_at: str,
    now: dt.datetime,
    timeout: int,
    workers: int,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    results: list[tuple[dict[str, Any] | None, str | None]] = []
    if supplied_session is not None:
        for listing in listings:
            results.append(
                fetch_detail_for_listing(
                    supplied_session,
                    listing=listing,
                    observed_at=observed_at,
                    now=now,
                    timeout=timeout,
                )
            )
    else:
        def worker(listing: Listing) -> tuple[dict[str, Any] | None, str | None]:
            session = configured_session()
            try:
                return fetch_detail_for_listing(
                    session,
                    listing=listing,
                    observed_at=observed_at,
                    now=now,
                    timeout=timeout,
                )
            finally:
                session.close()

        with futures.ThreadPoolExecutor(max_workers=workers) as executor:
            pending = [executor.submit(worker, listing) for listing in listings]
            for pending_result in pending:
                results.append(pending_result.result())
    rows = [row for row, _ in results if row is not None]
    rejected = Counter(reason for _, reason in results if reason is not None)
    if len(rows) + sum(rejected.values()) != len(listings):
        raise VeacomWatchError("Veacom classification did not account for every public listing")
    ids = [str(row["id"]) for row in rows]
    urls = [str(row["url"]) for row in rows]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise VeacomWatchError("Veacom passenger-car rows have duplicate stable identities")
    return rows, rejected


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    fx_rates: dict[str, tuple[float, str]] | None = None,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be positive")
    if fx_rates is not None and "CZK" in fx_rates:
        fx_rate, fx_date = fx_rates["CZK"]
    else:
        fx_rate, fx_date = fetch_ecb_units_per_eur("CZK")
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        first = enumerate_catalogue(active_session, now=current, timeout=timeout, fx_rate=fx_rate)
        second = enumerate_catalogue(active_session, now=current, timeout=timeout, fx_rate=fx_rate)
        if second.fingerprint != first.fingerprint:
            raise VeacomWatchError("Veacom catalogue changed during final reconciliation")
        rows, rejected = classify_all_listings(
            first.listings,
            supplied_session=supplied_session,
            observed_at=observed_at,
            now=current,
            timeout=timeout,
            workers=workers,
        )
        report = {
            "status": "ok",
            "connector_status": "ok",
            "catalogue_scope": "every public current Veacom upcoming-auction card",
            "declared": len(first.listings),
            "publicly_listed": len(first.listings),
            "visited": len(first.listings),
            "detail_pages_checked": len(first.listings),
            "normalized_rows": len(rows),
            "source_excluded": dict(sorted(rejected.items())),
            "full_catalogue_rechecked": True,
            "stable_ids_unique": True,
            "passenger_car_classification_complete": True,
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
    parser = argparse.ArgumentParser(description="Fetch every current public Veacom passenger-car lot")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout, workers=args.workers)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "VEACOM_WATCH_PASS",
        "row_count": payload["row_count"],
        "listed": payload["source_reports"][SOURCE_KEY]["publicly_listed"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
