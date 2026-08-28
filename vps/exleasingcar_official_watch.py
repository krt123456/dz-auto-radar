#!/usr/bin/env python3
"""Enumerate the public Exleasingcar vehicle-auction catalogue.

The public all-auctions page exposes a finite ``Filtr (N)`` total, twenty
vehicle cards per page, and a terminal page number.  This connector walks that
entire public catalogue, reconciles every card against the advertised total,
and re-reads page one before publishing the snapshot.  It deliberately uses
only summary fields visible in the catalogue cards; no login, bid, VIN, image,
or detail-only fields are requested.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from lxml import etree, html as lxml_html
from lxml.html import HtmlElement
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "exleasingcar"
SOURCE_NAME = "Exleasingcar"
# The public catalogue explicitly offers ``show-20``, ``show-40`` and
# ``show-60`` routes.  Start from the largest visible page size so a complete
# reconciled snapshot does not spend most of the service window walking the
# same catalogue in 20-card pages.
SOURCE_URL = "https://www.exleasingcar.com/en/auto-auction/show-60/1"
PAGE_SIZE = 60
DEFAULT_TIMEOUT = 30
DEFAULT_WORKERS = 16
MAX_PAGES = 2_000
# A live catalogue can advance during a complete pass.  First try a few strict
# atomic snapshots; when the upstream counter changes continuously, switch to
# the explicit moving-catalogue coverage mode below rather than dropping the
# entire large car source from publication.
STRICT_SNAPSHOT_ATTEMPTS = 3
MOVING_CATALOGUE_PASSES = 3

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}
# Exleasingcar changed the anonymous page counter from ``Filter (N)`` to the
# visible submit control ``Show results (N)``.  Both are first-party finite
# catalogue totals, so accept either form rather than silently publishing a
# partial first page when the UI wording changes.
TOTAL_RE = re.compile(
    r"(?:Filter|Filtr|Filtro|Filtre|Show\s+results)\s*\(\s*([0-9\s,.]+)\s*\)",
    re.I,
)
YEAR_RE = re.compile(r"\b(?:0[1-9]|1[0-2])\.(19[7-9]\d|20[0-2]\d)\b|\b(19[7-9]\d|20[0-2]\d)\b")
MILEAGE_RE = re.compile(r"\b([0-9][0-9\s.,\u00a0]*)\s*km\b", re.I)
PRICE_RE = re.compile(
    r"(?:minimum\s+price|minimum\s+bid|cena\s+minimalna|mindestpreis|"
    r"prix\s+minimum|prezzo\s+minimo|precio\s+m[ií]nimo)\s*:\s*"
    r"(?:€|eur)\s*([0-9][0-9\s.,\u00a0]*)",
    re.I,
)
PAGE_RE = re.compile(r"/(?:show-\d+/)?(\d+)/?$")
CARD_XPATH = (
    "//div[contains(concat(' ', normalize-space(@class), ' '), ' auto-block ') "
    "and @car-id]"
)
FLAG_XPATH = (
    ".//*[contains(concat(' ', normalize-space(@class), ' '), ' flag-image ')]"
)
PAGINATION_XPATH = (
    "//a[contains(concat(' ', normalize-space(@class), ' '), ' pagination-page ') "
    "and @href]"
)


class ExleasingcarWatchError(RuntimeError):
    """The public catalogue could not be reconciled as one complete snapshot."""


@dataclass(frozen=True)
class ParsedPage:
    total: int
    page_count: int
    page_url_prefix: str
    rows: list[dict[str, Any]]


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def positive_number(value: str) -> int | float | None:
    compact = re.sub(r"[^0-9,.-]", "", value or "")
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
    try:
        parsed = float(compact)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def parse_total(markup: str) -> int:
    match = TOTAL_RE.search(markup)
    if not match:
        raise ExleasingcarWatchError("Exleasingcar catalogue total is missing")
    total = positive_number(match.group(1))
    if total is None or int(total) != total:
        raise ExleasingcarWatchError("Exleasingcar catalogue total is invalid")
    return int(total)


def parse_page_url_prefix(tree: HtmlElement) -> tuple[int, str]:
    candidates: list[tuple[int, str]] = []
    for anchor in tree.xpath(PAGINATION_XPATH):
        text = clean(anchor.text_content())
        href = clean(anchor.get("href"))
        if not text.isdigit() or not href:
            continue
        page = int(text)
        if page < 1:
            continue
        match = PAGE_RE.search(href)
        if match is None or int(match.group(1)) != page:
            continue
        candidates.append((page, href.rsplit("/", 1)[0]))
    if not candidates:
        raise ExleasingcarWatchError("Exleasingcar terminal pagination is missing")
    page_count, prefix = max(candidates, key=lambda value: value[0])
    return page_count, prefix


def country_from_card(card: HtmlElement) -> str:
    for node in card.xpath(FLAG_XPATH):
        for css_class in clean(node.get("class")).split():
            match = re.fullmatch(r"flag-([a-z]{2})", str(css_class).lower())
            if match:
                return match.group(1).upper()
    return ""


def normalize_fuel(text: str) -> str:
    value = clean(text).casefold()
    if re.search(r"\b(?:petrol|gasoline|benzyna|benzin|essence|gasolina)\b", value):
        if re.search(r"\b(?:electric|elektr|hybrid|plug[ -]?in)\b", value):
            return "petrol/electric hybrid"
        return "petrol"
    if re.search(r"\b(?:diesel|gazole|olej napędowy)\b", value):
        if re.search(r"\b(?:electric|elektr|hybrid|plug[ -]?in)\b", value):
            return "diesel/electric hybrid"
        return "diesel"
    if re.search(r"\b(?:hybrid|plug[ -]?in|phev|hev)\b", value):
        return "hybrid"
    if re.search(r"\b(?:electric|elektrycz)\b", value):
        return "electric"
    if re.search(r"\b(?:lpg|gas)\b", value):
        return "gas"
    return "unknown"


def parse_card(card: HtmlElement, *, observed_at: str) -> dict[str, Any]:
    raw_id = clean(card.get("car-id"))
    if not raw_id.isdigit():
        raise ExleasingcarWatchError("Exleasingcar card has no stable vehicle ID")
    headings = card.xpath(".//h5")
    title = clean(headings[0].text_content() if headings else "")
    if not title:
        raise ExleasingcarWatchError(f"Exleasingcar card {raw_id} has no title")
    card_text = clean(" ".join(card.itertext()))
    year_match = YEAR_RE.search(card_text)
    year_value = (year_match.group(1) or year_match.group(2)) if year_match else ""
    mileage_match = MILEAGE_RE.search(card_text)
    price_match = PRICE_RE.search(card_text)
    price = positive_number(price_match.group(1)) if price_match else None
    country = country_from_card(card)
    if not country:
        raise ExleasingcarWatchError(f"Exleasingcar card {raw_id} has no country flag")
    return {
        "id": f"{SOURCE_KEY}:{raw_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": f"https://www.exleasingcar.com/en/auto-details/{raw_id}",
        "title": title,
        "model": title,
        "country": country,
        "asset_country": country,
        "category": "vehicle",
        "category_raw": "vehicle",
        "year": int(year_value) if year_value else None,
        "mileage_km": positive_number(mileage_match.group(1)) if mileage_match else None,
        "fuel": normalize_fuel(card_text),
        "price_amount": price,
        "price_currency": "EUR",
        "price_eur": price,
        "price_kind": "minimum_bid" if price is not None else "unknown",
        "price_label": "public minimum price" if price is not None else "price not shown in public catalogue card",
        "bid_visibility": "catalogue summary",
        "canonical_end_utc": None,
        "sale_end_utc": None,
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": "Public catalogue listing; confirm auction terms, condition, documents, fees, buyer requirements, and export before bidding.",
        "access_sale_note": "Auction participation and detail access may require a verified buyer account.",
        "auction_status": "active",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-catalogue",
        "evidence": "Public Exleasingcar all-auctions catalogue card.",
    }


def parse_page(markup: str, *, observed_at: str) -> ParsedPage:
    total = parse_total(markup)
    try:
        tree = lxml_html.fromstring(markup)
    except (etree.ParserError, ValueError) as error:
        raise ExleasingcarWatchError("Exleasingcar page markup is invalid") from error
    page_count, prefix = parse_page_url_prefix(tree)
    cards = tree.xpath(CARD_XPATH)
    rows = [parse_card(card, observed_at=observed_at) for card in cards]
    ids = [row["id"] for row in rows]
    if not rows or len(ids) != len(set(ids)):
        raise ExleasingcarWatchError("Exleasingcar page has missing or duplicate vehicle cards")
    return ParsedPage(total=total, page_count=page_count, page_url_prefix=prefix, rows=rows)


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8))
    return session


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def _build_watch_snapshot(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_at = now.astimezone(UTC).isoformat()
    session = session or configured_session()
    first_markup = fetch_markup(session, SOURCE_URL, timeout=timeout)
    first = parse_page(first_markup, observed_at=observed_at)
    if first.total > PAGE_SIZE * MAX_PAGES:
        raise ExleasingcarWatchError("Exleasingcar catalogue exceeds the page safety limit")
    expected_pages = math.ceil(first.total / PAGE_SIZE)
    if first.page_count != expected_pages:
        raise ExleasingcarWatchError(
            f"Exleasingcar pagination mismatch: total={first.total} pages={first.page_count}"
        )
    if len(first.rows) != min(PAGE_SIZE, first.total):
        raise ExleasingcarWatchError("Exleasingcar first page cardinality is invalid")

    def fetch_and_parse(page: int) -> tuple[int, list[dict[str, Any]]]:
        local_session = configured_session()
        markup = fetch_markup(local_session, f"{first.page_url_prefix}/{page}", timeout=timeout)
        parsed = parse_page(markup, observed_at=observed_at)
        if (
            parsed.total != first.total
            or parsed.page_count != first.page_count
            or parsed.page_url_prefix != first.page_url_prefix
        ):
            raise ExleasingcarWatchError("Exleasingcar catalogue changed during pagination")
        expected_rows = PAGE_SIZE if page < expected_pages else first.total - PAGE_SIZE * (page - 1)
        if len(parsed.rows) != expected_rows:
            raise ExleasingcarWatchError(
                f"Exleasingcar page {page} cardinality is invalid: {len(parsed.rows)}"
            )
        return page, parsed.rows

    pages: dict[int, list[dict[str, Any]]] = {1: first.rows}
    if expected_pages > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_and_parse, page): page for page in range(2, expected_pages + 1)}
            for future in concurrent.futures.as_completed(futures):
                page, rows = future.result()
                pages[page] = rows
    rows = [row for page in range(1, expected_pages + 1) for row in pages[page]]
    ids = [row["id"] for row in rows]
    urls = [row["url"] for row in rows]
    if len(rows) != first.total or len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise ExleasingcarWatchError("Exleasingcar total/ID reconciliation failed")

    final = parse_page(fetch_markup(session, SOURCE_URL, timeout=timeout), observed_at=observed_at)
    if final.total != first.total or [row["id"] for row in final.rows] != [row["id"] for row in first.rows]:
        raise ExleasingcarWatchError("Exleasingcar catalogue changed before the final check")
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every vehicle card in the public all-auctions catalogue",
        "declared": first.total,
        "visited": len(rows),
        "normalized_rows": len(rows),
        "page_size": PAGE_SIZE,
        "pages": expected_pages,
        "first_page_rechecked": True,
        "stable_ids_unique": True,
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


def _validate_moving_page(parsed: ParsedPage, *, page: int) -> bool:
    """Validate one self-consistent page from an advancing public catalogue.

    A page that falls beyond the current terminal page is simply superseded by
    a concurrent catalogue shrink.  It is not used for coverage; the final
    page-one probe below determines the current advertised total.
    """
    # The source can update the visible counter a few seconds before its
    # terminal pagination link.  In moving mode, pagination is the executable
    # page contract and the counter is checked only against accumulated unique
    # IDs after the final first-page probe.
    if parsed.page_count < 1 or parsed.page_count > MAX_PAGES:
        raise ExleasingcarWatchError("Exleasingcar moving catalogue has an invalid terminal page")
    if page > parsed.page_count:
        return False
    if page < parsed.page_count and len(parsed.rows) != PAGE_SIZE:
        raise ExleasingcarWatchError(
            f"Exleasingcar moving page {page} cardinality is invalid: {len(parsed.rows)}"
        )
    if page == parsed.page_count and not 1 <= len(parsed.rows) <= PAGE_SIZE:
        raise ExleasingcarWatchError(
            f"Exleasingcar moving page {page} cardinality is invalid: {len(parsed.rows)}"
        )
    return True


def _build_moving_catalogue_snapshot(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    """Cover a live catalogue that cannot remain atomically unchanged.

    The source exposes no snapshot token.  When its public total changes on
    every strict retry, every current pagination page is still read in full and
    accumulated by stable listing ID.  A final page-one probe supplies the
    current official total; publication is allowed only when the unique covered
    IDs meet or exceed that total.  This deliberately reports a moving coverage
    snapshot rather than claiming an atomic one.
    """
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_at = current.astimezone(UTC).isoformat()
    supplied_session = session
    active_session = session or configured_session()
    rows_by_id: dict[str, dict[str, Any]] = {}
    id_by_url: dict[str, str] = {}
    latest: ParsedPage | None = None
    max_pages_seen = 0

    def add_rows(parsed: ParsedPage) -> None:
        for row in parsed.rows:
            row_id = str(row["id"])
            row_url = str(row["url"])
            other_id = id_by_url.get(row_url)
            if other_id is not None and other_id != row_id:
                raise ExleasingcarWatchError("Exleasingcar moving catalogue reuses a URL for different IDs")
            previous = rows_by_id.get(row_id)
            if previous is not None and previous["url"] != row_url:
                raise ExleasingcarWatchError("Exleasingcar moving catalogue changes a stable ID URL")
            rows_by_id[row_id] = row
            id_by_url[row_url] = row_id

    def fetch_and_parse_page(page: int, prefix: str) -> tuple[int, ParsedPage]:
        local_session = configured_session()
        try:
            markup = fetch_markup(local_session, f"{prefix}/{page}", timeout=timeout)
            return page, parse_page(markup, observed_at=observed_at)
        finally:
            close = getattr(local_session, "close", None)
            if callable(close):
                close()

    try:
        seed = parse_page(fetch_markup(active_session, SOURCE_URL, timeout=timeout), observed_at=observed_at)
        for coverage_pass in range(1, MOVING_CATALOGUE_PASSES + 1):
            baseline = seed if coverage_pass == 1 else parse_page(
                fetch_markup(active_session, SOURCE_URL, timeout=timeout), observed_at=observed_at
            )
            if not _validate_moving_page(baseline, page=1):
                raise ExleasingcarWatchError("Exleasingcar first moving page became unavailable")
            add_rows(baseline)
            pending = set(range(2, baseline.page_count + 1))
            visited_pages = {1}
            while pending:
                batch = sorted(pending)
                pending = set()
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(fetch_and_parse_page, page, baseline.page_url_prefix): page
                        for page in batch
                    }
                    for future in concurrent.futures.as_completed(futures):
                        page, parsed = future.result()
                        if _validate_moving_page(parsed, page=page):
                            add_rows(parsed)
                            visited_pages.add(page)
                        if parsed.page_count > MAX_PAGES:
                            raise ExleasingcarWatchError("Exleasingcar moving catalogue exceeds the page safety limit")
                        for extra_page in range(max(visited_pages, default=0) + 1, parsed.page_count + 1):
                            pending.add(extra_page)
            max_pages_seen = max(max_pages_seen, len(visited_pages))
            latest = parse_page(fetch_markup(active_session, SOURCE_URL, timeout=timeout), observed_at=observed_at)
            if not _validate_moving_page(latest, page=1):
                raise ExleasingcarWatchError("Exleasingcar final moving page became unavailable")
            add_rows(latest)
            if len(rows_by_id) >= latest.total:
                rows = [rows_by_id[row_id] for row_id in sorted(rows_by_id)]
                report = {
                    "status": "ok",
                    "connector_status": "ok",
                    "catalogue_scope": "every vehicle card in the advancing public all-auctions catalogue",
                    "declared": latest.total,
                    "visited": len(rows),
                    "normalized_rows": len(rows),
                    "page_size": PAGE_SIZE,
                    "pages": latest.page_count,
                    "maximum_pages_seen": max_pages_seen,
                    "coverage_passes": coverage_pass,
                    "snapshot_mode": "moving_catalogue_coverage",
                    "final_first_page_rechecked": True,
                    "stable_ids_unique": True,
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
            seed = latest
    finally:
        if supplied_session is None:
            active_session.close()
    declared = latest.total if latest is not None else 0
    raise ExleasingcarWatchError(
        f"Exleasingcar moving coverage contains {len(rows_by_id)} unique IDs for declared total {declared}"
    )


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    if timeout < 5 or workers < 1 or workers > 16:
        raise ValueError("invalid Exleasingcar timeout/workers")
    last_error: ExleasingcarWatchError | None = None
    for attempt in range(1, STRICT_SNAPSHOT_ATTEMPTS + 1):
        try:
            payload = _build_watch_snapshot(
                session=session,
                now=now,
                timeout=timeout,
                workers=workers,
            )
            payload["source_reports"][SOURCE_KEY]["snapshot_attempts"] = attempt
            return payload
        except ExleasingcarWatchError as error:
            # A live catalogue may legitimately advance while hundreds of
            # public pages are read.  Restart from page one rather than
            # accepting a mixed snapshot; the higher default concurrency
            # keeps each retry short enough to obtain a stable complete pass.
            if not any(marker in str(error) for marker in ("catalogue changed", "pagination mismatch")):
                raise
            last_error = error
    payload = _build_moving_catalogue_snapshot(
        session=session,
        now=now,
        timeout=timeout,
        workers=workers,
    )
    report = payload["source_reports"][SOURCE_KEY]
    report["strict_snapshot_failures"] = STRICT_SNAPSHOT_ATTEMPTS
    report["strict_snapshot_last_error"] = str(last_error or "catalogue changed")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch every public Exleasingcar auction catalogue card")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout, workers=args.workers)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "EXLEASINGCAR_WATCH_PASS",
        "row_count": payload["row_count"],
        "pages": payload["source_reports"][SOURCE_KEY]["pages"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
