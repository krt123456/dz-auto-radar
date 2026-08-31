#!/usr/bin/env python3
"""Reconcile every public current Bilweb Auctions vehicle object in Sweden."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

import fx_rates
from fx_rates import fetch_ecb_units_per_eur, to_eur
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SWEDEN = ZoneInfo("Europe/Stockholm")
SOURCE_KEY = "bilweb"
SOURCE_NAME = "Bilweb Auctions"
ROOT_URL = "https://bilwebauctions.se"
AUCTIONS_URL = f"{ROOT_URL}/en/auktion"
DEFAULT_TIMEOUT = 35

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-US,en;q=0.9,sv;q=0.7",
}
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
MILEAGE_RE = re.compile(r"\b([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{1,7})\s*km\b", re.I)
END_RE = re.compile(r"\b(\d{1,2})\s+([A-Z]{3})\s+(\d{1,2}:\d{2})\b", re.I)
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


class BilwebWatchError(RuntimeError):
    """Bilweb's public current-auction view could not be reconciled safely."""


@dataclass(frozen=True)
class AuctionPage:
    slug: str
    declared_total: int
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Catalogue:
    slugs: tuple[str, ...]
    declared_total: int
    rows: tuple[dict[str, Any], ...]

    @property
    def fingerprint(self) -> tuple[tuple[str, ...], int, tuple[str, ...]]:
        return self.slugs, self.declared_total, tuple(str(row["id"]) for row in self.rows)


def clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def positive_number(value: Any) -> int | float | None:
    compact = re.sub(r"[^0-9,.-]", "", clean(value))
    if not compact:
        return None
    if "," in compact and "." in compact:
        compact = compact.replace(".", "").replace(",", ".")
    elif "," in compact:
        compact = compact.replace(",", ".")
    elif compact.count(".") > 1:
        compact = compact.replace(".", "")
    try:
        parsed = float(compact)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    digits = re.sub(r"[^0-9]", "", clean(value))
    return int(digits) if digits else None


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
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


def fetch_markup(session: requests.Session, url: str, *, timeout: int) -> str:
    response = session.get(url, timeout=timeout)
    try:
        response.raise_for_status()
        markup = response.text
    finally:
        response.close()
    if not markup:
        raise BilwebWatchError(f"Bilweb returned an empty page: {url}")
    return markup


def ongoing_auction_slugs(markup: str) -> tuple[str, ...]:
    soup = BeautifulSoup(markup, "html.parser")
    result: list[str] = []
    for card in soup.select(".EventPage-ongoingCard"):
        link = card.select_one('a[href^="/en/"]')
        href = clean(link.get("href") if link is not None else "")
        if not re.fullmatch(r"/en/[a-z0-9-]+", href):
            raise BilwebWatchError("Bilweb current-auction card has no canonical auction link")
        result.append(href.rsplit("/", 1)[-1])
    if len(result) != len(set(result)):
        raise BilwebWatchError("Bilweb current-auction index repeats an auction identity")
    return tuple(result)


def text_or_empty(node: Tag | None) -> str:
    return clean(node.get_text(" ", strip=True) if node is not None else "")


def parse_end(value: str, *, auction_year: int, now: dt.datetime) -> dt.datetime:
    match = END_RE.search(clean(value))
    if match is None:
        raise BilwebWatchError(f"Bilweb object has no valid countdown: {value!r}")
    month = MONTHS.get(match.group(2).upper())
    if month is None:
        raise BilwebWatchError(f"Bilweb object has an unknown countdown month: {value!r}")
    hour, minute = (int(part) for part in match.group(3).split(":"))
    try:
        local = dt.datetime(auction_year, month, int(match.group(1)), hour, minute, tzinfo=SWEDEN)
    except ValueError as exc:
        raise BilwebWatchError(f"Bilweb object has an invalid countdown: {value!r}") from exc
    # An auction page labelled for December can naturally finish in January of
    # the following year.  No other year is inferred.
    if local.astimezone(UTC) <= now and month == 1 and now.month == 12:
        local = local.replace(year=auction_year + 1)
    return local.astimezone(UTC)


def normalize_fuel(text: str) -> str:
    folded = text.casefold()
    if "hybrid" in folded:
        return "hybrid"
    if "diesel" in folded:
        return "diesel"
    if "petrol" in folded or "gasoline" in folded or "bensin" in folded:
        return "petrol"
    if "electric" in folded or "elektr" in folded:
        return "electric"
    if "lpg" in folded:
        return "lpg"
    return "unknown"


def row_to_watch(
    row: Tag, *, slug: str, auction_year: int, observed_at: str, now: dt.datetime, fx_rate: float | None = None
) -> dict[str, Any]:
    object_id = nonnegative_integer(row.get("id"))
    if object_id is None or object_id <= 0:
        raise BilwebWatchError("Bilweb public object row has no stable ID")
    title_link = row.select_one(".RowObject-title a[href]")
    title = text_or_empty(title_link)
    href = clean(title_link.get("href") if title_link is not None else "")
    if not title or not href.startswith("/en/"):
        raise BilwebWatchError(f"Bilweb object {object_id} has no canonical title link")
    description = text_or_empty(row.select_one(".RowObject-desc"))
    values: dict[str, str] = {}
    for group in row.select(".RowObject-infoGrid > div"):
        label = text_or_empty(group.select_one(".RowObject-infoLabel")).casefold()
        value = text_or_empty(group.select_one(".RowObject-infoValue"))
        if label:
            values[label] = value
    countdown = values.get("countdown") or values.get("countdown:")
    if not countdown:
        raise BilwebWatchError(f"Bilweb object {object_id} has no countdown")
    end = parse_end(countdown, auction_year=auction_year, now=now)
    if end <= now:
        raise BilwebWatchError(f"Bilweb object {object_id} is already ended in the current panel")
    bid_value = values.get("current bid") or values.get("highest bid")
    bid = positive_number(bid_value)
    if bid is not None:
        price_kind = "current_bid"
        price_label = "public current bid from Bilweb Auctions (EUR, ECB daily rate)"
    else:
        price_kind = "unknown"
        price_label = "Bilweb Auctions card does not state a positive current bid"
    reserve_text = text_or_empty(row.select_one(".Badge--reserve"))
    reserve_folded = reserve_text.casefold()
    no_reserve = True if "no reserve" in reserve_folded else None
    if "reserve price" in reserve_folded:
        no_reserve = False
    vehicle_text = f"{title} {description}"
    year_match = YEAR_RE.search(vehicle_text)
    mileage_match = MILEAGE_RE.search(vehicle_text)
    year = int(year_match.group(1)) if year_match else None
    mileage = positive_number(
        mileage_match.group(1).replace(" ", "").replace(".", "")
    ) if mileage_match else None
    image = row.select_one("img[src]")
    image_url = clean(image.get("src") if image is not None else "")
    return {
        "id": f"{SOURCE_KEY}:{slug}:{object_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": urljoin(ROOT_URL, href),
        "title": title,
        "model": title,
        "country": "SE",
        "asset_country": "SE",
        "category": "vehicle",
        "category_raw": "Bilweb current auction object",
        "year": year,
        "mileage": mileage,
        "mileage_km": mileage,
        "fuel": normalize_fuel(vehicle_text),
        "seller": SOURCE_NAME,
        "image_url": image_url or None,
        "price_amount": to_eur(bid, fx_rate) if (bid is not None and fx_rate) else bid,
        "price_currency": "EUR" if (bid is not None and fx_rate) else ("SEK" if bid is not None else ""),
        "price_eur": to_eur(bid, fx_rate) if (bid is not None and fx_rate) else None,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public Bilweb Auctions current-object card",
        "reserve_met": None,
        "no_reserve": no_reserve,
        "sale_terms": reserve_text or "Official Bilweb Auctions lot",
        "auction_status": "active",
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official Bilweb Auctions vehicle listing. Confirm vehicle condition, auction terms, "
            "fees, buyer requirements, and Algerian import eligibility before bidding."
        ),
        "access_sale_note": "Open the official Bilweb Auctions object to inspect all auction and pickup terms.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-current:{slug}:{object_id}",
        "evidence": "Official Bilweb Auctions public current-object card.",
    }


def parse_auction_page(
    markup: str, *, slug: str, observed_at: str, now: dt.datetime, fx_rate: float | None = None
) -> AuctionPage:
    soup = BeautifulSoup(markup, "html.parser")
    title = text_or_empty(soup.select_one(".ObjectListPage-title"))
    year_match = YEAR_RE.search(title)
    if year_match is None:
        raise BilwebWatchError(f"Bilweb auction {slug} has no event year")
    panel = soup.select_one("#active_objects")
    if panel is None:
        raise BilwebWatchError(f"Bilweb auction {slug} has no active-object panel")
    counter = soup.select_one("#total_objects")
    declared_total = nonnegative_integer(
        counter.get("value") if counter is not None and counter.get("value") is not None
        else text_or_empty(counter)
    )
    if declared_total is None:
        declared_total = nonnegative_integer(panel.get("data-totalobjects"))
    if declared_total is None:
        raise BilwebWatchError(f"Bilweb auction {slug} has no active object count")
    rows = tuple(
        row_to_watch(row, slug=slug, auction_year=int(year_match.group(1)), observed_at=observed_at, now=now, fx_rate=fx_rate)
        for row in panel.select(".RowObject.row-object")
    )
    ids = [str(row["id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise BilwebWatchError(f"Bilweb auction {slug} repeats a stable object ID")
    if len(rows) != declared_total:
        raise BilwebWatchError(
            f"Bilweb auction {slug} declared {declared_total} active objects but rendered {len(rows)}"
        )
    return AuctionPage(slug=slug, declared_total=declared_total, rows=rows)


def enumerate_catalogue(
    session: requests.Session, *, observed_at: str, now: dt.datetime, timeout: int, fx_rate: float | None = None
) -> Catalogue:
    slugs = ongoing_auction_slugs(fetch_markup(session, AUCTIONS_URL, timeout=timeout))
    auctions = [
        parse_auction_page(
            fetch_markup(session, f"{ROOT_URL}/en/{slug}", timeout=timeout),
            slug=slug,
            observed_at=observed_at,
            now=now,
            fx_rate=fx_rate,
        )
        for slug in slugs
    ]
    rows = tuple(row for auction in auctions for row in auction.rows)
    declared_total = sum(auction.declared_total for auction in auctions)
    ids = [str(row["id"]) for row in rows]
    urls = [str(row["url"]) for row in rows]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise BilwebWatchError("Bilweb current-auction catalogue has duplicate stable identities")
    if len(rows) != declared_total:
        raise BilwebWatchError("Bilweb current-auction total does not equal enumerated objects")
    return Catalogue(slugs=slugs, declared_total=declared_total, rows=rows)


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    fx_rates: dict[str, tuple[float, str]] | None = None,
) -> dict[str, Any]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session()
    if fx_rates is not None and "SEK" in fx_rates:
        fx_rate, fx_date = fx_rates["SEK"]
    else:
        fx_rate, fx_date = fetch_ecb_units_per_eur("SEK")
    try:
        first = enumerate_catalogue(
            active_session, observed_at=observed_at, now=current, timeout=timeout, fx_rate=fx_rate
        )
        second = enumerate_catalogue(
            active_session, observed_at=observed_at, now=current, timeout=timeout, fx_rate=fx_rate
        )
        if second.fingerprint != first.fingerprint:
            raise BilwebWatchError("Bilweb current-auction catalogue changed during final reconciliation")
        report = {
            "status": "ok",
            "connector_status": "ok",
            "catalogue_scope": "every public active Bilweb Auctions object in every current auction",
            "declared": first.declared_total,
            "publicly_listed": len(first.rows),
            "visited": len(first.rows),
            "normalized_rows": len(first.rows),
            "current_auctions": len(first.slugs),
            "auction_slugs": list(first.slugs),
            "full_catalogue_rechecked": True,
            "stable_ids_unique": True,
            "publication_ready": False,
        }
        return {
            "schema_version": 1,
            "lane": "official_auction_watch",
            "generated_at_utc": observed_at,
            "research_only": True,
            "publication_status": "review_required",
            "row_count": len(first.rows),
            "rows": list(first.rows),
            "source_reports": {SOURCE_KEY: report},
        }
    finally:
        if supplied_session is None:
            active_session.close()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch every public current Bilweb Auctions vehicle object")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "BILWEB_WATCH_PASS",
        "row_count": payload["row_count"],
        "current_auctions": payload["source_reports"][SOURCE_KEY]["current_auctions"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
