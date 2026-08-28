#!/usr/bin/env python3
"""Collect every public current Klaravik auction lot in Sweden and Denmark.

Klaravik exposes a first-party JSON catalogue for each country.  Unlike the
older vehicle-only adapter, this connector enumerates every active lot and
keeps its source category.  Cars remain classed separately for the primary
vehicle view; motorcycles, jet skis, computers, gaming equipment, electronics,
boats, machinery, and other public lots are preserved for the wider auction
view.  Each country catalogue is read twice and must retain the same advertised
total and stable lot IDs before a snapshot is emitted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
PAGE_SIZE = 60
DEFAULT_TIMEOUT = 30
MAX_PAGES = 500
HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


class KlaravikWatchError(RuntimeError):
    """The public Klaravik catalogue could not be reconciled safely."""


@dataclass(frozen=True)
class SourceSpec:
    key: str
    name: str
    country: str
    domain: str
    currency: str

    @property
    def origin(self) -> str:
        return f"https://{self.domain}"


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec("klaravik-se", "Klaravik Sweden", "SE", "www.klaravik.se", "SEK"),
    SourceSpec("klaravik-dk", "Klaravik Denmark", "DK", "www.klaravik.dk", "DKK"),
)


@dataclass(frozen=True)
class Catalogue:
    source: SourceSpec
    declared_total: int
    pages: int
    ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    ended_excluded: int

    @property
    def fingerprint(self) -> tuple[int, tuple[str, ...]]:
        return self.declared_total, tuple(sorted(self.ids))


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def ascii_fold(value: Any) -> str:
    return " ".join(
        unicodedata.normalize("NFKD", clean(value))
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
        .split()
    )


def positive_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number) if number.is_integer() else number


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


def source_url(source: SourceSpec, value: Any) -> str:
    url = clean(value)
    if not url:
        raise KlaravikWatchError(f"{source.key} lot has no canonical public URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise KlaravikWatchError(f"{source.key} lot URL is not HTTPS")
    host = parsed.hostname.casefold().removeprefix("www.")
    expected = source.domain.casefold().removeprefix("www.")
    if host != expected:
        raise KlaravikWatchError(f"{source.key} lot URL escaped its public domain")
    return url


def category_context(item: dict[str, Any]) -> str:
    return ascii_fold(" ".join(
        clean(item.get(key))
        for key in (
            "categoryNameLevel1", "categoryNameLevel2", "categoryNameLevel3",
            "name", "make", "model",
        )
    ))


def category_for_item(item: dict[str, Any]) -> str:
    text = category_context(item)
    # A residential building advertised together with a plot is still a
    # property, whereas an empty plot is classified as land below.
    if re.search(r"\b(?:villa|bostad(?:er)?|fritidshus|sommarhus|summer house|lejlighed|lagenhet|hus(?!vagn)\b)\b", text):
        return "property"
    if re.search(r"\b(?:tomt(?:er|en)?|grund(?:e|en|er)?|mark(?:omrade|areal|parcel)?|jordbruksmark|skogsmark|building plot|land parcel|byggeklar)\b", text):
        return "land"
    if re.search(r"\b(?:fastighet(?:er|en)?|ejendom(?:me)?|bostad(?:er)?|villa|fritidshus|sommarhus|summer house|lejlighed|lagenhet|commercial property|erhvervsejendom|warehouse property|hus(?!vagn)\b)\b", text):
        return "property"
    if re.search(r"\b(?:vandscooter[a-z]*|jet[- ]?ski[a-z]*|sea[- ]?doo|waverunner|personal watercraft)\b", text):
        return "jetski"
    if re.search(r"\b(?:playstation|xbox|nintendo|gaming|spilkonsol|game console|videogame)\b", text):
        return "gaming"
    if re.search(r"\b(?:computer|laptop|notebook|stationaer pc|stationar pc|baerbar|baerbare|dator)\b", text):
        return "computer"
    if re.search(r"\b(?:tv|television|projektor|projector|kamera|camera|hi fi|hifi|audio|stereo|monitor)\b", text):
        return "electronics"
    if re.search(r"\b(?:motorcykel|motorcycle|moped|scooter|mc )\b", f" {text}"):
        return "motorcycle"
    if re.search(r"\b(?:bat|boat|jolle|kajak|canoe|segelbat|segelbad)\b", text):
        return "boat"
    if re.search(r"\b(?:cykel|bicycle|bike)\b", text):
        return "bicycle"
    if re.search(r"\b(?:reservdel|reservedel|bildel|car part|dack|tyre|tire|falgar|felg|wheel)\b", text):
        return "part"
    if re.search(r"\b(?:lastbil|truck|tunga fordon|tunge koretojer|kranbil|tow truck|bargningsbil)\b", text):
        return "truck"
    if re.search(r"\b(?:skabil|van|varebil|kassevogn|transporter|light commercial)\b", text):
        return "van"
    if re.search(r"\b(?:personbil|passagerbil|car|bil|automobile)\b", text):
        return "car"
    if re.search(r"\b(?:fordon|koretojer|vehicle)\b", text):
        return "vehicle"
    if re.search(r"\b(?:entreprenad|lantbruk|landbrug|skogsbruk|construction|tractor|traktor|gr[a-z]*vmaskin|excavator|lift)\b", text):
        return "equipment"
    return "other"


def property_type_for_item(item: dict[str, Any], category: str) -> str:
    if category == "land":
        return "land"
    if category != "property":
        return ""
    text = category_context(item)
    if re.search(r"\b(?:erhverv|commercial|warehouse|industri|office|kontor|butik)\b", text):
        return "commercial"
    if re.search(r"\b(?:villa|bostad|fritidshus|sommarhus|lejlighed|lagenhet|hus(?!vagn)\b)\b", text):
        return "residential"
    return "property"


def fuel_for_item(item: dict[str, Any]) -> str:
    text = category_context(item)
    if re.search(r"\b(?:hybrid|phev|hev|plug in)\b", text):
        return "hybrid"
    if re.search(r"\b(?:bensin|benzin|petrol|gasoline)\b", text):
        return "petrol"
    if re.search(r"\b(?:diesel)\b", text):
        return "diesel"
    if re.search(r"\b(?:electric|elbil|elektrisk)\b", text):
        return "electric"
    return "unknown"


YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
MILEAGE_RE = re.compile(r"\b([0-9][0-9 .,'\u00a0]*)\s*km\b", re.I)


def parsed_year(item: dict[str, Any], *, now: dt.datetime) -> int | None:
    match = YEAR_RE.search(clean(item.get("name")))
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1950 <= value <= now.year + 1 else None


def parsed_mileage(item: dict[str, Any]) -> int | None:
    match = MILEAGE_RE.search(clean(item.get("name")))
    if match is None:
        return None
    raw = re.sub(r"[^0-9]", "", match.group(1))
    if not raw:
        return None
    value = int(raw)
    return value if value >= 0 else None


def location_for_item(item: dict[str, Any]) -> str:
    return ", ".join(filter(None, (clean(item.get("municipalityName")), clean(item.get("countyName")))))


def normalize_item(item: dict[str, Any], source: SourceSpec, *, observed_at: str, now: dt.datetime) -> dict[str, Any]:
    item_id = str(item.get("id") or "").strip()
    if not item_id.isdigit():
        raise KlaravikWatchError(f"{source.key} lot has no stable numeric ID")
    title = clean(item.get("name"))
    if not title:
        raise KlaravikWatchError(f"{source.key} lot {item_id} has no title")
    end = normalize_end(item.get("endDate"))
    if end is None:
        raise KlaravikWatchError(f"{source.key} lot {item_id} has no valid auction end")
    category_raw = " / ".join(filter(None, (
        clean(item.get("categoryNameLevel1")),
        clean(item.get("categoryNameLevel2")),
        clean(item.get("categoryNameLevel3")),
    )))
    category = category_for_item(item)
    property_type = property_type_for_item(item, category)
    fuel = fuel_for_item(item)
    current_bid = positive_number(item.get("currentBid"))
    starting_bid = positive_number(item.get("startingPrice"))
    if current_bid is not None:
        price_amount, price_kind, price_label = current_bid, "current_bid", "public current bid"
    elif starting_bid is not None:
        price_amount, price_kind, price_label = starting_bid, "starting_bid", "public starting bid"
    else:
        price_amount, price_kind, price_label = None, "unknown", "price not shown in public catalogue"
    if category in {"car", "van", "truck", "vehicle"} and fuel in {"diesel", "electric"}:
        eligibility_status = "not_eligible"
        eligibility_reason = "Declared fuel is outside the petrol/hybrid car filter; the public auction remains visible for review."
    else:
        eligibility_status = "review_required"
        eligibility_reason = "Public Klaravik auction listing; confirm the item condition, fees, collection, documents, and buyer requirements before bidding."
    reserve_met = item.get("reservationPriceReached")
    return {
        "id": f"klaravik:{source.country.casefold()}:{item_id}",
        "source": source.key,
        "source_key": source.key,
        "source_name": source.name,
        "url": source_url(source, item.get("url")),
        "title": title,
        "model": clean(" ".join(filter(None, (clean(item.get("make")), clean(item.get("model"))))) or title),
        "country": source.country,
        "asset_country": source.country,
        "category": category,
        "category_raw": category_raw or "public Klaravik catalogue",
        "property_type": property_type,
        "year": parsed_year(item, now=now),
        "mileage_km": parsed_mileage(item),
        "fuel": fuel,
        "seller": "Klaravik public auction seller",
        "location": location_for_item(item),
        "price_amount": price_amount,
        "price_currency": source.currency,
        "price_eur": price_amount if source.currency == "EUR" else None,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public catalogue summary",
        "bid_count": int(item["amountOfBids"]) if isinstance(item.get("amountOfBids"), int) and item["amountOfBids"] >= 0 else None,
        "minimum_next_bid": positive_number(item.get("nextBidStep")),
        "reserve_met": reserve_met if isinstance(reserve_met, bool) else None,
        "no_reserve": None,
        "sale_terms": "",
        "auction_status": "active",
        "canonical_end_utc": end,
        "sale_end_utc": end,
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": eligibility_status,
        "eligibility_reason": eligibility_reason,
        "access_sale_note": "Klaravik collection and buyer terms are published with each lot; verify them before bidding.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{source.key}:public-json-catalogue:{item_id}",
        "evidence": "Public Klaravik JSON catalogue fields: category, public bid/starting price, and auction end.",
    }


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, status=3, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8))
    return session


def fetch_page(session: Any, source: SourceSpec, *, page: int, timeout: int) -> tuple[int, int, list[dict[str, Any]]]:
    response = session.get(
        f"{source.origin}/api/products/list/search",
        params={"page": page, "pageSize": PAGE_SIZE}, headers=HEADERS, timeout=timeout,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise KlaravikWatchError(f"{source.key} page {page} is not JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    pagination = data.get("pagination") if isinstance(data, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(pagination, dict) or not isinstance(items, list):
        raise KlaravikWatchError(f"{source.key} page {page} has no public catalogue payload")
    total = pagination.get("totalCount")
    pages = pagination.get("totalPages")
    if not isinstance(total, int) or total < 0 or not isinstance(pages, int) or pages < 1:
        raise KlaravikWatchError(f"{source.key} page {page} has invalid public pagination")
    if not all(isinstance(item, dict) for item in items):
        raise KlaravikWatchError(f"{source.key} page {page} contains an invalid item")
    return total, pages, items


def fetch_catalogue(session: Any, source: SourceSpec, *, observed_at: str, now: dt.datetime, timeout: int) -> Catalogue:
    total, pages, first_items = fetch_page(session, source, page=1, timeout=timeout)
    expected_pages = max(1, math.ceil(total / PAGE_SIZE))
    if pages != expected_pages or pages > MAX_PAGES:
        raise KlaravikWatchError(f"{source.key} public pagination disagrees with its declared total")
    all_items = list(first_items)
    if total == 0:
        if first_items:
            raise KlaravikWatchError(f"{source.key} empty catalogue returned items")
    elif len(first_items) != min(PAGE_SIZE, total):
        raise KlaravikWatchError(f"{source.key} first page cardinality is invalid")
    for page in range(2, pages + 1):
        page_total, page_count, items = fetch_page(session, source, page=page, timeout=timeout)
        if page_total != total or page_count != pages:
            raise KlaravikWatchError(f"{source.key} catalogue changed during pagination")
        expected_items = PAGE_SIZE if page < pages else total - PAGE_SIZE * (page - 1)
        if len(items) != expected_items:
            raise KlaravikWatchError(f"{source.key} page {page} cardinality is invalid")
        all_items.extend(items)
    ids = [str(item.get("id") or "").strip() for item in all_items]
    if len(all_items) != total or not all(ids) or len(ids) != len(set(ids)):
        raise KlaravikWatchError(f"{source.key} total/ID reconciliation failed")
    rows: list[dict[str, Any]] = []
    ended_excluded = 0
    for item in all_items:
        if item.get("ended") is True:
            ended_excluded += 1
            continue
        row = normalize_item(item, source, observed_at=observed_at, now=now)
        if dt.datetime.fromisoformat(row["canonical_end_utc"]) <= now:
            ended_excluded += 1
            continue
        rows.append(row)
    rows.sort(key=lambda row: (str(row.get("canonical_end_utc") or ""), str(row["id"])))
    return Catalogue(source, total, pages, tuple(ids), tuple(rows), ended_excluded)


def build_watch(
    *,
    session: Any | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    source_specs: Iterable[SourceSpec] = SOURCES,
) -> dict[str, Any]:
    if timeout < 5:
        raise ValueError("invalid Klaravik timeout")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    observed_at = now.isoformat()
    root_session = session or configured_session()
    first_pass = [
        fetch_catalogue(root_session, source, observed_at=observed_at, now=now, timeout=timeout)
        for source in source_specs
    ]
    second_pass = [
        fetch_catalogue(root_session, source, observed_at=observed_at, now=now, timeout=timeout)
        for source in source_specs
    ]
    if len(first_pass) != len(second_pass):
        raise KlaravikWatchError("Klaravik source scope changed during recheck")
    for first, second in zip(first_pass, second_pass):
        if first.source != second.source or first.fingerprint != second.fingerprint:
            raise KlaravikWatchError(f"{first.source.key} catalogue changed before final check")
    rows = [row for catalogue in first_pass for row in catalogue.rows]
    ids = [str(row["id"]) for row in rows]
    urls = [str(row["url"]) for row in rows]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise KlaravikWatchError("Klaravik produced duplicate public lot identities")
    reports: dict[str, dict[str, Any]] = {}
    for catalogue in first_pass:
        categories = Counter(str(row["category"]) for row in catalogue.rows)
        reports[catalogue.source.key] = {
            "status": "ok",
            "connector_status": "ok",
            "catalogue_scope": "every public current Klaravik auction lot",
            "declared": catalogue.declared_total,
            "publicly_listed": catalogue.declared_total,
            "normalized_active": len(catalogue.rows),
            "ended_excluded": catalogue.ended_excluded,
            "pages": catalogue.pages,
            "full_catalogue_rechecked": True,
            "stable_ids_unique": True,
            "category_counts": dict(sorted(categories.items())),
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
    parser = argparse.ArgumentParser(description="Fetch every public Klaravik Sweden and Denmark auction lot")
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
