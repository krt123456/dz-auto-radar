#!/usr/bin/env python3
"""Broad-watch connector for Italy's official PVP vehicle catalogue.

PVP exposes a public search catalogue with a base auction amount and/or a
minimum offer.  It does *not* expose the live highest bid in that response.
This connector deliberately emits those amounts as ``minimum_offer`` or
``base_price`` and keeps the rows out of the strict eligible lane pending
per-lot sale-manager, identity, document, condition, and export checks.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


UTC = dt.timezone.utc


class _EuropeRomeFallback(dt.tzinfo):
    """EU Rome rules for minimal Windows Python installs without tzdata."""

    @staticmethod
    def _last_sunday(year: int, month: int) -> dt.datetime:
        if month == 12:
            next_month = dt.datetime(year + 1, 1, 1)
        else:
            next_month = dt.datetime(year, month + 1, 1)
        last_day = next_month - dt.timedelta(days=1)
        return last_day - dt.timedelta(days=(last_day.weekday() + 1) % 7)

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
    ROME: dt.tzinfo = ZoneInfo("Europe/Rome")
except ZoneInfoNotFoundError:
    ROME = _EuropeRomeFallback()
PVP_ORIGIN = "https://pvp.giustizia.it"
PVP_HOME_URL = f"{PVP_ORIGIN}/pvp/it/homepage.page"
PVP_DETAIL_URL = f"{PVP_ORIGIN}/pvp/it/detail_annuncio.page?idAnnuncio={{listing_id}}"
SOURCE_KEY = "pvp-giustizia"
SOURCE_NAME = "Portale delle Vendite Pubbliche (Ministero della Giustizia)"
HEADERS = {
    "User-Agent": "DZ-Auto-Radar/1.0 (+official public-auction monitor)",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.6",
    "Accept": "application/json, text/plain, */*",
}
SEARCH_BODY = {
    "tipoLotto": "MOBILI",
    "categoriaLotto": "AUTOVEICOLI_E_CICLI",
    "categoriaBene": ["AUTOVETTURE"],
    "flagRicerca": 0,
    "raggioIndirizzo": "25",
}

_BO_SERVICE_RE = re.compile(r"(?P<path>/bo-[A-Za-z0-9-]+/bo-ms)")
_REGISTRATION_DATE_PATTERNS = (
    re.compile(
        r"(?:data\s+(?:di\s+)?immatricolazione|prima\s+immatricolazione|"
        r"immatricolat[ao]\s+(?:in\s+data\s+)?|immatricolazione\s+(?:in\s+data\s+)?)"
        r"[^0-9]{0,24}(\d{1,2})[./-](\d{1,2})[./-](20\d{2})",
        re.I,
    ),
)
_REGISTRATION_YEAR_RE = re.compile(
    r"(?:anno\s+(?:di\s+)?immatricolazione|anno\s+imm\.?|"
    r"immatricolazione|anno)\D{0,18}(20\d{2})",
    re.I,
)
_MILEAGE_RE = re.compile(
    r"(?:chilometraggio|percorrenza|\bkm(?:\s+rilevati(?:\s+in\s+sede\s+di\s+perizia)?)?)"
    r"\s*(?:pari\s+a\s*)?[:=.-]?\s*(\d{1,3}(?:[.\s]\d{3})+|\d{2,7})\b",
    re.I,
)
_MODEL_RE = re.compile(
    r"\bmarca\s*[:=-]?\s*([A-Za-zÀ-ÿ0-9-]+(?:\s*-\s*[A-Za-zÀ-ÿ0-9-]+)?)"
    r"\s*,?\s*modello\s*[:=-]?\s*([^,;.\n]{1,60})",
    re.I,
)
_MAKE_RE = re.compile(
    r"\b(Abarth|Alfa\s+Romeo|Audi|BMW|Citro[eë]n|Cupra|Dacia|Fiat|Ford|Honda|"
    r"Hyundai|Iveco|Jaguar|Jeep|Kia|Land\s+Rover|Lancia|Lexus|Mazda|Mercedes(?:-Benz)?|"
    r"Mini|Mitsubishi|Nissan|Opel|Peugeot|Porsche|Renault|Seat|Skoda|Smart|Suzuki|"
    r"Tesla|Toyota|Volkswagen|Volvo|VW)\b",
    re.I,
)
_MAJOR_FAULT_RE = re.compile(
    r"\b(non\s+marciante|non\s+funzionante|motore\s+(?:non\s+)?funzionante|"
    r"motore\s+da\s+sostituire|incidentat[ao]|sinistrat[ao]|da\s+rottamare|"
    r"priva?\s+di\s+(?:carta\s+di\s+circolazione|documenti)|senza\s+documenti)\b",
    re.I,
)


def _clean(value: Any) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    text = text.replace("\ufffd", " ")
    return " ".join(text.split())


def _official_service_url(value: Any) -> str:
    """Resolve a dynamic PVP service path and reject off-domain URLs."""
    raw = _clean(value)
    if not raw:
        return ""
    resolved = urljoin(PVP_ORIGIN + "/", raw.lstrip("/"))
    parsed = urlparse(resolved)
    if parsed.scheme != "https" or parsed.hostname != "pvp.giustizia.it":
        raise ValueError("PVP service discovery returned a non-official host")
    return resolved


def discover_services(session: requests.Session, *, timeout: int = 30) -> dict[str, str]:
    """Discover the rotating PVP microservice paths from its official frontend."""
    home = session.get(PVP_HOME_URL, headers=HEADERS, timeout=timeout)
    home.raise_for_status()
    match = _BO_SERVICE_RE.search(html.unescape(home.text))
    if not match:
        raise ValueError("PVP backoffice discovery path is missing")
    config_url = urljoin(PVP_ORIGIN + "/", match.group("path").lstrip("/"))
    config_url = config_url.rstrip("/") + "/fe-config/area-annunci"
    config_response = session.get(config_url, headers=HEADERS, timeout=timeout)
    config_response.raise_for_status()
    config = config_response.json()
    service_map = config.get("msUrl") if isinstance(config, dict) else None
    if not isinstance(service_map, dict):
        raise ValueError("PVP frontend configuration has no msUrl map")
    search_base = _official_service_url(service_map.get("ricerca"))
    detail_base = _official_service_url(service_map.get("vendite"))
    if not search_base or not detail_base:
        raise ValueError("PVP search/detail service path is missing")
    return {
        "config_url": config_url,
        "search_url": search_base.rstrip("/") + "/ricerca/vendite",
        "detail_api_base": detail_base.rstrip("/") + "/vendite",
    }


def _response_page(response: requests.Response) -> dict[str, Any]:
    response.raise_for_status()
    envelope = response.json()
    payload = envelope.get("body") if isinstance(envelope, dict) else None
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        raise ValueError("PVP search response schema changed")
    return payload


def fetch_catalogue(
    session: requests.Session,
    search_url: str,
    *,
    timeout: int = 30,
    page_size: int = 1000,
    max_pages: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch every page, rather than assuming all future events fit page zero."""
    if page_size < 1 or page_size > 1000:
        raise ValueError("page_size must be between 1 and 1000")
    rows: list[dict[str, Any]] = []
    expected_total = 0
    total_pages = 1
    for page_number in range(max_pages):
        if page_number >= total_pages:
            break
        response = session.post(
            search_url,
            params={
                "page": page_number,
                "size": page_size,
                "sort": "dataOraVendita,desc",
                "language": "it",
            },
            json=SEARCH_BODY,
            headers=HEADERS,
            timeout=timeout,
        )
        payload = _response_page(response)
        if page_number == 0:
            try:
                total_pages = max(1, int(payload.get("totalPages") or 1))
                expected_total = max(0, int(payload.get("totalElements") or 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("PVP pagination metadata is invalid") from exc
            if total_pages > max_pages:
                raise ValueError(
                    f"PVP returned {total_pages} pages, above safety limit {max_pages}"
                )
        rows.extend(item for item in payload["content"] if isinstance(item, dict))
    if len(rows) < expected_total:
        raise ValueError(
            f"PVP catalogue is incomplete: fetched {len(rows)} of {expected_total} rows"
        )
    return rows, expected_total


def _sale_datetime_utc(item: dict[str, Any]) -> dt.datetime | None:
    raw = _clean(item.get("dataOraVendita"))
    if not raw:
        date_part = _clean(item.get("dataVendita"))
        time_part = _clean(item.get("orarioVendita")) or "23:59"
        raw = f"{date_part}T{time_part}" if date_part else ""
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ROME)
    return parsed.astimezone(UTC)


def _registration(text: str) -> tuple[dt.date | None, int | None]:
    for pattern in _REGISTRATION_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            day, month, year = (int(value) for value in match.groups())
            try:
                value = dt.date(year, month, day)
                return value, value.year
            except ValueError:
                pass
    match = _REGISTRATION_YEAR_RE.search(text)
    return (None, int(match.group(1))) if match else (None, None)


def _mileage(text: str) -> int | None:
    match = _MILEAGE_RE.search(text)
    if not match:
        return None
    try:
        value = int(re.sub(r"[.\s]", "", match.group(1)))
    except ValueError:
        return None
    return value if 0 < value < 2_000_000 else None


def _fuel(text: str) -> str:
    lower = text.casefold()
    diesel = bool(re.search(r"\b(?:diesel|gasolio|nafta)\b", lower))
    petrol = bool(re.search(r"\bbenzina\b", lower))
    electric = bool(re.search(r"\b(?:elettric[ao]|bev)\b", lower))
    hybrid = bool(re.search(r"\bibrid[ao]\b", lower))
    lpg = bool(re.search(r"\b(?:gpl|gas\s+di\s+petrolio\s+liquefatto)\b", lower))
    methane = bool(re.search(r"\bmetano\b", lower))
    if diesel and (electric or hybrid):
        return "diesel/electric hybrid"
    if diesel:
        return "diesel"
    if petrol and (electric or hybrid):
        return "petrol/electric hybrid"
    if petrol and lpg:
        return "petrol/lpg"
    if electric:
        return "electric"
    if petrol:
        return "petrol"
    if methane:
        return "methane"
    return "unknown"


def _model(text: str) -> str:
    match = _MODEL_RE.search(text)
    if match:
        make = _clean(match.group(1)).replace(" - ", "-")
        model = _clean(match.group(2))
        model = re.split(
            r"\b(?:targat[ao]|anno|alimentazione|cilindrata|colore|km)\b",
            model,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" ,-:")
        return f"{make} {model}".strip()
    make = _MAKE_RE.search(text)
    return _clean(make.group(1)) if make else ""


def _price(item: dict[str, Any]) -> tuple[float | None, str, str, float | None, float | None]:
    def positive(value: Any) -> float | None:
        try:
            parsed = float(value)
            return round(parsed, 2) if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    minimum = positive(item.get("offertaMinima"))
    base = positive(item.get("prezzoBaseAsta"))
    if minimum is not None:
        return minimum, "minimum_offer", "Offerta minima", base, minimum
    if base is not None:
        return base, "base_price", "Prezzo base d'asta", base, minimum
    # A missing amount must not be labelled as a base price: keep the lot and
    # make the absent public price explicit to the broad-watch consumer.
    return None, "unknown", "Prezzo non pubblicato", base, minimum


def _three_year_cutoff(today: dt.date) -> dt.date:
    try:
        return today.replace(year=today.year - 3)
    except ValueError:  # 29 February
        return today.replace(year=today.year - 3, day=28)


def _eligibility(
    *,
    text: str,
    registration_date: dt.date | None,
    year: int | None,
    fuel: str,
    now: dt.datetime,
) -> tuple[str, str]:
    """Pre-classify; PVP rows never become strict-eligible from summary data."""
    if fuel.startswith("diesel"):
        return "not_eligible", "Diesel is outside Algeria's used-vehicle import fuel gate."
    if fuel in {"methane"}:
        return "not_eligible", "The declared fuel is outside the accepted import fuel set."
    if _MAJOR_FAULT_RE.search(text):
        return "not_eligible", "The official description states a major fault or missing vehicle documents."
    cutoff = _three_year_cutoff(now.date())
    if registration_date and registration_date < cutoff:
        return "not_eligible", f"First registration {registration_date.isoformat()} is older than the rolling three-year cutoff."
    if registration_date is None and year is not None and year < cutoff.year:
        return "not_eligible", f"Registration year {year} is older than the rolling three-year window."
    missing: list[str] = []
    if registration_date is None:
        missing.append("exact first-registration date")
    if fuel == "unknown":
        missing.append("fuel")
    elif fuel == "petrol/lpg":
        missing.append("accepted-fuel confirmation")
    if missing:
        return (
            "unknown",
            "PVP summary needs " + ", ".join(missing)
            + "; it publishes only a minimum/base amount and per-manager participation still needs verification.",
        )
    return (
        "conditional",
        "Recent non-diesel candidate, but the amount is minimum/base—not a live bid; verify the sale manager, documents, condition, foreign participation, and export.",
    )


def item_to_row(item: dict[str, Any], *, now: dt.datetime) -> dict[str, Any] | None:
    listing_id = str(item.get("id") or "").strip()
    sale_at = _sale_datetime_utc(item)
    if not listing_id.isdigit() or sale_at is None or sale_at < now:
        return None
    description = _clean(item.get("descLotto"))
    registration_date, year = _registration(description)
    fuel = _fuel(description)
    model = _model(description)
    price, price_kind, price_label, base, minimum = _price(item)
    status, reason = _eligibility(
        text=description,
        registration_date=registration_date,
        year=year,
        fuel=fuel,
        now=now,
    )
    lot = _clean(item.get("numeroLotto"))
    fallback = description[:140].rstrip(" ,;:") or f"PVP vehicle announcement {listing_id}"
    title = f"{model} · Lotto {lot}" if model and lot else model or fallback
    bid_visibility = "base_or_minimum_only" if price is not None else "not_published"
    return {
        "id": f"{SOURCE_KEY}:{listing_id}",
        "source": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "source_key": SOURCE_KEY,
        "url": PVP_DETAIL_URL.format(listing_id=listing_id),
        "title": title,
        "model": model,
        "country": "IT",
        "year": year,
        "mileage_km": _mileage(description),
        "fuel": fuel,
        "price_amount": price,
        "price_currency": "EUR",
        "price_eur": price,
        "price_kind": price_kind,
        "sale_end_at": sale_at.isoformat(),
        "canonical_end_utc": sale_at.isoformat(),
        "last_seen_at": now.isoformat(),
        "eligibility_status": status,
        "eligibility_reason": reason,
        "bid_visibility": bid_visibility,
        "access_sale_note": (
            "Official PVP scheduled sale. The displayed amount is the minimum offer/base price, "
            "not the current bid; registration, deposit, bidding, and export depend on the per-lot sale manager."
        ),
        "evidence": (
            "Official PVP search API fields id, descLotto, prezzoBaseAsta/offertaMinima, "
            "and dataOraVendita; no live-current-bid value is used."
        ),
        # Extra display/audit fields retained alongside the common watch schema.
        "registration_date": registration_date.isoformat() if registration_date else None,
        "price_label": price_label,
        "base_price_eur": base,
        "minimum_offer_eur": minimum,
        "canonical_end_kind": "scheduled_sale_time",
        "lot_number": lot or None,
        "court": _clean(item.get("tribunale")) or None,
        "description": description,
    }


def build_watch(
    session: requests.Session | None = None,
    *,
    now: dt.datetime | None = None,
    timeout: int = 30,
    page_size: int = 1000,
    max_pages: int = 25,
) -> dict[str, Any]:
    session = session or requests.Session()
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    services = discover_services(session, timeout=timeout)
    catalogue, catalogue_total = fetch_catalogue(
        session,
        services["search_url"],
        timeout=timeout,
        page_size=page_size,
        max_pages=max_pages,
    )
    by_id: dict[str, dict[str, Any]] = {}
    for item in catalogue:
        row = item_to_row(item, now=now)
        if row:
            by_id[row["id"]] = row
    rows = sorted(by_id.values(), key=lambda row: (row["canonical_end_utc"], row["id"]))
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": now.isoformat(),
        "row_count": len(rows),
        "rows": rows,
        "source_reports": {
            SOURCE_KEY: {
                "catalogue_total": catalogue_total,
                "current_or_future": len(rows),
                "price_semantics": "minimum/base only; no live current bid is published by PVP",
                "search_api_url": services["search_url"],
            }
        },
        "catalogue_total": catalogue_total,
        "source_key": SOURCE_KEY,
        "source_url": PVP_HOME_URL,
        "search_api_url": services["search_url"],
        "price_semantics": "minimum/base only; never a live current bid",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch current/future official PVP vehicle announcements")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=25)
    args = parser.parse_args()
    payload = build_watch(timeout=args.timeout, page_size=args.page_size, max_pages=args.max_pages)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.out)
    print(json.dumps({
        "source": SOURCE_KEY,
        "catalogue_total": payload["catalogue_total"],
        "current_or_future": payload["row_count"],
        "output": str(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
