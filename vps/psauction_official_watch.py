#!/usr/bin/env python3
"""Reconcile every public current passenger-car lot at PS Auction Sweden.

PS Auction (Sweden's largest bankruptcy and business auction house since
1958) protects its site with an AWS WAF browser challenge, so this connector
reads the public search JSON (``/item/search/json/typ=fordon_bilar``) through
the loopback WAF-solving fetch daemon (``waf_fetch_daemon.py``), which holds a
real-browser session with solved WAF token and consent state.

The JSON search exposes the whole public cars catalogue with stable item
numbers, public leading bid (or starting price when unbidded), exact naive
Stockholm end times, and location.  Passenger cars are their own search type
(``fordon_bilar``); boats, campers, trailers, heavy vehicles, and ATVs are
separate search types and are therefore excluded structurally at source.

The catalogue is read twice; stable identities and facts must match between
passes while live bid movement is deliberately ignored.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")
SOURCE_KEY = "psauction-se"
SOURCE_NAME = "PS Auction Sweden"
SOURCE_ORIGIN = "https://psauction.se"
SEARCH_PATH = "/item/search/json/typ=fordon_bilar"
DEFAULT_TIMEOUT = 45
DEFAULT_SNAPSHOT_ATTEMPTS = 3
MAX_ATTEMPTS = 6
MAX_PAGES = 40
MAX_TOTAL_ITEMS = 5_000
MAX_PAGE_ITEMS = 60
ECB_SEK_FX_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.SEK.EUR.SP00.A"
    "?format=csvdata&lastNObservations=1"
)
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")

# Defensive title gate: the walked search type is cars by the source's own
# taxonomy, so these terms mark misfiled non-passenger vehicles.
NON_PASSENGER_TITLE_RE = re.compile(
    r"\b(?:"
    r"flak|chassi|skapvagn|pickup|lastbil|skåpbil|buss|minibuss|"
    r"truck|traktor|grävmaskin|hjullastare|släp|husvagn|husbil|campingbil|"
    r"motorcykel|moped|quad|snöskoter|båt|trailer|"
    r"ambulans|brandbil"
    r")\b",
    re.I,
)


class PsauctionWatchError(RuntimeError):
    """The public PS Auction car catalogue could not be reconciled."""


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def fold(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", clean(value)).casefold()
        if not unicodedata.combining(character)
    )


@dataclass(frozen=True)
class Lot:
    item_id: int
    number: str
    slug: str
    title: str
    end_utc: dt.datetime
    currency: str
    leading: bool
    has_recent_bid: bool
    leading_bid: int | float | None
    location: str
    active: bool
    cancelled: bool
    auction_ended: bool
    thumbnail: str

    @property
    def identity(self) -> int:
        return self.item_id

    @property
    def fingerprint(self) -> tuple[int, str, str, str]:
        # Live bid fields are intentionally absent: bids can change between
        # coherent enumeration passes without changing the public lot.
        return (
            self.item_id,
            self.number,
            self.slug,
            self.title,
        )


def parse_amount(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or value < 0:
            return None
        return int(value) if float(value).is_integer() else float(value)
    text = clean(value).replace(" ", "").replace("\xa0", "")
    text = re.sub(r"[^0-9,.-]", "", text)
    if not text:
        return None
    try:
        parsed = float(text.replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def parse_stockholm_end(value: Any, *, error: str) -> dt.datetime:
    raw = clean(value)
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", raw)
    if match is None:
        raise PsauctionWatchError(f"{error}: {value!r}")
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        return dt.datetime(year, month, day, hour, minute, tzinfo=STOCKHOLM).astimezone(UTC)
    except ValueError as exc:
        raise PsauctionWatchError(f"{error}: {value!r}") from exc


def fetch_ecb_sek_per_eur() -> tuple[float, str]:
    """Return (SEK per EUR, observation date) from the public ECB data API.

    The founder requires every public offer to be displayed in EUR; PS Auction
    publishes SEK bids, so each run converts at the ECB daily reference rate
    and records the rate used in the source report for auditability.
    """
    try:
        response = urllib.request.urlopen(ECB_SEK_FX_URL, timeout=30)
        text = response.read().decode("utf-8", "replace")
    except Exception as error:
        raise PsauctionWatchError(f"ECB SEK/EUR reference rate unavailable: {error}") from error
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise PsauctionWatchError("ECB SEK/EUR reference rate response is empty")
    header = lines[0].split(",")
    try:
        value_index = header.index("OBS_VALUE") if "OBS_VALUE" in header else header.index("value")
        date_index = header.index("TIME_PERIOD")
    except ValueError as error:
        raise PsauctionWatchError("ECB SEK/EUR reference rate CSV is malformed") from error
    fields = lines[1].split(",")
    try:
        rate = float(fields[value_index])
    except (ValueError, IndexError) as error:
        raise PsauctionWatchError("ECB SEK/EUR reference rate value is invalid") from error
    if not math.isfinite(rate) or rate <= 0:
        raise PsauctionWatchError("ECB SEK/EUR reference rate value is out of range")
    observation_date = fields[date_index] if date_index < len(fields) else ""
    return rate, observation_date


def to_eur(amount: int | float | None, sek_per_eur: float) -> int | float | None:
    """Convert a public SEK amount into EUR at the given reference rate."""
    if amount is None:
        return None
    converted = float(amount) / sek_per_eur
    converted = round(converted, 2)
    return int(converted) if converted.is_integer() else converted


def parse_lot(raw: Any, *, context: str) -> Lot:
    if not isinstance(raw, dict):
        raise PsauctionWatchError(f"PS Auction {context} lot is not an object")
    item_id = raw.get("id")
    if not isinstance(item_id, int) or item_id <= 0:
        raise PsauctionWatchError(f"PS Auction {context} lot has no integer id")
    number = clean(raw.get("number"))
    slug = clean(raw.get("slug"))
    title = clean(raw.get("name") or raw.get("altText"))
    if not number or not slug or not title:
        raise PsauctionWatchError(f"PS Auction lot {item_id} is missing identity fields")
    if re.search(r"\s|[\"'<>\\^`{}|]", slug):
        raise PsauctionWatchError(f"PS Auction lot {item_id} has an invalid public slug")
    end_utc = parse_stockholm_end(raw.get("endtime"), error=f"PS Auction lot {item_id} end time")
    active = raw.get("active") is True
    cancelled = raw.get("cancelled") is True or raw.get("aicancelled") is True
    auction_ended = raw.get("auctionended") is True
    leading_bid = parse_amount(raw.get("leadingbid"))
    currency = clean(raw.get("currency")).upper()
    if leading_bid is not None and currency != "SEK":
        raise PsauctionWatchError(f"PS Auction lot {item_id} has a non-SEK public bid")
    return Lot(
        item_id=item_id,
        number=number,
        slug=slug,
        title=title,
        end_utc=end_utc,
        currency=currency,
        leading=raw.get("leading") is True,
        has_recent_bid=raw.get("hasRecentBid") is True,
        leading_bid=leading_bid,
        location=clean(raw.get("location")),
        active=active,
        cancelled=cancelled,
        auction_ended=auction_ended,
        thumbnail=clean(raw.get("thumbnail")),
    )


def lot_detail_url(lot: Lot) -> str:
    return f"{SOURCE_ORIGIN}/item/view/{lot.number}/{lot.slug}"


def make_fetcher(args: argparse.Namespace) -> Callable[[str], tuple[int, str]]:
    base = args.fetch_base.rstrip("/")

    def fetch(url: str) -> tuple[int, str]:
        try:
            response = urllib.request.urlopen(f"{base}?{urllib.parse.urlencode({'url': url})}", timeout=args.timeout)
            payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise PsauctionWatchError(f"WAF fetch for {url} failed: {error}") from error
        if payload.get("error"):
            raise PsauctionWatchError(f"WAF fetch for {url} errored: {payload['error']}")
        return int(payload.get("status", 0)), str(payload.get("body", ""))

    return fetch


def fetch_search_page(
    fetch: Callable[[str], tuple[int, str]], page: int
) -> tuple[int, bool, list[Lot], int | None]:
    """Read one public search page; return (total, hasnext, lots, next_page)."""
    url = f"{SOURCE_ORIGIN}{SEARCH_PATH}" if page == 1 else f"{SOURCE_ORIGIN}{SEARCH_PATH}?page={page}"
    status, body = fetch(url)
    if status != 200:
        raise PsauctionWatchError(f"PS Auction search page {page} returned HTTP {status}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        raise PsauctionWatchError(f"PS Auction search page {page} returned invalid JSON") from error
    if not isinstance(data, dict) or "total" not in data or "items" not in data:
        raise PsauctionWatchError(f"PS Auction search page {page} has an unexpected payload shape")
    try:
        total = int(re.sub(r"\D", "", str(data.get("total", ""))) or "0")
    except ValueError as error:
        raise PsauctionWatchError(f"PS Auction search page {page} has an invalid total") from error
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_PAGE_ITEMS:
        raise PsauctionWatchError(f"PS Auction search page {page} has an invalid items list")
    lots = [parse_lot(raw, context=f"search page {page}") for raw in raw_items]
    if len(lots) != len(set(lot.identity for lot in lots)):
        raise PsauctionWatchError(f"PS Auction search page {page} has duplicate lot ids")
    has_next = data.get("hasnext") is True
    next_page = page + 1 if has_next else None
    return total, has_next, lots, next_page


def walk_catalogue(fetch: Callable[[str], tuple[int, str]]) -> tuple[int, list[Lot]]:
    """Read every public cars search page; return (total, lots)."""
    total, has_next, lots, next_page = fetch_search_page(fetch, 1)
    if total > MAX_TOTAL_ITEMS:
        raise PsauctionWatchError("PS Auction cars catalogue exceeds total safety limit")
    if has_next and next_page is not None and next_page > MAX_PAGES:
        raise PsauctionWatchError("PS Auction cars catalogue exceeds page safety limit")
    all_lots = list(lots)
    while next_page is not None:
        if next_page > MAX_PAGES:
            raise PsauctionWatchError("PS Auction cars catalogue exceeds page safety limit")
        page_total, has_next, page_lots, next_page = fetch_search_page(fetch, next_page)
        if page_total != total:
            raise PsauctionWatchError("PS Auction declared total changed within one walk")
        all_lots.extend(page_lots)
    identities = [lot.identity for lot in all_lots]
    if len(identities) != len(set(identities)):
        raise PsauctionWatchError("PS Auction walk has duplicate lot ids")
    if len(all_lots) > total:
        raise PsauctionWatchError("PS Auction walked more lots than its declared total")
    return total, all_lots


def assert_coherent(
    first: tuple[int, list[Lot]], second: tuple[int, list[Lot]]
) -> None:
    first_total, first_lots = first
    second_total, second_lots = second
    if first_total != second_total:
        raise PsauctionWatchError("PS Auction declared total changed between passes")
    first_map = {lot.identity: lot for lot in first_lots}
    second_map = {lot.identity: lot for lot in second_lots}
    if first_map.keys() != second_map.keys():
        raise PsauctionWatchError("PS Auction lot ids changed between passes")
    if any(first_map[key].fingerprint != second_map[key].fingerprint for key in first_map):
        raise PsauctionWatchError("PS Auction lot facts changed between passes")


def passenger_exclusion_reason(lot: Lot) -> str:
    if NON_PASSENGER_TITLE_RE.search(fold(lot.title)):
        return "commercial_or_non_passenger_title"
    return ""


def infer_fuel(title: str) -> str:
    value = fold(title)
    if re.search(r"\b(?:plug[ -]?in\s+hybrid|hybrid|hybrid)\b", value):
        return "hybrid"
    if re.search(r"\b(?:electric|el|elektrisk)\b", value):
        return "electric"
    if re.search(r"\b(?:bensin|petrol|gasoline)\b", value):
        return "gasoline"
    if re.search(r"\b(?:diesel|tdi|hdi|cdi|dci)\b", value):
        return "diesel"
    return "unknown"


def normalize_lot(lot: Lot, *, observed_at: str, sek_per_eur: float) -> dict[str, Any]:
    if lot.leading_bid is not None and lot.leading_bid > 0 and (lot.leading or lot.has_recent_bid):
        price = to_eur(lot.leading_bid, sek_per_eur)
        price_kind = "current_bid"
        price_label = f"public leading bid {lot.leading_bid} SEK (≈ EUR {price})"
    elif lot.leading_bid is not None and lot.leading_bid > 0:
        price = to_eur(lot.leading_bid, sek_per_eur)
        price_kind = "starting_bid"
        price_label = f"public starting bid {lot.leading_bid} SEK (≈ EUR {price})"
    else:
        price = None
        price_kind = "unknown"
        price_label = "no public bid yet"
    year_match = YEAR_RE.search(lot.title)
    return {
        "id": f"{SOURCE_KEY}:{lot.number}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": lot_detail_url(lot),
        "title": lot.title,
        "model": lot.title,
        "country": "SE",
        "asset_country": "SE",
        "category": "car",
        "category_raw": "PS Auction public Bilar search (typ=fordon_bilar)",
        "year": int(year_match.group(1)) if year_match else None,
        "mileage": None,
        "mileage_km": None,
        "fuel": infer_fuel(lot.title),
        "seller": SOURCE_NAME,
        "location": lot.location,
        "image_url": lot.thumbnail,
        "price_amount": price,
        "price_currency": "EUR" if price is not None else "",
        "price_eur": price,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public PS Auction search JSON",
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": "Official PS Auction current public auction lot",
        "auction_status": "active",
        "canonical_end_utc": lot.end_utc.isoformat(),
        "sale_end_utc": lot.end_utc.isoformat(),
        "sale_event_utc": None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public PS Auction current lot; confirm condition, fees, documents, collection, "
            "registration, and export requirements before bidding."
        ),
        "access_sale_note": "Auction participation and purchase may require a registered bidder account (BankID).",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:item:{lot.item_id}:{lot.number}",
        "evidence": "Public PS Auction Bilar search JSON rendered through the WAF fetch daemon.",
    }


def build_watch(
    *,
    fetch: Callable[[str], tuple[int, str]],
    now: dt.datetime | None = None,
    snapshot_attempts: int = DEFAULT_SNAPSHOT_ATTEMPTS,
    sek_per_eur: float | None = None,
) -> dict[str, Any]:
    if not 1 <= snapshot_attempts <= MAX_ATTEMPTS:
        raise ValueError("invalid PS Auction snapshot-attempts")
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    observed_at = current.isoformat()
    fx_sek_per_eur, fx_observation_date = fetch_ecb_sek_per_eur() if sek_per_eur is None else (
        float(sek_per_eur), "explicitly provided"
    )
    first: tuple[int, list[Lot]] | None = None
    second: tuple[int, list[Lot]] | None = None
    attempts_used = 0
    for _ in range(snapshot_attempts):
        attempts_used += 1
        first = walk_catalogue(fetch)
        second = walk_catalogue(fetch)
        try:
            assert_coherent(first, second)
            break
        except PsauctionWatchError:
            if attempts_used >= snapshot_attempts:
                raise
    assert first is not None and second is not None

    exclusions: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    current_lots = 0
    for lot in second[1]:
        if lot.cancelled or lot.auction_ended or lot.end_utc <= current:
            exclusions["ended_or_cancelled"] += 1
            continue
        if not lot.active:
            exclusions["not_active"] += 1
            continue
        current_lots += 1
        reason = passenger_exclusion_reason(lot)
        if reason:
            exclusions[reason] += 1
            continue
        rows.append(normalize_lot(lot, observed_at=observed_at, sek_per_eur=fx_sek_per_eur))

    report = {
        "status": "ok",
        "connector_status": "ok",
        "fx": {
            "base_currency": "SEK",
            "display_currency": "EUR",
            "sek_per_eur": fx_sek_per_eur,
            "observation_date": fx_observation_date,
            "source": "ECB daily reference rate" if fx_observation_date != "explicitly provided" else "explicitly provided",
        },
        "catalogue_scope": (
            "every current public lot in the source's own Bilar (cars) search type; boats, "
            "campers, trailers, heavy vehicles, and ATVs are separate search types excluded "
            "structurally at source"
        ),
        "declared": second[0],
        "visited": len(second[1]),
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
    parser = argparse.ArgumentParser(description="Fetch every current public PS Auction passenger-car lot")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fetch-base", default="http://127.0.0.1:8977/fetch")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--snapshot-attempts", type=int, default=DEFAULT_SNAPSHOT_ATTEMPTS)
    parser.add_argument("--fx-rate", type=float, default=None, help="Explicit SEK per EUR rate; otherwise fetched from ECB")
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(
        fetch=make_fetcher(args),
        snapshot_attempts=args.snapshot_attempts,
        sek_per_eur=args.fx_rate,
    )
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "PSAUCTION_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": report["declared"],
        "current_lots": report["current_lots"],
        "snapshot_attempts_used": report["snapshot_attempts_used"],
        "seconds": round(time.monotonic() - started, 1),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
