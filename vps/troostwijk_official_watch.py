#!/usr/bin/env python3
"""Reconcile every public current passenger-car lot at Troostwijk Auctions.

The Dutch marketplace publishes one Next.js server-rendered page per category
with a finite ``totalSize`` and a stable 48-lot page size.  Passenger cars
live in three explicit level-3 subcategories of the official ``Cars``
category (``Cars``, ``Oldtimers``, ``Classic cars >15``); vans, ambulances,
fire trucks, and other vehicles are separate subcategories and are therefore
excluded structurally at source.  Every lot page carries a serialized
``__NEXT_DATA__`` document whose ``lotsData`` lists each current lot with its
display ID, end time, public current bid, and asset location.

This connector walks the whole public passenger-car catalogue twice,
reconciles stable identities and facts, and only emits source-confirmed
passenger-car candidates.  Current-bid movement between the two reads is
deliberately ignored: it is live auction state, not catalogue identity.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import random
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
SOURCE_KEY = "troostwijk"
SOURCE_NAME = "Troostwijk Auctions"
SOURCE_BASE = "https://www.troostwijkauctions.com"
SOURCE_HOST = "www.troostwijkauctions.com"
CARS_ROOT_PATH = "/en/c/transport-logistics/cars/5196727d-c14f-48dc-a2f0-e75f50094a52"
DEFAULT_TIMEOUT = 40
DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DEFAULT_ATTEMPTS = 3
MAX_ATTEMPTS = 6
# Polite pacing between page requests per worker.  The marketplace rate-limits
# sustained high-bandwidth crawls (observed HTTP 403 mid-walk at ~4 req/s from
# a datacenter IP), so every page fetch sleeps a jittered delay first.
PAGE_DELAY_SECONDS = 1.0
MAX_PAGES_PER_SUBCATEGORY = 200
MAX_PAGE_LOTS = 100
MAX_TOTAL_LOTS = 20_000

# The level-3 subcategories of the official public Cars category.  The first
# tuple element must equal the source's own level-3 filter name (casefolded).
PASSENGER_SUBCATEGORIES: tuple[tuple[str, str], ...] = (
    ("cars", "/en/c/transport-logistics/cars/cars/f0091725-4cfa-411f-9162-22278111a313"),
    ("oldtimers", "/en/c/transport-logistics/cars/oldtimers/5ea75360-a3f6-4581-b275-077c75b904ee"),
    ("classic cars >15", "/en/c/transport-logistics/cars/classic-cars-%3E15/4308f27a-3c44-4bb0-8194-960b1bf19b51"),
)
KNOWN_LEVEL3_SUBCATEGORY_NAMES = frozenset({
    name for name, _ in PASSENGER_SUBCATEGORIES
} | {"vans", "other vehicles", "fire fighting trucks", "ambulances"})

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8",
}
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

# Defensive title gate.  The three walked subcategories are passenger-car
# catalogues by the source's own taxonomy, so anything matching these terms
# is an explicitly misfiled commercial vehicle, watercraft, or motorcycle.
NON_PASSENGER_TITLE_RE = re.compile(
    r"\b(?:"
    r"bestel(?:wagen|auto)|bedrijfswagen|vrachtwagen|truck|lorry|lkw|"
    r"kipper|kraanwagen|kraan|chassis\s*cab(?:ine)?|pick[- ]?up|"
    r"minibus|schoolbus|touringcar|brandweer(?:wagen)?|fire\s*truck|"
    r"ambulance|bus|"
    r"camper(?:wagen)?|motorhome|alco(?:ve|f)|bmobile|"
    r"aanhanger|oplegger|trailer|"
    r"motorfiets|motorcycle|motorbike|scooter|brommer|moped|quad|atv|utv|"
    r"jets?ki|sloep|speedboot|waterscooter|"
    r"\bboot\b|\bboat\b|zeil(?:jacht)?|\bjacht\b|\byacht\b"
    r")\b",
    re.I,
)


class TroostwijkWatchError(RuntimeError):
    """The public Troostwijk passenger-car catalogue could not be reconciled."""


@dataclass(frozen=True)
class Lot:
    lot_uuid: str
    display_id: str
    url_slug: str
    title: str
    end_utc: dt.datetime
    current_bid_eur: int | float | None
    currency: str
    country_code: str
    city: str
    image_url: str
    bidding_status: str

    @property
    def identity(self) -> str:
        return self.lot_uuid

    @property
    def fingerprint(self) -> tuple[str, str, str, str, str]:
        # Current bid is intentionally absent: live bids can change between
        # coherent enumeration passes without changing the public lot.
        return (
            self.lot_uuid,
            self.display_id,
            self.url_slug,
            self.title,
            self.end_utc.isoformat(),
        )


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def fold(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", clean(value)).casefold()
        if not unicodedata.combining(character)
    )


def parse_epoch_seconds(value: Any, *, error: str) -> dt.datetime:
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise TroostwijkWatchError(f"{error}: {value!r}") from exc
    # Public end targets must fall inside a sane window (2020..2100).
    if not 1_577_836_800 <= seconds <= 4_102_444_800:
        raise TroostwijkWatchError(f"{error} outside sane window: {seconds}")
    return dt.datetime.fromtimestamp(seconds, tz=UTC)


def parse_bid(raw: Any) -> int | float | None:
    """Convert the public current-bid cents into a positive EUR amount."""
    if not isinstance(raw, dict):
        return None
    cents = raw.get("cents")
    if not isinstance(cents, (int, float)) or isinstance(cents, bool):
        return None
    if not math.isfinite(float(cents)) or cents <= 0:
        return None
    euros = float(cents) / 100.0
    return int(euros) if euros.is_integer() else euros


def parse_page(markup: str, *, context: str) -> tuple[int, int, list[Lot]]:
    """Return (totalSize, pageSize, lots) from one server-rendered page."""
    match = NEXT_DATA_RE.search(markup)
    if match is None:
        raise TroostwijkWatchError(f"Troostwijk {context} page has no __NEXT_DATA__ document")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise TroostwijkWatchError(f"Troostwijk {context} page has invalid __NEXT_DATA__ JSON") from error
    try:
        props = data["props"]["pageProps"]
        lots_data = props["lotsData"]
        results = lots_data["results"]
        total_size = int(lots_data["totalSize"])
        page_size = int(props["pageSize"])
    except (KeyError, TypeError, ValueError) as error:
        raise TroostwijkWatchError(f"Troostwijk {context} page has invalid lots payload") from error
    if total_size < 0 or not 1 <= page_size <= MAX_PAGE_LOTS:
        raise TroostwijkWatchError(f"Troostwijk {context} page has invalid total/page size")
    if not isinstance(results, list) or len(results) > MAX_PAGE_LOTS:
        raise TroostwijkWatchError(f"Troostwijk {context} page has invalid result list")
    return total_size, page_size, [parse_lot(raw, context=context) for raw in results]


def parse_lot(raw: Any, *, context: str) -> Lot:
    if not isinstance(raw, dict):
        raise TroostwijkWatchError(f"Troostwijk {context} lot is not an object")
    lot_uuid = clean(raw.get("id"))
    display_id = clean(raw.get("displayId"))
    url_slug = clean(raw.get("urlSlug"))
    title = clean(raw.get("title"))
    if not lot_uuid or not display_id or not title:
        raise TroostwijkWatchError(f"Troostwijk {context} lot is missing identity fields")
    if not url_slug or " " in url_slug or display_id not in url_slug:
        raise TroostwijkWatchError(f"Troostwijk lot {display_id} has an invalid public slug")
    bid_raw = raw.get("currentBidAmount")
    currency = clean((bid_raw or {}).get("currency")) if isinstance(bid_raw, dict) else ""
    if bid_raw is not None and currency != "EUR":
        raise TroostwijkWatchError(f"Troostwijk lot {display_id} has a non-EUR public bid")
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    country_code = clean(location.get("countryCode")).upper()
    if country_code and not re.fullmatch(r"[A-Z]{2}", country_code):
        raise TroostwijkWatchError(f"Troostwijk lot {display_id} has an invalid asset country")
    image = raw.get("image") if isinstance(raw.get("image"), dict) else {}
    end_utc = parse_epoch_seconds(
        raw.get("endDate"), error=f"Troostwijk lot {display_id} has an invalid public end target"
    )
    return Lot(
        lot_uuid=lot_uuid,
        display_id=display_id,
        url_slug=url_slug,
        title=title,
        end_utc=end_utc,
        current_bid_eur=parse_bid(bid_raw),
        currency=currency,
        country_code=country_code or "NL",
        city=clean(location.get("city")),
        image_url=clean(image.get("url")) if isinstance(image, dict) else "",
        bidding_status=clean(raw.get("biddingStatus")),
    )


def lot_page_url(subcategory_path: str, page: int) -> str:
    if page == 1:
        return f"{SOURCE_BASE}{subcategory_path}"
    separator = "&" if "?" in subcategory_path else "?"
    return f"{SOURCE_BASE}{subcategory_path}{separator}page={page}"


def lot_detail_url(lot: Lot) -> str:
    return f"{SOURCE_BASE}/en/l/{quote(lot.url_slug, safe=':@$&()+,;=~*!')}"


def configured_session(*, workers: int = DEFAULT_WORKERS) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        status=4,
        backoff_factor=2.0,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=workers, pool_maxsize=workers))
    return session


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    if PAGE_DELAY_SECONDS > 0:
        time.sleep(PAGE_DELAY_SECONDS * (0.5 + random.random()))
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def verify_cars_taxonomy(session: requests.Session, *, timeout: int) -> None:
    """Fail closed when the source's own Cars subcategory list changes."""
    markup = fetch_markup(session, f"{SOURCE_BASE}{CARS_ROOT_PATH}", timeout=timeout)
    match = NEXT_DATA_RE.search(markup)
    if match is None:
        raise TroostwijkWatchError("Troostwijk Cars overview page has no __NEXT_DATA__ document")
    try:
        data = json.loads(match.group(1))
        filters = data["props"]["pageProps"]["initialFilters"]
    except json.JSONDecodeError as error:
        raise TroostwijkWatchError("Troostwijk Cars overview page has invalid __NEXT_DATA__ JSON") from error
    except (KeyError, TypeError) as error:
        raise TroostwijkWatchError("Troostwijk Cars overview page has invalid filters payload") from error
    names: set[str] = set()
    for entry in filters:
        if not isinstance(entry, dict) or entry.get("identifier") != "categoryLevel3":
            continue
        values = entry.get("filters")
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict) and item.get("name"):
                names.add(fold(item["name"]))
    if not names:
        raise TroostwijkWatchError("Troostwijk Cars overview exposes no level-3 subcategories")
    unexpected = sorted(names - KNOWN_LEVEL3_SUBCATEGORY_NAMES)
    missing = sorted(KNOWN_LEVEL3_SUBCATEGORY_NAMES - names)
    if unexpected or missing:
        raise TroostwijkWatchError(
            "Troostwijk Cars subcategory taxonomy changed: "
            f"unexpected={unexpected} missing={missing}; a human must classify any new subcategory"
        )


def walk_subcategory(
    session: requests.Session,
    subcategory_path: str,
    *,
    timeout: int,
    workers: int,
) -> tuple[int, list[Lot]]:
    """Read every page of one public subcategory; return (totalSize, lots)."""
    first_total, page_size, first_lots = parse_page(
        fetch_markup(session, lot_page_url(subcategory_path, 1), timeout=timeout),
        context=f"subcategory page 1 of {subcategory_path}",
    )
    if first_total > MAX_TOTAL_LOTS:
        raise TroostwijkWatchError("Troostwijk subcategory exceeds total lot safety limit")
    pages = math.ceil(first_total / page_size) if first_total else 0
    if pages > MAX_PAGES_PER_SUBCATEGORY:
        raise TroostwijkWatchError("Troostwijk subcategory exceeds page safety limit")
    lots: list[Lot] = list(first_lots)
    if pages > 1:

        def fetch_one(page: int) -> list[Lot]:
            _, _, page_lots = parse_page(
                fetch_markup(session, lot_page_url(subcategory_path, page), timeout=timeout),
                context=f"subcategory page {page} of {subcategory_path}",
            )
            return page_lots

        if workers == 1:
            for page in range(2, pages + 1):
                lots.extend(fetch_one(page))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                for page_lots in executor.map(fetch_one, range(2, pages + 1)):
                    lots.extend(page_lots)
    identities = [lot.identity for lot in lots]
    if len(identities) != len(set(identities)):
        raise TroostwijkWatchError(f"Troostwijk subcategory {subcategory_path} has duplicate lot IDs")
    if len(lots) > first_total:
        raise TroostwijkWatchError(
            f"Troostwijk subcategory {subcategory_path} visited more lots than its declared total"
        )
    return first_total, lots


def walk_catalogue(
    *,
    session: requests.Session,
    timeout: int,
    workers: int,
) -> dict[str, tuple[int, list[Lot]]]:
    """One complete pass over every passenger-car subcategory."""
    verify_cars_taxonomy(session, timeout=timeout)
    walked: dict[str, tuple[int, list[Lot]]] = {}
    for name, path in PASSENGER_SUBCATEGORIES:
        walked[name] = walk_subcategory(session, path, timeout=timeout, workers=workers)
    return walked


def assert_coherent(
    first: dict[str, tuple[int, list[Lot]]],
    second: dict[str, tuple[int, list[Lot]]],
) -> None:
    expected_names = {name for name, _ in PASSENGER_SUBCATEGORIES}
    if set(first) != expected_names or set(second) != expected_names:
        raise TroostwijkWatchError("Troostwijk subcategory membership changed between passes")
    for name, _ in PASSENGER_SUBCATEGORIES:
        first_total, first_lots = first[name]
        second_total, second_lots = second[name]
        if first_total != second_total:
            raise TroostwijkWatchError(
                f"Troostwijk subcategory {name} declared total changed between passes"
            )
        first_map = {lot.identity: lot for lot in first_lots}
        second_map = {lot.identity: lot for lot in second_lots}
        if first_map.keys() != second_map.keys():
            raise TroostwijkWatchError(f"Troostwijk subcategory {name} lot IDs changed between passes")
        if any(first_map[key].fingerprint != second_map[key].fingerprint for key in first_map):
            raise TroostwijkWatchError(f"Troostwijk subcategory {name} lot facts changed between passes")


def passenger_exclusion_reason(lot: Lot) -> str:
    if NON_PASSENGER_TITLE_RE.search(fold(lot.title)):
        return "commercial_or_non_passenger_title"
    return ""


def infer_fuel(title: str) -> str:
    value = fold(title)
    if re.search(r"\b(?:plug[ -]?in\s+hybrid|hybrid|hybride)\b", value):
        return "hybrid"
    if re.search(r"\b(?:electric|elektrisch|ev)\b", value):
        return "electric"
    if re.search(r"\b(?:benzine|petrol|gasoline|essence)\b", value):
        return "gasoline"
    if re.search(r"\b(?:diesel|tdi|hdi|cdi|dci)\b", value):
        return "diesel"
    return "unknown"


def normalize_lot(lot: Lot, *, subcategory: str, observed_at: str) -> dict[str, Any]:
    if lot.current_bid_eur is not None:
        price = lot.current_bid_eur
        price_kind = "current_bid"
    else:
        price = None
        price_kind = "unknown"
    year_match = YEAR_RE.search(lot.title)
    return {
        "id": f"{SOURCE_KEY}:{lot.lot_uuid}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": lot_detail_url(lot),
        "title": lot.title,
        "model": lot.title,
        "country": lot.country_code,
        "asset_country": lot.country_code,
        "category": "car",
        "category_raw": f"Troostwijk public Cars subcategory: {subcategory}",
        "year": int(year_match.group(1)) if year_match else None,
        "mileage": None,
        "mileage_km": None,
        "fuel": infer_fuel(lot.title),
        "seller": SOURCE_NAME,
        "location": lot.city,
        "image_url": lot.image_url,
        "price_amount": price,
        "price_currency": "EUR" if price is not None else "",
        "price_eur": price,
        "price_kind": price_kind,
        "price_label": (
            f"Huidig bod: EUR {price}" if price is not None else "No public current bid yet"
        ),
        "bid_visibility": "public Troostwijk auction lot card",
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": "Official Troostwijk current public auction lot",
        "auction_status": "active",
        "canonical_end_utc": lot.end_utc.isoformat(),
        "sale_end_utc": lot.end_utc.isoformat(),
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public Troostwijk current lot; confirm condition, fees, documents, collection, "
            "registration, and export requirements before bidding."
        ),
        "access_sale_note": "Auction participation and purchase may require a registered buyer account.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:lot:{lot.lot_uuid}:{lot.display_id}",
        "evidence": "Public Troostwijk Cars subcategory pages and serialized lot payloads.",
    }


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    snapshot_attempts: int = DEFAULT_ATTEMPTS,
) -> dict[str, Any]:
    if timeout < 5 or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("invalid Troostwijk timeout/workers")
    if not 1 <= snapshot_attempts <= MAX_ATTEMPTS:
        raise ValueError("invalid Troostwijk snapshot-attempts")
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session(workers=workers)
    try:
        first: dict[str, tuple[int, list[Lot]]] | None = None
        second: dict[str, tuple[int, list[Lot]]] | None = None
        attempts_used = 0
        for _ in range(snapshot_attempts):
            attempts_used += 1
            first = walk_catalogue(session=active_session, timeout=timeout, workers=workers)
            second = walk_catalogue(session=active_session, timeout=timeout, workers=workers)
            try:
                assert_coherent(first, second)
                break
            except TroostwijkWatchError:
                if attempts_used >= snapshot_attempts:
                    raise
        else:  # pragma: no cover - the raise above always fires first
            raise TroostwijkWatchError("Troostwijk catalogue never reconciled")
    finally:
        if supplied_session is None:
            active_session.close()

    assert first is not None and second is not None
    exclusions: Counter[str] = {}
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    current_lots = 0
    subcategory_stats: dict[str, dict[str, int]] = {}
    for name, _ in PASSENGER_SUBCATEGORIES:
        declared_total, lots = second[name]
        kept = 0
        for lot in lots:
            if lot.end_utc <= current:
                exclusions["ended_lot"] = exclusions.get("ended_lot", 0) + 1
                continue
            current_lots += 1
            if lot.identity in seen_ids:
                exclusions["cross_category_duplicate"] = exclusions.get("cross_category_duplicate", 0) + 1
                continue
            reason = passenger_exclusion_reason(lot)
            if reason:
                exclusions[reason] = exclusions.get(reason, 0) + 1
                continue
            seen_ids.add(lot.identity)
            rows.append(normalize_lot(lot, subcategory=name, observed_at=observed_at))
            kept += 1
        subcategory_stats[name] = {
            "declared_total": declared_total,
            "visited": len(lots),
            "current": kept,
        }

    declared_total = sum(stats["declared_total"] for stats in subcategory_stats.values())
    visited_total = sum(stats["visited"] for stats in subcategory_stats.values())
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": (
            "every current public lot in the source's own passenger-car Cars subcategories "
            "(Cars, Oldtimers, Classic cars >15); vans, ambulances, fire trucks, and other "
            "vehicles are separate subcategories and excluded structurally at source"
        ),
        "subcategories": subcategory_stats,
        "declared": declared_total,
        "visited": visited_total,
        "current_lots": current_lots,
        "passenger_cars": len(rows),
        "source_excluded": dict(sorted(exclusions.items())),
        "two_pass_verified": True,
        "stable_ids_unique": True,
        "snapshot_attempts_used": attempts_used,
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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch every current public Troostwijk passenger-car lot")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--snapshot-attempts", type=int, default=DEFAULT_ATTEMPTS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(
        timeout=args.timeout,
        workers=args.workers,
        snapshot_attempts=args.snapshot_attempts,
    )
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "TROOSTWIJK_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": report["declared"],
        "visited": report["visited"],
        "current_lots": report["current_lots"],
        "snapshot_attempts_used": report["snapshot_attempts_used"],
        "seconds": round(time.monotonic() - started, 1),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
