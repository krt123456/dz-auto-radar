#!/usr/bin/env python3
"""Official vehicle-auction watch for additional batch 1 sources.

Covers: VEBEG (de), PortalDrazeb (cz), AsteGiudiziarie (it),
CampenAuktioner (dk), AutoAuctions.gr (gr), Restwertboerse (ch),
CARAUKTION (ch), AutoAuction24 (ch), Romu (ee), WEBY (ee),
FINA Ponip (hr), NAV Hungary (hu), PKO Leasing (pl),
Exleasingcar (pl/ro), Auctionmaster (nl), 2trde (de),
NorthgateTrade (es), Troostwijk (nl), CarCollect (nl).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

UTC = dt.timezone.utc
PREVIOUS_SNAPSHOT_MAX_AGE = dt.timedelta(hours=8)
HEADERS = {
    "User-Agent": "DZ-Auto-Radar/1.0 (+official public-auction monitor)",
    "Accept-Language": "en,de;q=0.9,fr;q=0.8,es;q=0.8,it;q=0.8,nl;q=0.8,da;q=0.8,cs;q=0.8,hr;q=0.8,hu;q=0.8,pl;q=0.8,el;q=0.8,et;q=0.8,sv;q=0.8,sk;q=0.8",
}
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

SOURCE_NAMES = {
    "vebeg": "VEBEG",
    "portaldrazeb": "Portal drazeb",
    "astegiudiziarie": "Aste Giudiziarie",
    "campenauktioner": "Campen Auktioner",
    "autoauctionsgr": "Auto-Auctions.gr",
    "restwertboerse": "Restwertbörse.ch",
    "carauktion-ch": "CARAUKTION AG",
    "autoauction24-ch": "AutoAuction24.ch",
    "romu": "Romu",
    "weby": "WEBY",
    "fina-ponip": "FINA Ponip",
    "nav-hu": "NAV Elektronikus Árverés",
    "pkoleasing": "PKO Leasing Auctions",
    "exleasingcar": "Exleasingcar",
    "auctionmaster": "Auctionmaster",
    "2trde": "2trde Auctions",
    "northgatetrade": "Northgate Trade",
    "troostwijk": "Troostwijk Auctions",
    "carcollect": "CarCollect",
}

BRANDS = re.compile(
    r"\b(?:audi|bmw|citro[eë]n|dacia|fiat|ford|honda|hyundai|iveco|jeep|kia|land rover|"
    r"mazda|mercedes(?:-benz)?|mini|mitsubishi|nissan|opel|peugeot|porsche|renault|seat|"
    r"skoda|škoda|subaru|suzuki|tesla|toyota|volkswagen|volvo|vw|cupra|daf|man|scania|"
    r"alfa\s+romeo|bentley|cadillac|chevrolet|chrysler|corvette|crossover|ds|ferrari|"
    r"genesis|infiniti|jaguar|lamborghini|lancia|lexus|maserati|porsche|ram|rover|"
    r"saab|smart|subaru|suzuki|tesla|volvo)\b",
    re.I,
)
VEHICLE_WORDS = re.compile(
    r"\b(?:auto(?:mobil\w*|turism\w*)?|automa[sš]in\w*|vozidlo|vehicle|car|van|kombi|"
    r"camion|furgoneta|furgon|utilitaire|transporter|lieferwagen|lkw|nutzfahrzeug)\b",
    re.I,
)
NON_CAR = re.compile(
    r"\b(?:moto(?:cikl|ratas)\w*|moped\w*|skuter\w*|motorcycle\w*|traktor|komb[aá]jn|"
    r"container|kont[eé]ner|p[óo]tkocsi|prikolica|anhänger|remorque|remolc|"
    r"motor|engine|gearbox|transmissi|بسم الله|لا إله)\b",
    re.I,
)
DAMAGE = re.compile(
    r"\b(?:hav[aá]ri|po[šs]kod|nehod|salvage|vrak|nepojazd|nepojízd|"
    r"avari|daunat|dezmembr|sudau[zž]|remont|brand|feuer|incendio|"
    r"totalschaden|write.?off|accidenté|accidentado|damaged)\b",
    re.I,
)


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
    raw = clean(value)
    for sym in ("EUR", "CHF", "PLN", "HRK", "HUF", "RON", "DKK", "SEK", "BGN", "CZK", "$", "€", "£"):
        raw = raw.replace(sym, "")
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
        result = float(raw)
    except ValueError:
        return None
    return result if result > 0 else None


def parse_datetime_any(value: str, country: str = "de") -> dt.datetime | None:
    text = clean(value).strip()
    if not text:
        return None
    tz_map = {
        "de": "Europe/Berlin", "at": "Europe/Vienna", "ch": "Europe/Zurich",
        "it": "Europe/Rome", "fr": "Europe/Paris", "es": "Europe/Madrid",
        "nl": "Europe/Amsterdam", "dk": "Europe/Copenhagen", "se": "Europe/Stockholm",
        "fi": "Europe/Helsinki", "no": "Europe/Oslo", "pl": "Europe/Warsaw",
        "cz": "Europe/Prague", "sk": "Europe/Bratislava", "hu": "Europe/Budapest",
        "ro": "Europe/Bucharest", "hr": "Europe/Zagreb", "si": "Europe/Ljubljana",
        "ee": "Europe/Tallinn", "lv": "Europe/Riga", "lt": "Europe/Vilnius",
        "gr": "Europe/Athens", "bg": "Europe/Sofia", "pt": "Europe/Lisbon",
        "be": "Europe/Brussels", "lu": "Europe/Luxembourg", "mt": "Europe/Malta",
        "is": "Atlantic/Reykjavik",
    }
    try:
        tz = ZoneInfo(tz_map.get(country, "UTC"))
    except ZoneInfoNotFoundError:
        tz = UTC
    formats = (
        "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
        "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y/%m/%d %H:%M",
        "%d-%m-%Y %H:%M", "%d %b %Y %H:%M", "%d %B %Y %H:%M",
    )
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=tz).astimezone(UTC)
        except ValueError:
            continue
    return None


def parse_utc(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(clean(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def is_vehicle(title: str, description: str = "") -> bool:
    text = clean(f"{title} {description}")
    return not NON_CAR.search(text) and bool(BRANDS.search(text) or VEHICLE_WORDS.search(text))


def eligibility(title: str, description: str, year: int | None) -> tuple[str, str]:
    text = clean(f"{title} {description}")
    if DAMAGE.search(text):
        return "not_eligible", "Damage or salvage marker detected in official listing."
    if year is not None and year < 2023:
        return "not_eligible", "Vehicle year older than 3-year Algeria import window."
    return "review_required", "Official live vehicle notice; verify before purchase."


def fuel_from_text(value: Any) -> str:
    text = folded(value)
    if re.search(r"\b(?:e[- ]?hybrid|plug[- ]?in|hybrid|hibrid|phev|mhev|hev)\w*\b", text):
        return "hybrid"
    if re.search(r"\b(?:diesel|diisel|motorina|dyzel|dizel|nafta|gazole|tdi|hdi|cdi|dci)\b", text):
        return "diesel"
    if re.search(r"\b(?:electric|elektr\w*|bev)\w*\b", text):
        return "electric"
    if re.search(r"\b(?:petrol|benzin|benzina|essence|gasoline|tsi|tfsi|fsi|gdi)\b", text):
        return "petrol"
    return "unknown"


def parse_vehicle_year(title: Any, desc: Any = "") -> int | None:
    text = clean(f"{title} {desc}")
    match = re.search(r"(?:19|20)\d{2}", text)
    if match:
        year = int(match.group(0))
        if 1980 <= year <= dt.datetime.now(UTC).year + 1:
            return year
    return None


def fetch_rates(session: requests.Session, timeout: int) -> dict[str, float]:
    rates = {"EUR": 1.0}
    try:
        response = session.get(ECB_URL, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.content)
        for node in root.iter():
            if node.attrib.get("currency") and node.attrib.get("rate"):
                rates[node.attrib["currency"].upper()] = float(node.attrib["rate"])
    except Exception:
        pass
    return rates


def make_row(
    *, source: str, listing_id: str, country: str, url: str, title: str,
    now: dt.datetime, end: dt.datetime | None = None,
    price_amount: float | None = None, currency: str = "EUR", rate: float | None = 1.0,
    price_kind: str = "unknown", price_label: str = "", description: str = "",
    vehicle_year: int | None = None, mileage_km: int | None = None,
    fuel_value: str | None = None,
) -> dict[str, Any]:
    year = vehicle_year if vehicle_year is not None else parse_vehicle_year(title, description)
    fuel_kind = fuel_value or fuel_from_text(f"{title} {description}")
    status, reason = eligibility(title, description, year)
    price_eur = round(price_amount / rate, 2) if price_amount and rate and rate > 0 else None
    return {
        "id": f"{source}:{listing_id}", "source": source, "source_key": source,
        "source_name": SOURCE_NAMES.get(source, source), "country": country, "url": url,
        "title": clean(title), "model": clean(title), "year": year,
        "mileage_km": mileage_km, "fuel": fuel_kind,
        "price_amount": price_amount, "price_currency": currency, "price_eur": price_eur,
        "price_kind": price_kind, "price_label": clean(price_label),
        "canonical_end_utc": end.isoformat() if end else None,
        "sale_end_utc": end.isoformat() if end else None,
        "last_seen_at": now.isoformat(), "eligibility_status": status,
        "eligibility_reason": reason, "bid_visibility": "public" if price_amount else "unknown",
        "access_sale_note": "Foreign participation and export requirements must be confirmed.",
        "auction_status": "active", "damage": "yes" if DAMAGE.search(clean(f"{title} {description}")) else "",
        "documents": "", "description": clean(description)[:2500],
    }


def _get(session: requests.Session, url: str, timeout: int) -> requests.Response:
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
    raise last_error


# ─── VEBEG (Germany) ────────────────────────────────────────────────────────

VEBEG_SEARCH = "https://www.vebeg.de/de/verkauf/suchen.htm?DO_SUCHE=1&SUCH_MATGRUPPE=1010&page={page}"
VEBEG_BASE = "https://www.vebeg.de"


def harvest_vebeg(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    seen_ids: set[str] = set()
    pages_scanned = 0
    for page in range(1, 20):
        try:
            markup = _get(session, VEBEG_SEARCH.format(page=page), timeout).text
        except requests.RequestException:
            break
        pages_scanned += 1
        lot_matches = re.findall(r'SHOW_AUS=(\d+)&amp;SHOW_LOS=(\d+)', markup, re.I)
        if not lot_matches:
            break
        for aus_id, los_num in lot_matches:
            lot_id = f"{aus_id}.{los_num}"
            if lot_id in seen_ids:
                continue
            seen_ids.add(lot_id)
            lot_url = f"{VEBEG_BASE}/de/verkauf/suchen.htm?DO_SUCHE=1&SUCH_MATGRUPPE=1010&SHOW_AUS={aus_id}&SHOW_LOS={los_num}"
            try:
                detail = _get(session, lot_url, timeout).text
                title_match = re.search(r"<title>([^<]+)</title>", detail, re.I)
                title = clean(title_match.group(1)) if title_match else ""
                title = re.sub(r"\s*\|.*$", "", title).strip()
                if not title or not is_vehicle(title, ""):
                    continue
                end_match = re.search(r"Gebotstermin:\s*<b>(\d{2}\.\d{2}\.\d{4})\s*,\s*(\d{2}:\d{2})", detail, re.I)
                end = parse_datetime_any(f"{end_match.group(1)} {end_match.group(2)}", "de") if end_match else None
                price_match = re.search(r"(?:Startpreis|Gebotspreis|Preis)\s*:\s*(?:<[^>]+>)*\s*([0-9 .,]+)\s*(?:€|EUR)", detail, re.I)
                amount = parse_number(price_match.group(1)) if price_match else None
                rows.append(make_row(
                    source="vebeg", listing_id=lot_id, country="de", url=lot_url,
                    title=title, now=now, end=end, price_amount=amount, currency="EUR", rate=rates.get("EUR"),
                    price_kind="starting_bid" if amount else "unknown", price_label=("starting price" if amount else ""), description=clean(detail),
                ))
            except requests.RequestException:
                continue
    return rows, {"status": "ok", "pages_scanned": pages_scanned, "vehicle_rows": len(rows)}


# ─── Portal Drazeb (Czech Republic) ────────────────────────────────────────

PORTALDRAZEB_BASE = "https://www.portaldrazeb.cz"
PORTALDRAZEB_LIST = "https://www.portaldrazeb.cz/drazby/probihajici"


def harvest_portaldrazeb(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, PORTALDRAZEB_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/drazb[^"]*)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    detail_urls = [urljoin(PORTALDRAZEB_BASE, link) for link in unique_links if "/drazba/" in link]
    if not detail_urls:
        detail_urls = [urljoin(PORTALDRAZEB_BASE, link) for link in unique_links[:50]]
    for detail_url in detail_urls[:50]:
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:cena|price|vyvolávací|Kč)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        end_match = re.search(r"(?:konec|end|deadline|datum)[^0-9]*(\d{1,2}[\./-]\d{1,2}[\./-]\d{2,4})", detail, re.I)
        end = parse_datetime_any(end_match.group(1), "cz") if end_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="portaldrazeb", listing_id=lot_id, country="cz", url=detail_url,
            title=title, now=now, end=end, price_amount=amount, currency="CZK",
            rate=rates.get("CZK"), price_kind="starting_bid" if amount else "unknown", price_label=("starting price" if amount else ""),
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(detail_urls[:50]), "vehicle_rows": len(rows)}


# ─── Aste Giudiziarie (Italy) ───────────────────────────────────────────────

ASTEGIUDIZIARIE_BASE = "https://www.astegiudiziarie.it"
ASTEGIUDIZIARIE_SEARCH = "https://www.astegiudiziarie.it/"


def harvest_astegiudiziarie(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, ASTEGIUDIZIARIE_SEARCH, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/[^"]*vendita[^"]*|/[^"]*asta[^"]*)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:40]:
        if "auto" not in link.lower() and "veicolo" not in link.lower() and "motore" not in link.lower() and "auto" not in link.lower():
            continue
        detail_url = urljoin(ASTEGIUDIZIARIE_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:prezzo|price|base|valore)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        end_match = re.search(r"(?:scadenza|termine|end)[^0-9]*(\d{1,2}[\./-]\d{1,2}[\./-]\d{2,4})", detail, re.I)
        end = parse_datetime_any(end_match.group(1), "it") if end_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="astegiudiziarie", listing_id=lot_id, country="it", url=detail_url,
            title=title, now=now, end=end, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="starting_bid" if amount else "unknown", price_label=("starting price" if amount else ""),
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:40]), "vehicle_rows": len(rows)}


# ─── Campen Auktioner (Denmark) ─────────────────────────────────────────────

CAMPEN_BASE = "https://www.campenauktioner.dk"
CAMPEN_AUCTIONS = "https://www.campenauktioner.dk/"


def harvest_campenauktioner(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, CAMPEN_AUCTIONS, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="([^"]*auktion[^"]*|[^"]*bil[^"]*|[^"]*car[^"]*|[^"]*k[øo]ret[øo]j[^"]*)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:30]:
        if link.startswith("http") and "campenauktioner.dk" not in link:
            continue
        detail_url = urljoin(CAMPEN_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:pris|price|bud|DKK)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="campenauktioner", listing_id=lot_id, country="dk", url=detail_url,
            title=title, now=now, price_amount=amount, currency="DKK",
            rate=rates.get("DKK"), price_kind="current_bid" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:30]), "vehicle_rows": len(rows)}


# ─── Auto-Auctions.gr (Greece) ──────────────────────────────────────────────

AUTOAUCTIONSGR_BASE = "https://auto-auctions.gr"
AUTOAUCTIONSGR_LIST = "https://auto-auctions.gr/"


def harvest_autoauctionsgr(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, AUTOAUCTIONSGR_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    auction_links = re.findall(r'href="([^"]*auction[^"]*)"', markup, re.I)
    lot_links = re.findall(r'href="([^"]*lot[^"]*)"', markup, re.I)
    all_links = list(dict.fromkeys(auction_links + lot_links))
    detail_urls = [urljoin(AUTOAUCTIONSGR_BASE, link) for link in all_links[:40]]
    for detail_url in detail_urls:
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:price|amount|τιμή)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="autoauctionsgr", listing_id=lot_id, country="gr", url=detail_url,
            title=title, now=now, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="starting_bid" if amount else "unknown", price_label=("starting price" if amount else ""),
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(detail_urls), "vehicle_rows": len(rows)}


# ─── Restwertbörse.ch (Switzerland) ─────────────────────────────────────────

RESTWERTBOERSE_BASE = "https://www.restwertboerse.ch"
RESTWERTBOERSE_LIST = "https://www.restwertboerse.ch/?lng=en"


def harvest_restwertboerse(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, RESTWERTBOERSE_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="([^"]*)"', markup, re.I)
    vehicle_links = [l for l in links if any(kw in l.lower() for kw in ["fahrzeug", "auto", "car", "lot", "vehicle"])]
    unique_links = list(dict.fromkeys(vehicle_links))
    if not unique_links:
        unique_links = list(dict.fromkeys(links))[:30]
    for link in unique_links[:30]:
        if link.startswith("http") and "restwertboerse.ch" not in link:
            continue
        detail_url = urljoin(RESTWERTBOERSE_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:Restwert|price|CHF)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="restwertboerse", listing_id=lot_id, country="ch", url=detail_url,
            title=title, now=now, price_amount=amount, currency="CHF",
            rate=rates.get("CHF"), price_kind="guide_price" if amount else "unknown", price_label=("residual value estimate" if amount else ""),
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:30]), "vehicle_rows": len(rows)}


# ─── CARAUKTION AG (Switzerland) ────────────────────────────────────────────

CARAUKTION_BASE = "https://www.carauktion.ch"
CARAUKTION_LIST = "https://www.carauktion.ch/auction.html"


def harvest_carauktion(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, CARAUKTION_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    title_match = re.search(r"<title>([^<]+)</title>", markup, re.I)
    if title_match and "JavaScript" in clean(title_match.group(1)):
        return rows, {"status": "skipped", "reason": "requires_javascript"}
    links = re.findall(r'href="([^"]*)"', markup, re.I)
    vehicle_links = [l for l in links if any(kw in l.lower() for kw in ["lot", "fahrzeug", "auto", "car", "auction"])]
    unique_links = list(dict.fromkeys(vehicle_links))
    for link in unique_links[:50]:
        if link.startswith("http") and "carauktion.ch" not in link:
            continue
        detail_url = urljoin(CARAUKTION_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:CHF|price|Gebot)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="carauktion-ch", listing_id=lot_id, country="ch", url=detail_url,
            title=title, now=now, price_amount=amount, currency="CHF",
            rate=rates.get("CHF"), price_kind="current_bid" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:50]), "vehicle_rows": len(rows)}


# ─── AutoAuction24.ch (Switzerland) ─────────────────────────────────────────

AUTOAUCTION24_BASE = "https://www.autoauction24.ch"
AUTOAUCTION24_LIST = "https://www.autoauction24.ch/"


def harvest_autoauction24(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, AUTOAUCTION24_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/[^"]*fahrzeug[^"]*)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:50]:
        detail_url = urljoin(AUTOAUCTION24_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:CHF|price)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="autoauction24-ch", listing_id=lot_id, country="ch", url=detail_url,
            title=title, now=now, price_amount=amount, currency="CHF",
            rate=rates.get("CHF"), price_kind="current_bid" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:50]), "vehicle_rows": len(rows)}


# ─── Romu (Estonia) ─────────────────────────────────────────────────────────

ROMU_BASE = "https://www.romu.ee"
ROMU_LIST = "https://www.romu.ee/eripakkumised"


def harvest_romu(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, ROMU_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/[^"]*auto[^"]*)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:30]:
        detail_url = urljoin(ROMU_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:€|EUR|hinn)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="romu", listing_id=lot_id, country="ee", url=detail_url,
            title=title, now=now, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="price" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:30]), "vehicle_rows": len(rows)}


# ─── WEBY (Estonia) ─────────────────────────────────────────────────────────

WEBY_BASE = "https://www.weby.ee"
WEBY_LIST = "https://www.weby.ee/"


def harvest_weby(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, WEBY_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/[^"]*auction[^"]*|/[^"]*auto[^"]*)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:30]:
        detail_url = urljoin(WEBY_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:€|EUR|hinn)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="weby", listing_id=lot_id, country="ee", url=detail_url,
            title=title, now=now, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="price" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:30]), "vehicle_rows": len(rows)}


# ─── FINA Ponip (Croatia) ───────────────────────────────────────────────────

FINA_PONIP_BASE = "https://ponip.fina.hr"
FINA_PONIP_SEARCH = "https://ponip.fina.hr/ocevidnik-web/pretrazivanje/pokretnina"


def harvest_fina_ponip(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, FINA_PONIP_SEARCH, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    csv_match = re.search(r'(?:href|src)="([^"]*\.csv[^"]*)"', markup, re.I)
    if csv_match:
        csv_url = urljoin(FINA_PONIP_BASE, csv_match.group(1))
        try:
            csv_data = _get(session, csv_url, timeout).text
            lines = csv_data.strip().split("\n")
            for line in lines[1:200]:
                parts = line.split(";")
                if len(parts) >= 3:
                    title = clean(parts[1]) if len(parts) > 1 else ""
                    if is_vehicle(title, ""):
                        lot_id = clean(parts[0]) if parts[0] else hashlib.sha256(line.encode()).hexdigest()[:16]
                        amount = parse_number(parts[2]) if len(parts) > 2 else None
                        rows.append(make_row(
                            source="fina-ponip", listing_id=lot_id, country="hr", url=FINA_PONIP_SEARCH,
                            title=title, now=now, price_amount=amount, currency="EUR",
                            rate=rates.get("EUR"), price_kind="starting_bid" if amount else "unknown", price_label=("starting price" if amount else ""),
                        ))
        except requests.RequestException:
            pass
    links = re.findall(r'href="(/ocevidnik-web/[^"]*)"', markup, re.I)
    for link in list(dict.fromkeys(links))[:20]:
        detail_url = urljoin(FINA_PONIP_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="fina-ponip", listing_id=lot_id, country="hr", url=detail_url,
            title=title, now=now, description=clean(detail),
        ))
    return rows, {"status": "ok", "vehicle_rows": len(rows)}


# ─── NAV Hungary ─────────────────────────────────────────────────────────────

NAV_HU_BASE = "https://arveres.nav.gov.hu"
NAV_HU_ACTIVE = "https://arveres.nav.gov.hu/index-futo_arveresek-ingosag.html"


def harvest_nav_hu(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, NAV_HU_ACTIVE, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="([^"]*auction[^"]*|[^"]*arveres[^"]*|[^"]*item[^"]*)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    detail_urls = [urljoin(NAV_HU_BASE, link) for link in unique_links if link.startswith("/")][:30]
    for detail_url in detail_urls:
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:Ft|HUF|price)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="nav-hu", listing_id=lot_id, country="hu", url=detail_url,
            title=title, now=now, price_amount=amount, currency="HUF",
            rate=rates.get("HUF"), price_kind="starting_bid" if amount else "unknown", price_label=("starting price" if amount else ""),
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(detail_urls), "vehicle_rows": len(rows)}


# ─── PKO Leasing (Poland) ──────────────────────────────────────────────────

PKO_BASE = "https://aukcje.pkoleasing.pl"
PKO_LIST = "https://aukcje.pkoleasing.pl/"


def harvest_pkoleasing(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, PKO_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/[^"]*lot[^"]*|/[^"]*auction[^"]*|/[^"]*przetarg[^"]*)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:30]:
        detail_url = urljoin(PKO_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:PLN|zł|price)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="pkoleasing", listing_id=lot_id, country="pl", url=detail_url,
            title=title, now=now, price_amount=amount, currency="PLN",
            rate=rates.get("PLN"), price_kind="starting_bid" if amount else "unknown", price_label=("starting price" if amount else ""),
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:30]), "vehicle_rows": len(rows)}


# ─── Exleasingcar (Poland/Romania) ──────────────────────────────────────────

EXLEASINGCAR_PL = "https://exleasingcar.pl/"
EXLEASINGCAR_RO = "https://exleasingcar.ro/"


def harvest_exleasingcar(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    for base_url, country, currency in [(EXLEASINGCAR_PL, "pl", "PLN"), (EXLEASINGCAR_RO, "ro", "RON")]:
        try:
            markup = _get(session, base_url, timeout).text
        except requests.RequestException:
            continue
        links = re.findall(r'href="(/[^"]*car[^"]*|/[^"]*auto[^"]*|/[^"]*vehicle[^"]*)"', markup, re.I)
        unique_links = list(dict.fromkeys(links))
        for link in unique_links[:30]:
            detail_url = urljoin(base_url, link)
            try:
                detail = _get(session, detail_url, timeout).text
            except requests.RequestException:
                continue
            title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
            title = clean(title_match.group(1)) if title_match else ""
            if not title or not is_vehicle(title, clean(detail)):
                continue
            price_match = re.search(r"(?:PLN|RON|zł|price)[^0-9]*([0-9 .,]+)", detail, re.I)
            amount = parse_number(price_match.group(1)) if price_match else None
            lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
            rows.append(make_row(
                source="exleasingcar", listing_id=lot_id, country=country, url=detail_url,
                title=title, now=now, price_amount=amount, currency=currency,
                rate=rates.get(currency), price_kind="price" if amount else "unknown",
                description=clean(detail),
            ))
    return rows, {"status": "ok", "vehicle_rows": len(rows)}


# ─── Auctionmaster (Netherlands) ────────────────────────────────────────────

AUCTIONMASTER_BASE = "https://auctionmaster.com"
AUCTIONMASTER_LIST = "https://auctionmaster.com/en/c/cars-and-other-transport/"


def harvest_auctionmaster(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, AUCTIONMASTER_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/en/l/[^"]+)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:50]:
        detail_url = urljoin(AUCTIONMASTER_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:€|EUR|price)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = link.rstrip("/").rsplit("/", 1)[-1]
        rows.append(make_row(
            source="auctionmaster", listing_id=lot_id, country="nl", url=detail_url,
            title=title, now=now, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="current_bid" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:50]), "vehicle_rows": len(rows)}


# ─── 2trde (Germany) ────────────────────────────────────────────────────────

TWOTRDE_BASE = "https://www.2trde.com"
TWOTRDE_LIST = "https://www.2trde.com/de/"


def harvest_2trde(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, TWOTRDE_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="([^"]*fahrzeug[^"]*|[^"]*vehicle[^"]*|[^"]*auto[^"]*|/de/[a-z0-9-]+/[a-z0-9-]+)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:50]:
        if link.startswith("http") and "2trde.com" not in link:
            continue
        detail_url = urljoin(TWOTRDE_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:€|EUR|price|Startpreis)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="2trde", listing_id=lot_id, country="de", url=detail_url,
            title=title, now=now, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="starting_bid" if amount else "unknown", price_label=("starting price" if amount else ""),
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:50]), "vehicle_rows": len(rows)}


# ─── Northgate Trade (Spain) ────────────────────────────────────────────────

NORTHGATE_BASE = "https://vo.northgate.es"
NORTHGATE_LIST = "https://vo.northgate.es/"


def harvest_northgatetrade(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, NORTHGATE_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="([^"]*)"', markup, re.I)
    vehicle_links = [l for l in links if any(kw in l.lower() for kw in ["vehicle", "coche", "lot", "subasta", "auto"])]
    unique_links = list(dict.fromkeys(vehicle_links))
    if not unique_links:
        unique_links = list(dict.fromkeys(links))[:30]
    for link in unique_links[:40]:
        if link.startswith("http") and "northgate" not in link and "northgatetrade" not in link:
            continue
        detail_url = urljoin(NORTHGATE_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:€|EUR|price)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = hashlib.sha256(detail_url.encode()).hexdigest()[:16]
        rows.append(make_row(
            source="northgatetrade", listing_id=lot_id, country="es", url=detail_url,
            title=title, now=now, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="price" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:40]), "vehicle_rows": len(rows)}


# ─── Troostwijk (Netherlands) ───────────────────────────────────────────────

TROOSTWIJK_BASE = "https://www.troostwijkauctions.com"
TROOSTWIJK_LIST = "https://www.troostwijkauctions.com/en"


def harvest_troostwijk(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, TROOSTWIJK_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/en/[^"]+)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:50]:
        if "lot" not in link.lower() and "auction" not in link.lower() and "vehicle" not in link.lower():
            continue
        detail_url = urljoin(TROOSTWIJK_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:€|EUR|price|Startbod)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = link.rstrip("/").rsplit("/", 1)[-1]
        rows.append(make_row(
            source="troostwijk", listing_id=lot_id, country="nl", url=detail_url,
            title=title, now=now, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="current_bid" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:50]), "vehicle_rows": len(rows)}


# ─── CarCollect (Netherlands) ───────────────────────────────────────────────

CARCOLLECT_BASE = "https://www.carcollect.com"
CARCOLLECT_LIST = "https://www.carcollect.com/en"


def harvest_carcollect(session: requests.Session, now: dt.datetime, timeout: int, rates: dict[str, float]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    try:
        markup = _get(session, CARCOLLECT_LIST, timeout).text
    except requests.RequestException as exc:
        return rows, {"status": "error", "error": str(exc)[:200]}
    links = re.findall(r'href="(/en/[^"]+)"', markup, re.I)
    unique_links = list(dict.fromkeys(links))
    for link in unique_links[:50]:
        if "lot" not in link.lower() and "auction" not in link.lower() and "vehicle" not in link.lower():
            continue
        detail_url = urljoin(CARCOLLECT_BASE, link)
        try:
            detail = _get(session, detail_url, timeout).text
        except requests.RequestException:
            continue
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", detail, re.I | re.S)
        title = clean(title_match.group(1)) if title_match else ""
        if not title or not is_vehicle(title, clean(detail)):
            continue
        price_match = re.search(r"(?:€|EUR|price)[^0-9]*([0-9 .,]+)", detail, re.I)
        amount = parse_number(price_match.group(1)) if price_match else None
        lot_id = link.rstrip("/").rsplit("/", 1)[-1]
        rows.append(make_row(
            source="carcollect", listing_id=lot_id, country="nl", url=detail_url,
            title=title, now=now, price_amount=amount, currency="EUR",
            rate=rates.get("EUR"), price_kind="current_bid" if amount else "unknown",
            description=clean(detail),
        ))
    return rows, {"status": "ok", "detail_scanned": len(unique_links[:50]), "vehicle_rows": len(rows)}


# ─── Harvester Registry ─────────────────────────────────────────────────────

HARVESTERS: dict[str, Callable[..., tuple[list[dict], dict]]] = {
    "vebeg": harvest_vebeg,
    "portaldrazeb": harvest_portaldrazeb,
    "astegiudiziarie": harvest_astegiudiziarie,
    "campenauktioner": harvest_campenauktioner,
    "autoauctionsgr": harvest_autoauctionsgr,
    "restwertboerse": harvest_restwertboerse,
    "carauktion-ch": harvest_carauktion,
    "autoauction24-ch": harvest_autoauction24,
    "romu": harvest_romu,
    "weby": harvest_weby,
    "fina-ponip": harvest_fina_ponip,
    "nav-hu": harvest_nav_hu,
    "pkoleasing": harvest_pkoleasing,
    "exleasingcar": harvest_exleasingcar,
    "auctionmaster": harvest_auctionmaster,
    "2trde": harvest_2trde,
    "northgatetrade": harvest_northgatetrade,
    "troostwijk": harvest_troostwijk,
    "carcollect": harvest_carcollect,
}


def build_watch(*, timeout: int = 30, sources: list[str] | None = None,
                session: requests.Session | None = None, now: dt.datetime | None = None,
                rates: dict[str, float] | None = None) -> dict[str, Any]:
    session = session or requests.Session()
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    selected = sources or list(HARVESTERS)
    unknown = sorted(set(selected) - set(HARVESTERS))
    if unknown:
        raise ValueError("Unknown source(s): " + ", ".join(unknown))
    if rates is None:
        try:
            rates = fetch_rates(session, timeout)
        except Exception:
            rates = {"EUR": 1.0}
    rows: list[dict] = []
    reports: dict[str, Any] = {}
    for source in selected:
        try:
            source_rows, report = HARVESTERS[source](session, now, timeout, rates)
            rows.extend(source_rows)
            reports[source] = report
        except (requests.RequestException, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            reports[source] = {"status": "error", "row_count": 0, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
    unique: dict[str, dict] = {}
    seen_urls: set[str] = set()
    for row in rows:
        if row["id"] in unique or row["url"] in seen_urls:
            continue
        unique[row["id"]] = row
        seen_urls.add(row["url"])
    final = sorted(unique.values(), key=lambda r: (r.get("canonical_end_utc") or r.get("sale_event_utc") or "9999", r["source"], r["id"]))
    return {"schema_version": 1, "lane": "official_auction_watch", "generated_at_utc": now.isoformat(),
            "row_count": len(final), "rows": final, "source_reports": reports}


def apply_previous_snapshot_fallback(payload: dict, previous_path: Path, *, max_age: dt.timedelta = PREVIOUS_SNAPSHOT_MAX_AGE) -> dict:
    now = parse_utc(payload.get("generated_at_utc"))
    if now is None or not previous_path.is_file():
        return payload
    try:
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return payload
    if not isinstance(previous, dict) or previous.get("schema_version") != 1:
        return payload
    prior_rows = previous.get("rows", [])
    prior_reports = previous.get("source_reports", {})
    prior_generated = parse_utc(previous.get("generated_at_utc"))
    if prior_generated is None or not dt.timedelta(0) <= now - prior_generated <= max_age:
        return payload
    rows = payload.get("rows", [])
    reports = payload.get("source_reports", {})
    existing_ids = {r.get("id") for r in rows}
    existing_urls = {r.get("url") for r in rows}
    for source, report in list(reports.items()):
        if source not in HARVESTERS or not isinstance(report, dict) or report.get("status") != "error":
            continue
        retained = []
        for row in prior_rows:
            if not isinstance(row, dict) or (row.get("source_key") or row.get("source")) != source:
                continue
            rid = row.get("id")
            url = row.get("url")
            end = parse_utc(row.get("canonical_end_utc") or row.get("sale_end_utc"))
            if rid in existing_ids or url in existing_urls:
                continue
            if end is not None and end <= now:
                continue
            retained.append(row)
            existing_ids.add(rid)
            existing_urls.add(url)
        if retained:
            rows.extend(retained)
            reports[source] = {**report, "status": "partial", "vehicle_rows": len(retained)}
    rows.sort(key=lambda r: (r.get("canonical_end_utc") or r.get("sale_event_utc") or "9999", r.get("source", ""), r.get("id", "")))
    payload["row_count"] = len(rows)
    payload["rows"] = rows
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", action="append", choices=sorted(HARVESTERS))
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    payload = build_watch(timeout=args.timeout, sources=args.source)
    apply_previous_snapshot_fallback(payload, args.out)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.out)
    print(json.dumps({"result": "ADDITIONAL_BATCH_WATCH_PASS", "row_count": payload["row_count"],
                      "sources": {k: r.get("vehicle_rows", 0) for k, r in payload["source_reports"].items()}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
