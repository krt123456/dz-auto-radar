#!/usr/bin/env python3
"""Broad official-auction watch for Belgium, Poland, and Portugal.

The connector is deliberately conservative about price semantics:

* Fin Shop public-sale catalogues accept sealed submissions and expose no
  current/highest bid, so their rows have ``price_kind=sealed_bid`` and null
  prices.
* The Polish bailiff search cards expose an opening value, not a live bid.
* e-Leiloes is read through its official public API with the portal's missing
  TLS intermediate supplied alongside the normal system trust roots. Online
  auction bids and private-negotiation minimum values remain distinct.

This is a broad watch, not the strict Algeria-import lane.  Old and diesel
vehicles remain visible and carry an eligibility reason.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import html
import json
import os
import re
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


UTC = dt.timezone.utc
HEADERS = {
    "User-Agent": "DZ-Auto-Radar/1.0 (+official public-auction monitor)",
    "Accept-Language": "fr-BE,fr;q=0.9,pl-PL;q=0.8,pt-PT;q=0.7,en;q=0.5",
    "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
}

FINSHOP_INDEX_URL = "https://finshop.belgium.be/event?date=all"
FINSHOP_ORIGIN = "https://finshop.belgium.be"
POLAND_SEARCH_URL = "https://licytacje.komornik.pl/wyszukiwarka-licytacji"
PORTUGAL_HOME_URL = "https://www.e-leiloes.pt/"
PORTUGAL_API_URL = "https://www.e-leiloes.pt/api/Eventos/"
PORTUGAL_RULES_URL = "https://www.e-leiloes.pt/api/Text/Regras/"
DEFAULT_E_LEILOES_INTERMEDIATE_CA = Path(
    "/opt/sonardeals-radar/certs/sectigo-public-server-authentication-ca-dv-r36.pem"
)
E_LEILOES_PAGE_SIZE = 12
E_LEILOES_MAX_RECORDS = 5_000
E_LEILOES_CAR_SUBTYPES = {
    9: "passenger_car",
    10: "light_commercial",
    11: "heavy_vehicle",
    30: "other_registered_vehicle",
}
E_LEILOES_EXCLUDED_SUBTYPES = {
    12: "aircraft",
    13: "motorcycle",
    14: "agricultural_tractor",
    29: "boat",
}
E_LEILOES_MODALITIES = {
    1: "Leilao Online",
    2: "Negociacao Particular",
}
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

SOURCE_NAMES = {
    "finshop": "Fin Shop (Belgian Federal Public Service Finance)",
    "licytacje-komornik": "Portal Obwieszczen i Licytacji Komorniczych",
    "e-leiloes": "e-Leiloes (OSAE Portugal)",
}
SOURCE_CHOICES = tuple(SOURCE_NAMES)
PREVIOUS_SNAPSHOT_MAX_AGE = dt.timedelta(hours=8)
POLAND_PAGE_RETRIES = 3
POLAND_RETRY_BASE_DELAY_SECONDS = 0.2


class _CentralEuropeFallback(dt.tzinfo):
    @staticmethod
    def _last_sunday(year: int, month: int) -> dt.datetime:
        following = dt.datetime(year + (month == 12), month % 12 + 1, 1)
        last = following - dt.timedelta(days=1)
        return last - dt.timedelta(days=(last.weekday() + 1) % 7)

    def dst(self, value: dt.datetime | None) -> dt.timedelta:
        if value is None:
            return dt.timedelta(0)
        naive = value.replace(tzinfo=None)
        start = self._last_sunday(naive.year, 3).replace(hour=2)
        end = self._last_sunday(naive.year, 10).replace(hour=3)
        return dt.timedelta(hours=1) if start <= naive < end else dt.timedelta(0)

    def utcoffset(self, value: dt.datetime | None) -> dt.timedelta:
        return dt.timedelta(hours=1) + self.dst(value)

    def tzname(self, value: dt.datetime | None) -> str:
        return "CEST" if self.dst(value) else "CET"


class _PortugalFallback(dt.tzinfo):
    """Mainland Portugal fallback for hosts without the IANA tz database."""

    @staticmethod
    def _last_sunday(year: int, month: int) -> dt.datetime:
        following = dt.datetime(year + (month == 12), month % 12 + 1, 1)
        last = following - dt.timedelta(days=1)
        return last - dt.timedelta(days=(last.weekday() + 1) % 7)

    def dst(self, value: dt.datetime | None) -> dt.timedelta:
        if value is None:
            return dt.timedelta(0)
        naive = value.replace(tzinfo=None)
        # Current EU rules: clocks jump from 01:00 to 02:00 local on the
        # last Sunday in March and return at 02:00 local in October.
        start = self._last_sunday(naive.year, 3).replace(hour=2)
        end = self._last_sunday(naive.year, 10).replace(hour=2)
        return dt.timedelta(hours=1) if start <= naive < end else dt.timedelta(0)

    def utcoffset(self, value: dt.datetime | None) -> dt.timedelta:
        return self.dst(value)

    def tzname(self, value: dt.datetime | None) -> str:
        return "WEST" if self.dst(value) else "WET"


try:
    CENTRAL_EUROPE: dt.tzinfo = ZoneInfo("Europe/Brussels")
except ZoneInfoNotFoundError:
    CENTRAL_EUROPE = _CentralEuropeFallback()

try:
    PORTUGAL_TIME: dt.tzinfo = ZoneInfo("Europe/Lisbon")
except ZoneInfoNotFoundError:
    PORTUGAL_TIME = _PortugalFallback()


def clean(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(unicodedata.normalize("NFKC", text).replace("\ufffd", " ").split())


def folded(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value)).casefold()
    return "".join(character for character in text if not unicodedata.combining(character))


def parse_iso(
    value: Any,
    *,
    naive_timezone: dt.tzinfo = CENTRAL_EUROPE,
) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed.astimezone(UTC)


def three_year_cutoff(reference: dt.date) -> dt.date:
    try:
        return reference.replace(year=reference.year - 3)
    except ValueError:
        return reference.replace(year=reference.year - 3, day=28)


def extract_year(value: Any, *, reference_year: int) -> int | None:
    years = [
        int(match.group(1))
        for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", clean(value))
    ]
    years = [year for year in years if 1950 <= year <= reference_year + 1]
    return max(years) if years else None


def extract_mileage(value: Any) -> int | None:
    text = clean(value)
    patterns = (
        r"(?:przebieg|kilometrage|kilometerstand)\s*[:=-]?\s*([\d .]{2,12})\s*km\b",
        r"\b([\d][\d .]{1,10})\s*km\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        try:
            amount = int(re.sub(r"\D", "", match.group(1)))
        except ValueError:
            continue
        if 0 <= amount < 2_000_000:
            return amount
    return None


def normalize_fuel(value: Any) -> str:
    text = folded(value)
    diesel = bool(re.search(
        r"\b(?:diesel|olej napedowy|gasoleo|gazole|cdi|tdi|dci|hdi|bluehdi)\b",
        text,
    )) or bool(re.search(r"\b\d{2,4}\s*d\b", text))
    petrol = bool(re.search(r"\b(?:benzyna|essence|gasolina|petrol)\b", text))
    electric = bool(re.search(r"\b(?:elektryczn\w*|electrique|electric|bev)\b", text))
    hybrid = bool(re.search(r"\b(?:hybryd\w*|hybride|hybrid|phev|hev)\b", text))
    lpg = bool(re.search(r"\b(?:lpg|gpl)\b", text))
    if diesel and (electric or hybrid):
        return "diesel/electric hybrid"
    if diesel:
        return "diesel"
    if hybrid or (petrol and electric):
        return "petrol/electric hybrid"
    if electric:
        return "electric"
    if petrol and lpg:
        return "petrol/lpg"
    if petrol:
        return "petrol"
    return "unknown"


def classify_eligibility(
    *,
    now: dt.datetime,
    year: int | None,
    fuel: str,
    text: str,
    professional_only: bool = False,
) -> tuple[str, str]:
    if professional_only:
        return (
            "not_eligible",
            "The official Fin Shop event is restricted to automotive professionals.",
        )
    if fuel.startswith("diesel"):
        return "not_eligible", "Diesel is outside Algeria's used-vehicle import fuel gate."
    lowered = folded(text)
    if re.search(
        r"\b(?:uszkodzon\w*|non roulant|non roulante|pour pieces|epave|"
        r"avariad\w*|sinistrad\w*|salvad\w*|nao circula|nao funciona|para pecas)\b",
        lowered,
    ):
        return "not_eligible", "The official title states damage or a non-running/parts condition."
    cutoff = three_year_cutoff(now.date())
    if year is not None and year < cutoff.year:
        return (
            "not_eligible",
            f"Model/registration year {year} is older than the rolling three-year window.",
        )
    if year is None:
        return (
            "unknown",
            "The summary does not establish the first-registration year, fuel, documents, condition, bidder access, and exportability.",
        )
    if year == cutoff.year:
        return (
            "unknown",
            f"Year {year} needs the exact first-registration date; fuel, documents, condition, bidder access, and exportability also require verification.",
        )
    if fuel == "unknown":
        return (
            "unknown",
            "Recent-year candidate, but the summary does not establish fuel, exact first registration, documents, condition, bidder access, and exportability.",
        )
    return (
        "conditional",
        "Recent non-diesel candidate; verify exact first registration, documents, condition, bidder access, payment, and export before treating it as import-eligible.",
    )


def _official_response_text(
    session: requests.Session,
    url: str,
    *,
    allowed_hosts: set[str],
    timeout: int,
) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    final_host = (urlparse(response.url or url).hostname or "").lower()
    if final_host not in allowed_hosts:
        raise ValueError(f"official endpoint redirected to unapproved host {final_host!r}")
    return response.text


def fetch_ecb_rates(session: requests.Session, *, timeout: int) -> dict[str, float]:
    response = session.get(ECB_URL, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    rates = {"EUR": 1.0}
    for node in ET.fromstring(response.content).iter():
        currency, raw_rate = node.attrib.get("currency"), node.attrib.get("rate")
        if currency and raw_rate:
            rates[currency.upper()] = float(raw_rate)
    return rates


# ---------------------------------------------------------------------------
# Fin Shop Belgium


_FINSHOP_MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "oktober": 10,
}


class _FinShopProductParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        values = {name.lower(): value or "" for name, value in attrs}
        source = values.get("src", "")
        match = re.search(r"/web/image/product\.template/(\d+)/image_1024/([^?]+)", source)
        if not match:
            return
        product_id = match.group(1)
        title = clean(values.get("alt") or unquote(match.group(2)))
        if title:
            self.products[product_id] = title


def discover_finshop_event_urls(markup: str) -> list[str]:
    urls: dict[str, str] = {}
    for match in re.finditer(r"href=[\"']([^\"']*?/event/[^\"']+/(?:register|tickets))[^\"']*[\"']", markup, re.I):
        url = urljoin(FINSHOP_ORIGIN + "/", html.unescape(match.group(1)))
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "finshop.belgium.be":
            continue
        id_match = re.search(r"-(\d+)/(?:register|tickets)$", parsed.path)
        key = id_match.group(1) if id_match else parsed.path
        urls[key] = url
    return sorted(urls.values())


def _is_finshop_vehicle_event(url: str) -> bool:
    path = folded(urlparse(url).path)
    return bool(re.search(r"(?:vehicul|voertuig|automobil)", path))


def parse_finshop_sale_at(markup: str) -> dt.datetime | None:
    text = folded(markup)
    match = re.search(
        r"\b(\d{1,2})\s+([a-z]+)\s+(20\d{2})\s+(\d{1,2}):(\d{2})\b",
        text,
    )
    if not match:
        return None
    month = _FINSHOP_MONTHS.get(match.group(2))
    if month is None:
        return None
    try:
        local = dt.datetime(
            int(match.group(3)), month, int(match.group(1)),
            int(match.group(4)), int(match.group(5)), tzinfo=CENTRAL_EUROPE,
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def _finshop_event_title(markup: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", markup, re.I | re.S)
    return clean(match.group(1)).split(" | ", 1)[0] if match else "Fin Shop vehicle sale"


_NON_CAR_FINSHOP_RE = re.compile(
    r"\b(?:moto|motorfiets|scooter|quad|bike|maxsym|ww\s*125\w*|jet\s*14|"
    r"yamaha\s+max\s*125|kawasaki\s+er\s*5)\b",
    re.I,
)


def _finshop_is_car_or_truck(title: str) -> bool:
    return not _NON_CAR_FINSHOP_RE.search(folded(title))


def _finshop_model(title: str) -> str:
    value = re.sub(r"^\[[^]]+\]\s*", "", clean(title))
    value = re.sub(r"^(?:ref\.?\s*[^-]+|lot\s+[^-]+)\s*-\s*", "", value, flags=re.I)
    value = re.split(r"\s+-\s+(?:19|20)\d{2}\b", value, maxsplit=1)[0]
    return value.strip(" -")


def finshop_product_to_row(
    *,
    product_id: str,
    raw_title: str,
    event_url: str,
    event_title: str,
    sale_at: dt.datetime,
    now: dt.datetime,
    professional_only: bool,
) -> dict[str, Any] | None:
    if not product_id.isdigit() or sale_at <= now or not _finshop_is_car_or_truck(raw_title):
        return None
    title = re.sub(r"^\[[^]]+\]\s*", "", clean(raw_title))
    year = extract_year(title, reference_year=now.year)
    fuel = normalize_fuel(title)
    status, reason = classify_eligibility(
        now=now,
        year=year,
        fuel=fuel,
        text=title,
        professional_only=professional_only,
    )
    return {
        "id": f"finshop:event:{product_id}",
        "source": "finshop",
        "source_key": "finshop",
        "source_name": SOURCE_NAMES["finshop"],
        "url": f"{event_url}#product-template-{product_id}",
        "title": title,
        "model": _finshop_model(title),
        "country": "BE",
        "year": year,
        "mileage_km": extract_mileage(title),
        "fuel": fuel,
        "price_amount": None,
        "price_currency": "EUR",
        "price_eur": None,
        "price_kind": "sealed_bid",
        "price_label": "Sealed submission; no public current bid",
        "bid_visibility": "sealed_submission_no_live_price",
        "sale_end_at": sale_at.isoformat(),
        "canonical_end_utc": sale_at.isoformat(),
        "last_seen_at": now.isoformat(),
        "eligibility_status": status,
        "eligibility_reason": reason,
        "access_sale_note": (
            "Official Fin Shop public sale by sealed submission. No current/highest bid is published; "
            "verify the catalogue, bidder restriction, 20% fee, payment, collection, documents, and export."
        ),
        "evidence": (
            "Official Fin Shop Odoo event page, scheduled sale time, and product-template catalogue; "
            "no price is interpreted as a current bid."
        ),
        "event_title": event_title,
        "professional_only": professional_only,
    }


def harvest_finshop(
    session: requests.Session,
    *,
    now: dt.datetime,
    timeout: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index = _official_response_text(
        session,
        FINSHOP_INDEX_URL,
        allowed_hosts={"finshop.belgium.be"},
        timeout=timeout,
    )
    discovered = discover_finshop_event_urls(index)
    vehicle_events = [url for url in discovered if _is_finshop_vehicle_event(url)]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    catalogue_products = 0
    excluded_non_car = 0
    active_events = 0
    for event_url in vehicle_events:
        try:
            markup = _official_response_text(
                session,
                event_url,
                allowed_hosts={"finshop.belgium.be"},
                timeout=timeout,
            )
            sale_at = parse_finshop_sale_at(markup)
            if sale_at is None:
                raise ValueError("event sale date is missing")
            if sale_at <= now:
                continue
            parser = _FinShopProductParser()
            parser.feed(markup)
            if not parser.products:
                raise ValueError("vehicle event contains no product-template catalogue")
            active_events += 1
            catalogue_products += len(parser.products)
            event_title = _finshop_event_title(markup)
            professional_only = bool(re.search(r"profession", folded(event_title + " " + markup)))
            for product_id, title in parser.products.items():
                if not _finshop_is_car_or_truck(title):
                    excluded_non_car += 1
                    continue
                row = finshop_product_to_row(
                    product_id=product_id,
                    raw_title=title,
                    event_url=event_url,
                    event_title=event_title,
                    sale_at=sale_at,
                    now=now,
                    professional_only=professional_only,
                )
                if row:
                    rows.append(row)
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{event_url}:{type(exc).__name__}:{str(exc)[:180]}")
    unique = {row["id"]: row for row in rows}
    return list(unique.values()), {
        "status": "ok" if not errors else "partial",
        "index_url": FINSHOP_INDEX_URL,
        "event_pages_discovered": len(discovered),
        "vehicle_event_pages": len(vehicle_events),
        "active_vehicle_events": active_events,
        "catalogue_products": catalogue_products,
        "excluded_non_car_or_truck": excluded_non_car,
        "current_or_future_rows": len(unique),
        "price_semantics": "sealed submission; no public live/current bid",
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Polish National Council of Bailiffs auction portal


def _decode_nuxt_ref(data: list[Any], index: int, cache: dict[int, Any]) -> Any:
    if index < 0:
        return None
    if index >= len(data):
        raise ValueError("Nuxt reference is outside the payload")
    if index in cache:
        return cache[index]
    raw = data[index]
    if isinstance(raw, dict):
        result: dict[str, Any] = {}
        cache[index] = result
        for key, value in raw.items():
            result[key] = _decode_nuxt_ref(data, value, cache) if isinstance(value, int) else value
        return result
    if isinstance(raw, list):
        if raw and isinstance(raw[0], str) and raw[0] in {
            "ShallowReactive", "Reactive", "Ref", "EmptyRef",
        }:
            return _decode_nuxt_ref(data, raw[1], cache) if len(raw) > 1 and isinstance(raw[1], int) else None
        result_list: list[Any] = []
        cache[index] = result_list
        for value in raw:
            result_list.append(
                _decode_nuxt_ref(data, value, cache) if isinstance(value, int) else value
            )
        return result_list
    return raw


def parse_poland_search_page(
    markup: str,
    *,
    category: str,
) -> tuple[list[dict[str, Any]], int, dict[int, str]]:
    script_match = re.search(
        r"<script\b[^>]*(?:id=[\"']__NUXT_DATA__[\"']|data-nuxt-data=[\"']nuxt-app[\"'])[^>]*>(.*?)</script>",
        markup,
        re.I | re.S,
    )
    if not script_match:
        raise ValueError("official Polish page has no Nuxt search payload (possibly blocked)")
    try:
        data = json.loads(script_match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("official Polish Nuxt payload is not valid JSON") from exc
    if not isinstance(data, list):
        raise ValueError("official Polish Nuxt payload is not a reference array")
    container = next((
        value for value in data
        if isinstance(value, dict) and {"items", "count"}.issubset(value)
    ), None)
    if not isinstance(container, dict):
        raise ValueError("official Polish search payload has no items/count container")
    item_ref = container.get("items")
    count_ref = container.get("count")
    if not isinstance(item_ref, int) or not isinstance(count_ref, int):
        raise ValueError("official Polish search references are invalid")
    item_indexes = data[item_ref]
    advertised_count = data[count_ref]
    if not isinstance(item_indexes, list) or not isinstance(advertised_count, int):
        raise ValueError("official Polish search pagination schema changed")
    cache: dict[int, Any] = {}
    items = [
        _decode_nuxt_ref(data, index, cache)
        for index in item_indexes
        if isinstance(index, int)
    ]
    items = [item for item in items if isinstance(item, dict)]
    if any(str(item.get("subCategory") or "") != category for item in items):
        raise ValueError(f"official Polish page did not apply {category} filter")
    links: dict[int, str] = {}
    for match in re.finditer(r"href=[\"'](/licytacje/(\d+)/[^\"'#?]+)[\"']", markup, re.I):
        links[int(match.group(2))] = urljoin(POLAND_SEARCH_URL, html.unescape(match.group(1)))
    return items, advertised_count, links


def fetch_poland_category(
    session: requests.Session,
    *,
    category: str,
    timeout: int,
    page_size: int = 100,
    max_pages: int = 50,
) -> tuple[list[dict[str, Any]], dict[int, str], int, int]:
    if category not in {"CARS", "TRUCKS"}:
        raise ValueError("Polish category must be CARS or TRUCKS")
    items: dict[int, dict[str, Any]] = {}
    links: dict[int, str] = {}
    advertised_count: int | None = None
    pages = 0
    for page in range(max_pages):
        offset = page * page_size
        query = (
            f"{POLAND_SEARCH_URL}?mainCategory=MOVABLE&subCategory={category}"
            f"&limit={page_size}&offset={offset}"
        )
        # The official Polish endpoint intermittently drops TLS connections.
        # Retry only transport-level SSL/connection failures; HTTP errors and
        # parser/schema failures remain visible immediately.  This deliberately
        # keeps normal certificate and hostname verification enabled.
        for retry_number in range(POLAND_PAGE_RETRIES + 1):
            try:
                markup = _official_response_text(
                    session,
                    query,
                    allowed_hosts={"licytacje.komornik.pl"},
                    timeout=timeout,
                )
                break
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
                if retry_number >= POLAND_PAGE_RETRIES:
                    raise
                time.sleep(POLAND_RETRY_BASE_DELAY_SECONDS * (2 ** retry_number))
        page_items, count, page_links = parse_poland_search_page(markup, category=category)
        pages += 1
        if advertised_count is None:
            advertised_count = count
            if advertised_count > page_size * max_pages:
                raise ValueError("Polish result count exceeds pagination safety limit")
        for item in page_items:
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            items[item_id] = item
        links.update(page_links)
        if offset + len(page_items) >= count or len(page_items) < page_size:
            break
    else:
        raise ValueError("Polish pagination did not terminate")
    return list(items.values()), links, int(advertised_count or 0), pages


def _slugify(value: str) -> str:
    text = folded(value)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:120] or "pojazd"


def poland_item_to_row(
    item: dict[str, Any],
    *,
    category: str,
    detail_url: str | None,
    now: dt.datetime,
    pln_rate: float,
) -> dict[str, Any] | None:
    try:
        item_id = int(item.get("id"))
    except (TypeError, ValueError):
        return None
    title = clean(item.get("title"))
    if item_id <= 0 or not title:
        return None
    status_name = str(item.get("status") or "").upper()
    if status_name.startswith("CLOSED") or status_name in {"CANCELLED", "NOT_EVENTUATED"}:
        return None
    end = parse_iso(item.get("endAuctionAt")) or parse_iso(item.get("startAuctionAt"))
    if end is None or end <= now:
        return None
    try:
        opening = float(item.get("openingValue"))
    except (TypeError, ValueError):
        opening = 0.0
    if opening <= 0 or pln_rate <= 0:
        return None
    year = extract_year(title, reference_year=now.year)
    fuel = normalize_fuel(title)
    eligibility, reason = classify_eligibility(
        now=now,
        year=year,
        fuel=fuel,
        text=title,
    )
    if detail_url is None:
        detail_url = f"https://licytacje.komornik.pl/licytacje/{item_id}/{quote(_slugify(title))}"
    eauction = bool(item.get("eauction"))
    joinable = bool(item.get("joinable"))
    return {
        "id": f"licytacje-komornik:{item_id}",
        "source": "licytacje-komornik",
        "source_key": "licytacje-komornik",
        "source_name": SOURCE_NAMES["licytacje-komornik"],
        "url": detail_url,
        "title": title,
        "model": title,
        "country": "PL",
        "year": year,
        "mileage_km": extract_mileage(title),
        "fuel": fuel,
        "price_amount": round(opening, 2),
        "price_currency": "PLN",
        "price_eur": round(opening / pln_rate, 2),
        "price_kind": "starting_bid",
        "price_label": "Cena wywolania (opening price)",
        "bid_visibility": "opening_price_only_no_live_bid",
        "sale_end_at": end.isoformat(),
        "canonical_end_utc": end.isoformat(),
        "last_seen_at": now.isoformat(),
        "eligibility_status": eligibility,
        "eligibility_reason": reason,
        "access_sale_note": (
            "Official Polish bailiff auction search card. The amount is the opening price, not a live bid; "
            + ("electronic" if eauction else "stationary/in-person")
            + " participation, deposit, identity, documents, collection, and export must be checked."
        ),
        "evidence": (
            "Official licytacje.komornik.pl CARS/TRUCKS filtered Nuxt search payload: "
            "openingValue, start/end time, status, category, and participation flags."
        ),
        "official_status": status_name,
        "vehicle_category": category,
        "eauction": eauction,
        "joinable": joinable,
        "estimate_pln": item.get("estimate"),
        "province": clean(item.get("province")),
    }


def harvest_poland(
    session: requests.Session,
    *,
    now: dt.datetime,
    timeout: int,
    rates: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if rates is None:
        rates = fetch_ecb_rates(session, timeout=timeout)
    pln_rate = rates.get("PLN")
    if not pln_rate or pln_rate <= 0:
        raise ValueError("official ECB feed did not provide a positive PLN rate")
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    category_reports: dict[str, Any] = {}
    for category in ("CARS", "TRUCKS"):
        try:
            items, links, advertised_count, pages = fetch_poland_category(
                session,
                category=category,
                timeout=timeout,
            )
            category_rows = []
            for item in items:
                try:
                    item_id = int(item.get("id"))
                except (TypeError, ValueError):
                    item_id = 0
                row = poland_item_to_row(
                    item,
                    category=category,
                    detail_url=links.get(item_id),
                    now=now,
                    pln_rate=pln_rate,
                )
                if row:
                    category_rows.append(row)
            rows.extend(category_rows)
            category_reports[category] = {
                "advertised_count": advertised_count,
                "items_fetched": len(items),
                "current_or_future_rows": len(category_rows),
                "pages": pages,
            }
        except (requests.RequestException, ValueError) as exc:
            error = f"{category}:{type(exc).__name__}:{str(exc)[:200]}"
            errors.append(error)
            category_reports[category] = {"current_or_future_rows": 0, "error": error}
    unique = {row["id"]: row for row in rows}
    return list(unique.values()), {
        "status": "ok" if not errors else ("partial" if unique else "error"),
        "search_url": POLAND_SEARCH_URL,
        "categories": category_reports,
        "current_or_future_rows": len(unique),
        "price_semantics": "opening price only; no live/current bid is used",
        "ecb_pln_per_eur": pln_rate,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Portugal e-Leiloes


@contextlib.contextmanager
def e_leiloes_verify_bundle(
    intermediate_ca: str | Path,
) -> Iterable[str]:
    """Yield a temporary CA bundle containing normal roots plus the missing CA.

    The official server currently omits its Sectigo intermediate.  Requests
    must still perform normal chain and hostname verification; ``verify=False``
    is never used.
    """
    intermediate_path = Path(intermediate_ca)
    roots_path = Path(requests.certs.where())
    try:
        roots = roots_path.read_bytes()
        intermediate = intermediate_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"e-Leiloes TLS CA material is unavailable: {exc}") from exc
    if b"-----BEGIN CERTIFICATE-----" not in roots:
        raise ValueError("the Requests CA bundle is not a PEM certificate bundle")
    if (
        intermediate.count(b"-----BEGIN CERTIFICATE-----") != 1
        or intermediate.count(b"-----END CERTIFICATE-----") != 1
    ):
        raise ValueError("the e-Leiloes intermediate CA is not one PEM certificate")

    handle = tempfile.NamedTemporaryFile(
        mode="wb", prefix="dz-auto-e-leiloes-ca-", suffix=".pem", delete=False,
    )
    temporary_path = Path(handle.name)
    try:
        with handle:
            handle.write(roots.rstrip() + b"\n")
            handle.write(intermediate.rstrip() + b"\n")
        yield str(temporary_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _e_leiloes_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str] | None,
    timeout: int,
    verify: str,
) -> dict[str, Any]:
    response = session.get(
        url,
        headers=HEADERS,
        params=params,
        timeout=timeout,
        verify=verify,
    )
    response.raise_for_status()
    host = (urlparse(response.url or url).hostname or "").lower()
    if host not in {"e-leiloes.pt", "www.e-leiloes.pt"}:
        raise ValueError(f"e-Leiloes redirected to unapproved host {host!r}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("official e-Leiloes endpoint did not return JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("official e-Leiloes JSON root is not an object")
    if payload.get("errors") is True or payload.get("exception") is True:
        raise ValueError("official e-Leiloes API reported an error")
    return payload


def fetch_e_leiloes_catalogue(
    session: requests.Session,
    *,
    timeout: int,
    verify: str,
    page_size: int = E_LEILOES_PAGE_SIZE,
    max_records: int = E_LEILOES_MAX_RECORDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch every active type-2 record through stable ID pagination."""
    if page_size != E_LEILOES_PAGE_SIZE:
        raise ValueError(f"e-Leiloes page size must be {E_LEILOES_PAGE_SIZE}")
    first = 0
    pages = 0
    advertised_totals: set[int] = set()
    items: dict[int, dict[str, Any]] = {}
    while True:
        table_params = {
            "first": first,
            "rows": page_size,
            "sortField": "id",
            "sortOrder": 1,
            "filters": {"tipo": {"value": 2, "matchMode": "equals"}},
        }
        payload = _e_leiloes_json(
            session,
            PORTUGAL_API_URL,
            params={
                "tableParams": json.dumps(
                    table_params, ensure_ascii=False, separators=(",", ":"),
                )
            },
            timeout=timeout,
            verify=verify,
        )
        page = payload.get("list")
        pagination = payload.get("pagination")
        if not isinstance(page, list) or not isinstance(pagination, dict):
            raise ValueError("official e-Leiloes catalogue schema changed")
        try:
            response_first = int(pagination.get("first"))
            advertised_total = int(pagination.get("total"))
        except (TypeError, ValueError) as exc:
            raise ValueError("official e-Leiloes pagination values are invalid") from exc
        if response_first != first:
            raise ValueError("official e-Leiloes returned the wrong page offset")
        if advertised_total < 0 or advertised_total > max_records:
            raise ValueError("official e-Leiloes result count exceeds the safety limit")
        if len(page) > page_size:
            raise ValueError("official e-Leiloes returned an oversized page")
        advertised_totals.add(advertised_total)
        for item in page:
            if not isinstance(item, dict):
                raise ValueError("official e-Leiloes catalogue row is not an object")
            try:
                item_id = int(item.get("id"))
                type_id = int(item.get("tipoId"))
            except (TypeError, ValueError) as exc:
                raise ValueError("official e-Leiloes catalogue row identity is invalid") from exc
            if item_id <= 0 or type_id != 2:
                raise ValueError("official e-Leiloes API did not preserve the vehicle filter")
            items[item_id] = item
        pages += 1
        next_first = first + len(page)
        if next_first >= advertised_total:
            break
        if not page:
            raise ValueError("official e-Leiloes pagination stopped before the advertised total")
        first = next_first
        if pages > (max_records // page_size) + 1:
            raise ValueError("official e-Leiloes pagination did not terminate")

    # A changing total or duplicate/moved rows means this crawl cannot prove it
    # saw the complete catalogue. Fail closed; the next hourly run can retry.
    if len(advertised_totals) != 1:
        raise ValueError("official e-Leiloes catalogue changed during pagination")
    advertised_total = next(iter(advertised_totals), 0)
    if len(items) != advertised_total:
        raise ValueError("official e-Leiloes pagination did not yield every advertised row")
    return [items[key] for key in sorted(items)], {
        "catalogue_total": advertised_total,
        "catalogue_unique_rows": len(items),
        "pages": pages,
        "page_size": page_size,
        "sort": "id ascending",
        "filter": "tipoId=2",
    }


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if number > 0 else None


def e_leiloes_item_to_row(
    item: dict[str, Any],
    *,
    now: dt.datetime,
) -> dict[str, Any] | None:
    try:
        item_id = int(item.get("id"))
        type_id = int(item.get("tipoId"))
        subtype_id = int(item.get("subtipoId"))
        modality_id = int(item.get("modalidadeId"))
    except (TypeError, ValueError):
        return None
    reference = clean(item.get("referencia"))
    title = clean(item.get("titulo"))
    if (
        item_id <= 0
        or type_id != 2
        or subtype_id not in E_LEILOES_CAR_SUBTYPES
        or modality_id not in E_LEILOES_MODALITIES
        or not reference
        or not title
        or bool(item.get("cancelado"))
        or bool(item.get("terminado"))
    ):
        return None
    end = parse_iso(item.get("dataFim"), naive_timezone=PORTUGAL_TIME)
    start = parse_iso(item.get("dataInicio"), naive_timezone=PORTUGAL_TIME)
    if end is None or end <= now:
        return None
    minimum = _positive_float(item.get("valorMinimo"))
    base = _positive_float(item.get("valorBase"))
    public_offer = _positive_float(item.get("lanceAtual"))

    if modality_id == 1 and public_offer is not None:
        price = public_offer
        price_kind = "current_bid"
        price_label = "Lance atual publico (Leilao Online)"
        bid_visibility = "public_current_bid"
    else:
        if minimum is None:
            return None
        price = minimum
        price_kind = "minimum_bid"
        if modality_id == 1:
            price_label = (
                "Valor minimo oficial (85% do valor base); sem lance atual publico"
            )
            bid_visibility = "no_current_bid_minimum_value_only"
        else:
            price_label = (
                "Valor minimo da Negociacao Particular; nao e lance atual de leilao"
            )
            bid_visibility = (
                "highest_private_offer_is_not_relabelled_as_an_auction_bid"
            )

    year = extract_year(title, reference_year=now.year)
    fuel = normalize_fuel(title)
    eligibility, reason = classify_eligibility(
        now=now,
        year=year,
        fuel=fuel,
        text=title,
    )
    portugal_access = (
        "Official platform registration requires a Portuguese NIF, domicile in Portugal, "
        "and an IBAN for a bank account in Portugal; representation and export must be "
        "verified before bidding."
    )
    reason = f"{reason} {portugal_access}"
    if eligibility != "not_eligible":
        eligibility = "unknown"

    mode = E_LEILOES_MODALITIES[modality_id]
    return {
        "id": f"e-leiloes:{item_id}",
        "source": "e-leiloes",
        "source_key": "e-leiloes",
        "source_name": SOURCE_NAMES["e-leiloes"],
        "url": f"https://www.e-leiloes.pt/evento/{quote(reference, safe='')}",
        "title": title,
        "model": title,
        "country": "PT",
        "year": year,
        "mileage_km": extract_mileage(title),
        "fuel": fuel,
        "price_amount": price,
        "price_currency": "EUR",
        "price_eur": price,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": bid_visibility,
        "sale_end_at": end.isoformat(),
        "canonical_end_utc": end.isoformat(),
        "sale_start_at": start.isoformat() if start else None,
        "last_seen_at": now.isoformat(),
        "eligibility_status": eligibility,
        "eligibility_reason": reason,
        "access_sale_note": (
            f"Official e-Leiloes {mode}. {portugal_access} Inspect the asset, documents, "
            "charges, payment, collection, and export terms; the highest offer does not "
            "by itself guarantee adjudication."
        ),
        "evidence": (
            "Official e-leiloes.pt public Eventos API, filtered to vehicle type 2; "
            "official portal rules define current bids, minimum value, and access requirements."
        ),
        "official_reference": reference,
        "official_modality_id": modality_id,
        "official_sale_mode": mode,
        "official_subtype_id": subtype_id,
        "official_vehicle_class": E_LEILOES_CAR_SUBTYPES[subtype_id],
        "official_base_value_eur": base,
        "official_minimum_value_eur": minimum,
        "official_highest_offer_eur": public_offer,
        "official_started": bool(item.get("iniciado")),
        "district": clean(item.get("moradaDistrito")),
        "municipality": clean(item.get("moradaConcelho")),
    }


def harvest_e_leiloes(
    session: requests.Session,
    *,
    now: dt.datetime,
    timeout: int,
    intermediate_ca: str | Path = DEFAULT_E_LEILOES_INTERMEDIATE_CA,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with e_leiloes_verify_bundle(intermediate_ca) as verify:
        items, catalogue = fetch_e_leiloes_catalogue(
            session,
            timeout=timeout,
            verify=verify,
        )

    rows: list[dict[str, Any]] = []
    excluded_subtypes: dict[str, int] = {}
    dropped_non_current = 0
    for item in items:
        try:
            subtype_id = int(item.get("subtipoId"))
        except (TypeError, ValueError):
            subtype_id = -1
        if subtype_id not in E_LEILOES_CAR_SUBTYPES:
            label = E_LEILOES_EXCLUDED_SUBTYPES.get(subtype_id, f"subtype_{subtype_id}")
            excluded_subtypes[label] = excluded_subtypes.get(label, 0) + 1
            continue
        row = e_leiloes_item_to_row(item, now=now)
        if row is None:
            dropped_non_current += 1
            continue
        rows.append(row)
    unique = {row["id"]: row for row in rows}
    online = sum(row.get("official_modality_id") == 1 for row in unique.values())
    private = sum(row.get("official_modality_id") == 2 for row in unique.values())
    current_bids = sum(row.get("price_kind") == "current_bid" for row in unique.values())
    return list(unique.values()), {
        "status": "ok",
        "api_url": PORTUGAL_API_URL,
        "rules_url": PORTUGAL_RULES_URL,
        **catalogue,
        "excluded_non_car_vehicle_subtypes": excluded_subtypes,
        "dropped_cancelled_ended_or_invalid": dropped_non_current,
        "online_auction_rows": online,
        "private_negotiation_rows": private,
        "public_current_bid_rows": current_bids,
        "current_or_future_rows": len(unique),
        "price_semantics": (
            "Online auctions use a positive official lanceAtual as current_bid; otherwise "
            "valorMinimo. Private negotiations always use valorMinimo and never relabel "
            "the highest proposal as an auction bid."
        ),
        "access_semantics": (
            "Official rules require Portuguese NIF, Portuguese domicile, and Portuguese-bank IBAN."
        ),
        "tls_verification": (
            "hostname and certificate chain verified with system roots plus the official "
            "server's missing Sectigo intermediate; verify=False is never used"
        ),
        "errors": [],
    }


def probe_e_leiloes(
    session: requests.Session,
    *,
    timeout: int,
    now: dt.datetime | None = None,
    intermediate_ca: str | Path = DEFAULT_E_LEILOES_INTERMEDIATE_CA,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Backward-compatible name for the now fully verified live harvester."""
    return harvest_e_leiloes(
        session,
        now=(now or dt.datetime.now(UTC)).astimezone(UTC).replace(microsecond=0),
        timeout=timeout,
        intermediate_ca=intermediate_ca,
    )


# ---------------------------------------------------------------------------
# Combined feed and CLI


def build_watch(
    session: requests.Session | None = None,
    *,
    sources: Iterable[str] = SOURCE_CHOICES,
    now: dt.datetime | None = None,
    timeout: int = 25,
    rates: dict[str, float] | None = None,
    e_leiloes_intermediate_ca: str | Path = DEFAULT_E_LEILOES_INTERMEDIATE_CA,
) -> dict[str, Any]:
    session = session or requests.Session()
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC).replace(microsecond=0)
    requested = list(dict.fromkeys(sources))
    unknown = [source for source in requested if source not in SOURCE_CHOICES]
    if unknown:
        raise ValueError(f"unknown source(s): {', '.join(unknown)}")

    rows: list[dict[str, Any]] = []
    reports: dict[str, Any] = {}
    for source in requested:
        try:
            if source == "finshop":
                source_rows, report = harvest_finshop(session, now=now, timeout=timeout)
            elif source == "licytacje-komornik":
                source_rows, report = harvest_poland(
                    session, now=now, timeout=timeout, rates=rates,
                )
            else:
                source_rows, report = harvest_e_leiloes(
                    session,
                    now=now,
                    timeout=timeout,
                    intermediate_ca=e_leiloes_intermediate_ca,
                )
            rows.extend(source_rows)
            reports[source] = report
        except (requests.RequestException, ET.ParseError, ValueError, TypeError) as exc:
            reports[source] = {
                "status": "error",
                "current_or_future_rows": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }

    unique = {row["id"]: row for row in rows}
    output = sorted(
        unique.values(),
        key=lambda row: (row.get("canonical_end_utc") or "9999", row["id"]),
    )
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": now.isoformat(),
        "row_count": len(output),
        "rows": output,
        "source_reports": reports,
    }


def _fresh_previous_snapshot(
    path: Path,
    *,
    now: dt.datetime,
    max_age: dt.timedelta = PREVIOUS_SNAPSHOT_MAX_AGE,
) -> dict[str, Any] | None:
    """Load a structurally valid recent connector snapshot, if one exists."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != 1 or payload.get("lane") != "official_auction_watch":
        return None
    rows = payload.get("rows")
    reports = payload.get("source_reports")
    if not isinstance(rows, list) or not isinstance(reports, dict):
        return None
    if payload.get("row_count") != len(rows):
        return None
    generated_at = parse_iso(payload.get("generated_at_utc"))
    if generated_at is None:
        return None
    age = now - generated_at
    if age < dt.timedelta(0) or age > max_age:
        return None
    for row in rows:
        if not isinstance(row, dict) or not clean(row.get("id")):
            return None
        source = row.get("source_key") or row.get("source")
        if source not in SOURCE_CHOICES:
            return None
    return payload


def apply_previous_snapshot_fallback(
    payload: dict[str, Any],
    previous_path: Path,
    *,
    now: dt.datetime | None = None,
    max_age: dt.timedelta = PREVIOUS_SNAPSHOT_MAX_AGE,
) -> dict[str, Any]:
    """Retain recent rows only for a source whose new connector run failed.

    The fallback is intentionally applied at the CLI snapshot boundary rather
    than inside ``build_watch``.  Row timestamps are never rewritten, so an
    outage cannot keep an old offer alive indefinitely.
    """
    if now is None:
        now = parse_iso(payload.get("generated_at_utc"))
    if now is None or now.tzinfo is None:
        return payload
    now = now.astimezone(UTC).replace(microsecond=0)
    previous = _fresh_previous_snapshot(previous_path, now=now, max_age=max_age)
    if previous is None:
        return payload

    rows = payload.get("rows")
    reports = payload.get("source_reports")
    if not isinstance(rows, list) or not isinstance(reports, dict):
        return payload

    current_source_counts: dict[str, int] = {source: 0 for source in SOURCE_CHOICES}
    existing_ids = {
        str(row.get("id"))
        for row in rows
        if isinstance(row, dict) and row.get("id") is not None
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = row.get("source_key") or row.get("source")
        if source in current_source_counts:
            current_source_counts[source] += 1

    previous_generated_at = str(previous["generated_at_utc"])
    previous_reports = previous.get("source_reports", {})
    for source, report in list(reports.items()):
        if source not in SOURCE_CHOICES or not isinstance(report, dict):
            continue
        try:
            reported_rows = int(report.get("current_or_future_rows", 0))
        except (TypeError, ValueError):
            reported_rows = -1
        if (
            report.get("status") != "error"
            or reported_rows != 0
            or current_source_counts.get(source, 0) != 0
        ):
            continue

        retained: list[dict[str, Any]] = []
        for row in previous["rows"]:
            row_source = row.get("source_key") or row.get("source")
            if row_source != source or str(row.get("id")) in existing_ids:
                continue
            last_seen = parse_iso(row.get("last_seen_at"))
            end_at = parse_iso(row.get("canonical_end_utc") or row.get("sale_end_at"))
            if last_seen is None or not dt.timedelta(0) <= now - last_seen <= max_age:
                continue
            if end_at is not None and end_at <= now:
                continue
            retained.append(row)
            existing_ids.add(str(row["id"]))
        if not retained:
            continue

        rows.extend(retained)
        reports[source] = {
            **report,
            "status": "partial",
            "current_or_future_rows": len(retained),
            "connector_error": dict(report),
            "fallback": {
                "used": True,
                "reason": "connector_error_zero_rows",
                "retained_rows": len(retained),
                "snapshot_generated_at_utc": previous_generated_at,
                "max_age_hours": max_age.total_seconds() / 3600,
                "row_timestamps_preserved": True,
                "previous_source_status": (
                    previous_reports.get(source, {}).get("status")
                    if isinstance(previous_reports.get(source), dict)
                    else None
                ),
            },
        }

    rows.sort(key=lambda row: (row.get("canonical_end_utc") or "9999", row["id"]))
    payload["row_count"] = len(rows)
    return payload


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch current/future official vehicle auctions from Belgium, Poland, and Portugal"
    )
    parser.add_argument("--source", action="append", choices=SOURCE_CHOICES)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument(
        "--e-leiloes-intermediate-ca",
        type=Path,
        default=Path(os.environ.get(
            "E_LEILOES_INTERMEDIATE_CA",
            str(DEFAULT_E_LEILOES_INTERMEDIATE_CA),
        )),
        help=(
            "Sectigo intermediate PEM missing from the e-Leiloes server chain; "
            "it is combined with the normal Requests trust roots"
        ),
    )
    args = parser.parse_args()
    payload = build_watch(
        sources=args.source or SOURCE_CHOICES,
        timeout=max(3, args.timeout),
        e_leiloes_intermediate_ca=args.e_leiloes_intermediate_ca,
    )
    apply_previous_snapshot_fallback(payload, args.out)
    write_payload(args.out, payload)
    print(json.dumps({
        "row_count": payload["row_count"],
        "sources": {
            key: report.get("current_or_future_rows", 0)
            for key, report in payload["source_reports"].items()
        },
        "output": str(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
