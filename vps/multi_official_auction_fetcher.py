#!/usr/bin/env python3
"""Best-effort, fail-closed harvester for the remaining official auction portals.

Discovery uses ordinary HTTP first and a real browser for JavaScript portals.
Rows are emitted only when the official detail page exposes an explicit vehicle
year, current/start bid and timezone-resolved end time.  A portal failure is
reported per source and never invents a listing.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


UTC = dt.timezone.utc
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/135 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.8"}
FIELDNAMES = [
    "listing_id", "model_key", "title", "source", "source_url",
    "first_registration_date", "fuel", "engine_cc", "mileage_km",
    "price_eur", "seller_type", "accident_free", "service_history",
    "transmission", "country", "auction_end_at", "sale_term_code",
    "sale_certainty", "sale_certainty_note",
]
VEHICLE_WORDS = re.compile(
    r"\b(auto|automobile|car|cars|vehicle|vehicule|voiture|wagen|fahrzeug|pkw|kfz|"
    r"camion|fourgon|furgon|samochod|pojazd|fordon|bil|motor|moto|suv|van)\b", re.I)
MAKES = re.compile(
    r"\b(audi|bmw|citro[eë]n|cupra|dacia|fiat|ford|honda|hyundai|iveco|jaguar|jeep|"
    r"kia|land rover|lexus|mazda|mercedes|mini|mitsubishi|nissan|opel|peugeot|porsche|"
    r"renault|seat|skoda|smart|suzuki|tesla|toyota|volkswagen|volvo|vw)\b", re.I)
YEAR_LABEL = re.compile(
    r"(?:first registration|model year|year|erstzulassung|baujahr|1(?:ere|ère) mise en circulation|"
    r"annee|año|fecha de matriculacion|anno|immatricolazione|rok produkcji|rocznik|årsmodell|"
    r"registreringsår|bouwjaar|ano).{0,30}?(20(?:23|24|25|26))", re.I)
END_LABEL = re.compile(
    r"(?:auction end|closing(?: date| time)?|end date|versteigerungsende|auktionsende|"
    r"fin de la vente|date de vente|fecha fin|fine vendita|data fine|koniec licytacji|"
    r"slutdatum|slutar|einddatum|sluitingsdatum|fim)\D{0,45}"
    r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2})"
    r"(?:\D{0,12}(\d{1,2})[:.]([0-5]\d)(?::([0-5]\d))?)?", re.I)
PRICE_LABEL = re.compile(
    r"(?:current bid|highest bid|starting bid|start bid|aktuelles gebot|höchstgebot|startgebot|"
    r"enchere actuelle|mise a prix|prix actuel|puja actual|postura actual|precio salida|"
    r"offerta attuale|prezzo base|aktualna oferta|cena wywolawcza|hogsta bud|utrop|"
    r"hoogste bod|openingsbod|lance atual|valor base)\D{0,35}"
    r"([\d][\d\s.,]*)\s*(EUR|€|PLN|zł|SEK|kr)", re.I)
MILEAGE_LABEL = re.compile(
    r"(?:mileage|kilometerstand|kilometrage|kilométrage|kilometraje|chilometraggio|przebieg|"
    r"miltal|kilometerstand)\D{0,20}([\d .]+)\s*(?:km|kilometer)", re.I)


@dataclass(frozen=True)
class Source:
    key: str
    country: str
    currency: str
    timezone: str
    discovery_urls: tuple[str, ...]
    domains: tuple[str, ...]


SOURCES = (
    Source("encheres-du-domaine", "FR", "EUR", "Europe/Paris",
           ("https://encheres-domaine.gouv.fr/hermes/biens-mobiliers/vehicules/vehicules-tourisme",),
           ("encheres-domaine.gouv.fr",)),
    Source("boe-subastas", "ES", "EUR", "Europe/Madrid",
           ("https://subastas.boe.es/subastas_ava.php",), ("subastas.boe.es",)),
    Source("kronofogden", "SE", "SEK", "Europe/Stockholm",
           ("https://auktion.kronofogden.se/auk/w.ObjectList?inC=KFM&inA=WEB",),
           ("auktion.kronofogden.se", "kfm.auction2000.se")),
    Source("pvp-giustizia", "IT", "EUR", "Europe/Rome",
           ("https://pvp.giustizia.it/pvp/",), ("pvp.giustizia.it",)),
    Source("finshop", "BE", "EUR", "Europe/Brussels",
           ("https://finshop.belgium.be/shop?type=auction",), ("finshop.belgium.be",)),
    Source("onlineveilingmeester", "NL", "EUR", "Europe/Amsterdam",
           ("https://onlineveilingmeester.nl/nl/",), ("onlineveilingmeester.nl",)),
    Source("e-leiloes", "PT", "EUR", "Europe/Lisbon",
           ("https://www.e-leiloes.pt/",), ("e-leiloes.pt",)),
    Source("licytacje-komornik", "PL", "PLN", "Europe/Warsaw",
           ("https://licytacje.komornik.pl/",), ("licytacje.komornik.pl",)),
    Source("vebeg", "DE", "EUR", "Europe/Berlin",
           ("https://www.vebeg.de/de/auktionen/auktionen_suchen.htm?DO_SUCHE=1&SUCH_MATGRUPPE=1000",),
           ("vebeg.de",)),
)

OVM_LIST_URL = "https://onlineveilingmeester.nl/rest/nl/v2/kavels"
OVM_DETAIL_URL = "https://onlineveilingmeester.nl/rest/nl/v2/veilingen/{auction_id}/kavels/{lot_number}"
OVM_PUBLIC_URL = "https://onlineveilingmeester.nl/nl/veilingen/{auction_id}/kavels/{lot_number}"
OVM_VEHICLE_CATEGORIES = {10, 11}  # Personenauto's, Bedrijfsauto's
DOMAINE_GRAPHQL_URL = "https://encheres-domaine.gouv.fr/gateway/magento/graphql/"
DOMAINE_PUBLIC_URL = "https://encheres-domaine.gouv.fr/lot/{url_key}.html"
DOMAINE_CATEGORY_UID = "NQ=="  # Véhicules de tourisme, resolved from the official category API.
DOMAINE_QUERY = """query getCategoryLots($currentPage:Int $filter:ProductAttributeFilterInput! $pageSize:Int){products(currentPage:$currentPage filter:$filter pageSize:$pageSize sort:{start_auction_lot_at:ASC}){items{auction auction_type end_auction_lot_at id last_bid lot_number lot_status name price_auction professional_only short_description{html} start_auction_lot_at url_key} page_info{total_pages} total_count}}"""


def plain(value: Any) -> str:
    text = unicodedata.normalize("NFKD", html.unescape(str(value or "")))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split())


def official_url(url: str, source: Source) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in source.domains)


def ecb_rates(session: requests.Session, timeout: int) -> dict[str, float]:
    rates = {"EUR": 1.0}
    try:
        response = session.get(ECB_URL, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for node in root.iter():
            currency, rate = node.attrib.get("currency"), node.attrib.get("rate")
            if currency and rate:
                rates[currency.upper()] = float(rate)
    except (requests.RequestException, ET.ParseError, ValueError):
        pass
    return rates


def parse_amount(raw: str, currency: str, rates: dict[str, float]) -> int:
    compact = re.sub(r"\s+", "", raw)
    if "," in compact and "." in compact:
        compact = compact.replace(".", "").replace(",", ".") if compact.rfind(",") > compact.rfind(".") else compact.replace(",", "")
    elif "," in compact:
        tail = compact.rsplit(",", 1)[1]
        compact = compact.replace(",", ".") if len(tail) <= 2 else compact.replace(",", "")
    elif compact.count(".") > 1 or ("." in compact and len(compact.rsplit(".", 1)[1]) == 3):
        compact = compact.replace(".", "")
    try:
        amount = float(compact)
        rate = rates.get(currency.upper())
        return int(round(amount / rate)) if rate and amount > 0 else 0
    except ValueError:
        return 0


def parse_end(match: re.Match[str], timezone: str) -> dt.datetime | None:
    raw_date = match.group(1)
    parts = [int(value) for value in re.split(r"[-/.]", raw_date)]
    if parts[0] > 1900:
        year, month, day = parts
    else:
        day, month, year = parts
    hour, minute, second = int(match.group(2) or 23), int(match.group(3) or 59), int(match.group(4) or 0)
    try:
        return dt.datetime(year, month, day, hour, minute, second,
                           tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    except ValueError:
        return None


def parse_iso_utc(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def first_date(value: Any) -> str:
    """Return an exact source date only; never manufacture a day/month."""
    text = plain(value)
    for pattern in (r"\b(20\d{2}-\d{2}-\d{2})\b", r"\b(\d{1,2}[./-]\d{1,2}[./-]20\d{2})\b"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _ovm_detail_to_row(detail: dict[str, Any], *, now: dt.datetime) -> dict[str, str] | None:
    auction = detail.get("veiling") or {}
    data = detail.get("kavelData") or {}
    category = detail.get("categorie") or {}
    end = parse_iso_utc(detail.get("sluitingsDatumISO"))
    if (
        auction.get("type") != "DRZ"
        or not auction.get("isGeopend")
        or detail.get("isClosed")
        or not detail.get("zichtbaar", True)
        or not detail.get("buitenlandseBiederToegestaan")
        or category.get("id") not in OVM_VEHICLE_CATEGORIES
        or data.get("kavelDataType") != "AUTO"
        or end is None or end <= now
    ):
        return None
    try:
        year = int(data.get("bouwjaar") or 0)
        price = float(detail.get("hoogsteBod") or 0)
    except (TypeError, ValueError):
        return None
    if not 2023 <= year <= 2026 or price <= 0:
        return None
    specifications = BeautifulSoup(str(data.get("specificaties") or ""), "html.parser").get_text(" ", strip=True)
    condition_text = plain(" ".join([specifications, str(data.get("perceivedDeficiencies") or "")])).casefold()
    document_or_condition_blocks = (
        "kentekenbewijs ontbreekt", "registratiebewijs ontbreekt", "cvo dient nog", "niet rijvaardig",
        "niet verkeersveilig", "start niet", "motorschade", "versnellingsbak defect", "total loss",
    )
    if detail.get("heeftGeenAfgifte") or any(marker in condition_text for marker in document_or_condition_blocks):
        return None
    international = re.search(r"Eerste toelating internationaal\s*:\s*([^;|]+)", specifications, re.I)
    registration = first_date(data.get("registratiedatum")) or first_date(international.group(1) if international else "")
    if year == 2023 and not registration:
        return None
    auction_id = str(auction.get("id") or "")
    lot_number = str(detail.get("volgNummer") or "")
    item_id = str(detail.get("id") or "")
    if not (auction_id.isdigit() and lot_number and item_id.isdigit()):
        return None
    title = plain(data.get("naam") or detail.get("naam") or f"DRZ vehicle {item_id}")
    model_key = "_".join(filter(None, [plain(data.get("merk")).lower(), plain(data.get("model") or data.get("productType")).lower()]))
    return {
        "listing_id": item_id,
        "model_key": re.sub(r"[^a-z0-9]+", "_", model_key).strip("_")[:80],
        "title": title, "source": "onlineveilingmeester",
        "source_url": OVM_PUBLIC_URL.format(auction_id=auction_id, lot_number=lot_number),
        "first_registration_date": registration or str(year),
        "fuel": plain(data.get("brandstof")),
        "engine_cc": re.sub(r"\D", "", str(data.get("motorinhoud") or "")),
        "mileage_km": re.sub(r"\D", "", str(data.get("kilometerstand") or "")),
        "price_eur": f"{int(round(price))}.00", "seller_type": "government_auction",
        "accident_free": "unknown", "service_history": "unknown",
        "transmission": plain(data.get("transmissie") or data.get("versnellingsbak")), "country": "NL",
        "auction_end_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "sale_term_code": "auction",
        "sale_certainty": "auction",
        "sale_certainty_note": "DRZ government-property auction; official API confirms an open lot, foreign bidders, registration evidence and no explicit missing-document or major-condition marker. Verify export papers, fees and expert condition before bidding.",
    }


def harvest_onlineveilingmeester(session: requests.Session, *, timeout: int) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Use OVM's own structured API and admit DRZ government lots only."""
    now = dt.datetime.now(UTC)
    summaries: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for page in range(100):
        try:
            response = session.get(OVM_LIST_URL, params={"page": page, "size": 100}, headers=HEADERS, timeout=timeout)
            response.raise_for_status(); payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}")
            break
        for item in payload.get("content") or []:
            if isinstance(item, dict) and item.get("id") is not None:
                summaries[str(item["id"])] = item
        if payload.get("last") or page + 1 >= min(int(payload.get("totalPages") or 1), 100):
            break
    candidates: list[dict[str, Any]] = []
    for item in summaries.values():
        auction = item.get("veiling") or {}; category = item.get("categorie") or {}
        end = parse_iso_utc(item.get("sluitingsDatumISO"))
        if (
            auction.get("type") == "DRZ" and auction.get("isGeopend")
            and item.get("buitenlandseBiederToegestaan")
            and category.get("id") in OVM_VEHICLE_CATEGORIES
            and end is not None and end > now and float(item.get("hoogsteBod") or 0) > 0
            and re.search(r"\b202[3-6]\b", plain(item.get("naam")))
        ):
            candidates.append(item)
    rows: list[dict[str, str]] = []
    for item in candidates:
        auction_id = (item.get("veiling") or {}).get("id"); lot_number = item.get("volgNummer")
        try:
            response = session.get(OVM_DETAIL_URL.format(auction_id=auction_id, lot_number=lot_number), headers=HEADERS, timeout=timeout)
            response.raise_for_status(); row = _ovm_detail_to_row(response.json(), now=now)
            if row:
                rows.append(row)
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}")
    unique = {row["source_url"]: row for row in rows}
    return list(unique.values()), {
        "discovered": len(summaries), "eligible_candidates": len(candidates), "accepted": len(unique),
        "errors": errors[:20], "evidence": "Official OVM REST API; veiling.type=DRZ; foreign bidder=true",
    }


def _domaine_item_to_row(item: dict[str, Any], *, now: dt.datetime) -> dict[str, str] | None:
    """Convert one official Domaine API item, enforcing public/active/vehicle gates."""
    if item.get("professional_only") not in (0, False, "0") or str(item.get("auction_type")) != "1":
        return None
    start = parse_iso_utc(item.get("start_auction_lot_at")); end = parse_iso_utc(item.get("end_auction_lot_at"))
    if start is None or end is None or not (start <= now < end):
        return None
    description = BeautifulSoup(str((item.get("short_description") or {}).get("html") or ""), "html.parser").get_text(" ", strip=True)
    registration_match = re.search(
        r"(?:1\s*(?:ère|ere)|premi[eè]re)\s+mise\s+en\s+c[io]rculation(?:\s+le)?\s*:?[ ]*"
        r"(\d{1,2}[/-]\d{1,2}[/-]20\d{2})", description, re.I,
    )
    if not registration_match:
        return None
    registration = registration_match.group(1)
    year_match = re.search(r"20\d{2}", registration)
    year = int(year_match.group(0)) if year_match else 0
    try:
        price = float(item.get("last_bid") or item.get("price_auction") or 0)
    except (TypeError, ValueError):
        return None
    if not 2023 <= year <= 2026 or price <= 0:
        return None
    item_id = str(item.get("id") or ""); url_key = str(item.get("url_key") or "").strip()
    if not item_id.isdigit() or not url_key:
        return None
    title = plain(item.get("name")); mileage_match = re.search(r"([\d .]{2,})\s*km\b", description, re.I)
    fuel_match = re.search(r"\b(gazole|diesel|essence|hybride|[ée]lectri(?:que|cit[ée]))\b", description, re.I)
    return {
        "listing_id": item_id, "model_key": re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:80],
        "title": title, "source": "encheres-du-domaine",
        "source_url": DOMAINE_PUBLIC_URL.format(url_key=url_key), "first_registration_date": registration,
        "fuel": plain(fuel_match.group(1)) if fuel_match else "", "engine_cc": "",
        "mileage_km": re.sub(r"\D", "", mileage_match.group(1)) if mileage_match else "",
        "price_eur": f"{int(round(price))}.00", "seller_type": "government_auction",
        "accident_free": "unknown", "service_history": "unknown", "transmission": "", "country": "FR",
        "auction_end_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "sale_term_code": "auction", "sale_certainty": "auction",
        "sale_certainty_note": "French Domaine government auction; official API confirms a live public lot not restricted to automotive professionals. Verify export papers, fees and condition.",
    }


def harvest_domaine(session: requests.Session, *, timeout: int) -> tuple[list[dict[str, str]], dict[str, Any]]:
    now = dt.datetime.now(UTC); errors: list[str] = []; items: dict[str, dict[str, Any]] = {}
    # Establish the anti-bot session cookie using the portal's own redirect.
    try:
        request_html(session, "https://encheres-domaine.gouv.fr/", timeout)
    except requests.RequestException as exc:
        return [], {"discovered": 0, "accepted": 0, "errors": [f"{type(exc).__name__}:{str(exc)[:120]}"]}
    for page in range(1, 101):
        variables = {"currentPage": page, "pageSize": 100,
                     "filter": {"category_uid": {"eq": DOMAINE_CATEGORY_UID}, "lot_status": {"in": ["13", "14"]}}}
        try:
            response = session.get(DOMAINE_GRAPHQL_URL, params={"query": DOMAINE_QUERY,
                                   "operationName": "getCategoryLots",
                                   "variables": json.dumps(variables, separators=(",", ":"))},
                                   headers=HEADERS, timeout=timeout)
            response.raise_for_status(); products = response.json()["data"]["products"]
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}"); break
        for item in products.get("items") or []:
            if isinstance(item, dict) and item.get("id") is not None:
                items[str(item["id"])] = item
        if page >= int((products.get("page_info") or {}).get("total_pages") or 1):
            break
    rows = [row for item in items.values() if (row := _domaine_item_to_row(item, now=now))]
    unique = {row["source_url"]: row for row in rows}
    return list(unique.values()), {"discovered": len(items), "accepted": len(unique), "errors": errors[:20],
                                  "evidence": "Official French Domaine Magento GraphQL catalogue; public active lots only"}


def harvest_vebeg(source: Source, session: requests.Session, *, timeout: int) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Harvest VEBEG live vehicle auctions (not opaque tender sales)."""
    now = dt.datetime.now(UTC); errors: list[str] = []
    try:
        markup = request_html(session, source.discovery_urls[0], timeout)
    except requests.RequestException as exc:
        return [], {"discovered": 0, "accepted": 0, "errors": [f"{type(exc).__name__}:{str(exc)[:120]}"]}
    soup = BeautifulSoup(markup, "html.parser"); urls: list[str] = []
    for anchor in soup.select("a[href]"):
        url = urljoin(source.discovery_urls[0], str(anchor.get("href") or ""))
        if official_url(url, source) and "SHOW_AUS=" in url and "SHOW_LOS=" in url and url not in urls:
            urls.append(url)
    rows: list[dict[str, str]] = []
    for url in urls:
        try:
            text = plain(BeautifulSoup(request_html(session, url, timeout), "html.parser").get_text(" ", strip=True))
            title_match = re.search(r"Auktion/Los:\s*\d+[.]\d+\s+(.+?)\s+(?:Motorleistung|Daten\s+Erstzulassung)", text, re.I)
            lot_match = re.search(r"Auktion/Los:\s*(\d+[.]\d+)", text, re.I)
            reg_match = re.search(r"Erstzulassung:\s*(\d{1,2})/(\d{2,4})", text, re.I)
            end_match = re.search(r"Gebotstermin:\s*(\d{1,2}[.]\d{1,2}[.]20\d{2}),\s*(\d{1,2}):(\d{2})", text, re.I)
            price_match = re.search(r"(?:Aktuelles Gebot|Startpreis):\s*EUR\s*([\d.]+(?:,\d{2})?)", text, re.I)
            if not (title_match and lot_match and reg_match and end_match and price_match):
                continue
            year = int(reg_match.group(2)); year = year + 2000 if year < 100 else year
            if not 2024 <= year <= 2026:
                continue
            end = dt.datetime.strptime(f"{end_match.group(1)} {end_match.group(2)}:{end_match.group(3)}", "%d.%m.%Y %H:%M").replace(tzinfo=ZoneInfo(source.timezone)).astimezone(UTC)
            price = parse_amount(price_match.group(1), "EUR", {"EUR": 1.0})
            if end <= now or price <= 0:
                continue
            mileage_match = re.search(r"Laufleistung:\s*ca[.]?\s*([\d.]+)\s*km", text, re.I); title = plain(title_match.group(1))
            rows.append({
                "listing_id": lot_match.group(1), "model_key": re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:80],
                "title": title, "source": "vebeg", "source_url": url, "first_registration_date": str(year),
                "fuel": "", "engine_cc": "", "mileage_km": re.sub(r"\D", "", mileage_match.group(1)) if mileage_match else "",
                "price_eur": f"{price}.00", "seller_type": "government_auction", "accident_free": "unknown",
                "service_history": "unknown", "transmission": "", "country": "DE",
                "auction_end_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "sale_term_code": "auction", "sale_certainty": "auction",
                "sale_certainty_note": "VEBEG is wholly owned by the Federal Republic of Germany. Live auction price and end are explicit; verify bidder-account eligibility, export papers, VAT and condition.",
            })
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}")
    unique = {row["source_url"]: row for row in rows}
    return list(unique.values()), {"discovered": len(urls), "accepted": len(unique), "errors": errors[:20],
                                  "evidence": "VEBEG official live-auction pages; federally owned company"}


def listing_id(url: str) -> str:
    values = re.findall(r"(?:\d{4,}|[A-Z]{2}-?\d{3,})", url, re.I)
    return values[-1] if values else ""


def parse_detail(markup: str, url: str, source: Source, rates: dict[str, float],
                 *, now: dt.datetime | None = None) -> dict[str, str] | None:
    now = now or dt.datetime.now(UTC)
    soup = BeautifulSoup(markup, "html.parser")
    text = plain(soup.get_text(" ", strip=True))
    title_node = soup.select_one("h1") or soup.select_one("h2") or soup.select_one("meta[property='og:title']")
    title = plain(title_node.get("content") if title_node and title_node.name == "meta" else
                  title_node.get_text(" ", strip=True) if title_node else soup.title.string if soup.title else "")
    if not title or not (MAKES.search(title + " " + text[:3000]) and VEHICLE_WORDS.search(text[:5000])):
        return None
    year_match = YEAR_LABEL.search(text)
    year = int(year_match.group(1)) if year_match else 0
    if not 2023 <= year <= 2026:
        return None
    end_match = END_LABEL.search(text)
    end = parse_end(end_match, source.timezone) if end_match else None
    if end is None or end <= now:
        return None
    price_match = PRICE_LABEL.search(text)
    if not price_match:
        return None
    token = price_match.group(2).lower()
    currency = "EUR" if token in {"eur", "€"} else "PLN" if token in {"pln", "zł"} else "SEK"
    price = parse_amount(price_match.group(1), currency, rates)
    if price <= 0:
        return None
    item_id = listing_id(url)
    if not item_id or not official_url(url, source):
        return None
    mileage_match = MILEAGE_LABEL.search(text)
    mileage = re.sub(r"\D", "", mileage_match.group(1)) if mileage_match else ""
    reg_match = re.search(r"(?:erstzulassung|mise en circulation|first registration|matriculacion|immatricolazione)\D{0,20}(\d{1,2}[./-]\d{1,2}[./-]20\d{2})", text, re.I)
    fuel_match = re.search(r"(?:fuel|kraftstoff|carburant|energie|combustible|carburante|paliwo|bransle)\D{0,12}([A-Za-zÀ-ž-]{3,20})", text, re.I)
    key = re.sub(r"[^a-z0-9]+", "_", plain(title).lower()).strip("_")[:80]
    return {
        "listing_id": item_id, "model_key": key, "title": title, "source": source.key,
        "source_url": url, "first_registration_date": reg_match.group(1) if reg_match else str(year),
        "fuel": plain(fuel_match.group(1)) if fuel_match else "", "engine_cc": "",
        "mileage_km": mileage, "price_eur": f"{price}.00", "seller_type": "auction",
        "accident_free": "unknown", "service_history": "unknown", "transmission": "",
        "country": source.country, "auction_end_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sale_term_code": "auction", "sale_certainty": "auction",
        "sale_certainty_note": f"Official {source.key} auction; verify registration, fees and lot conditions before bidding.",
    }


def request_html(session: requests.Session, url: str, timeout: int) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    # Domaine's first response is a cookie-bound JavaScript redirect.
    redirect = re.search(r"window\.location\.href=['\"]([^'\"]+)", response.text)
    if redirect:
        response = session.get(urljoin(url, redirect.group(1)), headers=HEADERS, timeout=timeout)
        response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def browser_html(url: str, timeout: int) -> str:
    from patchright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(locale="en-US")
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(4000)
        markup = page.content()
        browser.close()
        return markup


def candidate_links(markup: str, base_url: str, source: Source) -> list[str]:
    soup = BeautifulSoup(markup, "html.parser")
    scored: list[tuple[int, str]] = []
    for anchor in soup.select("a[href]"):
        url = urljoin(base_url, str(anchor.get("href") or ""))
        label = plain(anchor.get_text(" ", strip=True))
        if not official_url(url, source) or url == base_url:
            continue
        path = urlparse(url).path.lower()
        vehicle_label = bool(MAKES.search(label) or VEHICLE_WORDS.search(label))
        vehicle_event = "/event/" in path and bool(VEHICLE_WORDS.search(label + " " + path))
        if not vehicle_label and not vehicle_event:
            continue
        score = (3 if MAKES.search(label) else 0) + (2 if VEHICLE_WORDS.search(label) else 0)
        score += 1 if re.search(r"auction|auktion|enchere|vente|subast|asta|lot|object|licit|veiling|product", path) else 0
        score += 1 if listing_id(url) else 0
        if score >= 2:
            scored.append((score, url))
    return [url for _, url in sorted(set(scored), key=lambda item: (-item[0], item[1]))]


def discover(session: requests.Session, source: Source, timeout: int, browser: bool,
             max_candidates: int) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    errors: list[str] = []
    frontier = list(source.discovery_urls)
    seen_pages: set[str] = set()
    index = 0
    while index < len(frontier) and index < 3:
        page_url = frontier[index]
        index += 1
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        try:
            markup = request_html(session, page_url, timeout)
            links = candidate_links(markup, page_url, source)
            if browser and len(links) < 2:
                links = candidate_links(browser_html(page_url, timeout), page_url, source)
            for url in links:
                if url not in urls:
                    urls.append(url)
            # One intermediate event/category hop is allowed.
            for url in links[:5]:
                if (not listing_id(url) or "/event/" in urlparse(url).path.lower()) and url not in frontier:
                    frontier.append(url)
        except Exception as exc:  # one portal must never abort all sources
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}")
    return urls[:max_candidates], errors


def harvest(source: Source, session: requests.Session, rates: dict[str, float], *, timeout: int,
            browser: bool, max_candidates: int, sleep_seconds: float) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if source.key == "encheres-du-domaine":
        return harvest_domaine(session, timeout=timeout)
    if source.key == "onlineveilingmeester":
        return harvest_onlineveilingmeester(session, timeout=timeout)
    if source.key == "vebeg":
        return harvest_vebeg(source, session, timeout=timeout)
    urls, errors = discover(session, source, timeout, browser, max_candidates)
    rows: list[dict[str, str]] = []
    browser_attempts = 0
    for url in urls:
        try:
            try:
                markup = request_html(session, url, timeout)
            except requests.RequestException:
                if not browser or browser_attempts >= 5:
                    raise
                browser_attempts += 1
                markup = browser_html(url, timeout)
            row = parse_detail(markup, url, source, rates)
            if row is None and browser and browser_attempts < 5:
                browser_attempts += 1
                row = parse_detail(browser_html(url, timeout), url, source, rates)
            if row:
                rows.append(row)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}")
        if sleep_seconds:
            time.sleep(sleep_seconds)
    unique = {row["source_url"]: row for row in rows}
    return list(unique.values()), {"discovered": len(urls), "accepted": len(unique), "errors": errors[:20]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-candidates", type=int, default=40)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    selected = [source for source in SOURCES if not args.source or source.key in args.source]
    session = requests.Session()
    rates = ecb_rates(session, args.timeout)
    rows: list[dict[str, str]] = []
    report: dict[str, Any] = {"generated_at_utc": dt.datetime.now(UTC).isoformat(), "sources": {}}
    for source in selected:
        source_rows, source_report = harvest(source, session, rates, timeout=args.timeout,
                                             browser=not args.no_browser,
                                             max_candidates=args.max_candidates,
                                             sleep_seconds=args.sleep)
        rows.extend(source_rows)
        report["sources"][source.key] = source_report
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader(); writer.writerows(rows)
    temporary.replace(args.out)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value["accepted"] for key, value in report["sources"].items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
