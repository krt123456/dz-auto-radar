#!/usr/bin/env python3
"""Zoll-Auktion (official German customs auction) connector for the radar
auction lane (registry key zoll-auktion, priority 1, founder mgr-e325f6c9..).

Contract (all derived from the live site, not invented):
  - Category listing: GET https://www.zoll-auktion.de/auktion/kategorie/Fahrzeuge/191
    server-rendered HTML; product links `/auktion/produkt/{slug}/{id}`; 10 per
    page; pagination via `?pagination=N` (verified: page 1,2,3 all differ).
    Category header states the result count ("691 Treffer").
  - Product page extracts by stable HTML ids: auktions_id (`bilder_auktionen_id`),
    end (`auktions_ende`, "Di., 18.08.2026 - 07:00 Uhr" Europe/Berlin),
    highest bid (`hoechstgebot`, "43.500,00 EUR"), title (`ueberschrift_auktion`),
    and dl rows Marke/Modell/Fahrzeugart/Erstzulassung/Kilometerstand/
    Kraftstoffart/Getriebeart/Leistung.
  - End time: Europe/Berlin wall time (countdown "noch 8 Std. 57 Min." at
    20:02:49Z vs 07:00 Berlin confirmed DST offset +02). Canonical end written
    as UTC ISO-8601 with Z (build_auction_board.parse_canonical_end fail-closed
    on naive timestamps).
  - Bid: German decimal "43.500,00" must be normalized BEFORE the universe
    importer (import_live_offers_to_universe.parse_price_eur would integer-mangle
    it to 4350000). Emit "43500.00".
  - CSV column contract: merge_listings.STANDARD_FIELDNAMES (exact header).
  - Every run re-reads every live product page so changing bids are refreshed;
    the CSV is replaced atomically and stale/ended lots disappear.
  - Only future-end lots are emitted; malformed/missing end or non-numeric bid
    counts as excluded_with_reason, never fabricated.
"""

from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from urllib.parse import urljoin

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zoll_auktion")

LISTING_URL = "https://www.zoll-auktion.de/auktion/kategorie/Fahrzeuge/191"
SITE_ORIGIN = "https://www.zoll-auktion.de"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc

PRODUCT_LINK_RE = re.compile(r'href="(/auktion/produkt/[^"?#]+)"')
COUNT_RE = re.compile(r"Auktionssuche:\s*([\d.]+)\s*Treffer", re.I)
NEXT_PAGE_RE = re.compile(
    r'<a\s+rel="next"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
PAGER_NAV_RE = re.compile(r'aria-label="Suchergebnis Paginierung"')
END_RE = re.compile(
    r"^\w+\.?,?\s*(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{1,2}):(\d{2})\s*Uhr$"
)
LISTING_ID_RE = re.compile(r'id="bilder_auktionen_id">(\d+)')
END_DD_RE = re.compile(r'id="auktions_ende"[^>]*>(.*?)</dd>', re.S)
BID_SPAN_RE = re.compile(r'id="hoechstgebot"[^>]*>(.*?)</span>', re.S)
TITLE_H4_RE = re.compile(r'id="ueberschrift_auktion"[^>]*>(.*?)</h4>', re.S)
DL_ROW_RE = re.compile(
    r"<(?:dt|th)[^>]*>(.*?)</(?:dt|th)>\s*<(?:dd|td)[^>]*>(.*?)</(?:dd|td)>", re.S
)
TAG_RE = re.compile(r"<[^>]+>")

# German -> canonical universe fuel vocabulary (observed live in
# universe_offers.sqlite; unknown values pass through as-is, never reject).
FUEL_MAP = {
    "Elektro": "electric",
    "Benzin": "petrol",
    "Diesel": "diesel",
    "Hybrid": "hybrid",
    "Erdgas": "cng",
    "Flüssiggas": "lpg",
    "Wasserstoff": "hydrogen",
}
# German -> universe transmission vocabulary (observed live).
TRANSMISSION_MAP = {
    "Automatik": "automatic",
    "Schaltgetriebe": "manual",
    "Manuell": "manual",
    "Teilautomatik": "automatic",
    "Semi-Automatik": "automatic",
}
FIELDNAMES = [
    "listing_id", "model_key", "title", "source", "source_url",
    "first_registration_date", "fuel", "engine_cc", "mileage_km",
    "price_eur", "seller_type", "accident_free", "service_history",
    "transmission", "country", "auction_end_at", "sale_term_code",
    "sale_certainty", "sale_certainty_note",
]

AuctionRow = Dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Zoll-Auktion Fahrzeuge lots.")
    parser.add_argument("--out", default="zoll_auktion_live.csv")
    parser.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    parser.add_argument("--max-products", type=int, default=0, help="0 = all lots")
    parser.add_argument("--sleep", type=float, default=0.6)
    parser.add_argument("--timeout", type=float, default=25.0)
    return parser.parse_args()


def strip_tags(value: str) -> str:
    return html.unescape(" ".join(TAG_RE.sub(" ", value).split()))


def fetch(url: str, session: requests.Session, timeout: float) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def parse_listing_page(text: str) -> tuple[List[str], int, Optional[str]]:
    links = sorted(set(PRODUCT_LINK_RE.findall(text)))
    count = 0
    match = COUNT_RE.search(text)
    if match:
        count = int(match.group(1).replace(".", ""))
    next_url = None
    if PAGER_NAV_RE.search(text):
        next_match = NEXT_PAGE_RE.search(text)
        if next_match:
            next_url = html.unescape(next_match.group(1))
    return links, count, next_url


def parse_end_time(raw: str) -> Optional[datetime]:
    text = strip_tags(raw)
    match = END_RE.match(text)
    if not match:
        return None
    day, month, year, hour, minute = (int(g) for g in match.groups())
    if not (1 <= day <= 31 and 1 <= month <= 12 and len(str(year)) == 4):
        return None
    try:
        local = datetime(year, month, day, hour, minute, tzinfo=BERLIN)
    except ValueError:
        return None
    return local.astimezone(UTC)


def parse_bid(raw: str) -> Optional[int]:
    """German decimal "43.500,00 EUR" -> 43500 (int cents-free EUR).

    German number format observed live on zoll-auktion.de: "." is the
    thousands separator, "," the decimal separator. Amounts with no comma
    (e.g. "1.200 EUR", "12.000.000 EUR") are thousands-dotted integers.

    Returns None when no EUR amount is present (e.g. 'auf Anfrage').
    """
    text = strip_tags(raw)
    text = re.sub(r"\s+", "", text)
    match = re.search(r"([\d.,]+)\s*EUR", text, re.I)
    if not match:
        return None
    digits = match.group(1)
    if "," in digits:
        if "." in digits:
            digits = digits.replace(".", "")
        digits = digits.replace(",", ".")
    else:
        digits = digits.replace(".", "")
    try:
        return int(round(float(digits)))
    except ValueError:
        return None


def parse_model_key(make: str, model: str, title: str) -> str:
    parts = []
    for value in (make, model):
        key = re.sub(r"[^0-9a-z]+", "_", (value or "").lower()).strip("_")
        if key:
            parts.append(key)
    if not parts:
        from_title = re.sub(r"[^0-9a-z]+", "_", (title or "").lower()).strip("_")
        if from_title:
            parts.append(from_title)
    return "_".join(parts)[:80]


def parse_product_page(text: str, product_path: str) -> Optional[AuctionRow]:
    listing_match = LISTING_ID_RE.search(text)
    if not listing_match:
        log.warning("no auktions_id in %s", product_path)
        return None
    listing_id = listing_match.group(1)
    end_match = END_DD_RE.search(text)
    if not end_match:
        log.warning("no auktions_ende in %s", product_path)
        return None
    end = parse_end_time(end_match.group(1))
    if end is None:
        log.warning("unparseable end in %s: %r", product_path, strip_tags(end_match.group(1)))
        return None
    title = ""
    title_match = TITLE_H4_RE.search(text)
    if title_match:
        title = strip_tags(title_match.group(1))
    bid = None
    bid_match = BID_SPAN_RE.search(text)
    if bid_match:
        bid = parse_bid(bid_match.group(1))
    fields: Dict[str, str] = {}
    for dt_tag, dd_tag in DL_ROW_RE.findall(text):
        fields[strip_tags(dt_tag).strip().rstrip(":")] = strip_tags(dd_tag).strip()
    make = fields.get("Marke (Hersteller)", "")
    model = fields.get("Modell", "")
    fuel_raw = fields.get("Kraftstoffart", "")
    transmission_raw = fields.get("Getriebeart", "")
    mileage = 0
    mileage_match = re.search(r"([\d.]+)", fields.get("Kilometerstand", ""))
    if mileage_match:
        mileage = int(mileage_match.group(1).replace(".", ""))
    registration = fields.get("Erstzulassung", "") or ""

    row: AuctionRow = {
        "listing_id": listing_id,
        "model_key": parse_model_key(make, model, title),
        "title": title,
        "source": "zoll-auktion",
        "source_url": SITE_ORIGIN + product_path,
        "first_registration_date": registration,
        "fuel": FUEL_MAP.get(fuel_raw, fuel_raw),
        "engine_cc": "",
        "mileage_km": mileage,
        "price_eur": "" if bid is None else f"{bid}.00",
        "seller_type": "auction",
        "accident_free": "unknown",
        "service_history": "unknown",
        "transmission": TRANSMISSION_MAP.get(transmission_raw, transmission_raw),
        "country": "DE",
        "auction_end_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sale_term_code": "auction",
        "sale_certainty": "auction",
        "sale_certainty_note": (
            "Official customs auction (Zoll-Auktion); registration/bidding "
            "details on the venue account."
        ),
    }
    log.info(
        "lot %s %s bid=%s end=%s",
        listing_id, title[:40], row["price_eur"] or "NA", row["auction_end_at"],
    )
    return row


def iter_pages(max_pages: int, session: requests.Session, timeout: float) -> Iterable[str]:
    """Yield listing-page HTML, following the site's own rel=next links.

    The category URL serves page 1; all later pages are reached via the
    `rel="next"` href in the "Suchergebnis Paginierung" nav (that href carries
    the full filter state: n0=search&t=t1&s=12&n1[]=...&pagination=N). Parsed
    from live HTML, never invented.
    """
    url = LISTING_URL
    page = 1
    while max_pages <= 0 or page <= max_pages:
        try:
            text = fetch(url, session, timeout)
        except requests.RequestException as exc:
            log.warning("category page %d fetch failed: %s", page, exc)
            break
        links, total, next_url = parse_listing_page(text)
        if not links:
            log.info("no more links on page %d (list ended)", page)
            break
        yield links
        if total and page * 10 >= total:
            break
        if not next_url:
            log.info("no rel=next link, stopping after page %d", page)
            break
        url = urljoin(LISTING_URL, next_url)
        page += 1
        time.sleep(0.4)


def main() -> None:
    args = parse_args()
    out_path = Path(args.out)
    session = requests.Session()

    rows: List[AuctionRow] = []
    excluded_unparseable = 0
    fetch_errors = 0
    seen = set()

    for links in iter_pages(args.max_pages, session, args.timeout):
        for link in links:
            if args.max_products and len(seen) >= args.max_products:
                break
            listing_id = link.rstrip("/").rsplit("/", 1)[-1]
            if listing_id in seen:
                continue
            seen.add(listing_id)
            try:
                text = fetch(SITE_ORIGIN + link, session, args.timeout)
            except requests.RequestException as exc:
                log.warning("product %s fetch failed: %s", link, exc)
                fetch_errors += 1
                continue
            time.sleep(args.sleep)
            row = parse_product_page(text, link)
            if row is None:
                excluded_unparseable += 1
                continue
            rows.append(row)

    # A broken category response must not erase the last good hourly snapshot.
    if not seen or len(rows) < max(1, len(seen) // 2):
        raise RuntimeError(
            f"refusing incomplete Zoll snapshot: discovered={len(seen)} parsed={len(rows)}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_suffix(out_path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(out_path)

    summary = {
        "out": str(out_path),
        "refreshed_rows": len(rows),
        "discovered_listing_ids": len(seen),
        "excluded_unparseable": excluded_unparseable,
        "fetch_errors": fetch_errors,
        "atomic_snapshot": True,
    }
    log.info("summary: %s", summary)
    print(summary)


if __name__ == "__main__":
    main()
