#!/usr/bin/env python3
"""Enumerate every public Schengen automobile auction at Ritchie Bros.

The official automobile catalogue is server-rendered and embeds a Next.js
``data.results`` payload.  Its unsorted offset pages can move while they are
being read, so page results are used only to discover candidate item numbers.
Once the bounded discovery union reaches the declared total, every candidate
is fetched twice through the catalogue's exact ``itemNumbers`` filter.  The
connector publishes only when both exact snapshots, stable public identities,
and the catalogue total agree.  It keeps only Schengen-located lots with an
explicit auction format; fixed-price and make-offer cards are counted but
never labelled as an auction.
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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
SOURCE_KEY = "rbauction-eu"
SOURCE_NAME = "Ritchie Bros. Europe"
CATALOGUE_URL = "https://www.rbauction.com/cp/automobile"
# The public endpoint currently caps a page at 120 even when a larger size is
# requested.  Keep the value explicit so page-length checks fail closed if the
# contract changes.
PAGE_SIZE = 120
ITEM_FILTER_BATCH_SIZE = 50
MAX_DISCOVERY_SWEEPS = 8
EXACT_VALIDATION_ROUNDS = 2
# Public catalogue growth must not be silently capped below the scale target.
# This remains an operational guard against a malformed counter, not a 200k
# publication ceiling; the broad-watch architecture will shard before a single
# artifact approaches its host-size limit.
MAX_CATALOGUE_ROWS = 1_000_000
DEFAULT_TIMEOUT = 35
SCHENGEN_COUNTRIES = frozenset({
    "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IS", "IT", "LI", "LT", "LU", "LV",
    "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
})
AUCTION_FORMATS = frozenset({"live auction", "online auction", "sealed bid"})

COUNTRY_CODES = {
    "AT": "AT", "AUT": "AT", "AUSTRIA": "AT",
    "BE": "BE", "BEL": "BE", "BELGIUM": "BE",
    "BG": "BG", "BGR": "BG", "BULGARIA": "BG",
    "CH": "CH", "CHE": "CH", "SWITZERLAND": "CH",
    "CZ": "CZ", "CZE": "CZ", "CZECH REPUBLIC": "CZ",
    "DE": "DE", "DEU": "DE", "GERMANY": "DE",
    "DK": "DK", "DNK": "DK", "DENMARK": "DK",
    "EE": "EE", "EST": "EE", "ESTONIA": "EE",
    "ES": "ES", "ESP": "ES", "SPAIN": "ES",
    "FI": "FI", "FIN": "FI", "FINLAND": "FI",
    "FR": "FR", "FRA": "FR", "FRANCE": "FR",
    "GR": "GR", "GRC": "GR", "GREECE": "GR",
    "HR": "HR", "HRV": "HR", "CROATIA": "HR",
    "HU": "HU", "HUN": "HU", "HUNGARY": "HU",
    "IS": "IS", "ISL": "IS", "ICELAND": "IS",
    "IT": "IT", "ITA": "IT", "ITALY": "IT",
    "LI": "LI", "LIE": "LI", "LIECHTENSTEIN": "LI",
    "LT": "LT", "LTU": "LT", "LITHUANIA": "LT",
    "LU": "LU", "LUX": "LU", "LUXEMBOURG": "LU",
    "LV": "LV", "LVA": "LV", "LATVIA": "LV",
    "MT": "MT", "MLT": "MT", "MALTA": "MT",
    "NL": "NL", "NLD": "NL", "NETHERLANDS": "NL",
    "NO": "NO", "NOR": "NO", "NORWAY": "NO",
    "PL": "PL", "POL": "PL", "POLAND": "PL",
    "PT": "PT", "PRT": "PT", "PORTUGAL": "PT",
    "RO": "RO", "ROU": "RO", "ROMANIA": "RO",
    "SE": "SE", "SWE": "SE", "SWEDEN": "SE",
    "SI": "SI", "SVN": "SI", "SLOVENIA": "SI",
    "SK": "SK", "SVK": "SK", "SLOVAKIA": "SK",
    "US": "US", "USA": "US", "UNITED STATES": "US",
    "CA": "CA", "CAN": "CA", "CANADA": "CA",
    "MX": "MX", "MEX": "MX", "MEXICO": "MX",
    "AU": "AU", "AUS": "AU", "AUSTRALIA": "AU",
    "AE": "AE", "ARE": "AE", "UNITED ARAB EMIRATES": "AE",
    "JP": "JP", "JPN": "JP", "JAPAN": "JP",
}

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}


class RitchieBrosWatchError(RuntimeError):
    """The public Ritchie Bros automobile catalogue could not be reconciled."""


class RitchieBrosSnapshotChanged(RitchieBrosWatchError):
    """A mutable catalogue boundary made one enumeration incomplete."""


@dataclass(frozen=True)
class CataloguePass:
    total: int
    pages: int
    all_ids: tuple[str, ...]
    identity_sha256: str
    publication_sha256: str
    rows: tuple[dict[str, Any], ...]
    rejected_counts: dict[str, int]
    discovery_sweeps: int
    discovery_page_fetches: int
    discovery_incomplete_sweeps: int
    discovery_duplicate_records: int
    validation_batches: int

    @property
    def fingerprint(self) -> tuple[int, tuple[str, ...], str, str]:
        return (
            self.total,
            self.all_ids,
            self.identity_sha256,
            self.publication_sha256,
        )


@dataclass(frozen=True)
class DiscoveryResult:
    total: int
    pages: int
    identities: dict[str, tuple[str, str, str]]
    sweeps: int
    page_fetches: int
    incomplete_sweeps: int
    duplicate_records: int


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def ascii_fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", clean(value))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


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
    if isinstance(value, str):
        compact = re.sub(r"[^0-9,.-]", "", value)
        if "," in compact and "." in compact:
            compact = compact.replace(".", "").replace(",", ".")
        elif "," in compact:
            compact = compact.replace(",", ".")
        value = compact
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return int(result) if result.is_integer() else result


def country_code(value: Any) -> str:
    return COUNTRY_CODES.get(ascii_fold(value).upper(), "")


def epoch_millis(value: Any, *, field: str) -> dt.datetime:
    amount = positive_number(value)
    if amount is None or amount < 946_684_800_000:
        raise RitchieBrosWatchError(f"Ritchie Bros listing has invalid {field}")
    try:
        return dt.datetime.fromtimestamp(float(amount) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise RitchieBrosWatchError(f"Ritchie Bros listing has invalid {field}") from exc


def slugify(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_fold(value)).strip("-")
    return slug or "automobile"


def normalize_fuel(value: Any) -> str:
    folded = ascii_fold(value)
    diesel = bool(re.search(r"\b(?:diesel|gazole)\b", folded))
    petrol = bool(re.search(r"\b(?:petrol|gasoline|essence|benzine|bensin)\b", folded))
    hybrid = "hybrid" in folded
    electric = bool(re.search(r"\b(?:electric|bev|ev)\b", folded))
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
    if re.search(r"\b(?:lpg|gpl|cng|lng)\b", folded):
        return "gas"
    return "unknown"


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


def parse_catalogue_results(markup: str) -> tuple[int, list[dict[str, Any]]]:
    soup = BeautifulSoup(markup, "html.parser")
    node = soup.find("script", id="__NEXT_DATA__")
    if node is None or not node.string:
        raise RitchieBrosWatchError("Ritchie Bros catalogue has no public Next data")
    try:
        payload = json.loads(node.string)
        results = payload["props"]["pageProps"]["data"]["results"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RitchieBrosWatchError("Ritchie Bros catalogue data has an invalid shape") from exc
    if not isinstance(results, dict):
        raise RitchieBrosWatchError("Ritchie Bros catalogue results are not an object")
    total = nonnegative_integer(results.get("totalAmount"))
    returned = nonnegative_integer(results.get("returnedAmount"))
    records = results.get("records")
    if (
        total is None
        or returned is None
        or total > MAX_CATALOGUE_ROWS
        or not isinstance(records, list)
        or not all(isinstance(record, dict) for record in records)
        or returned != len(records)
    ):
        raise RitchieBrosWatchError("Ritchie Bros catalogue pagination metadata is invalid")
    return total, records


def fetch_catalogue(
    session: requests.Session,
    *,
    params: dict[str, int | str],
    timeout: int,
) -> tuple[int, list[dict[str, Any]]]:
    response = session.get(
        CATALOGUE_URL,
        params=params,
        headers=HEADERS,
        timeout=timeout,
    )
    try:
        response.raise_for_status()
        total, records = parse_catalogue_results(response.text)
    finally:
        response.close()
    return total, records


def fetch_page(
    session: requests.Session,
    *,
    offset: int,
    timeout: int,
) -> tuple[int, list[dict[str, Any]]]:
    total, records = fetch_catalogue(
        session,
        params={"from": offset, "size": PAGE_SIZE},
        timeout=timeout,
    )
    expected = max(0, min(PAGE_SIZE, total - offset))
    if len(records) != expected:
        raise RitchieBrosSnapshotChanged(
            f"Ritchie Bros page {offset} declared total {total} but returned "
            f"{len(records)}, expected {expected}"
        )
    return total, records


def record_id(record: dict[str, Any]) -> str:
    native_id = clean(record.get("itemNumber"))
    if not native_id.isdigit():
        raise RitchieBrosWatchError("Ritchie Bros catalogue card has no stable item number")
    return native_id


def record_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    """Return the stable public identity needed to validate a candidate."""
    native_id = record_id(record)
    asset_guid = clean(record.get("assetGUID"))
    listing_id = clean(record.get("listingId"))
    if not asset_guid or not listing_id:
        raise RitchieBrosWatchError(
            f"Ritchie Bros item {native_id} has no stable asset/listing identity"
        )
    return native_id, asset_guid, listing_id


def row_from_record(
    record: dict[str, Any],
    *,
    observed_at: str,
    now: dt.datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    native_id = record_id(record)
    asset_country = country_code(record.get("locationCountry") or record.get("eventCountry"))
    if not asset_country:
        return None, "unknown_asset_country"
    if asset_country not in SCHENGEN_COUNTRIES:
        return None, "non_schengen_asset"
    buying_format = clean(record.get("buyingFormat") or record.get("buyingFormatFacetLabel"))
    if buying_format.casefold() not in AUCTION_FORMATS:
        return None, "non_auction_format"

    title = clean(record.get("assetDescription"))
    if not title:
        manufacturer = clean(record.get("manufacturerLocalized") or record.get("rawManufacturerName"))
        model = clean(record.get("modelLocalized") or record.get("rawModelName"))
        title = clean(f"{manufacturer} {model}")
    if not title:
        raise RitchieBrosWatchError(f"Ritchie Bros item {native_id} has no public title")
    end = epoch_millis(record.get("biddingEndTime"), field="bidding end time")
    if end <= now:
        return None, "already_ended"
    event_value = record.get("eventEndDateTime")
    event = epoch_millis(event_value, field="event end time") if event_value not in (None, "") else None
    currency = clean(record.get("priceCurrency") or "EUR").upper()
    starting_bid = positive_number(record.get("startPrice"))
    if starting_bid is None:
        price_kind = "unknown"
        price_label = "No public Ritchie Bros starting bid is displayed."
        bid_visibility = "not_publicly_disclosed"
    else:
        price_kind = "starting_bid"
        price_label = "Public Ritchie Bros starting bid."
        bid_visibility = "public_starting_bid"
    mileage = positive_number(record.get("usageKilometers"))
    features = clean(record.get("features"))
    fuel = normalize_fuel(f"{title} {features}")
    status_text = clean(record.get("listingStatus")).casefold()
    auction_status = "active" if status_text == "open" else "upcoming"
    location = clean(record.get("locationCity") or record.get("locationName") or record.get("itemSiteName"))
    sale_name = clean(record.get("eventAdvertisedName") or record.get("saleEventName"))
    asset_type = clean(record.get("assetTypeLocalized") or record.get("categoryLocalized") or "Automobile")
    model = clean(record.get("modelLocalized") or record.get("rawModelName") or title)
    return {
        "id": f"{SOURCE_KEY}:{native_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": f"https://www.rbauction.com/pdp/{slugify(title)}/{native_id}",
        "title": title,
        "model": model,
        "country": asset_country,
        "asset_country": asset_country,
        "category": "vehicle",
        "category_raw": asset_type,
        "year": nonnegative_integer(record.get("manufactureYear")),
        "mileage_km": int(mileage) if mileage is not None else None,
        "fuel": fuel,
        "price_amount": starting_bid,
        "price_currency": currency,
        "price_eur": starting_bid if currency == "EUR" else None,
        "price_kind": price_kind,
        "price_label": price_label,
        "bid_visibility": bid_visibility,
        "seller": SOURCE_NAME,
        "location": location,
        "sale_name": sale_name,
        "sale_id": clean(record.get("saleEventID") or record.get("eventPrimarySiteId")),
        "canonical_end_utc": end.isoformat(),
        "sale_end_utc": end.isoformat(),
        "sale_event_utc": event.isoformat() if event else None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public Ritchie Bros auction card; verify bidder registration, vehicle "
            "condition, fees, documents, collection and import terms before bidding."
        ),
        "access_sale_note": "Ritchie Bros bidder registration and lot-specific auction terms apply.",
        "auction_status": auction_status,
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:public-catalogue:{native_id}",
        "evidence": f"Public Ritchie Bros {buying_format} catalogue card.",
    }, None


def digest_lines(lines: list[str]) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def bounded_id_preview(values: set[str]) -> str:
    ordered = sorted(values)
    preview = ",".join(ordered[:5])
    return preview + (",..." if len(ordered) > 5 else "")


def discover_candidates(
    session: requests.Session,
    *,
    timeout: int,
) -> DiscoveryResult:
    """Build a bounded union from mutable offset pages without publishing it."""
    baseline_total: int | None = None
    candidates: dict[str, tuple[str, str, str]] = {}
    page_fetches = 0
    incomplete_sweeps = 0
    duplicate_records = 0

    for sweep in range(1, MAX_DISCOVERY_SWEEPS + 1):
        total, first_records = fetch_page(session, offset=0, timeout=timeout)
        page_fetches += 1
        if baseline_total is None:
            baseline_total = total
        elif total != baseline_total:
            raise RitchieBrosSnapshotChanged(
                "Ritchie Bros catalogue total changed during candidate discovery: "
                f"baseline={baseline_total} observed={total} sweep={sweep} offset=0"
            )

        assert baseline_total is not None
        sweep_ids: set[str] = set()

        def consume(records: list[dict[str, Any]], *, offset: int) -> None:
            nonlocal duplicate_records
            for record in records:
                identity = record_identity(record)
                native_id = identity[0]
                prior = candidates.get(native_id)
                if prior is not None and prior != identity:
                    raise RitchieBrosSnapshotChanged(
                        "Ritchie Bros stable identity changed during candidate discovery: "
                        f"item={native_id} sweep={sweep} offset={offset}"
                    )
                candidates.setdefault(native_id, identity)
                if native_id in sweep_ids:
                    duplicate_records += 1
                else:
                    sweep_ids.add(native_id)

        consume(first_records, offset=0)
        for offset in range(PAGE_SIZE, baseline_total, PAGE_SIZE):
            page_total, records = fetch_page(session, offset=offset, timeout=timeout)
            page_fetches += 1
            if page_total != baseline_total:
                raise RitchieBrosSnapshotChanged(
                    "Ritchie Bros catalogue total changed during candidate discovery: "
                    f"baseline={baseline_total} observed={page_total} "
                    f"sweep={sweep} offset={offset}"
                )
            consume(records, offset=offset)

        if len(sweep_ids) > baseline_total or len(candidates) > baseline_total:
            raise RitchieBrosSnapshotChanged(
                "Ritchie Bros candidate discovery exceeded the declared total: "
                f"declared={baseline_total} sweep_unique={len(sweep_ids)} "
                f"candidate_union={len(candidates)}"
            )
        if len(sweep_ids) != baseline_total:
            incomplete_sweeps += 1
        if len(candidates) == baseline_total:
            return DiscoveryResult(
                total=baseline_total,
                pages=(baseline_total + PAGE_SIZE - 1) // PAGE_SIZE,
                identities=candidates,
                sweeps=sweep,
                page_fetches=page_fetches,
                incomplete_sweeps=incomplete_sweeps,
                duplicate_records=duplicate_records,
            )

    assert baseline_total is not None
    raise RitchieBrosWatchError(
        "Ritchie Bros bounded candidate discovery exhausted: "
        f"sweeps={MAX_DISCOVERY_SWEEPS} declared={baseline_total} "
        f"candidate_union={len(candidates)} incomplete_sweeps={incomplete_sweeps} "
        f"duplicate_records={duplicate_records}"
    )


def fetch_exact_batch(
    session: requests.Session,
    *,
    item_numbers: tuple[str, ...],
    expected_identities: dict[str, tuple[str, str, str]],
    timeout: int,
) -> dict[str, dict[str, Any]]:
    if not item_numbers or len(item_numbers) > ITEM_FILTER_BATCH_SIZE:
        raise RitchieBrosWatchError("Ritchie Bros exact-validation batch is invalid")
    requested = set(item_numbers)
    total, records = fetch_catalogue(
        session,
        params={
            "itemNumbers": ",".join(item_numbers),
            "size": PAGE_SIZE,
        },
        timeout=timeout,
    )
    if total != len(item_numbers) or len(records) != len(item_numbers):
        raise RitchieBrosSnapshotChanged(
            "Ritchie Bros exact item filter returned an incomplete batch: "
            f"requested={len(item_numbers)} declared={total} returned={len(records)}"
        )

    records_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        identity = record_identity(record)
        native_id = identity[0]
        if native_id in records_by_id:
            raise RitchieBrosSnapshotChanged(
                f"Ritchie Bros exact item filter repeated item {native_id}"
            )
        if native_id not in requested:
            raise RitchieBrosSnapshotChanged(
                f"Ritchie Bros exact item filter returned unrequested item {native_id}"
            )
        if expected_identities[native_id] != identity:
            raise RitchieBrosSnapshotChanged(
                f"Ritchie Bros exact item identity changed for item {native_id}"
            )
        records_by_id[native_id] = record

    returned_ids = set(records_by_id)
    if returned_ids != requested:
        missing = bounded_id_preview(requested - returned_ids)
        extra = bounded_id_preview(returned_ids - requested)
        raise RitchieBrosSnapshotChanged(
            "Ritchie Bros exact item filter ID set mismatch: "
            f"missing={missing or '-'} extra={extra or '-'}"
        )
    return records_by_id


def validate_candidates(
    session: requests.Session,
    *,
    discovery: DiscoveryResult,
    timeout: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    all_ids = tuple(sorted(discovery.identities))
    records_by_id: dict[str, dict[str, Any]] = {}
    batches = 0
    for start in range(0, len(all_ids), ITEM_FILTER_BATCH_SIZE):
        item_numbers = all_ids[start:start + ITEM_FILTER_BATCH_SIZE]
        batch = fetch_exact_batch(
            session,
            item_numbers=item_numbers,
            expected_identities=discovery.identities,
            timeout=timeout,
        )
        overlap = set(records_by_id).intersection(batch)
        if overlap:
            raise RitchieBrosSnapshotChanged(
                "Ritchie Bros exact validation repeated IDs across batches: "
                f"{bounded_id_preview(overlap)}"
            )
        records_by_id.update(batch)
        batches += 1
    if set(records_by_id) != set(all_ids):
        raise RitchieBrosSnapshotChanged(
            "Ritchie Bros exact validation did not return the full candidate set"
        )
    return records_by_id, batches


def require_unchanged_total(
    session: requests.Session,
    *,
    expected_total: int,
    phase: str,
    timeout: int,
) -> None:
    observed_total, _ = fetch_page(session, offset=0, timeout=timeout)
    if observed_total != expected_total:
        raise RitchieBrosSnapshotChanged(
            "Ritchie Bros catalogue total changed around exact validation: "
            f"phase={phase} expected={expected_total} observed={observed_total}"
        )


def materialize_validated_snapshot(
    records_by_id: dict[str, dict[str, Any]],
    *,
    discovery: DiscoveryResult,
    observed_at: str,
    now: dt.datetime,
) -> CataloguePass:
    all_ids = tuple(sorted(discovery.identities))
    if set(records_by_id) != set(all_ids):
        raise RitchieBrosSnapshotChanged(
            "Ritchie Bros validated record set no longer matches discovery"
        )

    identity_lines: list[str] = []
    publication_lines: list[str] = []
    rows: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    for native_id in all_ids:
        identity = record_identity(records_by_id[native_id])
        identity_lines.append(json.dumps(identity, ensure_ascii=False, separators=(",", ":")))
        row, reason = row_from_record(
            records_by_id[native_id], observed_at=observed_at, now=now
        )
        if row is not None:
            rows.append(row)
            outcome: list[Any] = [native_id, "row", row]
        elif reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            outcome = [native_id, "rejected", reason]
        else:
            raise RitchieBrosWatchError(
                f"Ritchie Bros item {native_id} produced no publication outcome"
            )
        publication_lines.append(
            json.dumps(outcome, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    return CataloguePass(
        total=discovery.total,
        pages=discovery.pages,
        all_ids=all_ids,
        identity_sha256=digest_lines(identity_lines),
        publication_sha256=digest_lines(publication_lines),
        rows=tuple(sorted(rows, key=lambda row: str(row["id"]))),
        rejected_counts=dict(sorted(rejected.items())),
        discovery_sweeps=discovery.sweeps,
        discovery_page_fetches=discovery.page_fetches,
        discovery_incomplete_sweeps=discovery.incomplete_sweeps,
        discovery_duplicate_records=discovery.duplicate_records,
        validation_batches=0,
    )


def catalogue_pass(
    session: requests.Session,
    *,
    timeout: int,
    observed_at: str,
    now: dt.datetime,
) -> CataloguePass:
    discovery = discover_candidates(session, timeout=timeout)
    require_unchanged_total(
        session,
        expected_total=discovery.total,
        phase="before",
        timeout=timeout,
    )

    snapshots: list[CataloguePass] = []
    validation_batches = 0
    for round_number in range(1, EXACT_VALIDATION_ROUNDS + 1):
        records_by_id, batches = validate_candidates(
            session,
            discovery=discovery,
            timeout=timeout,
        )
        validation_batches += batches
        snapshots.append(
            materialize_validated_snapshot(
                records_by_id,
                discovery=discovery,
                observed_at=observed_at,
                now=now,
            )
        )
        require_unchanged_total(
            session,
            expected_total=discovery.total,
            phase="between" if round_number < EXACT_VALIDATION_ROUNDS else "after",
            timeout=timeout,
        )

    if len(snapshots) != EXACT_VALIDATION_ROUNDS:
        raise RitchieBrosWatchError("Ritchie Bros exact validation round count is invalid")
    first_fingerprint = snapshots[0].fingerprint
    if any(snapshot.fingerprint != first_fingerprint for snapshot in snapshots[1:]):
        raise RitchieBrosSnapshotChanged(
            "Ritchie Bros exact validation rounds disagree on the public snapshot"
        )
    return replace(snapshots[-1], validation_batches=validation_batches)


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    if timeout < 5:
        raise ValueError("Ritchie Bros timeout must be at least five seconds")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    observed_at = now.isoformat()
    owned_session = session is None
    active_session = session or configured_session()
    try:
        stable = catalogue_pass(
            active_session,
            timeout=timeout,
            observed_at=observed_at,
            now=now,
        )
    finally:
        if owned_session:
            active_session.close()
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every public automobile card reachable from the Ritchie Bros catalogue",
        "catalogue_total": stable.total,
        "listing_pages": stable.pages,
        "pagination_window_rows": PAGE_SIZE,
        "pagination_step_rows": PAGE_SIZE,
        "pagination_overlap_rows": 0,
        "overlap_records_observed": stable.discovery_duplicate_records,
        "stable_ids_unique": True,
        "full_catalogue_rechecked": True,
        "reconciliation_attempts": stable.discovery_sweeps,
        "transient_snapshot_retries": stable.discovery_incomplete_sweeps,
        "candidate_discovery_sweeps": stable.discovery_sweeps,
        "candidate_discovery_page_fetches": stable.discovery_page_fetches,
        "candidate_discovery_incomplete_sweeps": stable.discovery_incomplete_sweeps,
        "candidate_discovery_duplicate_records": stable.discovery_duplicate_records,
        "exact_item_filter_batch_size": ITEM_FILTER_BATCH_SIZE,
        "exact_validation_rounds": EXACT_VALIDATION_ROUNDS,
        "exact_validation_batches": stable.validation_batches,
        "validated_identity_sha256": stable.identity_sha256,
        "validated_publication_sha256": stable.publication_sha256,
        "schengen_auction_rows": len(stable.rows),
        "rejected_counts": stable.rejected_counts,
        "publication_ready": False,
    }
    return {
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": observed_at,
        "research_only": True,
        "publication_status": "review_required",
        "row_count": len(stable.rows),
        "rows": list(stable.rows),
        "source_reports": {SOURCE_KEY: report},
    }


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
    parser = argparse.ArgumentParser(
        description="Fetch every public Schengen Ritchie Bros automobile auction"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "RBAUCTION_WATCH_PASS",
        "row_count": payload["row_count"],
        "catalogue_total": report["catalogue_total"],
        "pages": report["listing_pages"],
        "seconds": round(time.monotonic() - started, 1),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
