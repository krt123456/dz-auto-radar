#!/usr/bin/env python3
"""Broad, evidence-labelled watch feed for BOE and Kronofogden vehicles.

This feed is deliberately separate from the strict Algeria-eligible feed.  It
keeps every live vehicle lot/object visible while preserving the important
distinction between a public current bid and a base/starting price.  It also
publishes the participation restriction instead of silently treating a
reachable listing as a remotely eligible listing.
"""
from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag


UTC = dt.timezone.utc
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/135 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,sv;q=0.8,en;q=0.7",
}

WATCH_FIELDNAMES = [
    "id", "source", "source_key", "source_name", "country", "url", "title",
    "price_amount", "price_currency", "price_eur", "price_kind",
    "sale_end_at", "registration_date", "year", "fuel", "mileage_km",
    "eligibility_status", "eligibility_reason", "bid_visibility",
    "last_seen_at",
]
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

BOE_SEARCH_URL = "https://subastas.boe.es/subastas_ava.php"
BOE_DETAIL_URL = "https://subastas.boe.es/detalleSubasta.php"
BOE_SEARCH_DATA = (
    ("campo[2]", "SUBASTA.ESTADO.CODIGO"), ("dato[2]", "EJ"),
    ("campo[3]", "BIEN.TIPO"), ("dato[3]", "V"),
    ("campo[4]", "BIEN.SUBTIPO"), ("dato[4]", "9101"),
    ("page_hits", "500"), ("sort_field[0]", "SUBASTA.FECHA_FIN"),
    ("sort_order[0]", "asc"), ("accion", "Buscar"),
)
BOE_RESTRICTION = (
    "BOE bidding requires an authenticated auction account (Spanish electronic "
    "identity path) and the required AEAT-compatible deposit/payment account; "
    "remote foreign-bidder access must be verified before treating the lot as eligible."
)

KRONO_BASE_URL = "https://auktion.kronofogden.se/auk/"
KRONO_LIST_URL = (
    KRONO_BASE_URL + "w.ObjectList?inC=KFM&inA=WEB&inSiteLang=SWEDISH"
    "&inCategoryId={category_id}&inPageNo={page}"
)
KRONO_LIVE_JS_URL = (
    KRONO_BASE_URL + "w2.Live_js?inAuctionId=WEB&inCompanyId=KFM&inSiteLang=SWEDISH"
)
KRONO_CATEGORIES = ("1393955", "1481838")  # passenger cars, light vans
KRONO_RESTRICTION = (
    "Kronofogden requires a Swedish personal/coordination or organisation number "
    "to register; vehicle ownership transfer also requires a Swedish number and address."
)
SWEDISH_MONTHS = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4, "maj": 5,
    "juni": 6, "juli": 7, "augusti": 8, "september": 9,
    "oktober": 10, "november": 11, "december": 12,
}


def clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def key(value: Any) -> str:
    value = unicodedata.normalize("NFKD", clean(value)).lower()
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def get_html(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def post_html(
    session: requests.Session, url: str, *, data: Any, timeout: int
) -> str:
    response = session.post(url, data=data, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def iso_now() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ecb_rates(session: requests.Session, *, timeout: int) -> dict[str, float]:
    rates = {"EUR": 1.0}
    try:
        response = session.get(ECB_URL, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        for node in ET.fromstring(response.content).iter():
            currency, rate = node.attrib.get("currency"), node.attrib.get("rate")
            if currency and rate:
                rates[currency.upper()] = float(rate)
    except (requests.RequestException, ET.ParseError, ValueError):
        pass
    return rates


def eur_amount(native: str, currency: str, rates: dict[str, float] | None) -> str:
    if not native:
        return ""
    rate = (rates or {}).get(currency.upper())
    if not rate:
        return native if currency.upper() == "EUR" else ""
    try:
        value = float(native) / rate
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    return str(int(round(value)))


def amount_string(value: Any) -> str:
    """Return an unambiguous dot-decimal number, or an empty string."""
    raw = re.sub(r"[^\d,.-]", "", clean(value))
    if not raw or not re.search(r"\d", raw):
        return ""
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
        return ""
    if number < 0:
        return ""
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def fuel_from_text(value: str, *, language: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).lower()
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    if language == "sv":
        match = re.search(r"drivmedel\s+([^\n\r,;]+)", normalized)
    else:
        match = re.search(r"(?:combustible|carburante)\s*[:\-]?\s*([^\n\r,;]+)", normalized)
    evidence = match.group(1).strip() if match else normalized
    has_petrol = bool(re.search(r"\b(?:bensin|gasolina|petrol)\b", evidence))
    has_electric = bool(re.search(r"\b(?:el|electric|electrico|electrica)\b", evidence))
    if re.search(r"\b(?:hybrid|hibrido|hibrida)\b", evidence) or (has_petrol and has_electric):
        return "hybrid"
    if re.search(r"\b(?:diesel|gasoleo|cdi|tdi|dci|hdi|bluehdi|d-4d)\b", evidence):
        return "diesel"
    if has_petrol:
        return "petrol"
    if has_electric:
        return "electric"
    if re.search(r"\b(?:gas|lpg|glp|cng|gnc)\b", evidence):
        return "gas"
    return "unknown"


def table_pairs(soup: BeautifulSoup | Tag) -> dict[str, list[str]]:
    pairs: dict[str, list[str]] = {}
    for row in soup.select("tr"):
        label = row.find("th")
        value = row.find("td")
        if not label or not value:
            continue
        pairs.setdefault(key(label.get_text(" ", strip=True)), []).append(
            clean(value.get_text(" ", strip=True))
        )
    return pairs


def first_pair(pairs: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = pairs.get(key(name)) or []
        if values:
            return values[0]
    return ""


# ---------------------------------------------------------------------------
# BOE Spain


def parse_boe_search(markup: str) -> list[str]:
    ids: set[str] = set()
    soup = BeautifulSoup(markup, "html.parser")
    for anchor in soup.select("a[href*='detalleSubasta.php']"):
        query = parse_qs(urlparse(urljoin(BOE_SEARCH_URL, str(anchor.get("href") or ""))).query)
        value = clean((query.get("idSub") or [""])[0])
        if re.fullmatch(r"SUB-[A-Z]{2}-\d{4}-[A-Z0-9-]+", value, re.I):
            ids.add(value.upper())
    return sorted(ids)


def parse_boe_general(markup: str) -> dict[str, Any]:
    soup = BeautifulSoup(markup, "html.parser")
    pairs = table_pairs(soup)
    text = clean(soup.get_text(" ", strip=True))
    end_match = re.search(
        r"Fecha de conclusi[oó]n.*?ISO:\s*([^\s)]+)", text, re.I
    )
    lot_text = first_pair(pairs, "Lotes")
    lot_count_match = re.search(r"\d+", lot_text)
    lot_count = int(lot_count_match.group()) if lot_count_match else 1
    return {
        "end": end_match.group(1) if end_match else "",
        "lot_count": max(1, lot_count),
        "has_separate_lots": key(lot_text) != "sin lotes",
        "auction_value": first_pair(pairs, "Valor subasta"),
        "minimum_bid": first_pair(pairs, "Puja mínima", "Puja minima"),
    }


def parse_boe_lot_numbers(markup: str) -> list[int]:
    soup = BeautifulSoup(markup, "html.parser")
    numbers = {
        int(match.group(1))
        for anchor in soup.select("a[id^='idTabLote']")
        if (match := re.fullmatch(r"idTabLote(\d+)", str(anchor.get("id") or "")))
    }
    return sorted(numbers) or [0]


def parse_boe_bid_page(markup: str) -> tuple[dict[int, str], bool]:
    """Return public numeric current bids and whether a login-hidden bid exists."""
    soup = BeautifulSoup(markup, "html.parser")
    text = clean(soup.get_text(" ", strip=True))
    hidden = bool(re.search(
        r"(?:Con puja\s*\(\s*inicie sesi[oó]n|ha recibido alguna puja.*?acceder como usuario)",
        text, re.I,
    ))
    bids: dict[int, str] = {}
    for row in soup.select("tr"):
        cells = [clean(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
        if len(cells) < 2 or not re.fullmatch(r"\d+", cells[0]):
            continue
        if "€" in cells[1] or "EUR" in cells[1].upper():
            amount = amount_string(cells[1])
            if amount and float(amount) > 0:
                bids[int(cells[0])] = amount
    # A single-lot public numeric bid need not have a lot-number column.
    if not bids:
        match = re.search(
            r"Puja m[aá]xima actual(?: de la subasta)?\s+([\d .]+(?:,\d+)?)\s*(?:€|EUR)",
            text, re.I,
        )
        if match:
            amount = amount_string(match.group(1))
            if amount and float(amount) > 0:
                bids[0] = amount
    return bids, hidden


def parse_boe_vehicle_lot(
    markup: str, *, auction_id: str, lot_number: int,
    general: dict[str, Any], public_bids: dict[int, str], hidden_bid: bool,
    observed_at: str,
) -> dict[str, str] | None:
    soup = BeautifulSoup(markup, "html.parser")
    vehicle_headings = [
        heading for heading in soup.find_all(["h3", "h4"])
        if re.search(r"Veh[ií]culo\s*\(Turismos\)", clean(heading.get_text(" ", strip=True)), re.I)
    ]
    if not vehicle_headings:
        return None
    pairs = table_pairs(soup)
    descriptions = pairs.get("descripcion") or []
    makes = pairs.get("marca") or []
    models = pairs.get("modelo") or []
    title_parts: list[str] = []
    for index in range(max(len(makes), len(models))):
        part = clean(" ".join([
            makes[index] if index < len(makes) else "",
            models[index] if index < len(models) else "",
        ]))
        if part and part not in title_parts:
            title_parts.append(part)
    if not title_parts:
        title_parts = [value for value in descriptions if key(value) not in {"descrito", "no consta"}]
    title = " / ".join(title_parts) or f"BOE vehicle lot {auction_id}"
    description = " | ".join(dict.fromkeys(descriptions))[:1000]

    reg_dates = pairs.get("fecha de matriculacion") or []
    reg_date = ""
    year = ""
    for value in reg_dates:
        match = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})", value)
        if match:
            reg_date = f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
            year = match.group(3)
            break
    registrations = pairs.get("matricula") or []
    registration = " / ".join(dict.fromkeys(registrations))
    if title.startswith("BOE vehicle lot") and registration:
        title = f"Vehicle {registration}"
    mileage = ""
    mileage_text = " ".join(descriptions + (pairs.get("kilometraje") or []))
    mileage_match = re.search(r"([\d .]+)\s*(?:km|kms|kilometros)", key(mileage_text))
    if mileage_match:
        mileage = str(int(re.sub(r"\D", "", mileage_match.group(1)) or "0"))

    lot_pairs_value = first_pair(pairs, "Valor subasta")
    lot_minimum = first_pair(pairs, "Puja mínima", "Puja minima")
    bid_key = lot_number if lot_number in public_bids else 0
    current = public_bids.get(bid_key, "")
    base = amount_string(lot_pairs_value or general.get("auction_value", ""))
    minimum = amount_string(lot_minimum or general.get("minimum_bid", ""))
    if current:
        price, price_kind = current, "current_bid"
        bid_visibility = "public_numeric"
    elif base and float(base) > 0:
        price, price_kind = base, "base_price"
        bid_visibility = "login_required_current_bid" if hidden_bid else "no_public_current_bid"
    elif minimum and float(minimum) > 0:
        price, price_kind = minimum, "minimum_offer"
        bid_visibility = "login_required_current_bid" if hidden_bid else "no_public_current_bid"
    else:
        price, price_kind = None, "unknown"
        bid_visibility = "login_required_current_bid" if hidden_bid else "unavailable"

    params = {"idSub": auction_id, "ver": "3"}
    if lot_number:
        params["idLote"] = str(lot_number)
    query = "&".join(f"{name}={value}" for name, value in params.items())
    lot_id = str(lot_number or 1)
    return {
        "id": f"boe:{auction_id}:{lot_id}",
        "source": "boe-subastas", "source_key": "boe-subastas",
        "source_name": "BOE Subastas",
        "country": "ES", "title": title,
        "url": f"{BOE_DETAIL_URL}?{query}",
        "sale_end_at": str(general.get("end") or ""),
        "registration_date": reg_date, "year": year,
        "fuel": fuel_from_text(" ".join([title, description]), language="es"),
        "mileage_km": mileage,
        "price_amount": price, "price_currency": "EUR", "price_eur": price,
        "price_kind": price_kind,
        "bid_visibility": bid_visibility,
        "eligibility_status": "conditional",
        "eligibility_reason": BOE_RESTRICTION,
        "last_seen_at": observed_at,
    }


def harvest_boe(
    session: requests.Session, *, timeout: int = 25
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    observed_at = iso_now()
    errors: list[str] = []
    search = post_html(session, BOE_SEARCH_URL, data=BOE_SEARCH_DATA, timeout=timeout)
    auction_ids = parse_boe_search(search)
    rows: list[dict[str, str]] = []
    lots_seen = 0
    for auction_id in auction_ids:
        try:
            general_markup = get_html(
                session, f"{BOE_DETAIL_URL}?idSub={auction_id}&ver=1", timeout=timeout
            )
            general = parse_boe_general(general_markup)
            default_lot_markup = get_html(
                session, f"{BOE_DETAIL_URL}?idSub={auction_id}&ver=3", timeout=timeout
            )
            lot_numbers = parse_boe_lot_numbers(default_lot_markup)
            bids_markup = get_html(
                session, f"{BOE_DETAIL_URL}?idSub={auction_id}&ver=5", timeout=timeout
            )
            public_bids, hidden_bid = parse_boe_bid_page(bids_markup)
            for lot_number in lot_numbers:
                lots_seen += 1
                lot_markup = default_lot_markup
                if lot_number not in (0, 1):
                    lot_markup = get_html(
                        session,
                        f"{BOE_DETAIL_URL}?idSub={auction_id}&ver=3&idLote={lot_number}",
                        timeout=timeout,
                    )
                row = parse_boe_vehicle_lot(
                    lot_markup, auction_id=auction_id, lot_number=lot_number,
                    general=general, public_bids=public_bids, hidden_bid=hidden_bid,
                    observed_at=observed_at,
                )
                if row:
                    rows.append(row)
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(f"{auction_id}:{type(exc).__name__}:{str(exc)[:160]}")
    unique = {row["id"]: row for row in rows}
    return list(unique.values()), {
        "source": "boe-subastas", "auction_events": len(auction_ids),
        "lots_scanned": lots_seen, "vehicle_rows": len(unique),
        "current_bid_rows": sum(row["price_kind"] == "current_bid" for row in unique.values()),
        "base_or_minimum_rows": sum(row["price_kind"] in {"base_price", "minimum_offer"} for row in unique.values()),
        "errors": errors[:100], "observed_at": observed_at,
    }


# ---------------------------------------------------------------------------
# Kronofogden Sweden


def parse_krono_list(markup: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(markup, "html.parser")
    cards = soup.select(".obj_list_speed_container.grid .obj_thumbnail[id]")
    if not cards:
        cards = soup.select(".obj_thumbnail[id]")
    values: list[dict[str, str]] = []
    for card in cards:
        link = card.select_one("a.obj_link[href*='w.object']")
        if not link:
            continue
        url = urljoin(KRONO_BASE_URL, str(link.get("href") or ""))
        query = parse_qs(urlparse(url).query)
        auction_id = clean((query.get("inA") or [""])[0])
        object_id = clean((query.get("inO") or [""])[0])
        internal_id = clean(card.get("id"))
        if not auction_id or auction_id.upper() == "WEB" or not object_id or not internal_id:
            continue
        text_node = card.select_one(".obj_txt_inner")
        card_text = clean(text_node.get_text(" ", strip=True) if text_node else "")
        title = re.sub(r"^[A-ZÅÄÖ]\d+\.\s*", "", card_text, flags=re.I)
        title = re.split(r"\b(?:Utrop|SEK)\b", title, maxsplit=1, flags=re.I)[0].strip()
        values.append({
            "auction_id": auction_id, "object_id": object_id,
            "internal_id": internal_id, "source_url": url, "title": title,
        })
    unique = {(item["auction_id"], item["object_id"]): item for item in values}
    return list(unique.values())


def parse_krono_live_js(markup: str) -> dict[str, list[str]]:
    matrix: dict[str, list[str]] = {}
    for match in re.finditer(r"Matrix\[\d+\]\s*=\s*(\[[^;\r\n]+\])\s*;", markup):
        try:
            values = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError):
            continue
        if isinstance(values, list) and len(values) >= 9 and str(values[0]).isdigit():
            matrix[str(values[0])] = [str(value) for value in values]
    return matrix


def fetch_krono_live_matrix(
    session: requests.Session, *, timeout: int
) -> dict[str, list[str]]:
    javascript = get_html(session, KRONO_LIVE_JS_URL, timeout=timeout)
    match = re.search(r"locUrl\s*=\s*[\"']([^\"']+\.ajax)", javascript)
    if not match:
        raise ValueError("Kronofogden rotating live-price endpoint was not found")
    live_url = urljoin(KRONO_BASE_URL, match.group(1))
    live_markup = get_html(session, live_url, timeout=timeout)
    matrix = parse_krono_live_js(live_markup)
    if not matrix:
        raise ValueError("Kronofogden live-price matrix was empty or changed schema")
    return matrix


def parse_swedish_end(value: str) -> str:
    match = re.search(
        r"\b(\d{1,2})\s+(januari|februari|mars|april|maj|juni|juli|augusti|"
        r"september|oktober|november|december)\s+(20\d{2})\s+(\d{1,2}):(\d{2})\b",
        key(value), re.I,
    )
    if not match:
        return ""
    local = dt.datetime(
        int(match.group(3)), SWEDISH_MONTHS[match.group(2).lower()],
        int(match.group(1)), int(match.group(4)), int(match.group(5)),
        tzinfo=ZoneInfo("Europe/Stockholm"),
    )
    return local.isoformat()


def parse_krono_detail(
    markup: str, *, item: dict[str, str], live: list[str] | None,
    observed_at: str, rates: dict[str, float] | None = None,
) -> dict[str, str]:
    soup = BeautifulSoup(markup, "html.parser")
    heading = soup.find("h1")
    title = clean(heading.get_text(" ", strip=True) if heading else item.get("title"))
    title = re.sub(r"^[A-ZÅÄÖ]\d+\.\s*", "", title, flags=re.I) or item.get("title") or "Vehicle"
    text_lines = soup.get_text("\n", strip=True)
    normalized_lines = clean(text_lines)

    first_registration = ""
    match = re.search(r"F[oö]rsta g[aå]ngen i trafik\s+(20\d{2}-\d{2}-\d{2})", text_lines, re.I)
    if match:
        first_registration = match.group(1)
    if not first_registration:
        match = re.search(r"(?:[Aa]rsmodell|Tillverknings[aå]r)\s+(20\d{2})", key(text_lines), re.I)
        year = match.group(1) if match else ""
    else:
        year = first_registration[:4]
    registration_match = re.search(r"Registreringsnummer\s+([A-Z0-9-]{3,12})", text_lines, re.I)
    registration = registration_match.group(1).upper() if registration_match else ""
    mileage = ""
    mileage_match = re.search(
        r"Avl[aä]st m[aä]tarst[aä]llning\s+([\d ]+)\s*(mil|km)", text_lines, re.I
    )
    if mileage_match:
        distance = int(re.sub(r"\D", "", mileage_match.group(1)) or "0")
        mileage = str(distance * 10 if key(mileage_match.group(2)) == "mil" else distance)

    end_text = ""
    panel = soup.select_one(f"#bid_list_container_{item['internal_id']}")
    if panel:
        end_text = panel.get_text(" ", strip=True)
    auction_end = parse_swedish_end(end_text or normalized_lines)

    current = amount_string(live[2]) if live and len(live) > 7 else ""
    starting = amount_string(live[7]) if live and len(live) > 7 else ""
    if current and float(current) > 0:
        price, price_kind = current, "current_bid"
    elif starting and float(starting) > 0:
        price, price_kind = starting, "base_price"
    else:
        # Static fallback preserves the label if the rotating endpoint has a gap.
        static_current = re.search(r"Budgivning\s+([\d ]+)\s+SEK", end_text, re.I)
        static_start = re.search(r"Startpris\s+([\d ]+)\s+SEK", end_text, re.I)
        if static_current:
            price, price_kind = amount_string(static_current.group(1)), "current_bid"
        elif static_start:
            price, price_kind = amount_string(static_start.group(1)), "base_price"
        else:
            price, price_kind = None, "unknown"

    fuel = fuel_from_text(text_lines, language="sv")
    description_match = re.search(
        r"Registreringsnummer\s+.+?(?=Enligt lag ska ett fordon|Vi säljer all egendom|Varunr)",
        normalized_lines, re.I,
    )
    description = clean(description_match.group(0))[:1000] if description_match else ""
    return {
        "id": f"kronofogden:{item['auction_id']}:{item['object_id']}",
        "source": "kronofogden", "source_key": "kronofogden",
        "source_name": "Kronofogden Auktionstorget",
        "country": "SE", "title": title, "url": item["source_url"],
        "sale_end_at": auction_end,
        "registration_date": first_registration, "year": year,
        "fuel": fuel, "mileage_km": mileage,
        "price_amount": price, "price_currency": "SEK",
        "price_eur": eur_amount(price, "SEK", rates) if price else None,
        "price_kind": price_kind,
        "bid_visibility": "public_live_matrix" if live else "static_fallback",
        "eligibility_status": "not_eligible",
        "eligibility_reason": KRONO_RESTRICTION,
        "last_seen_at": observed_at,
    }


def harvest_kronofogden(
    session: requests.Session, *, timeout: int = 25,
    rates: dict[str, float] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    observed_at = iso_now()
    errors: list[str] = []
    objects: dict[tuple[str, str], dict[str, str]] = {}
    category_counts: dict[str, int] = {}
    for category_id in KRONO_CATEGORIES:
        category_seen: set[tuple[str, str]] = set()
        for page in range(1, 11):
            try:
                markup = get_html(
                    session, KRONO_LIST_URL.format(category_id=category_id, page=page),
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                errors.append(f"category={category_id},page={page}:{type(exc).__name__}:{str(exc)[:140]}")
                break
            parsed = parse_krono_list(markup)
            new_count = 0
            for item in parsed:
                item_key = (item["auction_id"], item["object_id"])
                if item_key not in category_seen:
                    new_count += 1
                    category_seen.add(item_key)
                    objects[item_key] = item
            if not new_count:
                break
        category_counts[category_id] = len(category_seen)

    live_matrix: dict[str, list[str]] = {}
    try:
        live_matrix = fetch_krono_live_matrix(session, timeout=timeout)
    except (requests.RequestException, ValueError) as exc:
        errors.append(f"live-matrix:{type(exc).__name__}:{str(exc)[:160]}")

    rows: list[dict[str, str]] = []
    for item in objects.values():
        try:
            markup = get_html(session, item["source_url"], timeout=timeout)
            rows.append(parse_krono_detail(
                markup, item=item, live=live_matrix.get(item["internal_id"]),
                observed_at=observed_at, rates=rates,
            ))
        except requests.RequestException as exc:
            errors.append(
                f"{item['auction_id']}:{item['object_id']}:{type(exc).__name__}:{str(exc)[:140]}"
            )
    unique = {row["id"]: row for row in rows}
    return list(unique.values()), {
        "source": "kronofogden", "category_counts": category_counts,
        "listed_objects": len(objects), "vehicle_rows": len(unique),
        "live_matrix_objects_all_categories": len(live_matrix),
        "current_bid_rows": sum(row["price_kind"] == "current_bid" for row in unique.values()),
        "starting_price_rows": sum(row["price_kind"] == "base_price" for row in unique.values()),
        "errors": errors[:100], "observed_at": observed_at,
    }


def harvest(
    session: requests.Session, *, sources: Iterable[str], timeout: int
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    rows: list[dict[str, str]] = []
    report: dict[str, Any] = {"generated_at": iso_now(), "sources": {}}
    requested = set(sources)
    rates = ecb_rates(session, timeout=timeout) if "kronofogden" in requested else {"EUR": 1.0}
    if "boe-subastas" in requested:
        source_rows, source_report = harvest_boe(session, timeout=timeout)
        rows.extend(source_rows); report["sources"]["boe-subastas"] = source_report
    if "kronofogden" in requested:
        source_rows, source_report = harvest_kronofogden(session, timeout=timeout, rates=rates)
        rows.extend(source_rows); report["sources"]["kronofogden"] = source_report
    rows.sort(key=lambda row: (row.get("sale_end_at") or "9999", row["id"]))
    report["rows"] = len(rows)
    report["price_kinds"] = {
        kind_name: sum(row["price_kind"] == kind_name for row in rows)
        for kind_name in sorted({row["price_kind"] for row in rows})
    }
    return rows, report


def build_payload(rows: list[dict[str, str]], report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": report.get("generated_at") or iso_now(),
        "row_count": len(rows),
        "rows": rows,
        "source_reports": report.get("sources") or {},
    }


def write_rows(
    path: Path, rows: list[dict[str, str]], report: dict[str, Any] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=WATCH_FIELDNAMES, extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)
        temporary.replace(path)
    else:
        payload = build_payload(rows, report or {"generated_at": iso_now(), "sources": {}})
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", choices=("boe-subastas", "kronofogden"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()
    session = requests.Session()
    rows, report = harvest(
        session, sources=args.source or ("boe-subastas", "kronofogden"),
        timeout=max(5, args.timeout),
    )
    write_rows(args.out, rows, report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_suffix(args.report.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(args.report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
