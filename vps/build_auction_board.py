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
from typing import Any, Dict, Iterator, List, Optional

from auction_registry import (
    auction_source_by_key,
    auction_source_for_url,
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


def public_offer_id(source: Any, native_listing_id: Any) -> str:
    """The same public ID the regular lane would mint for this listing."""
    identity = [
        canonical_source_key(source),
        str(native_listing_id if native_listing_id is not None else "").strip(),
    ]
    return canonical_sha256(identity)

END_SOON_HOURS = 24
UTC = dt.timezone.utc
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
        if auction_source_for_url(url) is None:
            excluded("domain_mismatch")
            continue
        if not source_has_explicit_auction_semantics(source, raw_json):
            excluded("no_explicit_auction_semantics")
            continue
        raw_end = None
        if raw_json:
            try:
                raw_end = json.loads(raw_json).get("auction_end_at")
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
        try:
            bid = int(price)
        except (TypeError, ValueError):
            excluded("hidden_or_missing_price")
            continue
        if bid <= 0:
            excluded("hidden_or_missing_price")
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
            "country": str(country or "").strip().upper(),
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


def build_lane(
    database: Path,
    *,
    cutoff: str,
    regular_lane_ids: frozenset[str],
    regular_lane_urls: frozenset[str],
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Read-only build: connect, read offers via registry lane, return payload."""
    if generated_at is None:
        generated_at = dt.datetime.now(UTC).isoformat()

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
    return {
        "schema_version": 1,
        "lane": "auction",
        "registry_digest": registry_digest_json(),
        "generated_at_utc": generated_at,
        "lane_count": len(rows),
        "excluded_counts": counts,
        "rows": rows,
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
    args = parser.parse_args()

    cutoff = args.cutoff or (
        dt.datetime.now(UTC) - dt.timedelta(days=args.max_observation_age_days)
    ).isoformat()
    regular_ids, regular_urls = load_regular_board(args.board)
    payload = build_lane(args.database, cutoff=cutoff,
                         regular_lane_ids=regular_ids,
                         regular_lane_urls=regular_urls,
                         generated_at=args.generated_at)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps({
        "lane_count": payload["lane_count"],
        "excluded_counts": payload["excluded_counts"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())