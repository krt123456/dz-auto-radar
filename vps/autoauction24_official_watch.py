#!/usr/bin/env python3
"""Reconcile the public AutoAuction24 next-event passenger-car catalogue.

The public React application calls ``/api/getnextevent`` without a session.
Its response is the complete cars array for every upcoming event, so the
collector reconciles the full stable car-ID membership twice before emitting
the passenger-car subset.  Live bid values can change between reads and are
therefore taken from the second read rather than used as a snapshot key.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "autoauction24-ch"
SOURCE_NAME = "AutoAuction24.ch"
SOURCE_URL = "https://autoauction24.ch/"
API_URL = "https://autoauction24.ch/api/getnextevent"
DEFAULT_TIMEOUT = 35

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept": "application/json",
}
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
NON_PASSENGER_TEXT_RE = re.compile(
    r"\b(?:truck|lorry|bus|motorcycle|moped|tractor|trailer|excavator|"
    r"forklift|construction|commercial|delivery\s+van|panel\s+van|"
    r"motorhome|camper|wohnmobil|asuntoauto|matkailuauto)\b",
    re.I,
)
PASSENGER_BODIES = frozenset({
    "limousine", "saloon", "sedan", "estate car", "station wagon", "wagon",
    "coupe", "convertible", "cabriolet", "hatchback", "suv", "offroader", "suv/offroader",
})


class AutoAuction24WatchError(RuntimeError):
    pass


class AutoAuction24SnapshotChanged(AutoAuction24WatchError):
    pass


@dataclass(frozen=True)
class Snapshot:
    raw_count: int
    event_ids: tuple[int, ...]
    fingerprint: str
    rows: list[dict[str, Any]]
    exclusions: dict[str, int]


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def ascii_fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", clean(value))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def positive_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = re.sub(r"[^0-9,.-]", "", clean(value))
        if not text:
            return None
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
        elif "," in text:
            suffix = text.rsplit(",", 1)[-1]
            text = text.replace(",", ".") if len(suffix) <= 2 else text.replace(",", "")
        try:
            number = float(text)
        except ValueError:
            return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number) if number.is_integer() else number


def nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def require_positive_int(value: Any, *, field: str) -> int:
    number = nonnegative_int(value)
    if number is None or number < 1:
        raise AutoAuction24WatchError(f"AutoAuction24 {field} is invalid")
    return number


def parse_end(value: Any) -> dt.datetime:
    text = clean(value)
    if not text:
        raise AutoAuction24WatchError("AutoAuction24 car has no auction end time")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise AutoAuction24WatchError("AutoAuction24 car has an invalid auction end time") from exc
    if parsed.tzinfo is None:
        raise AutoAuction24WatchError("AutoAuction24 auction end time is timezone-naive")
    return parsed.astimezone(UTC)


def normalize_fuel(value: Any) -> str:
    folded = ascii_fold(value)
    if not folded:
        return "unknown"
    diesel = "diesel" in folded
    petrol = bool(re.search(r"\b(?:petrol|gasoline|benzine|benzin)\b", folded))
    hybrid = "hybrid" in folded or "plug-in" in folded
    electric = bool(re.search(r"\b(?:electric|ev)\b", folded))
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
    if re.search(r"\b(?:lpg|cng|gas)\b", folded):
        return "gas"
    return "unknown"


def title_from_car(car: dict[str, Any]) -> str:
    return clean(" ".join(
        str(car.get(key) or "")
        for key in ("car_name", "carmodel", "registercartype")
    ))


def passenger_exclusion_reason(car: dict[str, Any], title: str) -> str:
    body = ascii_fold(car.get("body"))
    text = " ".join((ascii_fold(title), body, ascii_fold(car.get("otherdescription"))))
    if NON_PASSENGER_TEXT_RE.search(text):
        return "explicit_non_car_text"
    # The API labels light commercial and people-carrier stock as "Van".
    # It supplies no passenger/cargo subtype, so retain only explicit
    # passenger-car body styles rather than guessing from a model name.
    if body not in PASSENGER_BODIES:
        return "body_not_passenger_car"
    return ""


def car_row(
    car: dict[str, Any],
    *,
    car_id: int,
    event_id: int,
    title: str,
    end: dt.datetime,
    observed_at: str,
) -> dict[str, Any]:
    current_bid = positive_number(car.get("currentbidprice"))
    minimum_price = positive_number(car.get("minimumprice"))
    if current_bid is not None:
        price_amount, price_kind, price_label = current_bid, "current_bid", "public current bid"
    elif minimum_price is not None:
        price_amount, price_kind, price_label = minimum_price, "starting_bid", "public minimum price"
    else:
        price_amount, price_kind, price_label = None, "unknown", "price not shown"
    registration = clean(car.get("first_reg"))
    year_match = YEAR_RE.search(registration) or YEAR_RE.search(title)
    mileage = nonnegative_int(car.get("mileage"))
    return {
        "id": f"{SOURCE_KEY}:{car_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": f"https://autoauction24.ch/car/{car_id}",
        "title": title,
        "model": clean(car.get("carmodel")),
        "country": "CH",
        "asset_country": "CH",
        "category": "car",
        "category_raw": clean(car.get("body")),
        "year": int(year_match.group(1)) if year_match else None,
        "registration_date": registration,
        "mileage_km": mileage,
        "mileage": mileage,
        "fuel": normalize_fuel(car.get("fuel")),
        "price_amount": price_amount,
        "price_currency": "CHF",
        "price_eur": None,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": "public next-event API",
        "minimum_next_bid": None,
        "seller": SOURCE_NAME,
        "location": clean(car.get("transport_by")),
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": end.isoformat(),
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": "Public AutoAuction24 next-event listing; confirm lot condition, fees, buyer requirements, and import eligibility before bidding.",
        "access_sale_note": "AutoAuction24 auction participation follows the platform buyer process.",
        "auction_status": "active",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:getnextevent:{event_id}",
        "evidence": "Public AutoAuction24 next-event API record.",
    }


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_snapshot(document: Any, *, now: dt.datetime, observed_at: str) -> Snapshot:
    if not isinstance(document, list):
        raise AutoAuction24WatchError("AutoAuction24 next-event response is not a list")
    records: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    seen_ids: set[int] = set()
    event_ids: list[int] = []
    for event in document:
        if not isinstance(event, dict):
            raise AutoAuction24WatchError("AutoAuction24 event is not an object")
        event_id = require_positive_int(event.get("id"), field="event ID")
        if event_id in event_ids:
            raise AutoAuction24WatchError("AutoAuction24 response has duplicate event IDs")
        if event.get("complete") is True:
            raise AutoAuction24WatchError("AutoAuction24 next-event response contains a completed event")
        event_ids.append(event_id)
        cars = event.get("cars")
        if not isinstance(cars, list):
            raise AutoAuction24WatchError("AutoAuction24 event has no cars array")
        for car in cars:
            if not isinstance(car, dict):
                raise AutoAuction24WatchError("AutoAuction24 car is not an object")
            car_id = require_positive_int(car.get("id"), field="car ID")
            if car_id in seen_ids:
                raise AutoAuction24WatchError("AutoAuction24 response has duplicate car IDs")
            seen_ids.add(car_id)
            car_event_id = car.get("eventid")
            if car_event_id not in (None, "") and require_positive_int(car_event_id, field="car event ID") != event_id:
                raise AutoAuction24WatchError("AutoAuction24 car belongs to the wrong event")
            title = title_from_car(car)
            if not title:
                raise AutoAuction24WatchError("AutoAuction24 car has no title")
            end = parse_end(car.get("auction_end_time"))
            body = clean(car.get("body"))
            records.append({
                "event_id": event_id,
                "car_id": car_id,
                "title": title,
                "body": body,
                "seats": nonnegative_int(car.get("number_of_seats")),
                "end": end.isoformat(),
            })
            reason = passenger_exclusion_reason(car, title)
            if reason:
                exclusions[reason] += 1
                continue
            if end <= now:
                exclusions["already_ended"] += 1
                continue
            rows.append(car_row(
                car, car_id=car_id, event_id=event_id, title=title,
                end=end, observed_at=observed_at,
            ))
    records.sort(key=lambda value: value["car_id"])
    rows.sort(key=lambda value: value["id"])
    if len(records) != len(seen_ids):
        raise AutoAuction24WatchError("AutoAuction24 raw-card coverage is invalid")
    return Snapshot(
        raw_count=len(records),
        event_ids=tuple(sorted(event_ids)),
        fingerprint=canonical_sha256(records),
        rows=rows,
        exclusions=dict(sorted(exclusions.items())),
    )


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, status=3, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))
    return session


def fetch_document(session: requests.Session, *, timeout: int) -> Any:
    response = session.get(API_URL, headers=HEADERS, timeout=timeout)
    try:
        response.raise_for_status()
        return response.json()
    except ValueError as exc:
        raise AutoAuction24WatchError("AutoAuction24 next-event response is not JSON") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    if timeout < 5:
        raise ValueError("invalid AutoAuction24 timeout")
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    observed_at = current.isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        first = parse_snapshot(fetch_document(active_session, timeout=timeout), now=current, observed_at=observed_at)
        second = parse_snapshot(fetch_document(active_session, timeout=timeout), now=current, observed_at=observed_at)
    finally:
        if supplied_session is None:
            active_session.close()
    if first.raw_count != second.raw_count or first.event_ids != second.event_ids or first.fingerprint != second.fingerprint:
        raise AutoAuction24SnapshotChanged("AutoAuction24 next-event membership changed during reconciliation")
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public car record in the next-event API",
        "declared": second.raw_count,
        "visited": second.raw_count,
        "normalized_rows": len(second.rows),
        "source_excluded": second.exclusions,
        "event_count": len(second.event_ids),
        "event_ids": list(second.event_ids),
        "two_pass_membership_verified": True,
        "membership_sha256": second.fingerprint,
        "publication_ready": False,
    }
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": observed_at,
        "research_only": True,
        "publication_status": "review_required",
        "row_count": len(second.rows),
        "rows": second.rows,
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
    parser = argparse.ArgumentParser(description="Fetch every public AutoAuction24 next-event car")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    print(json.dumps({
        "result": "AUTOAUCTION24_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": payload["source_reports"][SOURCE_KEY]["declared"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
