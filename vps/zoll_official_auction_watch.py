#!/usr/bin/env python3
"""Broad official-auction watch for Zoll-Auktion cars and vans.

The official ``Fahrzeuge`` category (191) also contains boats, motorcycles,
trailers, attachments, tractors and other machinery.  This connector reads all
three official passenger-car leaves and then inspects every lot in the two
official utility/emergency leaves to retain positively identified vans only.

Old, diesel, damaged and non-running vehicles stay visible in this broad feed;
they are classified through ``eligibility_status``/``eligibility_reason`` and
are never silently promoted into the strict Algeria-import lane.  Zoll's amount
is emitted as ``current_bid`` only when the official bid counter is positive.
With zero bids the same official amount is a ``base_price``.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
ORIGIN = "https://www.zoll-auktion.de"
VEHICLES_CATEGORY_URL = f"{ORIGIN}/auktion/kategorie/Fahrzeuge/191"
SOURCE_KEY = "zoll-auktion"
SOURCE_NAME = "Zoll-Auktion (Generalzolldirektion)"
HEADERS = {
    "User-Agent": "DZ-Auto-Radar/1.0 (+official public-auction monitor)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
}


class _CentralEuropeFallback(dt.tzinfo):
    """EU CET/CEST rules for Windows Python installations without tzdata."""

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


try:
    BERLIN: dt.tzinfo = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:
    BERLIN = _CentralEuropeFallback()


@dataclass(frozen=True)
class CategorySpec:
    category_id: int
    slug: str
    name: str
    passenger_car_leaf: bool

    @property
    def url(self) -> str:
        return f"{ORIGIN}/auktion/kategorie/{self.slug}/{self.category_id}"


# These are the official leaves currently nested below Fahrzeuge/191.  The
# other leaves are explicitly trailers, attachments, motorcycles, boats or
# agricultural/construction vehicles and are outside the requested car/van set.
CATEGORY_SPECS: tuple[CategorySpec, ...] = (
    CategorySpec(216, "Gebrauchtwagen", "Gebrauchtwagen", True),
    CategorySpec(215, "Jahreswagen_Kfz_bis_24_Monate", "Jahreswagen (Kfz bis 24 Monate)", True),
    CategorySpec(1102, "Unfall_Bastlerfahrzeuge_PKW", "Unfall- & Bastlerfahrzeuge (PKW)", True),
    CategorySpec(1124, "sonstige_Nutzfahrzeuge", "sonstige Nutzfahrzeuge", False),
    CategorySpec(1166, "Blaulichtfahrzeuge", "Blaulichtfahrzeuge", False),
)

PRODUCT_LINK_RE = re.compile(r'href=["\'](/auktion/produkt/[^"\'?#]+)["\']', re.I)
COUNT_RE = re.compile(r"Auktionssuche:\s*([\d.]+)\s*Treffer", re.I)
ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.I)
HREF_RE = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)
REL_NEXT_RE = re.compile(r"\brel=[\"']next[\"']", re.I)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(
    r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.I | re.S,
)
DL_ROW_RE = re.compile(
    r"<(?:dt|th)\b[^>]*>(.*?)</(?:dt|th)>\s*<(?:dd|td)\b[^>]*>(.*?)</(?:dd|td)>",
    re.I | re.S,
)
LISTING_ID_RE = re.compile(r'id=["\']bilder_auktionen_id["\'][^>]*>\s*(\d+)', re.I)
TITLE_RE = re.compile(r'id=["\']ueberschrift_auktion["\'][^>]*>(.*?)</h4>', re.I | re.S)
END_RE = re.compile(r'id=["\']auktions_ende["\'][^>]*>(.*?)</dd>', re.I | re.S)
AMOUNT_RE = re.compile(r'id=["\']hoechstgebot["\'][^>]*>(.*?)</span>', re.I | re.S)
BID_COUNT_RE = re.compile(r'id=["\']anz_gebote_zahl["\'][^>]*>\s*(\d+)', re.I)
BID_COUNT_FALLBACK_RE = re.compile(
    r'id=["\']anz_gebote_gesamt["\'][^>]*>\s*(\d+)', re.I
)
GERMAN_END_RE = re.compile(
    r"(?:\w+\.?\s*,?\s*)?(\d{1,2})\.(\d{1,2})\.(\d{4})\s*-\s*"
    r"(\d{1,2}):(\d{2})\s*Uhr",
    re.I,
)

PASSENGER_CATEGORIES = frozenset(spec.category_id for spec in CATEGORY_SPECS if spec.passenger_car_leaf)
ACCIDENT_CATEGORY_ID = 1102

_VAN_OR_LIGHT_MODEL_RE = re.compile(
    r"\b(?:sprinter|vito|viano|citan|crafter|caddy|multivan|caravelle|"
    r"(?:vw|volkswagen)\s+(?:t[4-7]|lt(?:\s*\d+)?|transporter)|"
    r"transit(?:\s+(?:connect|courier|custom))?|tourneo|boxer|"
    r"partner|expert|jumper|berlingo|jumpy|ducato|doblo|scudo|fiorino|master|"
    r"trafic|kangoo|movano|vivaro|combo|proace|hiace|nv\s?(?:200|250|300|400)|"
    r"primastar|interstar|townstar|daily|tge|porter|streetscooter|amarok|hilux|"
    r"navara|ranger|l200|d-max|musso)\b",
    re.I,
)
_VAN_BODY_RE = re.compile(
    r"\b(?:kleintransporter|kastenwagen|hochdachkombi|lieferwagen|kleinbus|"
    r"mannschaftstransportwagen|rettungswagen|ambulanzfahrzeug|"
    r"light\s+commercial\s+vehicle)\b",
    re.I,
)
_PASSENGER_TYPE_RE = re.compile(
    r"\b(?:fahrzeugart|fahrzeugklasse|aufbau(?:art)?)\s*:?\s*"
    r"(?:pkw|personenkraftwagen|limousine|kombi|coupe|coupé|cabrio|suv|"
    r"geländewagen|mehrzweckfahrzeug|van|kleinbus|hochdachkombi)\b",
    re.I,
)
_NON_VEHICLE_RE = re.compile(
    r"\b(?:anhänger|auflieger|kofferaufbau|wechselaufbau|anbauteil|anbaugerät|"
    r"geräteträger|kehrmaschine|aufsitzmäher|traktor|schlepper|radlader|bagger|"
    r"gabelstapler|hubsteiger|arbeitsbühne|boot|motorrad|quad|reifenpaket|"
    r"radsatz|felgensatz|ersatzteilpaket)\b",
    re.I,
)
_MAJOR_FAULT_RE = re.compile(
    r"\b(?:nicht\s+fahrbereit|nicht\s+betriebsbereit|nicht\s+verkehrssicher|"
    r"motorschaden|getriebeschaden|unfallfahrzeug|unfallschaden|totalschaden|"
    r"bastlerfahrzeug|ersatzteilspender|motor\s+startet\s+nicht|"
    r"fahrzeug\s+startet\s+nicht|fahrzeugpapiere\s+fehlen|"
    r"papiere\s+nicht\s+vorhanden|zulassungsbescheinigung\s+fehlt)\b",
    re.I,
)


def clean(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = TAG_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", text).replace("\ufffd", " ")
    return " ".join(text.split())


def folded(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value)).casefold()
    return "".join(character for character in text if not unicodedata.combining(character))


def _configured_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.35,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=12, pool_maxsize=12)
    session.mount("https://", adapter)
    return session


def fetch_text(session: Any, url: str, *, timeout: float) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "www.zoll-auktion.de":
        raise ValueError(f"refusing non-official Zoll URL: {url}")
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="strict")
    return str(response.text)


def parse_listing_page(text: str) -> tuple[list[str], int | None, str | None]:
    links = sorted(set(PRODUCT_LINK_RE.findall(text)))
    count_match = COUNT_RE.search(text)
    total = int(count_match.group(1).replace(".", "")) if count_match else None
    next_url = None
    for anchor in ANCHOR_RE.findall(text):
        if not REL_NEXT_RE.search(anchor):
            continue
        href_match = HREF_RE.search(anchor)
        if href_match:
            next_url = html.unescape(href_match.group(1))
            break
    return links, total, next_url


def crawl_category(
    session: Any,
    spec: CategorySpec,
    *,
    timeout: float,
    max_pages: int = 100,
) -> tuple[list[str], int]:
    url = spec.url
    visited_pages: set[str] = set()
    product_paths: set[str] = set()
    expected_total: int | None = None
    for _ in range(max_pages):
        if url in visited_pages:
            raise RuntimeError(f"Zoll pagination loop in category {spec.category_id}")
        visited_pages.add(url)
        page = fetch_text(session, url, timeout=timeout)
        links, total, next_url = parse_listing_page(page)
        if expected_total is None:
            if total is None:
                raise RuntimeError(f"Zoll result count missing in category {spec.category_id}")
            expected_total = total
        product_paths.update(links)
        if not next_url:
            break
        resolved = urljoin(ORIGIN + "/", next_url)
        parsed = urlparse(resolved)
        if parsed.scheme != "https" or parsed.hostname != "www.zoll-auktion.de":
            raise RuntimeError("Zoll pagination escaped the official host")
        url = resolved
    else:
        raise RuntimeError(f"Zoll category {spec.category_id} exceeded {max_pages} pages")

    expected_total = expected_total or 0
    if len(product_paths) != expected_total:
        raise RuntimeError(
            f"incomplete Zoll category {spec.category_id}: "
            f"discovered {len(product_paths)} of {expected_total}"
        )
    return sorted(product_paths), expected_total


def parse_german_money(value: Any) -> float | None:
    text = clean(value).replace("\xa0", " ")
    match = re.search(r"([0-9][0-9.]*?(?:,[0-9]{1,2})?)\s*(?:EUR|€)\b", text, re.I)
    if not match:
        return None
    raw = match.group(1).replace(".", "").replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if amount <= 0:
        return None
    return float(amount.quantize(Decimal("0.01")))


def parse_end(value: Any) -> dt.datetime | None:
    match = GERMAN_END_RE.search(clean(value))
    if not match:
        return None
    day, month, year, hour, minute = (int(part) for part in match.groups())
    try:
        local = dt.datetime(year, month, day, hour, minute, tzinfo=BERLIN)
    except ValueError:
        return None
    return local.astimezone(UTC)


def _product_json(text: str) -> dict[str, Any]:
    for raw in SCRIPT_RE.findall(text):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        candidates: Iterable[Any] = value if isinstance(value, list) else (value,)
        for candidate in candidates:
            if isinstance(candidate, dict) and str(candidate.get("@type", "")).casefold() == "product":
                return candidate
    return {}


def _facts(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in DL_ROW_RE.findall(text):
        normalized_key = clean(key).strip().rstrip(":")
        if normalized_key:
            result[normalized_key] = clean(value)
    return result


def _fact(facts: dict[str, str], *names: str) -> str:
    wanted = {folded(name).rstrip(":") for name in names}
    for key, value in facts.items():
        if folded(key).rstrip(":") in wanted:
            return value
    return ""


def _registration(facts: dict[str, str], description: str) -> tuple[dt.date | None, int | None]:
    candidates = [_fact(facts, "Erstzulassung")]
    candidates.extend(
        match.group(1)
        for match in re.finditer(
        r"\b(?:Erstzulassung|Erste\s+Zulassung)\s*:?\s*"
        r"(\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2})",
            description,
            re.I,
        )
    )
    parsed: list[dt.date] = []
    for raw in candidates:
        match = re.search(r"(\d{1,2})[./-](\d{1,2})[./-]((?:19|20)\d{2})", raw)
        if not match:
            continue
        day, month, year = (int(part) for part in match.groups())
        try:
            parsed.append(dt.date(year, month, day))
        except ValueError:
            pass
    if parsed:
        value = min(parsed)
        return value, value.year
    joined = " ".join(candidates + [description])
    match = re.search(
        r"\b(?:Erstzulassung|Erste\s+Zulassung|Baujahr|EZ)\s*:?\s*"
        r"(?:(?:\d{1,2})[./-])?((?:19|20)\d{2})\b",
        joined,
        re.I,
    )
    return (None, int(match.group(1))) if match else (None, None)


def _mileage(facts: dict[str, str], description: str) -> int | None:
    official_fact = _fact(facts, "Kilometerstand", "km-Stand", "Laufleistung")
    direct_match = re.search(r"([\d.\s]+)", official_fact)
    if direct_match:
        digits = re.sub(r"\D", "", direct_match.group(1))
        if digits and 0 <= int(digits) < 3_000_000:
            return int(digits)
    values = [description]
    patterns = (
        r"\bKilometerstand\s*:?\s*([\d.\s]+)",
        r"\b(?:km[- ]?Stand|Laufleistung)\s*:?\s*([\d.\s]+)",
    )
    for value in values:
        for pattern in patterns:
            match = re.search(pattern, value, re.I)
            if not match:
                continue
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                number = int(digits)
                if 0 <= number < 3_000_000:
                    return number
    return None


def normalize_fuel(value: Any) -> str:
    text = folded(value)
    diesel = bool(re.search(r"\b(?:diesel|tdi|cdi|dci|hdi)\b", text))
    petrol = bool(re.search(r"\b(?:benzin|ottomotor|super\s*e?\s*\d*)\b", text))
    # ``elektrisch`` occurs constantly in equipment lists (windows, mirrors,
    # seats) and is not fuel evidence.  Zoll's fuel value uses ``Elektro``.
    electric = bool(re.search(r"\b(?:elektro|electric|bev)\b", text))
    hybrid = bool(re.search(r"\b(?:hybrid|phev|hev)\w*\b", text))
    lpg = bool(re.search(r"\b(?:lpg|flussiggas|autogas)\b", text))
    cng = bool(re.search(r"\b(?:cng|erdgas)\b", text))
    if diesel and (electric or hybrid):
        return "diesel/electric hybrid"
    if diesel:
        return "diesel"
    if cng and petrol:
        return "petrol/cng"
    if cng:
        return "cng"
    if petrol and (electric or hybrid):
        return "petrol/electric hybrid"
    if petrol and lpg:
        return "petrol/lpg"
    if electric:
        return "electric"
    if petrol:
        return "petrol"
    if lpg:
        return "lpg"
    return "unknown"


def _fuel_evidence(facts: dict[str, str], title: str, description: str) -> str:
    direct = _fact(facts, "Kraftstoffart", "Kraftstoff", "Motorart", "Energiequelle / Kraftstoff")
    if direct:
        return direct
    for pattern in (
        r"\b(?:Kraftstoffart|Kraftstoff|Energiequelle(?:\s*/\s*Kraftstoff)?)\s*:?\s*"
        r"([A-Za-zÄÖÜäöüß /+.-]{2,45})",
        r"\bMotorart(?:\s*/[^:]{0,35})?\s*:?\s*([A-Za-zÄÖÜäöüß /+.-]{2,35})",
    ):
        match = re.search(pattern, description, re.I)
        if match:
            return match.group(1)
    # A concise official title/intro can still explicitly say Benzin, Diesel,
    # Elektro or Hybrid.  Avoid the rest of the equipment prose.
    return " ".join((title, description[:250]))


def classify_vehicle(
    *,
    category_id: int,
    title: str,
    facts: dict[str, str],
    description: str,
) -> str | None:
    vehicle_facts = " ".join(
        _fact(facts, name)
        for name in (
            "Marke (Hersteller)", "Marke", "Fabrikat", "Modell", "Typ", "Typ / Untertyp",
            "Fahrzeugart", "Fahrzeugklasse", "Aufbau", "Aufbauart", "Erstzulassung",
        )
    )
    # Identity/body signals are deliberately kept near the official vehicle
    # heading.  Later prose may mention a van, tyre class or comparison that
    # does not describe the auctioned object itself.
    identity_context = " ".join((title, vehicle_facts, description[:450]))
    if category_id in PASSENGER_CATEGORIES:
        # These are official passenger-car leaves, not the sibling accessory
        # leaves.  Mentions of trailers/parts in condition prose must not erase
        # the actual car (including accident and parts-donor whole cars).
        return "passenger_car"

    if _NON_VEHICLE_RE.search(title):
        return None
    if _PASSENGER_TYPE_RE.search(identity_context):
        return "passenger_car"
    if _VAN_BODY_RE.search(identity_context) or _VAN_OR_LIGHT_MODEL_RE.search(identity_context):
        return "van_or_light_commercial"
    return None


def _three_year_cutoff(reference: dt.date) -> dt.date:
    try:
        return reference.replace(year=reference.year - 3)
    except ValueError:
        return reference.replace(year=reference.year - 3, day=28)


def classify_eligibility(
    *,
    category_id: int,
    registration_date: dt.date | None,
    year: int | None,
    fuel: str,
    description: str,
    now: dt.datetime,
) -> tuple[str, str]:
    if fuel.startswith("diesel"):
        return "not_eligible", "Diesel is outside Algeria's used-vehicle import fuel gate."
    if fuel in {"cng", "lpg", "petrol/lpg", "petrol/cng"}:
        return "not_eligible", "The declared fuel is outside the accepted used-vehicle import fuel set."
    cutoff = _three_year_cutoff(now.date())
    if registration_date is not None and registration_date < cutoff:
        return (
            "not_eligible",
            f"First registration {registration_date.isoformat()} is older than the rolling three-year cutoff.",
        )
    if registration_date is None and year is not None and year < cutoff.year:
        return "not_eligible", f"Registration year {year} is older than the rolling three-year window."
    if category_id == ACCIDENT_CATEGORY_ID:
        return "not_eligible", "The official Zoll category marks this as an accident/builder passenger vehicle."
    if _MAJOR_FAULT_RE.search(description):
        return "not_eligible", "The official description reports major damage, non-running condition, or missing vehicle documents."
    missing: list[str] = []
    if registration_date is None:
        missing.append("exact first-registration date")
    if fuel == "unknown":
        missing.append("fuel")
    if missing:
        return (
            "unknown",
            "Official Zoll details still need " + ", ".join(missing)
            + "; verify documents, technical condition, bidder access, and export before bidding.",
        )
    return (
        "conditional",
        "Recent non-diesel candidate; strict eligibility still requires original documents, expert condition verification, bidder access, and export confirmation.",
    )


def parse_product_page(
    text: str,
    product_path: str,
    *,
    category: CategorySpec,
    now: dt.datetime,
) -> tuple[dict[str, Any] | None, str]:
    listing_match = LISTING_ID_RE.search(text)
    expected_id = product_path.rstrip("/").rsplit("/", 1)[-1]
    if not listing_match or listing_match.group(1) != expected_id:
        return None, "bad_listing_id"
    listing_id = listing_match.group(1)
    title_match = TITLE_RE.search(text)
    product = _product_json(text)
    title = clean(title_match.group(1)) if title_match else clean(product.get("name"))
    if not title:
        return None, "missing_title"
    end_match = END_RE.search(text)
    end = parse_end(end_match.group(1)) if end_match else None
    if end is None:
        return None, "missing_or_bad_end"
    if end <= now:
        return None, "ended_during_refresh"

    facts = _facts(text)
    description = clean(product.get("description"))
    if not description:
        # This fallback is only used for classification/evidence extraction;
        # scripts/styles are removed first to avoid unrelated page controls.
        body = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
        body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.I | re.S)
        description = clean(body)
    vehicle_kind = classify_vehicle(
        category_id=category.category_id,
        title=title,
        facts=facts,
        description=description,
    )
    if vehicle_kind is None:
        return None, "not_car_or_van"

    amount_match = AMOUNT_RE.search(text)
    amount = parse_german_money(amount_match.group(1)) if amount_match else None
    count_match = BID_COUNT_RE.search(text) or BID_COUNT_FALLBACK_RE.search(text)
    bid_count = int(count_match.group(1)) if count_match else None
    if amount is None or bid_count is None:
        price_amount = None
        price_kind = "unknown"
        price_label = "Preis/Gebotsstand nicht sicher veröffentlicht"
        bid_visibility = "not_published_or_unverified"
    elif bid_count > 0:
        price_amount = amount
        price_kind = "current_bid"
        price_label = "Höchstgebot"
        bid_visibility = "live_current_bid"
    else:
        price_amount = amount
        price_kind = "base_price"
        price_label = "Startpreis (0 Gebote)"
        bid_visibility = "base_only"

    registration_date, year = _registration(facts, description)
    fuel = normalize_fuel(_fuel_evidence(facts, title, description))
    status, reason = classify_eligibility(
        category_id=category.category_id,
        registration_date=registration_date,
        year=year,
        fuel=fuel,
        description=description,
        now=now,
    )
    make = _fact(facts, "Marke (Hersteller)", "Marke", "Fabrikat")
    model_name = _fact(facts, "Modell", "Typ / Untertyp", "Typ")
    model = " ".join(part for part in (make, model_name) if part).strip()
    if not model:
        model = title.lstrip("0123456789 ")[:120]
    url = urljoin(ORIGIN + "/", product_path)
    observed = now.isoformat()
    return {
        "id": f"{SOURCE_KEY}:{listing_id}",
        "source": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "source_key": SOURCE_KEY,
        "url": url,
        "title": title,
        "model": model,
        "country": "DE",
        "year": year,
        "mileage_km": _mileage(facts, description),
        "fuel": fuel,
        "price_amount": price_amount,
        "price_currency": "EUR",
        "price_eur": price_amount,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_count": bid_count,
        "bid_visibility": bid_visibility,
        "sale_end_at": end.isoformat(),
        "canonical_end_utc": end.isoformat(),
        "last_seen_at": observed,
        "eligibility_status": status,
        "eligibility_reason": reason,
        "access_sale_note": (
            "Official Zoll-Auktion public lot. Registration and bidding account requirements, "
            "collection, payment, documents, and export must be verified on the official lot page."
        ),
        "evidence": (
            "Official Zoll product page fields Auktionsende, Höchstgebot, Anzahl Gebote, "
            "vehicle facts and description. A positive bid counter is required for current_bid."
        ),
        "registration_date": registration_date.isoformat() if registration_date else None,
        "vehicle_kind": vehicle_kind,
        "official_category_id": category.category_id,
        "official_category_name": category.name,
    }, ""


_THREAD_LOCAL = threading.local()


def _thread_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "zoll_session", None)
    if session is None:
        session = _configured_session()
        _THREAD_LOCAL.zoll_session = session
    return session


def build_watch(
    session: Any | None = None,
    *,
    now: dt.datetime | None = None,
    timeout: float = 30,
    workers: int = 8,
    category_specs: Sequence[CategorySpec] = CATEGORY_SPECS,
) -> dict[str, Any]:
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    if workers < 1 or workers > 16:
        raise ValueError("workers must be between 1 and 16")
    listing_session = session or _configured_session()

    discovered_by_path: dict[str, CategorySpec] = {}
    category_counts: dict[str, int] = {}
    for spec in category_specs:
        paths, count = crawl_category(listing_session, spec, timeout=timeout)
        category_counts[str(spec.category_id)] = count
        for path in paths:
            previous = discovered_by_path.get(path)
            if previous is None or (spec.passenger_car_leaf and not previous.passenger_car_leaf):
                discovered_by_path[path] = spec

    def parse_one(item: tuple[str, CategorySpec]) -> tuple[dict[str, Any] | None, str]:
        path, spec = item
        detail_session = session or _thread_session()
        page = fetch_text(detail_session, urljoin(ORIGIN + "/", path), timeout=timeout)
        return parse_product_page(page, path, category=spec, now=now)

    rows: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    errors: list[str] = []
    items = sorted(discovered_by_path.items())
    if workers == 1:
        iterator: Iterable[Any] = items
        for item in iterator:
            try:
                row, reason = parse_one(item)
            except Exception as exc:  # preserve prior atomic snapshot on any missed official lot
                errors.append(f"{item[0]}: {exc}")
                continue
            if row is not None:
                rows.append(row)
            else:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_items = {executor.submit(parse_one, item): item for item in items}
            for future in concurrent.futures.as_completed(future_items):
                item = future_items[future]
                try:
                    row, reason = future.result()
                except Exception as exc:
                    errors.append(f"{item[0]}: {exc}")
                    continue
                if row is not None:
                    rows.append(row)
                else:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1

    structural_failures = sum(
        count for reason, count in reason_counts.items()
        if reason in {"bad_listing_id", "missing_title", "missing_or_bad_end"}
    )
    if errors or structural_failures:
        sample = errors[:3]
        raise RuntimeError(
            "refusing incomplete Zoll snapshot: "
            f"fetch_errors={len(errors)} structural_failures={structural_failures} sample={sample}"
        )

    unique_rows = {row["id"]: row for row in rows}
    rows = sorted(unique_rows.values(), key=lambda row: (row["canonical_end_utc"], row["id"]))
    current_bid_rows = sum(row["price_kind"] == "current_bid" for row in rows)
    base_price_rows = sum(row["price_kind"] == "base_price" for row in rows)
    unknown_price_rows = sum(row["price_kind"] == "unknown" for row in rows)
    report = {
        "parent_category_id": 191,
        "category_counts": category_counts,
        "catalogue_total": sum(category_counts.values()),
        "discovered_product_urls": len(discovered_by_path),
        "vehicle_rows": len(rows),
        "current_bid_rows": current_bid_rows,
        "base_price_rows": base_price_rows,
        "unknown_price_rows": unknown_price_rows,
        "excluded_non_vehicle": reason_counts.get("not_car_or_van", 0),
        "ended_during_refresh": reason_counts.get("ended_during_refresh", 0),
        "price_semantics": "current_bid only when official bid_count > 0; zero bids = base_price",
        "category_url": VEHICLES_CATEGORY_URL,
    }
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": now.isoformat(),
        "row_count": len(rows),
        "rows": rows,
        "source_reports": {SOURCE_KEY: report},
        "source_key": SOURCE_KEY,
        "source_url": VEHICLES_CATEGORY_URL,
        **report,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch all current Zoll-Auktion car/van lots")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    payload = build_watch(timeout=args.timeout, workers=args.workers)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "source": SOURCE_KEY,
        "catalogue_total": payload["catalogue_total"],
        "discovered_product_urls": payload["discovered_product_urls"],
        "vehicle_rows": payload["row_count"],
        "current_bid_rows": payload["current_bid_rows"],
        "base_price_rows": payload["base_price_rows"],
        "unknown_price_rows": payload["unknown_price_rows"],
        "excluded_non_vehicle": payload["excluded_non_vehicle"],
        "output": str(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
