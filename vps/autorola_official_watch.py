#!/usr/bin/env python3
"""Collect the complete public Autorola Europe vehicle-auction catalogue.

Autorola's dealer catalogue is server rendered for anonymous visitors.  The
auction index exposes every public sc?aid= route, and each route declares
its exact result count in a "Displays: x to y of z" marker.  This connector
walks every page for every public route, rechecks each route's first page, and
refuses to write a partial snapshot when a route changes while it is scanned.

Prices and bid amounts that are not unambiguously public are kept unknown.
The broad auction watch therefore retains the lot, its official scheduled end,
location, year, mileage and fuel, without inventing a bid or a fixed price.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
CATALOGUE_ZONE = ZoneInfo("Europe/Brussels")
SOURCE_KEY = "autorola-eu"
SOURCE_NAME = "Autorola Europe"
AUCTIONS_URL = "https://www.autorola.eu/dealer/auctions"
CATALOGUE_URL = "https://www.autorola.eu/dealer/sc"
DETAIL_BASE_URL = "https://www.autorola.eu/dealer/"
# The public endpoint accepts up to 1,000 cards per catalogue response.  A
# single authoritative page avoids a moving near-close auction shifting rows
# between page boundaries while it is reconciled.
PAGE_SIZE = 1_000
# This is an operational safety bound, not a publication cap.  It is large
# enough for every presently public Autorola route and can be raised from CLI.
DEFAULT_MAX_CATALOGUE_ROWS = 1_000_000
DEFAULT_TIMEOUT = 35
DEFAULT_WORKERS = 6
ROUTE_RECHECK_ATTEMPTS = 3

SCHENGEN_COUNTRIES = frozenset({
    "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IS", "IT", "LI", "LT", "LU", "LV",
    "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
})
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
SHOWING_RE = re.compile(
    r"Displays:\s*(?P<start>\d+)\s+to\s+(?P<end>\d+)\s+of\s+(?P<total>\d+)",
    re.IGNORECASE,
)
END_RE = re.compile(
    r"^End(?:\s+(?P<day>today|tomorrow|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday))?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?\s*(?:hs)?\+?\s*$",
    re.IGNORECASE,
)
DATED_END_RE = re.compile(
    r"^End\s+(?P<date_day>\d{1,2})[./-](?P<date_month>\d{1,2})"
    r"(?:[./-](?P<date_year>\d{4}))?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?\s*(?:hs)?\+?\s*$",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}


class AutorolaWatchError(RuntimeError):
    """The public Autorola catalogue could not be reconciled completely."""


@dataclass(frozen=True)
class AuctionSpec:
    aid: str
    name: str


@dataclass(frozen=True)
class ParsedPage:
    total: int
    start: int
    end: int
    card_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    rejected_counts: dict[str, int]


@dataclass(frozen=True)
class RouteSnapshot:
    spec: AuctionSpec
    total: int
    pages: int
    card_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    rejected_counts: dict[str, int]


@dataclass(frozen=True)
class Harvest:
    routes: tuple[RouteSnapshot, ...]
    catalogue_total: int
    pages: int
    card_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    rejected_counts: dict[str, int]


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def folded(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", clean(value))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def increment(counts: dict[str, int], reason: str, amount: int = 1) -> None:
    counts[reason] = counts.get(reason, 0) + amount


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.45,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def close_session(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        close()


def request_html(
    session: Any,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int,
) -> str:
    response = session.get(url, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    final_url = str(getattr(response, "url", url) or url)
    if urlparse(final_url).path.rstrip("/").endswith("/dealer/login"):
        raise AutorolaWatchError("Autorola public catalogue redirected to login")
    markup = str(getattr(response, "text", "") or "")
    if not markup:
        raise AutorolaWatchError("Autorola public catalogue returned an empty page")
    return markup


def route_key(specs: Iterable[AuctionSpec]) -> tuple[str, ...]:
    return tuple(sorted((spec.aid for spec in specs), key=int))


def parse_auction_index(markup: str) -> tuple[tuple[AuctionSpec, ...], tuple[str, ...]]:
    """Extract public card routes and separately count login-only eAuctions."""
    soup = BeautifulSoup(markup, "html.parser")
    public: dict[str, AuctionSpec] = {}
    restricted: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "")
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        aid = str((query.get("aid") or [""])[0]).strip()
        if not aid.isdigit():
            continue
        route = parsed.path.rstrip("/").rsplit("/", 1)[-1].casefold()
        if route == "joinauction":
            restricted.add(aid)
            continue
        if route != "sc":
            continue
        name = clean(anchor.get_text(" ", strip=True))
        # Image links deliberately repeat the same route without a name.
        if not name:
            continue
        previous = public.get(aid)
        if previous is not None and previous.name != name:
            raise AutorolaWatchError(f"Autorola auction {aid} has conflicting public names")
        public[aid] = AuctionSpec(aid=aid, name=name)
    if not public:
        raise AutorolaWatchError("Autorola auction index has no public catalogue routes")
    specs = tuple(sorted(public.values(), key=lambda spec: int(spec.aid)))
    return specs, tuple(sorted(restricted, key=int))


def parse_showing(soup: BeautifulSoup) -> tuple[int, int, int]:
    marker = soup.select_one(".showing")
    text = clean(marker.get_text(" ", strip=True) if marker else "")
    match = SHOWING_RE.search(text)
    if match is None:
        raise AutorolaWatchError("Autorola catalogue page has no exact display counter")
    start = int(match.group("start"))
    end = int(match.group("end"))
    total = int(match.group("total"))
    if total <= 0 or start <= 0 or end < start or end > total:
        raise AutorolaWatchError("Autorola catalogue page has invalid display counter")
    return start, end, total


def first_query_value(anchor: Tag, key: str) -> str:
    href = str(anchor.get("href") or "")
    value = (parse_qs(urlparse(href).query).get(key) or [""])[0]
    return str(value).strip()


def country_from_location(cell: Tag | None) -> str:
    if cell is None:
        return ""
    for image in cell.select("img[src]"):
        source = str(image.get("src") or "")
        match = re.search(r"/([A-Za-z]{2})\.gif(?:[?#]|$)", source)
        if match is not None:
            return match.group(1).upper()
    text = clean(cell.get_text(" ", strip=True))
    match = re.match(r"^([A-Z]{2})(?:\s*,|\s*$)", text)
    return match.group(1) if match is not None else ""


def parse_year(value: Any) -> int | None:
    match = re.search(r"\b((?:19|20)\d{2})\b", clean(value))
    return int(match.group(1)) if match else None


def parse_mileage(value: Any) -> int | None:
    text = folded(value)
    if "km" not in text:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    amount = int(digits)
    return amount if amount >= 0 else None


def normalize_fuel(value: Any) -> str:
    text = folded(value)
    diesel = bool(re.search(r"\b(?:diesel|gazole|gasoleo)\b", text))
    petrol = bool(re.search(r"\b(?:petrol|gasoline|essence|benzin|benzine)\b", text))
    electric = bool(re.search(r"\b(?:electric|electrique|electrico|bev|ev)\b", text))
    hybrid = "hybrid" in text or "hybride" in text
    if diesel and hybrid:
        return "diesel/electric hybrid"
    if petrol and hybrid:
        return "petrol/electric hybrid"
    if hybrid:
        return "hybrid"
    if electric:
        return "electric"
    if diesel:
        return "diesel"
    if petrol:
        return "petrol"
    if re.search(r"\b(?:lpg|gpl|cng|lng)\b", text):
        return "gas"
    return "unknown"


def classify_vehicle(value: Any) -> str:
    text = folded(value)
    if re.search(r"\b(?:motorcycle|motorbike|motorfiets|scooter|moto)\b", text):
        return "motorcycle"
    if re.search(r"\b(?:truck|lorry|tractor|trailer|semi[- ]?trailer)\b", text):
        return "truck"
    if re.search(
        r"\b(?:van|transit|crafter|sprinter|ducato|jumper|boxer|master|"
        r"commercial vehicle)\b", text
    ):
        return "van"
    return "car"


def parse_end(value: Any, *, now: dt.datetime) -> dt.datetime | None:
    """Convert Autorola's public English relative end marker to UTC."""
    text = clean(value)
    local_now = now.astimezone(CATALOGUE_ZONE)
    match = END_RE.match(text)
    if match is not None:
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        second = int(match.group("second") or 0)
        if hour > 23 or minute > 59 or second > 59:
            return None
        day = (match.group("day") or "").casefold()
        target_date = local_now.date()
        if day == "tomorrow":
            target_date += dt.timedelta(days=1)
        elif day in WEEKDAYS:
            days = (WEEKDAYS[day] - local_now.weekday()) % 7
            target_date += dt.timedelta(days=days)
        try:
            output = dt.datetime.combine(
                target_date, dt.time(hour, minute, second), tzinfo=CATALOGUE_ZONE
            )
        except ValueError:
            return None
        # "End Monday" when it is already Monday can mean next Monday after
        # today's listed time.  A bare/today marker cannot safely be moved.
        if day in WEEKDAYS and output <= local_now:
            output += dt.timedelta(days=7)
        if day in {"", "today"} and output <= local_now:
            return None
        return output.astimezone(UTC)

    match = DATED_END_RE.match(text)
    if match is None:
        return None
    year = int(match.group("date_year") or local_now.year)
    month = int(match.group("date_month"))
    day = int(match.group("date_day"))
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    second = int(match.group("second") or 0)
    try:
        output = dt.datetime(year, month, day, hour, minute, second, tzinfo=CATALOGUE_ZONE)
    except ValueError:
        return None
    if match.group("date_year") is None and output <= local_now:
        try:
            output = output.replace(year=output.year + 1)
        except ValueError:
            return None
    return output.astimezone(UTC)


def page_row(
    title_cell: Tag,
    *,
    spec: AuctionSpec,
    observed_at: str,
    now: dt.datetime,
) -> tuple[str, dict[str, Any] | None, str | None]:
    title_row = title_cell.parent
    if not isinstance(title_row, Tag) or title_row.name != "tr":
        raise AutorolaWatchError(f"Autorola auction {spec.aid} has a malformed title row")
    link: Tag | None = None
    eid = ""
    for candidate in title_cell.select("a[href]"):
        candidate_eid = first_query_value(candidate, "eid")
        if candidate_eid.isdigit() and clean(candidate.get_text(" ", strip=True)):
            link = candidate
            eid = candidate_eid
            break
    if link is None or not eid:
        raise AutorolaWatchError(f"Autorola auction {spec.aid} item has no public vehicle id")
    linked_aid = first_query_value(link, "aid")
    if linked_aid and linked_aid != spec.aid:
        raise AutorolaWatchError(f"Autorola item {eid} escaped auction {spec.aid}")
    title = clean(link.get_text(" ", strip=True))
    metadata = title_row.find_next_sibling("tr")
    if not isinstance(metadata, Tag):
        raise AutorolaWatchError(f"Autorola item {eid} has no metadata row")
    location_cell = metadata.select_one("td.location")
    end_cell = metadata.select_one("td.auctionEnd")
    if end_cell is None:
        raise AutorolaWatchError(f"Autorola item {eid} has no public auction end")
    country = country_from_location(location_cell)
    if not country:
        return eid, None, "country_unknown"
    if country not in SCHENGEN_COUNTRIES:
        return eid, None, "country_outside_schengen"
    end_text = clean(end_cell.get_text(" ", strip=True))
    end = parse_end(end_text, now=now)
    if end is None:
        return eid, None, "unparseable_or_elapsed_end"
    if end <= now:
        return eid, None, "already_ended"

    category = classify_vehicle(title)
    if category != "car":
        return eid, None, "not_passenger_car"

    price_cell = title_row.select_one("td.price")
    raw_price_label = clean(price_cell.get_text(" ", strip=True) if price_cell else "")
    if not raw_price_label:
        raw_price_label = "Price/bid not public on the anonymous catalogue"
    detail_url = urljoin(DETAIL_BASE_URL, str(link.get("href") or ""))
    reg_cell = title_row.select_one("td.regDate")
    mileage_cell = title_row.select_one("td.mileage")
    registration = clean(reg_cell.get_text(" ", strip=True) if reg_cell else "")
    mileage_text = clean(mileage_cell.get_text(" ", strip=True) if mileage_cell else "")
    location = clean(location_cell.get_text(" ", strip=True) if location_cell else "")
    return eid, {
        "id": f"{SOURCE_KEY}:{spec.aid}:{eid}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": detail_url,
        "title": title,
        "model": title,
        "country": country,
        "asset_country": country,
        "category": category,
        "year": parse_year(registration) or parse_year(title),
        "registration_date": registration,
        "mileage": parse_mileage(mileage_text),
        "fuel": normalize_fuel(title),
        "seller": spec.name,
        "location": location,
        "price_kind": "unknown",
        "price_currency": "EUR",
        "price_amount": None,
        "price_eur": None,
        "price_label": raw_price_label,
        "bid_visibility": "not_public",
        "sale_terms": f"Autorola catalogue auction: {spec.name}; {end_text}",
        "auction_status": "active",
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public Autorola catalogue item.  The anonymous catalogue does not "
            "publish a verified current bid or bidder/import eligibility."
        ),
        "access_sale_note": (
            "Verify the official listing, condition, access requirements and final "
            "price before bidding."
        ),
        "raw_evidence_ref": f"autorola-eu:auction:{spec.aid}:vehicle:{eid}",
        "adapter_authorized": True,
    }, None


def parse_catalogue_page(
    markup: str,
    *,
    spec: AuctionSpec,
    observed_at: str,
    now: dt.datetime,
) -> ParsedPage:
    soup = BeautifulSoup(markup, "html.parser")
    start, end, total = parse_showing(soup)
    expected = end - start + 1
    card_ids: list[str] = []
    rows: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    for title_cell in soup.select("td.title"):
        eid, row, reason = page_row(
            title_cell, spec=spec, observed_at=observed_at, now=now
        )
        if eid in card_ids:
            raise AutorolaWatchError(f"Autorola auction {spec.aid} repeats item {eid}")
        card_ids.append(eid)
        if row is not None:
            rows.append(row)
        elif reason:
            increment(rejected, reason)
    if len(card_ids) != expected:
        raise AutorolaWatchError(
            f"Autorola auction {spec.aid} page declares {expected} cards but "
            f"exposes {len(card_ids)}"
        )
    return ParsedPage(
        total=total,
        start=start,
        end=end,
        card_ids=tuple(card_ids),
        rows=tuple(rows),
        rejected_counts=dict(sorted(rejected.items())),
    )


def fetch_page(
    session: Any,
    *,
    spec: AuctionSpec,
    page: int,
    observed_at: str,
    now: dt.datetime,
    timeout: int,
) -> ParsedPage:
    markup = request_html(
        session,
        CATALOGUE_URL,
        params={
            "aid": spec.aid,
            "tnoipp": str(PAGE_SIZE),
            "tsri": "0",
            "tsd": "UP",
            "tcsp": str(page),
        },
        timeout=timeout,
    )
    return parse_catalogue_page(markup, spec=spec, observed_at=observed_at, now=now)


def combine_counts(pages: Iterable[ParsedPage]) -> dict[str, int]:
    output: dict[str, int] = {}
    for page in pages:
        for reason, count in page.rejected_counts.items():
            increment(output, reason, count)
    return dict(sorted(output.items()))


def scan_auction(
    spec: AuctionSpec,
    *,
    now: dt.datetime,
    observed_at: str,
    timeout: int,
    max_catalogue_rows: int,
    session_factory: Callable[[], Any],
) -> RouteSnapshot:
    last_error = ""
    for _attempt in range(ROUTE_RECHECK_ATTEMPTS):
        session = session_factory()
        try:
            first = fetch_page(
                session, spec=spec, page=0, observed_at=observed_at, now=now, timeout=timeout
            )
            if first.total > max_catalogue_rows:
                raise AutorolaWatchError(
                    f"Autorola auction {spec.aid} declares {first.total} cards, "
                    f"over configured maximum {max_catalogue_rows}"
                )
            pages_required = math.ceil(first.total / PAGE_SIZE)
            pages = [first]
            stable = first.start == 1 and first.end == min(PAGE_SIZE, first.total)
            for page in range(1, pages_required):
                parsed = fetch_page(
                    session, spec=spec, page=page, observed_at=observed_at, now=now,
                    timeout=timeout,
                )
                expected_start = page * PAGE_SIZE + 1
                expected_end = min((page + 1) * PAGE_SIZE, first.total)
                if (
                    parsed.total != first.total
                    or parsed.start != expected_start
                    or parsed.end != expected_end
                ):
                    stable = False
                    break
                pages.append(parsed)
            if stable:
                check = fetch_page(
                    session, spec=spec, page=0, observed_at=observed_at, now=now,
                    timeout=timeout,
                )
                if check.total != first.total or check.card_ids != first.card_ids:
                    stable = False
            card_ids = tuple(card_id for page in pages for card_id in page.card_ids)
            if stable and len(card_ids) == first.total and len(set(card_ids)) == first.total:
                rows = tuple(row for page in pages for row in page.rows)
                return RouteSnapshot(
                    spec=spec,
                    total=first.total,
                    pages=pages_required,
                    card_ids=card_ids,
                    rows=rows,
                    rejected_counts=combine_counts(pages),
                )
            last_error = "catalogue changed while pages were scanned"
        finally:
            close_session(session)
    raise AutorolaWatchError(f"Autorola auction {spec.aid} did not stabilize: {last_error}")


def harvest(
    specs: Iterable[AuctionSpec],
    *,
    now: dt.datetime,
    observed_at: str,
    timeout: int,
    max_workers: int,
    max_catalogue_rows: int,
    session_factory: Callable[[], Any],
) -> Harvest:
    ordered_specs = tuple(sorted(specs, key=lambda spec: int(spec.aid)))
    snapshots: list[RouteSnapshot] = []
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {
            executor.submit(
                scan_auction,
                spec,
                now=now,
                observed_at=observed_at,
                timeout=timeout,
                max_catalogue_rows=max_catalogue_rows,
                session_factory=session_factory,
            ): spec
            for spec in ordered_specs
        }
        for future in concurrent.futures.as_completed(futures):
            spec = futures[future]
            try:
                snapshots.append(future.result())
            except Exception as exc:
                failures.append(f"{spec.aid}: {type(exc).__name__}: {str(exc)[:220]}")
    if failures:
        raise AutorolaWatchError(
            "Autorola public catalogue has incomplete routes: " + "; ".join(sorted(failures))
        )
    snapshots.sort(key=lambda snapshot: int(snapshot.spec.aid))
    all_ids = tuple(card_id for snapshot in snapshots for card_id in snapshot.card_ids)
    if len(all_ids) != len(set(all_ids)):
        raise AutorolaWatchError("Autorola reuses a public vehicle id inside more than one route")
    rows = tuple(row for snapshot in snapshots for row in snapshot.rows)
    ids = [str(row["id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise AutorolaWatchError("Autorola normalized rows are not unique")
    counts: dict[str, int] = {}
    for snapshot in snapshots:
        for reason, count in snapshot.rejected_counts.items():
            increment(counts, reason, count)
    return Harvest(
        routes=tuple(snapshots),
        catalogue_total=len(all_ids),
        pages=sum(snapshot.pages for snapshot in snapshots),
        card_ids=all_ids,
        rows=rows,
        rejected_counts=dict(sorted(counts.items())),
    )


def fetch_index(
    *,
    timeout: int,
    session_factory: Callable[[], Any],
) -> tuple[tuple[AuctionSpec, ...], tuple[str, ...]]:
    session = session_factory()
    try:
        return parse_auction_index(request_html(session, AUCTIONS_URL, timeout=timeout))
    finally:
        close_session(session)


def build_watch(
    *,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int = DEFAULT_WORKERS,
    max_catalogue_rows: int = DEFAULT_MAX_CATALOGUE_ROWS,
    session_factory: Callable[[], Any] = configured_session,
) -> dict[str, Any]:
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    observed_at = now.isoformat()
    initial_specs, initial_restricted = fetch_index(
        timeout=timeout, session_factory=session_factory
    )
    captured = harvest(
        initial_specs,
        now=now,
        observed_at=observed_at,
        timeout=timeout,
        max_workers=max_workers,
        max_catalogue_rows=max_catalogue_rows,
        session_factory=session_factory,
    )
    final_specs, final_restricted = fetch_index(timeout=timeout, session_factory=session_factory)
    # If a new auction route appeared or expired during the scan, rebuild from
    # the final authoritative index once.  A second concurrent change is
    # fail-closed instead of publishing an incomplete catalogue.
    if route_key(initial_specs) != route_key(final_specs):
        captured = harvest(
            final_specs,
            now=now,
            observed_at=observed_at,
            timeout=timeout,
            max_workers=max_workers,
            max_catalogue_rows=max_catalogue_rows,
            session_factory=session_factory,
        )
        settled_specs, settled_restricted = fetch_index(
            timeout=timeout, session_factory=session_factory
        )
        if route_key(final_specs) != route_key(settled_specs):
            raise AutorolaWatchError("Autorola auction index changed during final reconciliation")
        final_specs, final_restricted = settled_specs, settled_restricted

    rows = sorted(
        captured.rows,
        key=lambda row: (str(row.get("canonical_end_utc") or ""), str(row["id"])),
    )
    capture_id = hashlib.sha256(
        json.dumps(
            {"routes": route_key(final_specs), "ids": captured.card_ids},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    report = {
        "status": "ok",
        "auction_routes": len(final_specs),
        "restricted_eauction_routes": len(set(initial_restricted) | set(final_restricted)),
        "catalogue_total": captured.catalogue_total,
        "discovered_unique": len(captured.card_ids),
        "pages": captured.pages,
        "current_or_future_vehicle_rows": len(rows),
        "rejected_counts": captured.rejected_counts,
        "raw_capture_id": capture_id,
    }
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "source": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "generated_at_utc": observed_at,
        "row_count": len(rows),
        "rows": rows,
        "source_reports": {SOURCE_KEY: report},
        "catalogue_total": captured.catalogue_total,
        "auction_routes": len(final_specs),
        "restricted_eauction_routes": report["restricted_eauction_routes"],
        "rejected_counts": captured.rejected_counts,
        "raw_capture_id": capture_id,
    }


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-catalogue-rows", type=int, default=DEFAULT_MAX_CATALOGUE_ROWS)
    args = parser.parse_args()
    payload = build_watch(
        timeout=max(3, args.timeout),
        max_workers=max(1, args.max_workers),
        max_catalogue_rows=max(1, args.max_catalogue_rows),
    )
    atomic_write(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "AUTOROLA_WATCH_PASS",
        "auction_routes": report["auction_routes"],
        "catalogue_total": report["catalogue_total"],
        "row_count": payload["row_count"],
        "pages": report["pages"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
