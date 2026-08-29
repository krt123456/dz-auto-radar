#!/usr/bin/env python3
"""Collect only public Klaravik passenger-car auction cards.

Klaravik's dedicated ``Personbiler`` pages in Sweden and Denmark are normal
server-rendered public catalogues. This collector reads those pages directly,
instead of the site's broad all-lot JSON endpoint, so motorcycles, jet skis,
boats, property, land, equipment, computers, and other categories never enter
the watch input. Some cards in the dedicated category are nevertheless vans
or minibuses; their visible titles are excluded before publication.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
PAGE_SIZE = 60
DEFAULT_TIMEOUT = 35
MAX_PAGES = 100
SNAPSHOT_ATTEMPTS = 3
HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "sv-SE,sv;q=0.9,da-DK,da;q=0.8,en;q=0.7",
}
OBJECTS_IN_LIST_RE = re.compile(r"window\.objectsInList\s*=\s*([0-9]+)", re.I)
CARD_ID_RE = re.compile(r"^product_card--([0-9]+)$")
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
MILEAGE_RE = re.compile(r"\b([0-9][0-9\s,.\u00a0]*)\s*km\b", re.I)
NON_PASSENGER_TEXT_RE = re.compile(
    r"\b(?:motorcykel|motorcycle|moped|scooter|atv|utv|vandscooter|"
    r"jet[ -]?ski|sea[- ]?doo|waverunner|boat|b[aå]t|trailer|campingvogn|"
    r"autocamper|husbil|motorhome|tractor|traktor|gr[aä]vmaskin|excavator|"
    r"forklift|lastbil|truck|kranbil|varebil|varevogn|kassevogn|sk[aå]pbil|"
    r"budbil|minibus|bus|pickup|pick[- ]?up|transit|light truck|"
    r"computer|laptop|gaming|console|property|land)\b",
    re.I,
)
PASSENGER_CAR_MAKE_RE = re.compile(
    r"\b(?:mercedes(?:[- ]benz)?|land rover|range rover|alfa romeo|"
    r"volkswagen|renault|peugeot|citro[eë]n|opel|vauxhall|toyota|bmw|audi|"
    r"ford|nissan|hyundai|kia|honda|mazda|fiat|skoda|seat|volvo|mitsubishi|"
    r"suzuki|dacia|chevrolet|jeep|porsche|mini|lexus|subaru|jaguar|chrysler|"
    r"dodge|tesla|ssangyong|isuzu|daihatsu|infiniti|genesis|cupra|smart|"
    r"chery|geely|haval|byd|mg|ds|vw)\b",
    re.I,
)


class KlaravikWatchError(RuntimeError):
    """The public passenger-car catalogue could not be reconciled safely."""


class KlaravikSnapshotChanged(KlaravikWatchError):
    """The live Personbiler category changed while it was being read."""


@dataclass(frozen=True)
class SourceSpec:
    key: str
    name: str
    country: str
    domain: str
    currency: str
    category_path: str

    @property
    def origin(self) -> str:
        return f"https://{self.domain}"

    @property
    def category_url(self) -> str:
        return urljoin(self.origin, self.category_path)


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "klaravik-se", "Klaravik Sweden", "SE", "www.klaravik.se", "SEK",
        "/auktion/fordon/latta-fordon/personbilar/",
    ),
    SourceSpec(
        "klaravik-dk", "Klaravik Denmark", "DK", "www.klaravik.dk", "DKK",
        "/auction/koretojer/lette-koretojer/personbiler/",
    ),
)


@dataclass(frozen=True)
class ParsedPage:
    total: int
    page: int
    page_count: int
    listed_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    non_passenger_excluded: int
    ended_excluded: int


@dataclass(frozen=True)
class Catalogue:
    source: SourceSpec
    declared_total: int
    pages: int
    listed_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    non_passenger_excluded: int
    ended_excluded: int
    snapshot_attempts: int


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def ascii_fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", clean(value))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def positive_number(value: Any) -> int | float | None:
    compact = re.sub(r"[^0-9,.-]", "", clean(value))
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
    elif "." in compact and len(compact.rsplit(".", 1)[-1]) == 3:
        compact = compact.replace(".", "")
    try:
        parsed = float(compact)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def normalize_end(value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def page_url(source: SourceSpec, page: int) -> str:
    if page < 1 or page > MAX_PAGES:
        raise KlaravikWatchError("Klaravik page exceeds the safety limit")
    if page == 1:
        return source.category_url
    return f"{source.category_url}?{urlencode({'page': page})}"


def canonical_lot_url(source: SourceSpec, href: Any) -> str:
    parsed = urlsplit(urljoin(source.category_url, clean(href)))
    expected = source.domain.casefold().removeprefix("www.")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if (
        parsed.scheme != "https" or host != expected or parsed.username is not None
        or parsed.password is not None or not parsed.path
    ):
        raise KlaravikWatchError(f"{source.key} card leaves its official public domain")
    return f"https://{source.domain}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")


def is_passenger_car_title(title: str) -> bool:
    folded = ascii_fold(title)
    if NON_PASSENGER_TEXT_RE.search(folded):
        return False
    return "personbil" in folded or bool(PASSENGER_CAR_MAKE_RE.search(folded))


def normalize_fuel(value: Any) -> str:
    folded = ascii_fold(value)
    diesel = bool(re.search(
        r"\b(?:diesel|gazole|tdi|cdi|dci|hdi|crdi|tdci|tddi|bluehdi|d[2-5]|[0-9]{2,3}\s*d|[0-9]{2,3}d)\b",
        folded,
    ))
    petrol = bool(re.search(
        r"\b(?:bensin|benzin|petrol|gasoline|essence|tfsi|tsi|ecoboost|gdi|mpi|fsi)\b",
        folded,
    ))
    hybrid = bool(re.search(
        r"\b(?:hybrid|mildhybrid|phev|hev|plug[- ]?in|e[- ]?hybrid|tfsi e|tsi e)\b",
        folded,
    ))
    electric = bool(re.search(r"\b(?:electric|elektrisk|elbil)\b", folded))
    if diesel and hybrid:
        return "diesel/electric hybrid"
    if diesel:
        return "diesel"
    if petrol and hybrid:
        return "petrol/electric hybrid"
    if hybrid:
        return "hybrid"
    if electric:
        return "electric"
    if petrol:
        return "petrol"
    return "unknown"


def parsed_year(title: str, *, now: dt.datetime) -> int | None:
    match = YEAR_RE.search(title)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1950 <= value <= now.year + 1 else None


def parsed_mileage(title: str) -> int | None:
    match = MILEAGE_RE.search(title)
    if match is None:
        return None
    value = positive_number(match.group(1))
    return int(value) if isinstance(value, (int, float)) and value >= 0 else None


def parse_declared_total(markup: str) -> int:
    match = OBJECTS_IN_LIST_RE.search(markup)
    if match is None:
        raise KlaravikWatchError("Klaravik Personbiler category has no public item total")
    return int(match.group(1))


ECB_FX_URL_TEMPLATE = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.{currency}.EUR.SP00.A"
    "?format=csvdata&lastNObservations=1"
)


def fetch_ecb_units_per_eur(currency: str) -> tuple[float, str]:
    """Return (currency units per 1 EUR, observation date) from the ECB data API.

    The founder requires every public offer to be displayed in EUR; Klaravik
    Sweden publishes SEK bids and Klaravik Denmark publishes DKK bids, so each
    run converts at the ECB daily reference rate and records it in the source
    report for auditability.
    """
    url = ECB_FX_URL_TEMPLATE.format(currency=currency)
    try:
        response = urllib.request.urlopen(url, timeout=30)
        text = response.read().decode("utf-8", "replace")
    except Exception as error:
        raise KlaravikWatchError(f"ECB {currency}/EUR reference rate unavailable: {error}") from error
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise KlaravikWatchError(f"ECB {currency}/EUR reference rate response is empty")
    header = lines[0].split(",")
    try:
        value_index = header.index("OBS_VALUE") if "OBS_VALUE" in header else header.index("value")
        date_index = header.index("TIME_PERIOD")
    except ValueError as error:
        raise KlaravikWatchError(f"ECB {currency}/EUR reference rate CSV is malformed") from error
    fields = lines[1].split(",")
    try:
        rate = float(fields[value_index])
    except (ValueError, IndexError) as error:
        raise KlaravikWatchError(f"ECB {currency}/EUR reference rate value is invalid") from error
    if not math.isfinite(rate) or rate <= 0:
        raise KlaravikWatchError(f"ECB {currency}/EUR reference rate value is out of range")
    observation_date = fields[date_index] if date_index < len(fields) else ""
    return rate, observation_date


def parse_card(
    card: Tag,
    source: SourceSpec,
    *,
    observed_at: str,
    now: dt.datetime,
    fx_rate: float | None = None,
) -> tuple[str, dict[str, Any] | None]:
    id_match = CARD_ID_RE.fullmatch(clean(card.get("id")))
    if id_match is None:
        raise KlaravikWatchError(f"{source.key} category card has no stable numeric ID")
    item_id = id_match.group(1)
    link = card.select_one("a[href]")
    if link is None:
        raise KlaravikWatchError(f"{source.key} car {item_id} has no public card link")
    url = canonical_lot_url(source, link.get("href"))
    title_node = card.select_one(".product_card__title")
    title = clean(title_node.get_text(" ", strip=True) if title_node else link.get("title"))
    if not title:
        raise KlaravikWatchError(f"{source.key} car {item_id} has no title")
    close_node = card.select_one("[data-auction-close]")
    end = normalize_end(close_node.get("data-auction-close") if close_node else None)
    if end is None:
        raise KlaravikWatchError(f"{source.key} car {item_id} has no exact public auction end")
    if not is_passenger_car_title(title):
        return "not_passenger_car", None
    if dt.datetime.fromisoformat(end) <= now:
        return "ended", None
    price_node = card.select_one(".product_card__current-bid")
    price = positive_number(price_node.get_text(" ", strip=True) if price_node else "")
    bid_node = card.select_one("[id^='antbids_']")
    bid_value = positive_number(bid_node.get_text(" ", strip=True) if bid_node else "")
    location_node = card.select_one(".product_card__info-text")
    reserve_classes = {str(value) for value in (card.get("class") or [])}
    reserve_met = False if "product_card--reserve-not-reached" in reserve_classes else (
        True if card.select_one(".product_card__reserve-reached-tag") is not None else None
    )
    if price is not None and source.currency == "EUR":
        emitted_amount, emitted_currency, price_eur = price, "EUR", price
        price_label = f"public current bid {price} EUR"
    elif price is not None and fx_rate is not None and fx_rate > 0:
        emitted_amount = round(price / fx_rate, 2)
        if emitted_amount.is_integer():
            emitted_amount = int(emitted_amount)
        emitted_currency, price_eur = "EUR", emitted_amount
        price_label = f"public current bid {price} {source.currency} (≈ EUR {emitted_amount})"
    else:
        emitted_amount, emitted_currency, price_eur = price, source.currency, None
        price_label = "public current bid" if price is not None else "price not shown in public catalogue card"
    return "car", {
        "id": f"klaravik:{source.country.casefold()}:{item_id}",
        "source": source.key,
        "source_key": source.key,
        "source_name": source.name,
        "url": url,
        "title": title,
        "model": title,
        "country": source.country,
        "asset_country": source.country,
        "category": "car",
        "category_raw": "Klaravik Personbiler",
        "year": parsed_year(title, now=now),
        "mileage_km": parsed_mileage(title),
        "fuel": normalize_fuel(title),
        "seller": "Klaravik public auction seller",
        "location": clean(location_node.get_text(" ", strip=True) if location_node else ""),
        "price_amount": emitted_amount,
        "price_currency": emitted_currency,
        "price_eur": price_eur,
        "price_kind": "current_bid" if price is not None else "unknown",
        "price_label": price_label,
        "bid_visibility": "public passenger-car catalogue summary",
        "bid_count": int(bid_value) if isinstance(bid_value, int) else None,
        "reserve_met": reserve_met,
        "no_reserve": card.select_one(".product_card__no-reserve-tag") is not None,
        "auction_status": "active",
        "canonical_end_utc": end,
        "sale_end_utc": end,
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": "Public Klaravik passenger-car listing; confirm condition, fees, collection, documents, and buyer requirements before bidding.",
        "access_sale_note": "Klaravik collection and buyer terms are published with each lot; verify them before bidding.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{source.key}:public-personbiler:{item_id}",
        "evidence": "Public Klaravik Personbiler category card: title, price, bid count, and auction end.",
    }


def parse_page(
    markup: str,
    source: SourceSpec,
    *,
    page: int,
    observed_at: str,
    now: dt.datetime,
    fx_rate: float | None = None,
) -> ParsedPage:
    total = parse_declared_total(markup)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    if pages > MAX_PAGES:
        raise KlaravikWatchError(f"{source.key} Personbiler category exceeds the page safety limit")
    expected_cards = PAGE_SIZE if page < pages else total - PAGE_SIZE * (page - 1)
    cards = BeautifulSoup(markup, "html.parser").select("article.product_card[id^='product_card--']")
    if len(cards) != expected_cards:
        raise KlaravikSnapshotChanged(
            f"{source.key} Personbiler page {page} cardinality changed ({len(cards)} != {expected_cards})"
        )
    listed_ids: list[str] = []
    rows: list[dict[str, Any]] = []
    non_passenger_excluded = 0
    ended_excluded = 0
    for card in cards:
        id_match = CARD_ID_RE.fullmatch(clean(card.get("id")))
        if id_match is None:
            raise KlaravikWatchError(f"{source.key} category card has no stable numeric ID")
        listed_ids.append(id_match.group(1))
        result, row = parse_card(card, source, observed_at=observed_at, now=now, fx_rate=fx_rate)
        if result == "not_passenger_car":
            non_passenger_excluded += 1
        elif result == "ended":
            ended_excluded += 1
        elif row is not None:
            rows.append(row)
        else:
            raise KlaravikWatchError(f"{source.key} card {id_match.group(1)} has an invalid result")
    if len(listed_ids) != len(set(listed_ids)):
        raise KlaravikSnapshotChanged(f"{source.key} Personbiler page {page} has duplicate IDs")
    return ParsedPage(total, page, pages, tuple(listed_ids), tuple(rows), non_passenger_excluded, ended_excluded)


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, status=3, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))
    return session


def fetch_page(
    session: Any,
    source: SourceSpec,
    *,
    page: int,
    observed_at: str,
    now: dt.datetime,
    timeout: int,
    fx_rate: float | None = None,
) -> ParsedPage:
    response = session.get(page_url(source, page), headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return parse_page(response.text, source, page=page, observed_at=observed_at, now=now, fx_rate=fx_rate)


def _collect_coherent_snapshot(
    session: Any,
    source: SourceSpec,
    *,
    observed_at: str,
    now: dt.datetime,
    timeout: int,
    fx_rate: float | None = None,
) -> Catalogue:
    first = fetch_page(session, source, page=1, observed_at=observed_at, now=now, timeout=timeout, fx_rate=fx_rate)
    pages: dict[int, ParsedPage] = {1: first}
    for page in range(2, first.page_count + 1):
        parsed = fetch_page(session, source, page=page, observed_at=observed_at, now=now, timeout=timeout, fx_rate=fx_rate)
        if parsed.total != first.total or parsed.page_count != first.page_count:
            raise KlaravikSnapshotChanged(f"{source.key} Personbiler category changed during pagination")
        pages[page] = parsed
    listed_ids = tuple(listing_id for page in range(1, first.page_count + 1) for listing_id in pages[page].listed_ids)
    if len(listed_ids) != first.total or len(listed_ids) != len(set(listed_ids)):
        raise KlaravikSnapshotChanged(f"{source.key} Personbiler total/ID reconciliation failed")
    final = fetch_page(session, source, page=1, observed_at=observed_at, now=now, timeout=timeout)
    if final.total != first.total or final.page_count != first.page_count or final.listed_ids != first.listed_ids:
        raise KlaravikSnapshotChanged(f"{source.key} Personbiler category changed before final check")
    rows = tuple(row for page in range(1, first.page_count + 1) for row in pages[page].rows)
    row_ids = [str(row["id"]) for row in rows]
    row_urls = [str(row["url"]) for row in rows]
    if len(row_ids) != len(set(row_ids)) or len(row_urls) != len(set(row_urls)):
        raise KlaravikSnapshotChanged(f"{source.key} produced duplicate passenger-car identities")
    return Catalogue(
        source, first.total, first.page_count, listed_ids, rows,
        sum(p.non_passenger_excluded for p in pages.values()),
        sum(p.ended_excluded for p in pages.values()), 0,
    )


def build_watch(
    *,
    session: Any | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    source_specs: Iterable[SourceSpec] = SOURCES,
    fx_rates: dict[str, float] | None = None,
) -> dict[str, Any]:
    if timeout < 5:
        raise ValueError("invalid Klaravik timeout")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    observed_at = now.isoformat()
    root_session = session or configured_session()
    currencies = sorted({source.currency for source in source_specs if source.currency != "EUR"})
    resolved_rates: dict[str, tuple[float, str]] = {}
    for currency in currencies:
        explicit = (fx_rates or {}).get(currency)
        resolved_rates[currency] = (
            (float(explicit), "explicitly provided") if explicit else fetch_ecb_units_per_eur(currency)
        )
    catalogues: list[Catalogue] = []
    try:
        for source in source_specs:
            fx_rate, fx_date = resolved_rates.get(source.currency, (None, "n/a"))
            last_change: KlaravikSnapshotChanged | None = None
            for attempt in range(1, SNAPSHOT_ATTEMPTS + 1):
                try:
                    captured = _collect_coherent_snapshot(
                        root_session, source, observed_at=observed_at, now=now, timeout=timeout, fx_rate=fx_rate,
                    )
                except KlaravikSnapshotChanged as exc:
                    last_change = exc
                    continue
                catalogues.append(Catalogue(
                    captured.source, captured.declared_total, captured.pages, captured.listed_ids,
                    captured.rows, captured.non_passenger_excluded, captured.ended_excluded, attempt,
                ))
                break
            else:
                assert last_change is not None
                raise KlaravikWatchError(
                    f"{source.key} Personbiler category did not stabilize after {SNAPSHOT_ATTEMPTS} attempts"
                ) from last_change
    finally:
        if session is None:
            root_session.close()
    rows = [row for catalogue in catalogues for row in catalogue.rows]
    reports = {
        catalogue.source.key: {
            "status": "ok",
            "connector_status": "ok",
            "catalogue_scope": "every public official Klaravik Personbiler category card; non-passenger titles excluded",
            "declared": catalogue.declared_total,
            "publicly_listed": catalogue.declared_total,
            "normalized_active": len(catalogue.rows),
            "non_passenger_excluded": catalogue.non_passenger_excluded,
            "ended_excluded": catalogue.ended_excluded,
            "pages": catalogue.pages,
            "first_page_rechecked": True,
            "stable_ids_unique": True,
            "snapshot_attempts": catalogue.snapshot_attempts,
            "category_counts": {"car": len(catalogue.rows)},
            "fx": (
                {
                    "base_currency": resolved_rates[catalogue.source.currency][1] and catalogue.source.currency,
                    "display_currency": "EUR",
                    "units_per_eur": resolved_rates[catalogue.source.currency][0],
                    "observation_date": resolved_rates[catalogue.source.currency][1],
                    "source": "ECB daily reference rate",
                }
                if catalogue.source.currency != "EUR" and catalogue.source.currency in resolved_rates
                else {"base_currency": catalogue.source.currency, "display_currency": "EUR", "units_per_eur": 1.0}
            ),
            "publication_ready": False,
        }
        for catalogue in catalogues
    }
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": observed_at,
        "research_only": True,
        "publication_status": "review_required",
        "row_count": len(rows),
        "rows": rows,
        "source_reports": reports,
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
    parser = argparse.ArgumentParser(description="Fetch public Klaravik passenger-car category cards")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    report_counts = {key: value["normalized_active"] for key, value in payload["source_reports"].items()}
    print(json.dumps({"result": "KLARAVIK_WATCH_PASS", "row_count": payload["row_count"], "sources": report_counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
