#!/usr/bin/env python3
"""Broad official-auction watch for France, Czechia, Germany/Austria and DRZ.

This feed is intentionally separate from the strict Algeria-import lane.  It
keeps every current/future public vehicle lot visible, including old and diesel
vehicles, and records why each row is not eligible, conditional, or still
unknown.  A starting/minimum amount is never presented as a live current bid.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


UTC = dt.timezone.utc
HEADERS = {
    "User-Agent": "DZ-Auto-Radar/1.0 (+official public-auction monitor)",
    "Accept-Language": "fr-FR,fr;q=0.9,de;q=0.8,nl;q=0.7,cs;q=0.6,en;q=0.5",
    "Accept": "application/json, text/html, */*",
}

DOMAINE_HOME = "https://encheres-domaine.gouv.fr/"
DOMAINE_GRAPHQL_URL = "https://encheres-domaine.gouv.fr/gateway/magento/graphql/"
DOMAINE_DETAIL_URL = "https://encheres-domaine.gouv.fr/lot/{url_key}.html"
DOMAINE_CATEGORY_UID = "NQ=="
DOMAINE_QUERY = """query getCategoryLots($currentPage:Int $filter:ProductAttributeFilterInput! $pageSize:Int){products(currentPage:$currentPage filter:$filter pageSize:$pageSize sort:{start_auction_lot_at:ASC}){items{auction auction_type end_auction_lot_at id last_bid lot_number lot_status name price_auction professional_only short_description{html} start_auction_lot_at url_key} page_info{total_pages} total_count}}"""

CZECH_API = "https://nabidkamajetku.gov.cz/api/Property/AuctionList"
CZECH_DETAIL_URL = "https://nabidkamajetku.gov.cz/Home/AuctionDetail/{item_id}"
CZECH_PAYLOAD = {
    "ListType": "active",
    "Page": 1,
    "PageSize": 100,
    "Order": "Default",
    "OrderDesc": "true",
    "CategoryId": 39,
    "Fulltext": "",
    "OrgId": "",
    "OrganizationType": 0,
    "OrganizationId": 0,
    "LocalityId": 0,
    "MunicipialityId": 0,
    "CadastreId": 0,
    "AuctionModeId": 0,
    "ContactZipCode": "",
    "PropertyAuthor": "",
}

JUSTIZ_CATEGORY_URL = "https://www.justiz-auktion.de/Fahrzeuge~1848"
JUSTIZ_SEARCH_URL = "https://www.justiz-auktion.de/auction_search.php"
JUSTIZ_ORIGIN = "https://www.justiz-auktion.de/"

OVM_LIST_URL = "https://onlineveilingmeester.nl/rest/nl/v2/kavels"
OVM_DETAIL_URL = "https://onlineveilingmeester.nl/rest/nl/v2/veilingen/{auction_id}/kavels/{lot_number}"
OVM_PUBLIC_URL = "https://onlineveilingmeester.nl/nl/veilingen/{auction_id}/kavels/{lot_number}"
OVM_VEHICLE_CATEGORIES = {10, 11}

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

SOURCE_NAMES = {
    "encheres-du-domaine": "Les Enchères du Domaine (DGFiP)",
    "nabidka-majetku": "Nabídka majetku ÚZSVM",
    "justiz-auktion": "Justiz-Auktion Deutschland & Österreich",
    "onlineveilingmeester": "Domeinen Roerende Zaken / Onlineveilingmeester",
}


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


try:
    CENTRAL_EUROPE: dt.tzinfo = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:
    CENTRAL_EUROPE = _CentralEuropeFallback()


def clean(value: Any) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unicodedata.normalize("NFKC", value).replace("\ufffd", " ").split())


def folded(value: Any) -> str:
    value = unicodedata.normalize("NFKD", clean(value)).casefold()
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def parse_number(value: Any) -> float | None:
    raw = clean(value).replace("€", "").replace("Kč", "").replace("EUR", "")
    raw = re.sub(r"[^0-9,.-]", "", raw)
    if not raw or raw in {"-", ".", ","}:
        return None
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        tail = raw.rsplit(",", 1)[1]
        raw = raw.replace(",", ".") if len(tail) <= 2 else raw.replace(",", "")
    elif raw.count(".") > 1 or ("." in raw and len(raw.rsplit(".", 1)[1]) == 3):
        raw = raw.replace(".", "")
    try:
        number = float(raw)
    except ValueError:
        return None
    return number if number >= 0 else None


def parse_iso(value: Any, *, naive_tz: dt.tzinfo = UTC) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed.astimezone(UTC)


def parse_date(value: Any) -> dt.date | None:
    raw = clean(value)
    for pattern in (r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b"):
        match = re.search(pattern, raw)
        if not match:
            continue
        parts = [int(part) for part in match.groups()]
        year, month, day = parts if parts[0] > 1900 else (parts[2], parts[1], parts[0])
        try:
            return dt.date(year, month, day)
        except ValueError:
            continue
    return None


def three_year_cutoff(reference: dt.date) -> dt.date:
    try:
        return reference.replace(year=reference.year - 3)
    except ValueError:
        return reference.replace(year=reference.year - 3, day=28)


def normalize_fuel(value: Any) -> str:
    text = folded(value)
    diesel = bool(re.search(r"\b(?:diesel|gazole|gasolio|nafta|motorova nafta|nm|tdi|hdi|cdi|dci)\b", text))
    petrol = bool(re.search(r"\b(?:essence|benzin|benzine|benzina|natural|zazehov|ba 9[58])\b", text))
    electric = bool(re.search(r"\b(?:electrique|elektrisch|elektro|electric|bev)\b", text))
    hybrid = bool(re.search(r"\b(?:hybrid|hybride|phev|hev)\w*\b", text))
    lpg = bool(re.search(r"\b(?:lpg|gpl)\b", text))
    if diesel and hybrid:
        return "diesel/electric hybrid"
    if diesel:
        return "diesel"
    if hybrid:
        return "petrol/electric hybrid" if petrol else "hybrid"
    if petrol and lpg:
        return "petrol/lpg"
    if electric and petrol:
        return "petrol/electric hybrid"
    if electric:
        return "electric"
    if petrol:
        return "petrol"
    return "unknown"


def parse_mileage(value: Any) -> int | None:
    text = clean(value)
    match = re.search(
        r"(?:kilometerstand|kilometrage|afgelezen tellerstand|stav tachometru|najeto|\bkm)"
        r"\s*(?:[:=]|pari a)?\s*([0-9][0-9 .]{1,10})\s*(?:km)?\b",
        text,
        re.I,
    )
    if not match:
        match = re.search(r"\b([0-9][0-9 .]{2,10})\s*km\b", text, re.I)
    if not match:
        return None
    try:
        amount = int(re.sub(r"\D", "", match.group(1)))
    except ValueError:
        return None
    return amount if 0 <= amount < 2_000_000 else None


def classify_eligibility(
    *,
    fuel: str,
    registration_date: dt.date | None,
    year: int | None,
    text: str,
    now: dt.datetime,
    participation_block: str = "",
    condition_patterns: tuple[str, ...] = (),
) -> tuple[str, str]:
    if participation_block:
        return "not_eligible", participation_block
    if fuel.startswith("diesel"):
        return "not_eligible", "Diesel is outside Algeria's used-vehicle import fuel gate."
    cutoff = three_year_cutoff(now.date())
    if registration_date and registration_date < cutoff:
        return "not_eligible", f"First registration {registration_date.isoformat()} is older than the rolling three-year cutoff."
    if registration_date is None and year is not None and year < cutoff.year:
        return "not_eligible", f"Vehicle year {year} is older than the rolling three-year window."
    lowered = folded(text)
    for pattern in condition_patterns:
        if re.search(pattern, lowered, re.I):
            return "not_eligible", "The official lot description states a major condition or document problem."
    missing: list[str] = []
    if registration_date is None:
        missing.append("exact first-registration date")
    if fuel == "unknown":
        missing.append("fuel")
    if missing:
        return "unknown", "Official summary still needs " + ", ".join(missing) + "; verify documents, condition, foreign bidding and export."
    return "conditional", "Recent non-diesel candidate; verify vehicle documents, condition, fees, foreign bidding and export before treating it as import-eligible."


def _row(
    *,
    source: str,
    listing_id: str,
    country: str,
    url: str,
    title: str,
    model: str,
    year: int | None,
    registration_date: dt.date | None,
    fuel: str,
    mileage_km: int | None,
    price_amount: float,
    price_currency: str,
    price_eur: float | None,
    price_kind: str,
    price_label: str,
    sale_end: dt.datetime,
    now: dt.datetime,
    status: str,
    reason: str,
    bid_visibility: str,
    note: str,
    evidence: str,
    description: str,
    **extras: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": f"{source}:{listing_id}",
        "source": source,
        "source_name": SOURCE_NAMES[source],
        "source_key": source,
        "country": country,
        "url": url,
        "title": title,
        "model": model,
        "year": year,
        "registration_date": registration_date.isoformat() if registration_date else None,
        "mileage_km": mileage_km,
        "fuel": fuel,
        "price_amount": price_amount,
        "price_currency": price_currency,
        "price_eur": price_eur,
        "price_kind": price_kind,
        "price_label": price_label,
        "sale_end_at": sale_end.isoformat(),
        "canonical_end_utc": sale_end.isoformat(),
        "last_seen_at": now.isoformat(),
        "eligibility_status": status,
        "eligibility_reason": reason,
        "bid_visibility": bid_visibility,
        "access_sale_note": note,
        "evidence": evidence,
        "description": description,
    }
    result.update(extras)
    return result


def fetch_ecb_rates(session: requests.Session, *, timeout: int) -> dict[str, float]:
    rates = {"EUR": 1.0}
    response = session.get(ECB_URL, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    for node in root.iter():
        currency, raw_rate = node.attrib.get("currency"), node.attrib.get("rate")
        if currency and raw_rate:
            rates[currency.upper()] = float(raw_rate)
    return rates


def _domaine_response_json(response: requests.Response, session: requests.Session, *, timeout: int) -> dict[str, Any]:
    response.raise_for_status()
    for _ in range(3):
        redirect = re.search(r"window\.location\.href=['\"]([^'\"]+)", response.text)
        if not redirect:
            break
        response = session.get(urljoin(response.url, html.unescape(redirect.group(1))), headers=HEADERS, timeout=timeout)
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Domaine GraphQL response is not an object")
    return payload


def domaine_item_to_row(item: dict[str, Any], *, now: dt.datetime) -> dict[str, Any] | None:
    if str(item.get("auction_type")) != "1":
        return None
    if str(item.get("lot_status")) not in {"13", "14"}:
        return None
    start = parse_iso(item.get("start_auction_lot_at"))
    end = parse_iso(item.get("end_auction_lot_at"))
    if start is None or end is None or end <= now:
        return None
    listing_id = str(item.get("id") or "")
    url_key = str(item.get("url_key") or "").strip()
    if not listing_id.isdigit() or not url_key:
        return None
    description = clean((item.get("short_description") or {}).get("html"))
    description_folded = folded(description)
    registration = None
    registration_match = re.search(
        r"(?:1.{0,4}|premiere)\s+mise\s+en\s+(?:circulation|corculation)\D{0,16}"
        r"(\d{1,2}[./-]\d{1,2}[./-]20\d{2})",
        description_folded,
        re.I,
    )
    if registration_match:
        registration = parse_date(registration_match.group(1))
    year = registration.year if registration else None
    fuel = normalize_fuel(description)
    current = parse_number(item.get("last_bid")) or 0
    base = parse_number(item.get("price_auction")) or 0
    price = current if current > 0 else base
    if price <= 0:
        return None
    price_kind = "current_bid" if current > 0 else "base_price"
    professional_only = item.get("professional_only") not in (0, False, "0")
    status, reason = classify_eligibility(
        fuel=fuel,
        registration_date=registration,
        year=year,
        text=description,
        now=now,
        participation_block=(
            "This official Domaine lot is restricted to automotive-sector professionals."
            if professional_only else ""
        ),
        condition_patterns=(
            r"(?:non roulant|ne roule pas|moteur hs|vehicule accidente|pour pieces|absence de certificat d.immatriculation)",
        ),
    )
    title = clean(item.get("name")) or f"Domaine vehicle lot {listing_id}"
    return _row(
        source="encheres-du-domaine",
        listing_id=listing_id,
        country="FR",
        url=DOMAINE_DETAIL_URL.format(url_key=url_key),
        title=title,
        model=title,
        year=year,
        registration_date=registration,
        fuel=fuel,
        mileage_km=parse_mileage(description),
        price_amount=price,
        price_currency="EUR",
        price_eur=price,
        price_kind=price_kind,
        price_label="Enchère actuelle" if current > 0 else "Mise à prix",
        sale_end=end,
        now=now,
        status=status,
        reason=reason,
        bid_visibility="public_current_bid" if current > 0 else "base_price_only",
        note=(
            "Official Domaine professional-only lot; shown for broad monitoring but not eligible for a normal private bidder."
            if professional_only else
            "Official public Domaine lot. Upcoming lots show their base price until public bidding starts."
        ),
        evidence="Official DGFiP GraphQL fields for status, public/professional gate, base/current price, start and end.",
        description=description,
        sale_start_at=start.isoformat(),
        lot_number=item.get("lot_number"),
        auction_id=item.get("auction"),
        professional_only=professional_only,
    )


def harvest_domaine(session: requests.Session, *, now: dt.datetime, timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    session.get(DOMAINE_HOME, headers=HEADERS, timeout=timeout).raise_for_status()
    items: dict[str, dict[str, Any]] = {}
    total = 0
    total_pages = 1
    for page in range(1, 101):
        if page > total_pages:
            break
        variables = {
            "currentPage": page,
            "pageSize": 100,
            "filter": {"category_uid": {"eq": DOMAINE_CATEGORY_UID}, "lot_status": {"in": ["13", "14"]}},
        }
        response = session.get(
            DOMAINE_GRAPHQL_URL,
            params={"query": DOMAINE_QUERY, "operationName": "getCategoryLots", "variables": json.dumps(variables, separators=(",", ":"))},
            headers=HEADERS,
            timeout=timeout,
        )
        payload = _domaine_response_json(response, session, timeout=timeout)
        products = ((payload.get("data") or {}).get("products") or {})
        if not isinstance(products.get("items"), list):
            raise ValueError("Domaine products schema changed")
        if page == 1:
            total = int(products.get("total_count") or 0)
            total_pages = int((products.get("page_info") or {}).get("total_pages") or 1)
        for item in products["items"]:
            if isinstance(item, dict) and item.get("id") is not None:
                items[str(item["id"])] = item
    rows = [row for item in items.values() if (row := domaine_item_to_row(item, now=now))]
    rows.sort(key=lambda row: (row["canonical_end_utc"], row["id"]))
    return rows, {
        "catalogue_total": total,
        "fetched_unique": len(items),
        "current_or_future_vehicle_rows": len(rows),
        "professional_only_rows": sum(bool(row.get("professional_only")) for row in rows),
        "public_rows": sum(not bool(row.get("professional_only")) for row in rows),
        "price_semantics": "last_bid is current_bid; otherwise price_auction is base_price",
        "api_url": DOMAINE_GRAPHQL_URL,
    }


_CZECH_ACCESSORY = re.compile(
    r"\b(?:kolobezk|motocykl|moped|prives|naves|disky|pneumatik|nosic|kamera|prilba|nahradni\s+dily|jizdni\s+kolo)\w*\b",
    re.I,
)
_CZECH_VEHICLE = re.compile(r"\b(?:osobni|dodavkovy|uzitkovy|nakl(?:adni)?\.?)\s+automobil\b", re.I)


def _czech_registration(text: str) -> dt.date | None:
    match = re.search(
        r"(?:datum\s+(?:prvni|1\.?)\s+registrace|prvni\s+registrace|uvedeni\s+do\s+provozu)"
        r"\s*(?:vozidla)?\s*(?::|od|dne)?\s*(\d{1,2}[./-]\d{1,2}[./-]20\d{2}|20\d{2}-\d{1,2}-\d{1,2})\b",
        folded(text),
        re.I,
    )
    return parse_date(match.group(1)) if match else None


def czech_item_to_row(item: dict[str, Any], *, now: dt.datetime, rates: dict[str, float]) -> dict[str, Any] | None:
    try:
        active = int(item.get("AuctionStatus") or 0) == 1
    except (TypeError, ValueError):
        active = False
    start = parse_iso(item.get("StartDate"), naive_tz=UTC)
    end = parse_iso(item.get("EndDate"), naive_tz=UTC)
    if not active or start is None or end is None or not (start <= now < end):
        return None
    listing_id = str(item.get("Id") or "")
    title = clean(item.get("Name"))
    description = clean(item.get("Description"))
    title_folded, all_folded = folded(title), folded(title + " " + description)
    if not listing_id.isdigit() or _CZECH_ACCESSORY.search(title_folded) or not _CZECH_VEHICLE.search(all_folded):
        return None
    registration = _czech_registration(description)
    year = registration.year if registration else None
    if year is None:
        year_match = re.search(r"(?:rok\s+vyroby|vyroben)\D{0,12}(20\d{2}|19\d{2})\b", all_folded, re.I)
        year = int(year_match.group(1)) if year_match else None
    fuel = normalize_fuel(description)
    price = parse_number(item.get("Price")) or 0
    if price <= 0 or item.get("NoPrice") is True:
        return None
    bids = int(item.get("NbrOfBids") or 0)
    price_kind = "current_bid" if bids > 0 else "base_price"
    restriction = ""
    if re.search(r"\b(?:pouze obcan ceske republiky|trvaly pobyt v cr|sidlo v cr)\b", all_folded):
        restriction = "The lot explicitly restricts participation to a Czech resident/entity."
    status, reason = classify_eligibility(
        fuel=fuel,
        registration_date=registration,
        year=year,
        text=description,
        now=now,
        participation_block=restriction,
        condition_patterns=(
            r"(?:neni provozuschop|nepojizd|motor nelze nastartovat|technicky nezpusobil|havarovan|na nahradni dily|doklady chybi|bez technickeho prukazu)",
        ),
    )
    czk_per_eur = rates.get("CZK") or 0
    price_eur = round(price / czk_per_eur, 2) if czk_per_eur > 0 else None
    return _row(
        source="nabidka-majetku",
        listing_id=listing_id,
        country="CZ",
        url=CZECH_DETAIL_URL.format(item_id=listing_id),
        title=title,
        model=title,
        year=year,
        registration_date=registration,
        fuel=fuel,
        mileage_km=parse_mileage(description),
        price_amount=price,
        price_currency="CZK",
        price_eur=price_eur,
        price_kind=price_kind,
        price_label="Aktuální cena" if bids > 0 else "Vyvolávací cena",
        sale_end=end,
        now=now,
        status=status,
        reason=reason,
        bid_visibility="public_current_price",
        note="Official ÚZSVM state-property auction; participation declaration and export remain per-lot checks.",
        evidence="Official ÚZSVM AuctionList API: active status, Price, bid count, start/end and full description.",
        description=description,
        sale_start_at=start.isoformat(),
        bid_count=bids,
        district=clean(item.get("DistrictName")) or None,
    )


def harvest_czech(session: requests.Session, *, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    page_count = 1
    catalogue_total = 0
    for page in range(1, 101):
        if page > page_count:
            break
        body = dict(CZECH_PAYLOAD)
        body["Page"] = page
        response = session.post(CZECH_API, json=body, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("Auctions"), list):
            raise ValueError("ÚZSVM AuctionList schema changed")
        page_count = int(payload.get("PageCount") or 1)
        catalogue_total = int(payload.get("PropertyTotalCount") or len(payload["Auctions"]))
        for item in payload["Auctions"]:
            if isinstance(item, dict) and item.get("Id") is not None:
                items[str(item["Id"])] = item
    rows = [row for item in items.values() if (row := czech_item_to_row(item, now=now, rates=rates))]
    rows.sort(key=lambda row: (row["canonical_end_utc"], row["id"]))
    return rows, {
        "category_property_total": catalogue_total,
        "active_transport_items_returned": len(items),
        "active_car_and_van_rows": len(rows),
        "price_semantics": "Price is the public current amount; bid count distinguishes current_bid from base_price",
        "api_url": CZECH_API,
    }


_JUSTIZ_EXCLUDE = re.compile(
    r"\b(?:motorrad|kraftrad|mofa|moped|e[- ]?scooter|elektroleichtmofa|kehrmaschine|"
    r"fussmatten|fußmatten|reifen|felgen|anhanger|anhänger|fahrrad|pedelec|piaggio\s+ape|sr2e)\b",
    re.I,
)
_CAR_MAKE = re.compile(
    r"\b(?:Abarth|Alfa\s+Romeo|Audi|BMW|Citroen|Citroën|Cupra|Dacia|Daimler|Ferrari|Fiat|Ford|Honda|Hyundai|Iveco|Jaguar|Jeep|Kia|Land\s+Rover|Lexus|Mazda|Mercedes(?:-Benz)?|Mini|Mitsubishi|Nissan|Opel|Peugeot|Porsche|Renault|Rolls\s+Royce|Seat|Skoda|Škoda|Smart|Subaru|Suzuki|Tesla|Toyota|Volkswagen|Volvo|VW)\b",
    re.I,
)


@dataclass
class JustizSummary:
    listing_id: str
    title: str
    url: str
    country: str
    end: dt.datetime
    start_price: float
    current_price: float


def parse_justiz_summaries(markup: str) -> tuple[list[JustizSummary], int]:
    total_match = re.search(r"Suchergebnisse\s*\((\d+)\s*Treffer", clean(markup), re.I)
    total = int(total_match.group(1)) if total_match else 0
    chunks = re.split(r"(?=<li\s+id=[\"']rlaid\d+[\"'])", markup, flags=re.I)
    rows: list[JustizSummary] = []
    for chunk in chunks:
        id_match = re.match(r"<li\s+id=[\"']rlaid(\d+)[\"']", chunk, re.I)
        title_match = re.search(r"<h5>\s*<a\s+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>\s*</h5>", chunk, re.I | re.S)
        if not id_match or not title_match:
            continue
        text = clean(chunk[:10000])
        end_match = re.search(r"\((\d{1,2}\.\d{1,2}\.20\d{2})\s+(\d{1,2}):(\d{2})\s+Uhr\)", text, re.I)
        start_match = re.search(r"Startgebot:\s*([0-9., ]+)\s*(?:€|EUR)", text, re.I)
        current_html = re.search(r"<p\s+class=[\"']gebot[\"'][^>]*>(.*?)</p>", chunk, re.I | re.S)
        if not end_match or not start_match:
            continue
        try:
            local_end = dt.datetime.strptime(
                f"{end_match.group(1)} {end_match.group(2)}:{end_match.group(3)}", "%d.%m.%Y %H:%M"
            ).replace(tzinfo=CENTRAL_EUROPE)
        except ValueError:
            continue
        href = html.unescape(title_match.group(1))
        title = clean(title_match.group(2))
        country = "AT" if re.search(r"\bOsterreich\b|\bÖsterreich\b", text, re.I) else "DE"
        start_price = parse_number(start_match.group(1)) or 0
        current_price = parse_number(clean(current_html.group(1))) if current_html else 0
        rows.append(JustizSummary(
            listing_id=id_match.group(1),
            title=title,
            url=urljoin(JUSTIZ_ORIGIN, href),
            country=country,
            end=local_end.astimezone(UTC),
            start_price=start_price,
            current_price=current_price or 0,
        ))
    return rows, total


def _label_value(text: str, label: str, next_label: str) -> str:
    match = re.search(re.escape(label) + r"\s*(.*?)\s*" + re.escape(next_label), text, re.I | re.S)
    return clean(match.group(1)) if match else ""


def justiz_detail_to_row(summary: JustizSummary, markup: str, *, now: dt.datetime) -> dict[str, Any] | None:
    if summary.end <= now or _JUSTIZ_EXCLUDE.search(summary.title):
        return None
    text = clean(markup)
    model = _label_value(text, "Marke / Typ:", "Erstzulassung:")
    if (
        not model
        or len(model) > 180
        or _JUSTIZ_EXCLUDE.search(summary.title + " " + model)
        or not _CAR_MAKE.search(summary.title + " " + model)
    ):
        return None
    raw_registration = _label_value(text, "Erstzulassung:", "Leistung (kW/PS):")
    registration = parse_date(raw_registration[:40])
    year = registration.year if registration else None
    if year is None:
        year_match = re.search(r"\b(?:Baujahr|Bj\.)\s*[:.]?\s*((?:19|20)\d{2})\b", text, re.I)
        year = int(year_match.group(1)) if year_match else None
    fuel_raw = _label_value(text, "Antriebsart/Kraftstoff:", "Getriebeart:")
    fuel = normalize_fuel(fuel_raw)
    mileage_raw = _label_value(text, "Kilometerstand:", "Antriebsart/Kraftstoff:")
    mileage = parse_number(mileage_raw)
    mileage_km = int(mileage) if mileage is not None and mileage < 2_000_000 else None
    price = summary.current_price if summary.current_price > 0 else summary.start_price
    if price <= 0:
        return None
    current = summary.current_price > 0
    status, reason = classify_eligibility(
        fuel=fuel,
        registration_date=registration,
        year=year,
        text=text,
        now=now,
        condition_patterns=(r"fahrbereit:\s*nein", r"papiere vorhanden:\s*nein", r"(?:bastlerfahrzeug|teiletrager|motorschaden|nicht fahrbereit)"),
    )
    return _row(
        source="justiz-auktion",
        listing_id=summary.listing_id,
        country=summary.country,
        url=summary.url,
        title=summary.title,
        model=model,
        year=year,
        registration_date=registration,
        fuel=fuel,
        mileage_km=mileage_km,
        price_amount=price,
        price_currency="EUR",
        price_eur=price,
        price_kind="current_bid" if current else "base_price",
        price_label="Aktuelles Gebot" if current else "Startgebot",
        sale_end=summary.end,
        now=now,
        status=status,
        reason=reason,
        bid_visibility="public_current_bid" if current else "base_price_only",
        note="Official Justiz-Auktion vehicle lot. Account, collection, documents and export remain buyer checks.",
        evidence="Official Fahrzeuge category summary plus official per-lot structured vehicle fields.",
        description=text[:8000],
    )


def harvest_justiz(session: requests.Session, *, now: dt.datetime, timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    session.get(JUSTIZ_CATEGORY_URL, headers=HEADERS, timeout=timeout).raise_for_status()
    response = session.post(
        JUSTIZ_SEARCH_URL,
        data={"resultListLimit": "50", "sortField": "enddate", "sortOrder": "ASC", "bt_saveSortOrder": "Speichern"},
        headers=HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    summaries, total = parse_justiz_summaries(response.text)
    all_summaries = {summary.listing_id: summary for summary in summaries}
    for start in range(50, total, 50):
        page = session.get(JUSTIZ_SEARCH_URL, params={"start": start}, headers=HEADERS, timeout=timeout)
        page.raise_for_status()
        page_rows, _ = parse_justiz_summaries(page.text)
        all_summaries.update({summary.listing_id: summary for summary in page_rows})
    rows: list[dict[str, Any]] = []
    detail_failures = 0
    for summary in all_summaries.values():
        try:
            detail = session.get(summary.url, headers=HEADERS, timeout=timeout)
            detail.raise_for_status()
            row = justiz_detail_to_row(summary, detail.text, now=now)
            if row:
                rows.append(row)
        except requests.RequestException:
            detail_failures += 1
    rows.sort(key=lambda row: (row["canonical_end_utc"], row["id"]))
    return rows, {
        "fahrzeuge_category_total": total,
        "summaries_fetched": len(all_summaries),
        "car_and_van_rows": len(rows),
        "excluded_accessories_motorcycles_and_machinery": max(0, len(all_summaries) - len(rows) - detail_failures),
        "detail_failures": detail_failures,
        "price_semantics": "public Aktuelles Gebot when positive; otherwise Startgebot",
        "category_url": JUSTIZ_CATEGORY_URL,
    }


def _ovm_registration(data: dict[str, Any], specifications: str) -> dt.date | None:
    direct = parse_date(data.get("registratiedatum"))
    if direct:
        return direct
    for label in ("Eerste toelating internationaal", "Eerste toelating nationaal"):
        match = re.search(re.escape(label) + r"\s*:\s*([^;|]{1,50})", specifications, re.I)
        if match and (parsed := parse_date(match.group(1))):
            return parsed
    return None


def ovm_detail_to_row(detail: dict[str, Any], *, now: dt.datetime) -> dict[str, Any] | None:
    auction, category, data = detail.get("veiling") or {}, detail.get("categorie") or {}, detail.get("kavelData") or {}
    end = parse_iso(detail.get("sluitingsDatumISO"))
    if (
        auction.get("type") != "DRZ"
        or not auction.get("isGeopend")
        or detail.get("isClosed")
        or detail.get("zichtbaar") is False
        or category.get("id") not in OVM_VEHICLE_CATEGORIES
        or data.get("kavelDataType") != "AUTO"
        or end is None
        or end <= now
    ):
        return None
    listing_id = str(detail.get("id") or "")
    auction_id = str(auction.get("id") or "")
    lot_number = str(detail.get("volgNummer") or "")
    if not listing_id.isdigit() or not auction_id.isdigit() or not lot_number:
        return None
    specifications = clean(data.get("specificaties"))
    deficiencies = clean(data.get("perceivedDeficiencies"))
    description = clean(" ".join(str(data.get(field) or "") for field in ("specificaties", "perceivedDeficiencies", "naam")))
    title = clean(data.get("naam") or detail.get("naam")) or f"DRZ vehicle {listing_id}"
    model = " ".join(filter(None, [clean(data.get("merk")), clean(data.get("model") or data.get("productType"))])) or title
    registration = _ovm_registration(data, specifications)
    try:
        year = int(data.get("bouwjaar") or 0) or (registration.year if registration else None)
    except (TypeError, ValueError):
        year = registration.year if registration else None
    fuel_match = re.search(r"Brandstof\s*:\s*([^;|]{1,50})", specifications, re.I)
    fuel = normalize_fuel(data.get("brandstof") or (fuel_match.group(1) if fuel_match else ""))
    mileage = parse_mileage(data.get("kilometerstand") or specifications)
    current = parse_number(detail.get("hoogsteBod")) or 0
    opening = parse_number(detail.get("openingsBod")) or 0
    try:
        bids = int(detail.get("aantalBiedingen") or 0)
    except (TypeError, ValueError):
        bids = 0
    has_current = bids > 0 and current > 0
    price = current if has_current else opening
    if price <= 0:
        return None
    participation = "" if detail.get("buitenlandseBiederToegestaan") else "The official lot does not allow a foreign bidder."
    status, reason = classify_eligibility(
        fuel=fuel,
        registration_date=registration,
        year=year,
        text=specifications + " " + deficiencies,
        now=now,
        participation_block=participation,
        condition_patterns=(r"(?:start|rijdt)\s*:\s*nee", r"(?:kentekenbewijs|registratiebewijs)\s+(?:ontbreekt|niet aanwezig)", r"(?:niet rijvaardig|motorschade|total loss)"),
    )
    return _row(
        source="onlineveilingmeester",
        listing_id=listing_id,
        country="NL",
        url=OVM_PUBLIC_URL.format(auction_id=auction_id, lot_number=lot_number),
        title=title,
        model=model,
        year=year,
        registration_date=registration,
        fuel=fuel,
        mileage_km=mileage,
        price_amount=price,
        price_currency="EUR",
        price_eur=price,
        price_kind="current_bid" if has_current else "base_price",
        price_label="Hoogste bod" if has_current else "Openingsbod",
        sale_end=end,
        now=now,
        status=status,
        reason=reason,
        bid_visibility="public_current_bid" if has_current else "base_price_only",
        note="Official DRZ open passenger-car/van lot on OVM; fees, award, documents, collection and export remain per-lot checks.",
        evidence="Official OVM REST detail: DRZ type, open/visible status, vehicle category, public bid/opening amount and end.",
        description=description,
        bid_count=bids,
        foreign_bidder_allowed=bool(detail.get("buitenlandseBiederToegestaan")),
    )


def harvest_ovm(session: requests.Session, *, now: dt.datetime, timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    catalogue_total = 0
    total_pages = 1
    for page_number in range(100):
        if page_number >= total_pages:
            break
        response = session.get(OVM_LIST_URL, params={"page": page_number, "size": 100}, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
            raise ValueError("OVM list schema changed")
        if page_number == 0:
            catalogue_total = int(payload.get("totalElements") or 0)
            total_pages = int(payload.get("totalPages") or 1)
            if total_pages > 100:
                raise ValueError("OVM pagination exceeds safety limit")
        for item in payload["content"]:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            auction, category = item.get("veiling") or {}, item.get("categorie") or {}
            end = parse_iso(item.get("sluitingsDatumISO"))
            if auction.get("type") == "DRZ" and auction.get("isGeopend") and category.get("id") in OVM_VEHICLE_CATEGORIES and end and end > now:
                summaries[str(item["id"])] = item
    rows: list[dict[str, Any]] = []
    detail_failures = 0
    for item in summaries.values():
        auction_id = (item.get("veiling") or {}).get("id")
        lot_number = item.get("volgNummer")
        try:
            response = session.get(
                OVM_DETAIL_URL.format(auction_id=auction_id, lot_number=lot_number),
                headers=HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            row = ovm_detail_to_row(response.json(), now=now)
            if row:
                rows.append(row)
        except (requests.RequestException, ValueError):
            detail_failures += 1
    rows.sort(key=lambda row: (row["canonical_end_utc"], row["id"]))
    return rows, {
        "catalogue_total": catalogue_total,
        "open_drz_passenger_or_van_candidates": len(summaries),
        "open_drz_vehicle_rows": len(rows),
        "detail_failures": detail_failures,
        "price_semantics": "Hoogste bod with bid count is current_bid; otherwise openingsBod is base_price",
        "api_url": OVM_LIST_URL,
    }


HARVESTERS: dict[str, Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]] = {
    "encheres-du-domaine": harvest_domaine,
    "nabidka-majetku": harvest_czech,
    "justiz-auktion": harvest_justiz,
    "onlineveilingmeester": harvest_ovm,
}


def build_watch(
    session: requests.Session | None = None,
    *,
    now: dt.datetime | None = None,
    timeout: int = 30,
    sources: list[str] | None = None,
    rates: dict[str, float] | None = None,
) -> dict[str, Any]:
    session = session or requests.Session()
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    selected = sources or list(HARVESTERS)
    unknown = sorted(set(selected) - set(HARVESTERS))
    if unknown:
        raise ValueError("Unknown source(s): " + ", ".join(unknown))
    if rates is None:
        try:
            rates = fetch_ecb_rates(session, timeout=timeout)
        except (requests.RequestException, ET.ParseError, ValueError):
            rates = {"EUR": 1.0}
    rows: list[dict[str, Any]] = []
    reports: dict[str, Any] = {}
    for source in selected:
        harvester = HARVESTERS[source]
        try:
            if source == "nabidka-majetku":
                source_rows, report = harvester(session, now=now, timeout=timeout, rates=rates)
            else:
                source_rows, report = harvester(session, now=now, timeout=timeout)
            rows.extend(source_rows)
            reports[source] = report
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            reports[source] = {"row_count": 0, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
    unique = {row["id"]: row for row in rows}
    final_rows = sorted(unique.values(), key=lambda row: (row["canonical_end_utc"], row["source"], row["id"]))
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": now.isoformat(),
        "row_count": len(final_rows),
        "rows": final_rows,
        "source_reports": reports,
        "eligibility_counts": {
            status: sum(row["eligibility_status"] == status for row in final_rows)
            for status in ("conditional", "unknown", "not_eligible")
        },
        "price_kinds": {
            kind: sum(row["price_kind"] == kind for row in final_rows)
            for kind in ("current_bid", "base_price")
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch broad official vehicle-auction watch rows")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", action="append", choices=sorted(HARVESTERS))
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    payload = build_watch(timeout=args.timeout, sources=args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.out)
    print(json.dumps({
        "lane": payload["lane"],
        "row_count": payload["row_count"],
        "sources": {key: report for key, report in payload["source_reports"].items()},
        "output": str(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
