#!/usr/bin/env python3
"""Add observed Ouedkniss DZD price references to an auction lane.

Only public, priced automobile announcements matching the detected make/model
and model year are used.  A reference needs at least two independent listings;
otherwise the row is left with ``ouedkniss_reference = null``.  Results are
cached so the hourly auction refresh does not hammer Ouedkniss.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import statistics
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests


UTC = dt.timezone.utc
GRAPHQL_URL = "https://api.ouedkniss.com/graphql"
BASE_URL = "https://www.ouedkniss.com"
QUERY = """
query SearchQuery($q:String,$filter:SearchFilterInput){
  search(q:$q, filter:$filter){ announcements{
    data{ id title slug price pricePreview priceUnit
      smallDescription{ specification{ codename } valueText } }
    paginatorInfo{ lastPage total }
  }}
}
"""
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/135 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}
MAKES = (
    "alfa romeo", "aston martin", "land rover", "mercedes benz", "volkswagen",
    "audi", "bmw", "byd", "citroen", "cupra", "dacia", "ds", "fiat", "ford",
    "honda", "hyundai", "iveco", "jaguar", "jeep", "kia", "lexus", "mazda",
    "mini", "mitsubishi", "nissan", "opel", "peugeot", "porsche", "renault",
    "seat", "skoda", "smart", "suzuki", "tesla", "toyota", "volvo", "vw",
)
MAKE_ALIASES = {"vw": "Volkswagen", "mercedes": "Mercedes Benz"}
NOISE = {
    "style", "advanced", "business", "comfort", "edition", "executive", "line",
    "long", "premium", "sport", "automatic", "automatik", "diesel", "hybrid",
    "essence", "benzine", "tdi", "tsi", "dci", "hdi", "bluehdi", "e", "s",
}


def now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat()


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def year_from(*values: Any) -> int:
    match = re.search(r"\b(20(?:2[0-9]|3[0-5]))\b", " ".join(map(str, values)))
    return int(match.group(1)) if match else 0


def price_dzd(item: dict[str, Any]) -> int:
    try:
        raw = int(float(item.get("price") or 0))
    except (TypeError, ValueError):
        raw = 0
    if raw >= 500_000:
        return raw
    try:
        preview = float(item.get("pricePreview") or 0)
    except (TypeError, ValueError):
        preview = 0
    return int(round(preview * 10_000)) if preview > 0 else 0


def fuel_kind(value: Any) -> str:
    text = normalized(value)
    if any(word in text for word in ("hybrid", "hybride", "hجين")):
        return "hybrid"
    if any(word in text for word in ("electric", "electro", "elektr", "electrique", "ev", "bev")):
        return "electric"
    if any(word in text for word in ("diesel", "gazole", "mazout", "dci", "tdi", "hdi")):
        return "diesel"
    if any(word in text for word in ("petrol", "benzin", "essence", "tsi", "tce")):
        return "petrol"
    return ""


def item_fuel(item: dict[str, Any]) -> str:
    values = [item.get("title") or ""]
    for entry in item.get("smallDescription") or []:
        code = str((entry.get("specification") or {}).get("codename") or "")
        if code in {"energie", "car-engine"}:
            values.extend(str(value) for value in (entry.get("valueText") or []))
    return fuel_kind(" ".join(values))


def search_identity(row: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    text = normalized(f"{row.get('model', '')} {row.get('title', '')}")
    words = text.split()
    make = ""
    make_index = -1
    for candidate in sorted(MAKES, key=len, reverse=True):
        candidate_words = candidate.split()
        for index in range(max(0, len(words) - len(candidate_words) + 1)):
            if words[index:index + len(candidate_words)] == candidate_words:
                make, make_index = candidate, index + len(candidate_words)
                break
        if make:
            break
    if not make:
        return None
    model_words: list[str] = []
    for word in words[make_index:]:
        if word in NOISE or re.fullmatch(r"\d(?:\.\d)?", word):
            continue
        if re.fullmatch(r"20\d\d", word):
            continue
        if word == make.split()[-1] or word in model_words:
            continue
        model_words.append(word)
        if len(model_words) == 1 and len(model_words[0]) > 1:
            break
        if len(model_words) == 2:
            break
    if not model_words:
        return None
    display_make = MAKE_ALIASES.get(make, make.title())
    make_term = normalized(display_make).split()[0]
    return " ".join([display_make, *model_words]), tuple([make_term, *map(normalized, model_words)])


def query_page(session: requests.Session, query: str, page: int, timeout: int) -> list[dict[str, Any]]:
    payload = {
        "operationName": "SearchQuery",
        "query": QUERY,
        "variables": {"q": query, "filter": {"categorySlug": "automobiles", "page": page}},
    }
    error: Exception | None = None
    for delay in (0, 1, 3):
        if delay:
            time.sleep(delay)
        try:
            response = session.post(GRAPHQL_URL, json=payload, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if data.get("errors"):
                return []
            return (((data.get("data") or {}).get("search") or {})
                    .get("announcements") or {}).get("data") or []
        except (requests.RequestException, ValueError) as exc:
            error = exc
    proxy = os.environ.get("OUEDKNISS_PROXY", "").strip()
    if not proxy:
        try:
            with socket.create_connection(("127.0.0.1", 1080), timeout=0.25):
                proxy = "socks5h://127.0.0.1:1080"
        except OSError:
            proxy = ""
    if proxy:
        try:
            response = session.post(GRAPHQL_URL, json=payload, headers=HEADERS,
                                    proxies={"http": proxy, "https": proxy}, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if not data.get("errors"):
                return (((data.get("data") or {}).get("search") or {})
                        .get("announcements") or {}).get("data") or []
        except (requests.RequestException, ValueError) as exc:
            error = exc
    raise RuntimeError(f"Ouedkniss query failed after retries: {type(error).__name__}")


def trim_outliers(values: list[int]) -> list[int]:
    if len(values) < 4:
        return values
    ordered = sorted(values)
    median = statistics.median(ordered)
    mad = statistics.median(abs(value - median) for value in ordered)
    if mad <= 0:
        return values
    # A wide six-MAD fence is conservative but still removes obvious typo or
    # trim-level mismatches from small Ouedkniss samples.
    lo, hi = median - 6 * mad, median + 6 * mad
    kept = [value for value in values if lo <= value <= hi]
    return kept if len(kept) >= 2 else values


def observe(session: requests.Session, label: str, terms: tuple[str, ...], year: int,
            *, fuel: str = "", pages: int, timeout: int, sleep_seconds: float) -> dict[str, Any] | None:
    accepted: dict[str, tuple[int, str]] = {}
    for page in range(1, pages + 1):
        for item in query_page(session, f"{label} {year}", page, timeout):
            item_id = str(item.get("id") or "")
            title = normalized(item.get("title"))
            if not item_id or not all(term in title for term in terms):
                continue
            listing_year = year_from(item.get("title"), item.get("slug"))
            if listing_year and abs(listing_year - year) > 1:
                continue
            if fuel and item_fuel(item) != fuel:
                continue
            price = price_dzd(item)
            if not 1_000_000 <= price <= 100_000_000:
                continue
            slug = str(item.get("slug") or "").strip("/")
            accepted[item_id] = (price, f"{BASE_URL}/{slug}" if slug else BASE_URL)
        if sleep_seconds:
            time.sleep(sleep_seconds)
    if len(accepted) < 2:
        return None
    pairs = list(accepted.values())
    kept_values = trim_outliers([price for price, _ in pairs])
    kept_set = set(kept_values)
    evidence = [url for price, url in pairs if price in kept_set][:5]
    return {
        "average_dzd": int(round(statistics.fmean(kept_values))),
        "median_dzd": int(round(statistics.median(kept_values))),
        "sample_count": len(kept_values),
        "min_dzd": min(kept_values),
        "max_dzd": max(kept_values),
        "model_query": label,
        "model_year": year,
        "observed_at_utc": now_iso(),
        "source": "Ouedkniss",
        "evidence_urls": evidence,
    }


def parse_time(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def load_cache(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def enrich(lane: dict[str, Any], cache: dict[str, Any], *, ttl_hours: int,
           pages: int, timeout: int, sleep_seconds: float, max_queries: int) -> tuple[dict[str, Any], dict[str, Any]]:
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    session = requests.Session()
    queried = 0
    hits = 0
    now = dt.datetime.now(UTC)
    for row in lane.get("rows") or []:
        row["ouedkniss_reference"] = None
        identity = search_identity(row)
        try:
            year = int(row.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        if identity is None or not 2000 <= year <= 2035:
            continue
        label, terms = identity
        comparable_fuel = fuel_kind(row.get("fuel"))
        key = f"{normalized(label)}|{year}" + (f"|{comparable_fuel}" if comparable_fuel else "")
        cached = entries.get(key) if isinstance(entries.get(key), dict) else None
        cached_at = parse_time((cached or {}).get("cached_at_utc"))
        fresh = cached_at is not None and now - cached_at < dt.timedelta(hours=ttl_hours)
        if not fresh and queried < max_queries:
            queried += 1
            try:
                reference = observe(session, label, terms, year, fuel=comparable_fuel, pages=pages,
                                    timeout=timeout, sleep_seconds=sleep_seconds)
                cached = {"cached_at_utc": now_iso(), "reference": reference}
                entries[key] = cached
            except RuntimeError as exc:
                cached = cached or {"cached_at_utc": now_iso(), "reference": None,
                                    "error": str(exc)}
                entries[key] = cached
        reference = (cached or {}).get("reference")
        if isinstance(reference, dict) and reference.get("sample_count", 0) >= 2:
            row["ouedkniss_reference"] = reference
            hits += 1
    lane["ouedkniss_reference_summary"] = {
        "matched_rows": hits,
        "lane_rows": len(lane.get("rows") or []),
        "cache_ttl_hours": ttl_hours,
        "generated_at_utc": now_iso(),
    }
    return lane, {"schema_version": 1, "updated_at_utc": now_iso(), "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lane", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--ttl-hours", type=int, default=6)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--max-queries", type=int, default=80)
    args = parser.parse_args()
    lane = json.loads(args.lane.read_text(encoding="utf-8"))
    cache = load_cache(args.cache)
    lane, cache = enrich(lane, cache, ttl_hours=args.ttl_hours, pages=args.pages,
                         timeout=args.timeout, sleep_seconds=args.sleep,
                         max_queries=args.max_queries)
    atomic_write_json(args.lane, lane)
    atomic_write_json(args.cache, cache)
    print(json.dumps(lane["ouedkniss_reference_summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
