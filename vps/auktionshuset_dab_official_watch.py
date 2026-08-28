#!/usr/bin/env python3
"""Reconcile every public active Auktionshuset dab vehicle lot in Denmark."""
from __future__ import annotations

import argparse
import datetime as dt
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
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
DENMARK = ZoneInfo("Europe/Copenhagen")
SOURCE_KEY = "auktionshuset-dab"
SOURCE_NAME = "Auktionshuset dab"
ROOT_URL = "https://www.auktionshuset.dk"
LOT_URL = f"{ROOT_URL}/lot/"
PAGE_SIZE = 48
DEFAULT_TIMEOUT = 35
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
RESULT_RE = re.compile(r"\b(\d+)\s+lots?\b", re.I)
END_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})$")
HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
}


class AuktionshusetDabWatchError(RuntimeError):
    """The public Auktionshuset dab vehicle category could not be reconciled."""


@dataclass(frozen=True)
class Catalogue:
    category_id: str
    declared_total: int
    all_ids: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def fingerprint(self) -> tuple[str, int, tuple[str, ...]]:
        return self.category_id, self.declared_total, tuple(sorted(self.all_ids))


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def positive_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    compact = re.sub(r"[^0-9,.-]", "", str(value))
    if "," in compact and "." in compact:
        compact = compact.replace(".", "").replace(",", ".")
    elif "," in compact:
        compact = compact.replace(",", ".")
    elif compact.count(".") > 1:
        compact = compact.replace(".", "")
    try:
        result = float(compact)
    except ValueError:
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return int(result) if result.is_integer() else result


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


def fetch_markup(
    session: requests.Session, *, category_id: str | None, page: int, timeout: int
) -> str:
    params: dict[str, str | int] = {"auctionStatus": 1, "page": page}
    if category_id:
        params["categories[0]"] = category_id
    response = session.get(LOT_URL, params=params, timeout=timeout)
    try:
        response.raise_for_status()
        markup = response.text
    finally:
        response.close()
    if not markup:
        raise AuktionshusetDabWatchError(f"Auktionshuset dab returned an empty vehicle page {page}")
    return markup


def vehicle_category_id(markup: str) -> str:
    soup = BeautifulSoup(markup, "html.parser")
    for checkbox in soup.select('input[name="categories[]"]'):
        label = checkbox.find_parent("label")
        label_text = clean(label.get_text(" ", strip=True) if label is not None else "")
        value = clean(checkbox.get("value"))
        if label_text.casefold() == "køretøjer" and value:
            return value
    raise AuktionshusetDabWatchError("Auktionshuset dab page has no public vehicle category identity")


def declared_total(soup: BeautifulSoup, *, page: int) -> int:
    result_node = soup.select_one(".filter .result")
    result = clean(result_node.get_text(" ", strip=True) if result_node is not None else "")
    match = RESULT_RE.search(result)
    if match is None:
        raise AuktionshusetDabWatchError(f"Auktionshuset dab page {page} has no declared lot total")
    return int(match.group(1))


def public_lot_cards(markup: str, *, page: int) -> tuple[int, list[Tag]]:
    soup = BeautifulSoup(markup, "html.parser")
    return declared_total(soup, page=page), list(soup.select("li.lot-item"))


def parse_end(value: Any) -> dt.datetime:
    text = clean(value)
    match = END_RE.fullmatch(text)
    if match is None:
        raise AuktionshusetDabWatchError(f"Auktionshuset dab lot has invalid public end time: {text!r}")
    try:
        local = dt.datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}").replace(tzinfo=DENMARK)
    except ValueError as exc:
        raise AuktionshusetDabWatchError(f"Auktionshuset dab lot has invalid public end time: {text!r}") from exc
    return local.astimezone(UTC)


def normalize_fuel(text: str) -> str:
    folded = text.casefold()
    if "hybrid" in folded:
        return "hybrid"
    if "diesel" in folded:
        return "diesel"
    if "benzin" in folded or "petrol" in folded or "gasoline" in folded:
        return "petrol"
    if "elektrisk" in folded or re.search(r"\belbil\b", folded):
        return "electric"
    if "gas" in folded or "lpg" in folded:
        return "lpg"
    return "unknown"


def card_to_watch(card: Tag, *, observed_at: str, now: dt.datetime) -> dict[str, Any]:
    native_id = clean(card.get("id"))
    end = parse_end(card.get("data-ends"))
    if not native_id or end <= now:
        raise AuktionshusetDabWatchError("Auktionshuset dab active result contains an invalid or ended lot")
    link = next(
        (
            candidate for candidate in card.select('a[href*="/lots/"]')
            if str(candidate.get("href") or "").startswith("/auktioner/")
        ),
        None,
    )
    href = clean(link.get("href") if link is not None else "")
    title = clean(card.select_one("h3").get_text(" ", strip=True) if card.select_one("h3") else "")
    if not href or not title:
        raise AuktionshusetDabWatchError(f"Auktionshuset dab lot {native_id} has no canonical card title or URL")
    bid_node = card.select_one("p.bid-amount")
    bid = positive_number(bid_node.get_text(" ", strip=True) if bid_node is not None else None)
    if bid is None:
        price_kind, price_label = "unknown", "Auktionshuset dab card has no positive current bid"
    else:
        price_kind, price_label = "current_bid", "Public Auktionshuset dab highest bid"
    image = card.select_one("img[src]")
    image_url = clean(image.get("src") if image is not None else "")
    year_match = YEAR_RE.search(title)
    return {
        "id": f"{SOURCE_KEY}:{native_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": urljoin(ROOT_URL, href),
        "title": title,
        "model": title,
        "country": "DK",
        "asset_country": "DK",
        "category": "vehicle",
        "category_raw": "Auktionshuset dab public Køretøjer category",
        "year": int(year_match.group(1)) if year_match else None,
        "mileage": None,
        "mileage_km": None,
        "fuel": normalize_fuel(title),
        "seller": SOURCE_NAME,
        "image_url": image_url or None,
        "price_amount": bid,
        "price_currency": "DKK" if bid is not None else "",
        "price_eur": None,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public Auktionshuset dab vehicle lot card",
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": "Official Auktionshuset dab public vehicle auction; displayed bid may exclude or include stated fees.",
        "auction_status": "active",
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official Auktionshuset dab vehicle lot. Confirm vehicle condition, fees, buyer requirements, "
            "and Algerian import eligibility before bidding."
        ),
        "access_sale_note": "Open the official Auktionshuset dab lot to inspect bidding, pickup, and export terms.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-vehicle-category:{native_id}",
        "evidence": "Official Auktionshuset dab public vehicle lot card.",
    }


def enumerate_catalogue(
    session: requests.Session, *, observed_at: str, now: dt.datetime, timeout: int
) -> Catalogue:
    unfiltered = fetch_markup(session, category_id=None, page=1, timeout=timeout)
    category_id = vehicle_category_id(unfiltered)
    first_markup = fetch_markup(session, category_id=category_id, page=1, timeout=timeout)
    total, first_cards = public_lot_cards(first_markup, page=1)
    if total and not first_cards:
        raise AuktionshusetDabWatchError("Auktionshuset dab declared vehicles but rendered no first-page cards")
    page_size = len(first_cards)
    expected_pages = math.ceil(total / page_size) if total else 0
    cards = list(first_cards)
    for page in range(2, expected_pages + 1):
        page_total, page_cards = public_lot_cards(
            fetch_markup(session, category_id=category_id, page=page, timeout=timeout), page=page
        )
        if page_total != total:
            raise AuktionshusetDabWatchError(f"Auktionshuset dab page {page} changed the declared vehicle total")
        expected_cards = min(page_size, total - len(cards))
        if len(page_cards) != expected_cards:
            raise AuktionshusetDabWatchError(
                f"Auktionshuset dab page {page} returned {len(page_cards)} cards, expected {expected_cards}"
            )
        cards.extend(page_cards)
    if len(cards) != total:
        raise AuktionshusetDabWatchError(f"Auktionshuset dab returned {len(cards)} cards for declared total {total}")
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    rows: list[dict[str, Any]] = []
    for card in cards:
        row = card_to_watch(card, observed_at=observed_at, now=now)
        if row["id"] in seen_ids or row["url"] in seen_urls:
            raise AuktionshusetDabWatchError("Auktionshuset dab pagination repeats a stable lot identity")
        seen_ids.add(str(row["id"]))
        seen_urls.add(str(row["url"]))
        rows.append(row)
    return Catalogue(category_id=category_id, declared_total=total, all_ids=tuple(seen_ids), rows=tuple(rows))


def build_watch(
    *, session: requests.Session | None = None, now: dt.datetime | None = None, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    current = (now or dt.datetime.now(UTC)).astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        first = enumerate_catalogue(active_session, observed_at=observed_at, now=current, timeout=timeout)
        second = enumerate_catalogue(active_session, observed_at=observed_at, now=current, timeout=timeout)
    finally:
        if supplied_session is None:
            active_session.close()
    if first.fingerprint != second.fingerprint:
        raise AuktionshusetDabWatchError("Auktionshuset dab vehicle catalogue changed during final reconciliation")
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public active Auktionshuset dab vehicle-category lot",
        "vehicle_category_id": first.category_id,
        "declared": first.declared_total,
        "publicly_listed": first.declared_total,
        "normalized_active": len(first.rows),
        "pages": math.ceil(first.declared_total / PAGE_SIZE) if first.declared_total else 0,
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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch every public Auktionshuset dab vehicle lot")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "AUKTIONSHUSET_DAB_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": payload["source_reports"][SOURCE_KEY]["declared"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
