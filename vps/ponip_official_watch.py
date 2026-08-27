#!/usr/bin/env python3
"""Build one reconciled snapshot from FINA PONIP's official CSV export.

PONIP publishes its complete register as a CSV attachment.  Unlike a visual
search page, the export has no hidden page boundary: the response's declared
byte length is the finite catalogue boundary.  This connector reads that
entire file, validates the required fields and unique auction identities, and
publishes only future/current rows whose official description identifies a
vehicle.  It never invents fuel, model year, mileage, or a current bid.

The PONIP visual detail route is UUID-based but the CSV exposes the stable
``ID nadmetanja`` instead.  Each result therefore links to the official public
search surface with that ID in the query string; the ID is also retained in
the visible title and row identity for manual lookup.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


UTC = dt.timezone.utc
CROATIA = ZoneInfo("Europe/Zagreb")
SOURCE_KEY = "fina-ponip"
SOURCE_NAME = "FINA PONIP"
SOURCE_URL = "https://ponip.fina.hr/ocevidnik-web/preuzmi/csv"
SEARCH_URL = "https://ponip.fina.hr/ocevidnik-web/pretrazivanje/pokretnina"
DEFAULT_TIMEOUT = 300
CHUNK_SIZE = 64 * 1024

HEADERS = {
    "User-Agent": "SonarDeals-Auction-Monitor/1.0",
    "Accept-Language": "hr,en;q=0.8",
}
REQUIRED_COLUMNS = frozenset({
    "Opis",
    "Vrsta predmeta prodaje",
    "ID nadmetanja",
    "Datum i vrijeme početka nadmetanja",
    "Datum i vrijeme završetka nadmetanja",
    "Početna cijena za nadmetanje",
})
VEHICLE_RE = re.compile(
    r"\b(?:automobil\w*|vozil\w*|motocikl\w*|moped\w*|skuter\w*|"
    r"kamion\w*|autobus\w*|kombi\w*|traktor\w*|prikolic\w*|"
    r"poluprikolic\w*|quad\w*|radni\s+stroj\w*)\b",
    re.I,
)


class PonipWatchError(RuntimeError):
    """The official PONIP export could not be reconciled safely."""


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def positive_number(value: Any) -> int | float | None:
    compact = re.sub(r"[^0-9,.-]", "", clean(value))
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


def parse_local_datetime(value: Any) -> dt.datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise PonipWatchError(f"PONIP has an invalid auction timestamp: {text!r}") from exc
    return parsed.replace(tzinfo=CROATIA)


def canonical_url(auction_id: str) -> str:
    query = urlencode((("idNadmetanja", auction_id),))
    return f"{SEARCH_URL}?{query}"


def is_vehicle_row(row: dict[str, str | None]) -> bool:
    kind = clean(row.get("Vrsta predmeta prodaje")).casefold()
    if kind not in {"pokretnina", "imovina"}:
        return False
    return VEHICLE_RE.search(clean(row.get("Opis"))) is not None


def row_to_watch(
    row: dict[str, str | None], *, observed_at: str, now: dt.datetime
) -> dict[str, Any] | None:
    auction_id = clean(row.get("ID nadmetanja"))
    if not auction_id:
        raise PonipWatchError("PONIP row has no stable auction ID")
    end = parse_local_datetime(row.get("Datum i vrijeme završetka nadmetanja"))
    if end is None or end <= now:
        return None
    if not is_vehicle_row(row):
        return None
    description = clean(row.get("Opis"))
    if not description:
        raise PonipWatchError(f"PONIP auction {auction_id} has no public description")
    start = parse_local_datetime(row.get("Datum i vrijeme početka nadmetanja"))
    price = positive_number(row.get("Početna cijena za nadmetanje"))
    title = f"PONIP {auction_id}: {description[:300]}"
    return {
        "id": f"{SOURCE_KEY}:{auction_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": canonical_url(auction_id),
        "title": title,
        "model": description[:300],
        "country": "HR",
        "asset_country": "HR",
        "category": "vehicle",
        "category_raw": clean(row.get("Vrsta predmeta prodaje")),
        "year": None,
        "mileage_km": None,
        "fuel": "unknown",
        "price_amount": price,
        "price_currency": "EUR",
        "price_eur": price,
        "price_kind": "starting_bid" if price is not None else "unknown",
        "price_label": (
            "official starting bid from PONIP export"
            if price is not None else "starting bid not supplied in PONIP export"
        ),
        "bid_visibility": "official export",
        "canonical_end_utc": end.astimezone(UTC).isoformat(),
        "sale_end_utc": end.astimezone(UTC).isoformat(),
        "sale_event_utc": start.astimezone(UTC).isoformat() if start else None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Official PONIP export identifies a vehicle listing. Confirm vehicle "
            "condition, exact asset composition, terms, fees, buyer requirements, "
            "and import eligibility before bidding."
        ),
        "access_sale_note": "Use the official PONIP auction ID shown in this listing to inspect the source record.",
        "auction_status": "upcoming" if start and start > now else "active",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:official-csv:{auction_id}",
        "evidence": "Official FINA PONIP CSV export row.",
    }


def parse_export(
    payload: bytes, *, observed_at: str, now: dt.datetime
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    try:
        decoded = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PonipWatchError("PONIP export is not valid UTF-8") from exc
    reader = csv.DictReader(io.StringIO(decoded, newline=""), delimiter=";")
    fields = frozenset(reader.fieldnames or ())
    missing = sorted(REQUIRED_COLUMNS - fields)
    if missing:
        raise PonipWatchError(f"PONIP export is missing required columns: {', '.join(missing)}")

    all_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    stats = {"csv_rows": 0, "future_or_current": 0, "vehicle_rows": 0}
    for raw_row in reader:
        stats["csv_rows"] += 1
        auction_id = clean(raw_row.get("ID nadmetanja"))
        if not auction_id:
            raise PonipWatchError("PONIP export has a row without a stable auction ID")
        if auction_id in all_ids:
            raise PonipWatchError(f"PONIP export duplicates auction ID {auction_id}")
        all_ids.add(auction_id)
        end = parse_local_datetime(raw_row.get("Datum i vrijeme završetka nadmetanja"))
        if end is None or end <= now:
            continue
        stats["future_or_current"] += 1
        if not is_vehicle_row(raw_row):
            continue
        normalized = row_to_watch(raw_row, observed_at=observed_at, now=now)
        if normalized is None:
            raise PonipWatchError(f"PONIP active vehicle {auction_id} was not normalized")
        rows.append(normalized)
        stats["vehicle_rows"] += 1

    if stats["csv_rows"] == 0:
        raise PonipWatchError("PONIP export contains no data rows")
    ids = [row["id"] for row in rows]
    urls = [row["url"] for row in rows]
    if len(ids) != len(set(ids)) or len(urls) != len(set(urls)):
        raise PonipWatchError("PONIP active vehicle identities are not unique")
    return rows, stats


def configured_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2))
    return session


def fetch_export(session: requests.Session, *, timeout: int) -> tuple[bytes, int]:
    response = session.get(SOURCE_URL, headers=HEADERS, timeout=timeout, stream=True)
    try:
        response.raise_for_status()
        try:
            declared = int(str(response.headers.get("Content-Length") or ""))
        except ValueError as exc:
            raise PonipWatchError("PONIP export has no valid declared byte length") from exc
        if declared <= 0:
            raise PonipWatchError("PONIP export has an invalid declared byte length")
        pieces: list[bytes] = []
        received = 0
        for piece in response.iter_content(chunk_size=CHUNK_SIZE):
            if not piece:
                continue
            pieces.append(piece)
            received += len(piece)
        if received != declared:
            raise PonipWatchError(
                f"PONIP export byte reconciliation failed: declared={declared} received={received}"
            )
        return b"".join(pieces), declared
    finally:
        response.close()


def build_watch(
    *,
    session: requests.Session | None = None,
    now: dt.datetime | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    if timeout < 5:
        raise ValueError("invalid PONIP timeout")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_at = now.astimezone(UTC).isoformat()
    supplied_session = session
    active_session = session or configured_session()
    try:
        payload, declared_bytes = fetch_export(active_session, timeout=timeout)
    finally:
        if supplied_session is None:
            active_session.close()
    rows, stats = parse_export(payload, observed_at=observed_at, now=now)
    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": "every byte of the official PONIP CSV export; current/future vehicle-described rows",
        "declared_bytes": declared_bytes,
        "received_bytes": len(payload),
        "csv_rows": stats["csv_rows"],
        "future_or_current_rows": stats["future_or_current"],
        "vehicle_rows": stats["vehicle_rows"],
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
    parser = argparse.ArgumentParser(description="Fetch the complete official FINA PONIP CSV export")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(timeout=args.timeout)
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "PONIP_WATCH_PASS",
        "row_count": payload["row_count"],
        "csv_rows": report["csv_rows"],
        "future_or_current_rows": report["future_or_current_rows"],
        "seconds": round(time.monotonic() - started, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
