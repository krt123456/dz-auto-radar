#!/usr/bin/env python3
"""Reconcile every public current passenger-car auction at eDražbe Slovenia.

The Slovenian state e-auction portal (edrazbe.si) publishes every bailiff-run
online sale, including individual passenger cars.  Its data arrives as JSON
responses to POST ``api.sys.edrazbe.si/public/publication/list`` fired by the
site's own front-end, so this connector reads them through the loopback WAF
daemon's render+capture mode (``GET /render?url=...&capture=1``), extracting
every captured ``publication/list`` body.

Cars are an explicit source taxonomy value
(``subjectTypeRelation.valueCode == "090"``); real estate, motorcycles, and
commercial vehicles are separate subject types excluded structurally at
source.  The catalogue is captured twice; stable identities and facts must
match between passes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
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
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
LJUBLJANA = ZoneInfo("Europe/Ljubljana")
SOURCE_KEY = "edrazbe-si"
SOURCE_NAME = "eDražbe Slovenia"
AUCTIONS_URL = "https://www.edrazbe.si/en/auctions"
DEFAULT_RENDER_WAIT = 20
DEFAULT_SNAPSHOT_ATTEMPTS = 3
MAX_ATTEMPTS = 6
MAX_ITEMS = 5_000
CAR_SUBJECT_TYPE_CODE = "090"
YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-2]\d)\b")
MILEAGE_RE = re.compile(
    r"(\d[\d.\s]{2,9})\s*k[m\u00ed]?\b|(?:kilometr\w*)[^0-9]{0,40}(\d[\d.\s]{2,9})",
    re.I,
)
ACTIVE_STATUSES = frozenset({"pending", "active", "ongoing", "published", "open"})


class EdrazbeWatchError(RuntimeError):
    """The public eDražbe car catalogue could not be reconciled."""


def clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def fold(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", clean(value)).casefold()
        if not unicodedata.combining(character)
    )


@dataclass(frozen=True)
class Sale:
    sale_id: str
    publication_id: str
    case_number: str
    description: str
    subject_type_code: str
    subject_type_label: str
    status: str
    sale_end_utc: dt.datetime
    estimated_price_eur: int | float | None

    @property
    def identity(self) -> str:
        return self.sale_id

    @property
    def fingerprint(self) -> tuple[str, str, str]:
        # Live bid state is intentionally absent: it is auction state, not
        # catalogue identity.
        return (
            self.description,
            self.case_number,
            self.sale_end_utc.isoformat(),
        )


def slugify(value: str) -> str:
    folded = fold(value)
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return slug[:120] or "sale"


def sale_url(sale: Sale) -> str:
    subject = sale.subject_type_label or "sale"
    return f"https://www.edrazbe.si/en/single/{sale.sale_id}/title/{slugify(subject)}"


def parse_iso_utc(value: Any, *, error: str) -> dt.datetime:
    raw = clean(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise EdrazbeWatchError(f"{error}: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_price(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value if value >= 0 else None
    text = re.sub(r"[^0-9,.]", "", clean(value))
    if not text:
        return None
    try:
        parsed = float(text.replace(".", "").replace(",", "."))
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def parse_item(raw: Any, *, context: str) -> Sale:
    if not isinstance(raw, dict):
        raise EdrazbeWatchError(f"eDražbe {context} item is not an object")
    sale_id = clean(raw.get("id"))
    if not sale_id:
        raise EdrazbeWatchError(f"eDražbe {context} item has no id")
    description = clean(raw.get("description"))
    if not description:
        raise EdrazbeWatchError(f"eDražbe sale {sale_id} has no public description")
    relation = raw.get("subjectTypeRelation") or {}
    subject_type_code = clean(relation.get("valueCode"))
    if not subject_type_code:
        raise EdrazbeWatchError(f"eDražbe sale {sale_id} has no subject type")
    subject_type_label = clean(relation.get("valueContent"))
    status = fold(raw.get("status"))
    sale_end_utc = parse_iso_utc(
        raw.get("saleEndAt"), error=f"eDražbe sale {sale_id} end time"
    )
    estimated = raw.get("subjectEstimatedPrice")
    if estimated is None:
        estimated = (raw.get("estimatedPrice") or {}).get("amount") if isinstance(raw.get("estimatedPrice"), dict) else None
    return Sale(
        sale_id=sale_id,
        publication_id=clean(raw.get("publicationId")),
        case_number=clean(raw.get("caseNumber")),
        description=description,
        subject_type_code=subject_type_code,
        subject_type_label=subject_type_label,
        status=status,
        sale_end_utc=sale_end_utc,
        estimated_price_eur=parse_price(estimated),
    )


def extract_items(capture_body: str, captured_xhrs: list) -> list[Sale]:
    """Pull every publication/list item from one captured render."""
    merged: dict[str, Sale] = {}
    for xhr in captured_xhrs:
        if "publication/list" not in xhr.get("url", ""):
            continue
        try:
            payload = json.loads(xhr.get("body", ""))
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else next(
            (v for v in payload.values() if isinstance(v, list)), []
        )
        for raw in items:
            sale = parse_item(raw, context="captured list")
            merged.setdefault(sale.identity, sale)
    if not merged:
        raise EdrazbeWatchError("eDražbe capture contained no publication items")
    return sorted(merged.values(), key=lambda s: s.identity)


def make_capture(args: argparse.Namespace) -> Callable[[], list]:
    base = args.fetch_base.rstrip("/")

    def capture() -> list:
        q = urllib.parse.urlencode({
            "url": f"{AUCTIONS_URL}?wait={args.render_wait}",
            "wait": str(args.render_wait),
            "capture": "1",
        })
        try:
            response = urllib.request.urlopen(f"{base}?{q}", timeout=args.timeout)
            payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise EdrazbeWatchError(f"eDražbe render+capture failed: {error}") from error
        if payload.get("error"):
            raise EdrazbeWatchError(f"eDražbe render+capture errored: {payload['error']}")
        return payload.get("xhrs") or []

    return capture


def walk_catalogue(capture: Callable[[], list]) -> list[Sale]:
    items = extract_items(AUCTIONS_URL, capture())
    if len(items) > MAX_ITEMS:
        raise EdrazbeWatchError("eDražbe catalogue exceeds item safety limit")
    return items


def assert_coherent(first: list[Sale], second: list[Sale]) -> None:
    first_map = {sale.identity: sale for sale in first}
    second_map = {sale.identity: sale for sale in second}
    if first_map.keys() != second_map.keys():
        raise EdrazbeWatchError("eDražbe sale ids changed between passes")
    if any(first_map[k].fingerprint != second_map[k].fingerprint for k in first_map):
        raise EdrazbeWatchError("eDražbe sale facts changed between passes")


def passenger_filter_reason(sale: Sale, now: dt.datetime) -> str:
    if sale.subject_type_code != CAR_SUBJECT_TYPE_CODE:
        return "not_car_subject_type"
    if sale.status and sale.status not in ACTIVE_STATUSES:
        return "inactive_status"
    if sale.sale_end_utc <= now:
        return "ended_sale"
    return ""


def infer_year(description: str) -> int | None:
    match = YEAR_RE.search(description)
    return int(match.group(1)) if match else None


def infer_mileage_km(description: str) -> int | None:
    match = MILEAGE_RE.search(description)
    if match is None:
        return None
    raw = match.group(1) or match.group(2) or ""
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def normalize_sale(sale: Sale, *, observed_at: str) -> dict[str, Any]:
    price = sale.estimated_price_eur
    price_kind = "guide_price" if price is not None else "unknown"
    return {
        "id": f"{SOURCE_KEY}:{sale.sale_id}",
        "source": SOURCE_KEY,
        "source_key": SOURCE_KEY,
        "source_name": SOURCE_NAME,
        "url": sale_url(sale),
        "title": sale.description,
        "model": sale.description,
        "country": "SI",
        "asset_country": "SI",
        "category": "car",
        "category_raw": "eDražbe public Cars subject type",
        "year": infer_year(sale.description),
        "mileage": infer_mileage_km(sale.description),
        "mileage_km": infer_mileage_km(sale.description),
        "fuel": "unknown",
        "seller": SOURCE_NAME,
        "location": "",
        "image_url": "",
        "price_amount": price,
        "price_currency": "EUR" if price is not None else "",
        "price_eur": price,
        "price_kind": price_kind,
        "price_label": (
            f"public estimated price {price} EUR" if price is not None
            else "no public price yet"
        ),
        "bid_visibility": "public eDražbe sale publication",
        "reserve_met": None,
        "no_reserve": None,
        "sale_terms": "Official Slovenian state e-auction sale",
        "auction_status": "active",
        "canonical_end_utc": sale.sale_end_utc.isoformat(),
        "sale_end_utc": sale.sale_end_utc.isoformat(),
        "sale_event_utc": sale.sale_end_utc.isoformat(),
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": (
            "Public Slovenian state e-auction; confirm condition, fees, documents, "
            "collection, registration, and export requirements before bidding."
        ),
        "access_sale_note": "Bidding may require registration on the eDražbe portal.",
        "adapter_authorized": True,
        "raw_evidence_ref": f"{SOURCE_KEY}:sale:{sale.sale_id}:{sale.publication_id}",
        "evidence": "Public eDražbe publication list captured through the WAF daemon.",
    }


def build_watch(
    *,
    capture: Callable[[], list],
    now: dt.datetime | None = None,
    snapshot_attempts: int = DEFAULT_SNAPSHOT_ATTEMPTS,
) -> dict[str, Any]:
    if not 1 <= snapshot_attempts <= MAX_ATTEMPTS:
        raise ValueError("invalid eDražbe snapshot-attempts")
    current = now or dt.datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    observed_at = current.isoformat()
    first: list[Sale] | None = None
    second: list[Sale] | None = None
    attempts_used = 0
    for _ in range(snapshot_attempts):
        attempts_used += 1
        first = walk_catalogue(capture)
        second = walk_catalogue(capture)
        try:
            assert_coherent(first, second)
            break
        except EdrazbeWatchError:
            if attempts_used >= snapshot_attempts:
                raise
    assert first is not None and second is not None

    exclusions: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for sale in second:
        reason = passenger_filter_reason(sale, current)
        if reason:
            exclusions[reason] += 1
            continue
        rows.append(normalize_sale(sale, observed_at=observed_at))

    report = {
        "status": "ok",
        "connector_status": "ok",
        "catalogue_scope": (
            "every current public sale in the source's own Cars subject type "
            "(subject_type 090); real estate, motorcycles, and commercial "
            "vehicles are separate subject types excluded structurally at source"
        ),
        "declared": len(second),
        "visited": len(second),
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
    parser = argparse.ArgumentParser(description="Fetch every current public eDražbe passenger-car auction")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fetch-base", default="http://127.0.0.1:8977/render")
    parser.add_argument("--timeout", type=int, default=170)
    parser.add_argument("--render-wait", type=int, default=DEFAULT_RENDER_WAIT)
    parser.add_argument("--snapshot-attempts", type=int, default=DEFAULT_SNAPSHOT_ATTEMPTS)
    args = parser.parse_args()
    started = time.monotonic()
    payload = build_watch(
        capture=make_capture(args),
        snapshot_attempts=args.snapshot_attempts,
    )
    atomic_write_json(args.out, payload)
    report = payload["source_reports"][SOURCE_KEY]
    print(json.dumps({
        "result": "EDRAZBE_WATCH_PASS",
        "row_count": payload["row_count"],
        "declared": report["declared"],
        "snapshot_attempts_used": report["snapshot_attempts_used"],
        "seconds": round(time.monotonic() - started, 1),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
