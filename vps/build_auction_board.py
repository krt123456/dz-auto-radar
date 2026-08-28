# -*- coding: utf-8 -*-
"""Auction lane builder — dark foundation (not yet wired into publish).

Builds the optional `auction_lane` from the positive auction source registry
(auction_registry.py). Design contract (RADAR_AUCTION_DISCOVERY_20260815.md):
  - lane built ONLY from positive registry entries; never from source-name
    substring or blank auction semantics;
  - canonical UTC end, current EUR bid, first/last seen, link validation,
    access/sale semantics, source-evidence key per row;
  - fail closed: missing registry entry, domain mismatch, malformed/naive/expired
    end, hidden price, or ambiguous auction semantics => row excluded from the
    auction lane (never dropped from the universe, never labelled invalid);
  - no ID or URL may appear in both the regular lane and the auction lane;
  - auction sorts: ending soon, lowest current EUR bid, newest vetted source.
  - observed savings/discount are NEVER called profit or ROI (executive rule).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from auction_registry import (
    auction_source_by_key,
    auction_source_publication_status,
    auction_url_matches_source,
    source_has_explicit_auction_semantics,
    registry_digest_json,
)
from source_identity import source_key as canonical_source_key


def canonical_sha256(value: Any) -> str:
    """Byte-identical to the regular lane's public id hashing (Rule 4)."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    """Publish JSON snapshots atomically so the dashboard never reads a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def public_offer_id(source: Any, native_listing_id: Any) -> str:
    """The same public ID the regular lane would mint for this listing."""
    identity = [
        canonical_source_key(source),
        str(native_listing_id if native_listing_id is not None else "").strip(),
    ]
    return canonical_sha256(identity)

END_SOON_HOURS = 24
UTC = dt.timezone.utc
MONITORED_SCHEMA_VERSION = 1
MONITORED_LANE = "official_auction_watch"
MONITORED_MAX_AGE = dt.timedelta(hours=8)
MONITORED_PRICE_KINDS = frozenset({
    "current_bid", "starting_bid", "minimum_bid", "guide_price",
    "sealed_bid", "hidden", "unknown", "minimum_offer", "base_price",
})
MONITORED_ELIGIBILITY = frozenset({
    "eligible", "review_required", "not_eligible", "conditional", "unknown",
})
SCHENGEN_COUNTRIES = frozenset({
    "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IS", "IT", "LI", "LT", "LU", "LV",
    "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
})
SOURCE_COUNTRY_OVERRIDES = {
    # Exleasingcar presents one cross-border inventory under a single public
    # catalogue; the asset country is read from each card's official flag.
    "exleasingcar": SCHENGEN_COUNTRIES,
    # Vavato's public Cars category combines Belgian, Dutch, French, and
    # German assets; country comes from the public lot card when present.
    "vavato": SCHENGEN_COUNTRIES,
    "justiz-auktion": frozenset({"DE", "AT"}),
    "retrade": frozenset({"DK", "FI", "NO", "SE"}),
}
# Founder directive (mgr-fb167017e21a4f598f763b1211af2888): the auction lane
# shows ONLY model years 2023-2026 (cars eligible for import to Algeria in
# 2026, i.e. not more than ~3 years old), and year 2023 rows additionally
# require a full day+month registration date.
FOUNDER_MIN_YEAR = 2023
FOUNDER_MAX_YEAR = 2026
_REG_DATE_RE = re.compile(
    r"^\s*(?:\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2})\s*$"
)
_ISO_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?"
    r"(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?\s*$"
)


def parse_canonical_end(value: Any) -> Optional[dt.datetime]:
    """Return aware UTC datetime or None (fail closed) for malformed/naive ends."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _ISO_RE.match(text)
    if not m:
        return None
    year, month, day, hour, minute = (int(m.group(i)) for i in range(1, 6))
    second = int(m.group(6) or 0)
    tz_group = m.group(7) or ""
    if not tz_group:
        return None  # naive timestamps fail closed: never assume a zone
    if tz_group.upper() == "Z":
        tz = UTC
    else:
        sign = 1 if tz_group[0] == "+" else -1
        body = tz_group[1:].replace(":", "")
        tz = dt.timezone(sign * dt.timedelta(hours=int(body[:2]), minutes=int(body[2:4] or 0)))
    try:
        return dt.datetime(year, month, day, hour, minute, second, tzinfo=tz).astimezone(UTC)
    except ValueError:
        return None


def import_eligible_fuel(value: Any) -> bool:
    """Algeria decree 23-74: electric, petrol, or petrol/electric hybrid only."""
    fuel = str(value or "").strip().casefold()
    if not fuel:
        return False
    if any(token in fuel for token in ("diesel", "gazole", "gasóleo", "tdi", "cdi")):
        return False
    if any(token in fuel for token in ("electric", "elektro", "électri", "electri")):
        if "hybrid" in fuel or "hybride" in fuel:
            return any(token in fuel for token in ("petrol", "benzin", "essence", "gasoline"))
        return True
    return any(token in fuel for token in ("petrol", "benzin", "essence", "gasoline"))




def founder_eligible(
    year: Any, raw_json: str, *, now: Optional[dt.datetime] = None
) -> tuple[bool, str]:
    """Founder lane policy (mgr-fb1670...): years 2023-2026 only; year 2023
    additionally requires a full day+month registration date.

    Reason codes (never silently dropped, never labelled invalid):
      year_outside_2023_2026      - missing/zero year or outside 2023-2026
      year_2023_without_day_month - year 2023 but first_registration_date is
                                    absent or not a full DD.MM.YYYY date
      registration_older_than_three_years - exact registration exceeds the
                                            rolling three-year legal limit
    """
    try:
        y = int(year)
    except (TypeError, ValueError):
        return False, "year_outside_2023_2026"
    if y < FOUNDER_MIN_YEAR or y > FOUNDER_MAX_YEAR:
        return False, "year_outside_2023_2026"
    if y == 2023:
        now = now or dt.datetime.now(UTC)
        reg = ""
        if raw_json:
            try:
                reg = str(json.loads(raw_json).get("first_registration_date") or "")
            except (ValueError, TypeError):
                reg = ""
        if not _REG_DATE_RE.match(reg):
            return False, "year_2023_without_day_month"
        registered = None
        for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                registered = dt.datetime.strptime(reg.strip(), date_format).date()
                break
            except ValueError:
                pass
        if registered is None:
            return False, "year_2023_without_day_month"
        today = now.astimezone(UTC).date()
        try:
            cutoff = today.replace(year=today.year - 3)
        except ValueError:  # 29 February -> 28 February three years earlier
            cutoff = today.replace(year=today.year - 3, day=28)
        if registered < cutoff:
            return False, "registration_older_than_three_years"
    return True, ""

def lane_query() -> str:
    return """
        SELECT source, source_listing_id, source_url, title, make_model, country,
               price_eur, year, mileage_km, fuel, seller_type, last_seen_at,
               raw_json
        FROM offers INDEXED BY idx_offers_last_seen
        WHERE last_seen_at >= ?
        ORDER BY last_seen_at DESC, id
    """


def auction_rows(
    connection: sqlite3.Connection,
    *,
    cutoff: str,
    regular_lane_ids: frozenset[str] = frozenset(),
    regular_lane_urls: frozenset[str] = frozenset(),
    now: Optional[dt.datetime] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Yield lane rows; every exclusion is counted with its reason code."""
    now = now or dt.datetime.now(UTC)
    rows: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    seen_origins: set[tuple[str, str]] = set()

    def excluded(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    for raw in connection.execute(lane_query(), (cutoff,)):
        source, source_listing_id, raw_url, title, model, country, price, year, \
            mileage, fuel, seller, last_seen, raw_json = raw
        source = " ".join(str(source or "").split())
        url = str(raw_url or "").strip()
        raw_json = str(raw_json or "")
        listing_id = str(source_listing_id or "").strip()

        if not listing_id or not url:
            excluded("missing_identity_or_url")
            continue
        reg = auction_source_by_key(source)
        if reg is None:
            excluded("not_in_registry")
            continue
        if auction_source_publication_status(reg.key) not in {"accepted", "migration"}:
            excluded("source_not_publishable")
            continue
        if not auction_url_matches_source(url, reg.key):
            excluded("domain_mismatch")
            continue
        country_code = str(country or "").strip().upper()
        allowed_countries = SOURCE_COUNTRY_OVERRIDES.get(
            reg.key, frozenset({reg.country.upper()})
        )
        if country_code not in SCHENGEN_COUNTRIES or country_code not in allowed_countries:
            excluded("country_mismatch")
            continue
        if not source_has_explicit_auction_semantics(source, raw_json):
            excluded("no_explicit_auction_semantics")
            continue
        raw_end = None
        raw_payload: Dict[str, Any] = {}
        if raw_json:
            try:
                parsed_payload = json.loads(raw_json)
                if not isinstance(parsed_payload, dict):
                    raise TypeError("raw payload is not an object")
                raw_payload = parsed_payload
                raw_end = raw_payload.get("auction_end_at")
            except (ValueError, TypeError):
                excluded("malformed_raw_json")
                continue
        end = parse_canonical_end(raw_end)
        if end is None:
            excluded("malformed_or_naive_end")
            continue
        if end <= now:
            excluded("already_ended")
            continue
        if raw_payload.get("sale_term_code") != "auction-current-bid":
            excluded("not_confirmed_current_bid")
            continue
        try:
            bid = int(price)
        except (TypeError, ValueError):
            excluded("hidden_or_missing_price")
            continue
        if bid <= 0:
            excluded("hidden_or_missing_price")
            continue
        ok_year, year_reason = founder_eligible(year, raw_json, now=now)
        if not ok_year:
            excluded(year_reason)
            continue
        if not import_eligible_fuel(fuel):
            excluded("fuel_not_import_eligible")
            continue
        origin = (source, listing_id)
        if origin in seen_origins:
            excluded("identity_duplicate")
            continue
        seen_origins.add(origin)

        offer_id = f"{source}:{listing_id}"
        if (
            offer_id in regular_lane_ids
            or public_offer_id(source, listing_id) in regular_lane_ids
            or url in regular_lane_urls
        ):
            excluded("cross_lane_duplicate")
            continue

        ends_soon = (end - now) <= dt.timedelta(hours=END_SOON_HOURS)
        rows.append({
            "id": offer_id,
            "source": source,
            "source_key": source,
            "registry_key": reg.key,
            "registry_priority": reg.priority,
            "url": url,
            "title": " ".join(str(title or "").split()),
            "model": str(model or "").strip(),
            "country": country_code,
            "year": int(year) if year else None,
            "mileage": int(mileage) if mileage else None,
            "fuel": str(fuel or "").strip(),
            "seller": str(seller or "").strip(),
            "current_bid_eur": bid,
            "canonical_end_utc": end.isoformat(),
            "ends_soon": ends_soon,
            "first_seen_at": None,
            "last_seen_at": str(last_seen or ""),
            "access_sale_note": _access_sale_note(reg.country),
            "evidence": reg.evidence,
        })
    rows.sort(key=_lane_sort_key)
    return rows, counts


def _access_sale_note(country: str) -> str:
    notes = {
        "de": "Official customs/justice auctions; public registration, vetted by evidence key.",
        "fr": "Official state auctions; per-lot professional restrictions may apply.",
        "nl": "Government surplus via onlineveilingmeester; public/private/business registration.",
        "pl": "Court enforcement portal; low starting prices possible.",
        "be": "Federal seized/customs/surplus; some professional-only events.",
        "it": "Ministry of Justice portal; bidding may hand off to registered sale managers.",
        "es": "Official BOE auctions; identity/representation requirements apply.",
        "se": "Swedish enforcement authority auctions.",
        "pt": "Official judicial auctions.",
        "fi": "Large inventory; international EU-company flow documented.",
    }
    return notes.get(country, "Official auction venue; verify access before bidding.")


def _lane_sort_key(row: Dict[str, Any]) -> tuple:
    end = dt.datetime.fromisoformat(row["canonical_end_utc"])
    return (
        0 if row["ends_soon"] else 1,
        end.timestamp(),
        row["current_bid_eur"],
        row["registry_priority"],
    )


def _positive_number(value: Any) -> Optional[int | float]:
    """Return a finite positive price without turning a hidden price into zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (number > 0 and number < float("inf")):
        return None
    return int(number) if number.is_integer() else number


def _strict_monitored_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a strict row into the broader monitored-auction display contract."""
    return {
        **row,
        "price_eur": row["current_bid_eur"],
        "price_kind": "current_bid",
        "price_currency": "EUR",
        "price_amount": row["current_bid_eur"],
        "price_label": "current bid",
        "bid_visibility": "public",
        "registration_date": "",
        "sale_end_utc": row["canonical_end_utc"],
        "eligibility_status": "eligible",
        "eligibility_reason": (
            "Passed the strict source, live-price, end-time, age, fuel, and "
            "cross-lane gates; final bidder/lot checks still apply."
        ),
    }


def _normalize_monitored_row(
    value: Any,
    *,
    generated_at: dt.datetime,
) -> tuple[Optional[Dict[str, Any]], str]:
    """Validate a broad-watch row while refusing to promote it to strict eligible.

    Connector assertions are useful evidence, but only ``auction_rows`` owns the
    strict-eligible decision.  A broad row may therefore remain review-required
    or explicitly not eligible; an input claim of ``eligible`` is downgraded.
    """
    if not isinstance(value, dict):
        return None, "not_an_object"
    source = " ".join(str(value.get("source_key") or value.get("source") or "").split())
    row_id = str(value.get("id") or "").strip()
    url = str(value.get("url") or "").strip()
    title = " ".join(str(value.get("title") or "").split())
    if not source or not row_id or not url or not title:
        return None, "missing_identity"
    registry = auction_source_by_key(source)
    if registry is None:
        return None, "not_in_registry"
    adapter_authorized = value.get("adapter_authorized") is True
    if (
        auction_source_publication_status(registry.key) not in {"accepted", "migration"}
        and not adapter_authorized
    ):
        return None, "source_not_publishable"
    if not auction_url_matches_source(url, registry.key):
        return None, "domain_mismatch"
    country = str(value.get("country") or registry.country).strip().upper()
    allowed_countries = SOURCE_COUNTRY_OVERRIDES.get(
        registry.key, frozenset({registry.country.upper()})
    )
    if country not in SCHENGEN_COUNTRIES or country not in allowed_countries:
        return None, "country_mismatch"

    input_price_kind = str(value.get("price_kind") or "unknown").strip().lower()
    if input_price_kind not in MONITORED_PRICE_KINDS:
        return None, "bad_price_kind"
    price_kind = {
        "minimum_offer": "minimum_bid",
        "base_price": "starting_bid",
    }.get(input_price_kind, input_price_kind)
    price_currency = str(value.get("price_currency") or "EUR").strip().upper()
    price_amount = _positive_number(value.get("price_amount"))
    price = _positive_number(value.get("price_eur"))
    if price is None and price_currency == "EUR":
        price = price_amount
    if (
        price_kind in {"current_bid", "starting_bid", "minimum_bid", "guide_price"}
        and price is None
        and price_amount is None
    ):
        return None, "priced_kind_without_price"
    if price_kind in {"hidden", "unknown"}:
        price = None

    raw_end = (
        value.get("canonical_end_utc")
        or value.get("sale_end_utc")
        or value.get("sale_end_at")
        or value.get("sale_end")
    )
    end = parse_canonical_end(raw_end) if raw_end not in (None, "") else None
    if raw_end not in (None, "") and end is None:
        return None, "bad_end"
    if end is not None and end <= generated_at:
        return None, "already_ended"
    raw_event = value.get("sale_event_utc") or value.get("scheduled_sale_at")
    event = parse_canonical_end(raw_event) if raw_event not in (None, "") else None
    if raw_event not in (None, "") and event is None:
        return None, "bad_sale_event"

    last_seen = parse_canonical_end(value.get("last_seen_at") or value.get("observed_at_utc"))
    if last_seen is None:
        return None, "bad_last_seen"
    if last_seen > generated_at + dt.timedelta(minutes=5):
        return None, "future_last_seen"
    if generated_at - last_seen > MONITORED_MAX_AGE:
        return None, "stale_last_seen"
    input_status = str(value.get("eligibility_status") or "review_required").strip().lower()
    if input_status not in MONITORED_ELIGIBILITY:
        return None, "bad_eligibility_status"
    status = {
        "conditional": "review_required",
        "unknown": "review_required",
    }.get(input_status, input_status)
    if status == "eligible":
        status = "review_required"
    reason = " ".join(str(value.get("eligibility_reason") or "").split())
    if not reason:
        reason = "Strict Algerian import and bidder eligibility have not both been verified."

    def optional_int(key: str) -> Optional[int]:
        raw = value.get(key)
        if raw in (None, "") or isinstance(raw, bool):
            return None
        try:
            number = int(raw)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    return {
        "id": row_id,
        "source": registry.key,
        "source_key": registry.key,
        "registry_key": registry.key,
        "registry_priority": registry.priority,
        "url": url,
        "title": title,
        "model": " ".join(str(value.get("model") or "").split()),
        "country": country,
        "asset_country": country,
        "category": " ".join(str(value.get("category") or "unknown").split()).lower(),
        "category_raw": " ".join(str(value.get("category_raw") or "").split()).lower(),
        "property_type": " ".join(str(value.get("property_type") or "").split()).lower(),
        "year": optional_int("year"),
        "mileage": optional_int("mileage") if value.get("mileage") not in (None, "") else optional_int("mileage_km"),
        "fuel": " ".join(str(value.get("fuel") or "").split()),
        "seller": " ".join(str(value.get("seller") or "").split()),
        "location": " ".join(str(value.get("location") or "").split()),
        "price_eur": price,
        "price_kind": price_kind,
        "price_currency": price_currency,
        "price_amount": price_amount if price_amount is not None else (
            price if price_currency == "EUR" else None
        ),
        "price_label": " ".join(str(value.get("price_label") or "").split()),
        "bid_visibility": " ".join(str(value.get("bid_visibility") or "").split()),
        "bid_count": optional_int("bid_count"),
        "minimum_next_bid": _positive_number(value.get("minimum_next_bid")),
        "reserve_met": value.get("reserve_met") if isinstance(value.get("reserve_met"), bool) else None,
        "no_reserve": value.get("no_reserve") if isinstance(value.get("no_reserve"), bool) else None,
        "sale_terms": " ".join(str(value.get("sale_terms") or "").split()),
        "auction_status": " ".join(str(value.get("auction_status") or "").split()),
        "registration_date": " ".join(str(value.get("registration_date") or "").split()),
        "canonical_end_utc": end.isoformat() if end else None,
        "sale_end_utc": end.isoformat() if end else None,
        "sale_event_utc": event.isoformat() if event else None,
        "ends_soon": bool(end and end - generated_at <= dt.timedelta(hours=END_SOON_HOURS)),
        "first_seen_at": value.get("first_seen_at"),
        "last_seen_at": last_seen.isoformat(),
        "eligibility_status": status,
        "eligibility_reason": reason,
        "access_sale_note": " ".join(str(
            value.get("access_sale_note") or _access_sale_note(registry.country)
        ).split()),
        "raw_evidence_ref": " ".join(str(value.get("raw_evidence_ref") or "").split()),
        "publication_attribution": " ".join(str(
            value.get("publication_attribution") or ""
        ).split()),
        "publication_license": " ".join(str(
            value.get("publication_license") or ""
        ).split()),
        "publication_license_url": " ".join(str(
            value.get("publication_license_url") or ""
        ).split()),
        "adapter_authorized": adapter_authorized,
        "evidence": registry.evidence,
    }, ""


def monitored_rows(
    strict_rows: Sequence[Dict[str, Any]],
    input_paths: Sequence[Path],
    *,
    generated_at: str,
) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Merge strict rows with fail-closed broad-watch JSON connector outputs.

    Input contract (one or more files): ``schema_version=1``, an aware
    ``generated_at_utc``, ``row_count``, and ``rows``.  Strict rows always win
    on duplicate id or URL, so a connector cannot weaken an eligibility label.
    """
    generated = parse_canonical_end(generated_at)
    if generated is None:
        raise RuntimeError("monitored auction generation timestamp is invalid")
    output = [_strict_monitored_row(row) for row in strict_rows]
    seen_ids = {str(row["id"]) for row in output}
    seen_urls = {str(row["url"]) for row in output}
    rejected: Dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for path in input_paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            reject("input_unreadable")
            continue
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != MONITORED_SCHEMA_VERSION
            or document.get("lane") != MONITORED_LANE
        ):
            reject("input_bad_schema")
            continue
        source_generated = parse_canonical_end(document.get("generated_at_utc"))
        raw_rows = document.get("rows")
        if source_generated is None or not isinstance(raw_rows, list):
            reject("input_bad_metadata")
            continue
        if document.get("row_count", len(raw_rows)) != len(raw_rows):
            reject("input_count_mismatch")
            continue
        if source_generated > generated + dt.timedelta(minutes=5):
            reject("input_future")
            continue
        if generated - source_generated > MONITORED_MAX_AGE:
            reject("input_stale")
            continue
        for raw in raw_rows:
            row, reason = _normalize_monitored_row(raw, generated_at=generated)
            if row is None:
                reject(reason)
                continue
            if row["id"] in seen_ids or row["url"] in seen_urls:
                reject("duplicate")
                continue
            seen_ids.add(row["id"])
            seen_urls.add(row["url"])
            output.append(row)
    output.sort(key=lambda row: (
        parse_canonical_end(row.get("canonical_end_utc")) or dt.datetime.max.replace(tzinfo=UTC),
        row["registry_priority"],
        row["id"],
    ))
    return output, rejected


def build_monitored_watch(
    rows: Sequence[Dict[str, Any]],
    *,
    generated_at: str,
    rejected_counts: Dict[str, int],
    connector_reports: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Create the standalone same-origin broad-watch publication artifact."""
    source_counts: Dict[str, Dict[str, int]] = {}
    for row in rows:
        source = str(row.get("source_key") or row.get("source") or "")
        counters = source_counts.setdefault(
            source, {"row_count": 0, "eligible": 0, "review_required": 0, "not_eligible": 0},
        )
        counters["row_count"] += 1
        status = str(row.get("eligibility_status") or "review_required")
        if status in counters:
            counters[status] += 1
    connector_reports = connector_reports or {}
    all_sources = sorted(set(source_counts) | set(connector_reports))
    reports = []
    for source in all_sources:
        counters = source_counts.get(
            source,
            {"row_count": 0, "eligible": 0, "review_required": 0, "not_eligible": 0},
        )
        reports.append({"source": source, **counters, **connector_reports.get(source, {})})
    return {
        "schema_version": MONITORED_SCHEMA_VERSION,
        "lane": MONITORED_LANE,
        "registry_digest": registry_digest_json(),
        "generated_at_utc": generated_at,
        "row_count": len(rows),
        "source_reports": reports,
        "rejected_counts": dict(sorted(rejected_counts.items())),
        "rows": list(rows),
    }


def connector_report_summary(input_paths: Sequence[Path]) -> Dict[str, Dict[str, Any]]:
    """Preserve a small public health summary, including zero/error sources."""
    output: Dict[str, Dict[str, Any]] = {}
    count_keys = (
        "current_or_future_rows", "vehicle_rows", "current_or_future",
        "accepted", "open_drz_vehicle_rows", "car_and_van_rows",
        "normalized_active", "catalogue_total", "discovered_unique",
        "current_or_future_vehicle_rows",
    )
    for path in input_paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(document, dict):
            continue
        generated = document.get("generated_at_utc")
        raw_reports = document.get("source_reports")
        entries: List[tuple[str, Any]] = []
        if isinstance(raw_reports, dict):
            entries = [(str(key), value) for key, value in raw_reports.items()]
        elif isinstance(raw_reports, list):
            entries = [
                (str(value.get("source") or ""), value)
                for value in raw_reports if isinstance(value, dict)
            ]
        for source, raw in entries:
            registry = auction_source_by_key(source)
            if registry is None or not isinstance(raw, dict):
                continue
            error = str(raw.get("error") or "").strip()
            errors = raw.get("errors")
            if not error and isinstance(errors, list) and errors:
                error = "; ".join(str(value) for value in errors[:2])
            status = str(raw.get("status") or raw.get("connector_status") or "").strip().lower()
            if error:
                status = "error"
            elif status not in {"ok", "partial", "blocked", "error"}:
                status = "ok"
            declared_rows: Optional[int] = None
            for key in count_keys:
                value = raw.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    declared_rows = value
                    break
            output[source] = {
                "connector_status": status,
                "connector_generated_at_utc": generated,
                "connector_declared_rows": declared_rows,
                "connector_error": error[:300] or None,
                "connector_capture_id": str(raw.get("raw_capture_id") or "")[:100] or None,
            }
    return output


def build_lane(
    database: Path,
    *,
    cutoff: str,
    regular_lane_ids: frozenset[str],
    regular_lane_urls: frozenset[str],
    generated_at: Optional[str] = None,
    monitored_generated_at: Optional[str] = None,
    monitored_inputs: Sequence[Path] = (),
) -> Dict[str, Any]:
    """Read-only build: connect, read offers via registry lane, return payload."""
    if generated_at is None:
        generated_at = dt.datetime.now(UTC).isoformat()
    if monitored_generated_at is None:
        monitored_generated_at = generated_at

    def connect(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
        con.execute("PRAGMA busy_timeout=120000")
        con.row_factory = sqlite3.Row
        return con

    con = connect(database)
    try:
        rows, counts = auction_rows(
            con, cutoff=cutoff,
            regular_lane_ids=regular_lane_ids, regular_lane_urls=regular_lane_urls,
        )
    finally:
        con.close()
    broad_rows, broad_rejected = monitored_rows(
        rows, monitored_inputs, generated_at=monitored_generated_at,
    )
    return {
        "schema_version": 1,
        "lane": "auction",
        "registry_digest": registry_digest_json(),
        "generated_at_utc": generated_at,
        "lane_count": len(rows),
        "excluded_counts": counts,
        "rows": rows,
        "monitored_schema_version": MONITORED_SCHEMA_VERSION,
        "monitored_generated_at_utc": monitored_generated_at,
        "monitored_count": len(broad_rows),
        "monitored_rejected_counts": broad_rejected,
        "monitored_rows": broad_rows,
    }


def load_regular_board(board_path: Optional[Path]) -> tuple[frozenset[str], frozenset[str]]:
    """Extract the regular lane's public ids and urls from the accepted board.

    Never invented: the board.json written by build_observed_value_board.py is
    the authoritative accepted snapshot of the regular lane. Any auction row
    whose id or url already lives there is a cross-lane duplicate.
    """
    if board_path is None or not board_path.is_file():
        return frozenset(), frozenset()
    try:
        board = json.loads(board_path.read_text(encoding="utf-8"))
        offers = board.get("offers")
        if not isinstance(offers, list):
            raise ValueError("board offers are not a list")
    except (ValueError, OSError, TypeError) as exc:
        raise RuntimeError(f"regular board is unusable for cross-lane dedupe: {exc}") from exc
    ids: set[str] = set()
    urls: set[str] = set()
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        if isinstance(offer.get("id"), str) and offer["id"]:
            ids.add(offer["id"])
        url = str(offer.get("u") or "").strip()
        if url:
            urls.add(url)
    return frozenset(ids), frozenset(urls)


def normalize_lane_url(url: str) -> str:
    m = re.match(r"^https?://([^/\s]+)", url)
    if not m:
        return ""
    host = m.group(1).lower()
    return f"https://{host}/"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path,
                        default=Path("/home/krt/car_deal_finder/universe_offers.sqlite"))
    parser.add_argument("--cutoff", default=None,
                        help="last_seen_at cutoff (ISO); default 30 days ago")
    parser.add_argument("--board", type=Path, default=None,
                        help="regular lane board.json for cross-lane dedupe")
    parser.add_argument("--generated-at", default=None,
                        help="bind the lane to this board generation (ISO)")
    parser.add_argument("--output", type=Path, default=Path("/tmp/auction_lane.json"))
    parser.add_argument("--max-observation-age-days", type=int, default=30)
    parser.add_argument(
        "--monitored-input", type=Path, action="append", default=[],
        help="repeatable broad official-auction watch JSON (schema version 1)",
    )
    parser.add_argument(
        "--monitored-output", type=Path, default=None,
        help="standalone same-origin watch artifact; defaults beside --output",
    )
    args = parser.parse_args()

    cutoff = args.cutoff or (
        dt.datetime.now(UTC) - dt.timedelta(days=args.max_observation_age_days)
    ).isoformat()
    regular_ids, regular_urls = load_regular_board(args.board)
    payload = build_lane(args.database, cutoff=cutoff,
                         regular_lane_ids=regular_ids,
                         regular_lane_urls=regular_urls,
                         generated_at=args.generated_at,
                         monitored_generated_at=dt.datetime.now(UTC).isoformat(),
                         monitored_inputs=args.monitored_input)
    atomic_write_json(args.output, payload)
    # Legacy/main refresh callers build only the encrypted strict lane.  They
    # must not overwrite a fresh broad snapshot with a strict-only file merely
    # because they do not know about the new connector inputs yet.
    if args.monitored_output is not None or args.monitored_input:
        monitored_output = (
            args.monitored_output or args.output.with_name("official_auction_watch.json")
        )
        watch = build_monitored_watch(
            payload["monitored_rows"], generated_at=payload["monitored_generated_at_utc"],
            rejected_counts=payload["monitored_rejected_counts"],
            connector_reports=connector_report_summary(args.monitored_input),
        )
        atomic_write_json(monitored_output, watch)
    print(json.dumps({
        "lane_count": payload["lane_count"],
        "monitored_count": payload["monitored_count"],
        "excluded_counts": payload["excluded_counts"],
        "monitored_rejected_counts": payload["monitored_rejected_counts"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
