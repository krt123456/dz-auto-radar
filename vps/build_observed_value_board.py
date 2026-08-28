#!/usr/bin/env python3
"""Build a truthful European peer-price ranking from a stable universe snapshot.

The public lane deliberately publishes no Algerian customs, landed-cost,
resale-price, profit, or ROI estimate.  A candidate is compared with recent
listings for the same model, year, normalized fuel, and 25,000 km band.  Its
own source family is excluded from the benchmark and every peer family is
capped so one feed cannot dominate the result.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

try:
    from .source_identity import (
        IdentityError,
        canonical_source_identity,
        source_family as canonical_source_family,
        source_identity_keys as canonical_source_identity_keys,
        source_key as canonical_source_key,
        autoscout24_non_detail_url,
    )
except ImportError:
    from source_identity import (
        IdentityError,
        canonical_source_identity,
        source_family as canonical_source_family,
        source_identity_keys as canonical_source_identity_keys,
        source_key as canonical_source_key,
        autoscout24_non_detail_url,
    )

try:
    from .listing_condition import condition_exclusion_reason
except ImportError:
    from listing_condition import condition_exclusion_reason


ALGORITHM = "schengen-observed-peer-value-v7-live-verified"
SCHENGEN_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
        "FR", "GR", "HR", "HU", "IS", "IT", "LI", "LT", "LU", "LV",
        "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
    }
)
MIN_PRICE_EUR = 4_000
MAX_PRICE_EUR = 45_000
MAX_MILEAGE_KM = 180_000
MILEAGE_BAND_KM = 25_000
DEFAULT_MAX_OBSERVATION_AGE_HOURS = 72
DEFAULT_MIN_PEER_COUNT = 20
DEFAULT_MIN_PEER_SOURCES = 3
DEFAULT_MIN_PEER_COUNTRIES = 2
DEFAULT_SOURCE_SAMPLE_CAP = 25
DEFAULT_MAX_DISPERSION = 0.35
DEFAULT_MIN_MEDIAN_RATIO = 0.60
# The ranked pool is deliberately larger than the public 10,000-offer target.
# Liveness is established after ranking, so dead or indeterminate rows near the
# top must not prevent lower-ranked live offers from filling the public board.
DEFAULT_TOP_N = 60_000
MAX_TOP_N = 100_000
MAX_CSV_FIELD_BYTES = 64 * 1024 * 1024
csv.field_size_limit(min(MAX_CSV_FIELD_BYTES, os.sys.maxsize))

SEMANTIC_PRICE_PATTERNS = (
    re.compile(r"\bcesja\b"),
    re.compile(r"\b(?:leasing|lease|lizing|лизинг)\b"),
    re.compile(
        r"\b(?:lease|leasing|contract|contrat|contrato|umow\w*|najem)\b.{0,40}"
        r"\b(?:takeover|transfer|assignment|cession|cesion|cessione|subentro|preluare|ubernahme|ubertragung)\b|"
        r"\b(?:takeover|transfer|assignment|cession|cesion|cessione|subentro|preluare|ubernahme|ubertragung)\b.{0,40}"
        r"\b(?:lease|leasing|contract|contrat|contrato|umow\w*|najem)\b"
    ),
    re.compile(
        r"\b(?:down\s*payment|deposit|transfer\s*fee|kaucja|zaliczka|wplata\s+wlasna|"
        r"oplata\s+wstepna|anzahlung|abloese(?:gebuhr)?|kaution|acompte|apport\s+initial|"
        r"depot\s+de\s+garantie|anticipo|deposito|entrada|avans|aanbetaling|borgsom|kontantinsats)\b"
    ),
    re.compile(
        r"\b(?:installments?|instalments?|monthly\s+payment|finance\s+payment|monatsrate|"
        r"ratenzahlung|mensualite|cuota\s+mensual|maandtermijn|manadsavgift|manedlig\s+ydelse|"
        r"rata|raty|rataln\w*|miesieczn\w*\s+rat\w*|credit\s+restant|restschuld)\b"
    ),
    re.compile(r"\b(?:financement|finanzierung|kredyt|flex\s+lease|loa|lld)\b"),
    re.compile(r"\b(?:odstepne|takeover)\b"),
)
RISK_PATTERN = re.compile(
    r"\b(?:salvage|accident(?:ed)?|damaged|unfall|motorschaden|bastler|epave|"
    # Text is accent-folded before matching. Keep the French participles
    # explicit so endommagement/endommager do not become broad false positives.
    r"endommag(?:e|ee|es|ees)|"
    r"sinistr\w*|uszkodz\w*|powypadk\w*|pour\s+pieces|parts\s+only|non\s+runner)\b"
)
GHOST_PATTERN = re.compile(
    r"\b(?:bez\s+koroze|stk\s+do|vykup|wykup|skup|zamian\w*|wymian\w*|"
    r"poszuk\w*|szukam|kupie|koupim|financovani|finansowanie|dofinansowanie|"
    r"przedplata|zaliczka)\b"
)
DIESEL_PATTERN = re.compile(
    r"\b(?:diesel|dizel|tdi|hdi|dci|cdi|crdi|jtd|multijet|ecoblue|bluedci|bluehdi|d-4d|tdci)\b"
)
ELECTRIC_PATTERN = re.compile(
    r"\b(?:electric|electrique|elektrisch|elektro|elettrica|ev|bev|tesla|leaf|zoe|"
    r"ioniq|enyaq|polestar|vinfast|e-tron|etron|id[ .-]?[3457])\b"
)
PLUGIN_PATTERN = re.compile(r"\b(?:plug[ -]?in|phev|gte|ehybrid|e-hybrid|recharge|p400e|t8)\b")
GAS_PATTERN = re.compile(r"\b(?:lpg|gpl|cng|gnc|tgi)\b")
PETROL_PATTERN = re.compile(
    r"\b(?:petrol|essence|benzin|benzina|benzyna|bensin|gasolina|gasoline)\b"
)
HYBRID_PATTERN = re.compile(r"\b(?:hybrid|hybride|hybryda|hibrid|ibrid|mhev|hev)\b")
LARGUS_FACT_PATTERN = re.compile(
    r"/annonce-[0-9a-f-]{36}-.+-((?:19|20)\d{2})-(\d{1,7})km(?:[/?#]|$)",
    re.IGNORECASE,
)
COMPACT_OFFER_TYPES = {
    "id": str, "m": str, "t": str, "p": int, "q1": int, "mp": int,
    "sv": int, "sp": float, "dp": float, "pn": int, "ps": int,
    "pc": int, "y": int, "km": int, "f": str, "c": str, "s": str,
    "u": str, "ls": str, "v": int,
}
FORBIDDEN_ECONOMICS_FIELDS = frozenset(
    {
        "profit", "roi", "landed_cost", "resale_dzd", "customs_dzd",
        "algerian_price", "effective_profit", "pr", "rd", "ld", "cd",
        "ci", "cb",
    }
)


@dataclass
class ScanEvidence:
    scanned_recent_rows: int = 0
    eligible_rows: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    sources: Counter[str] = field(default_factory=Counter)
    countries: Counter[str] = field(default_factory=Counter)
    digest: Any = field(default_factory=hashlib.sha256)


@dataclass(frozen=True)
class BlockedSourceEvidence:
    keys: frozenset[str]
    policy_sha256: str | None
    quarantine_sha256: str | None
    keys_sha256: str
    sources: tuple[str, ...]


def normalized_text(value: Any) -> str:
    folded = unicodedata.normalize(
        "NFKD",
        str(value or "").casefold().translate(
            str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss"})
        ),
    )
    return " ".join(
        "".join(char for char in folded if not unicodedata.combining(char)).split()
    )


def source_key(value: Any) -> str:
    return canonical_source_key(value)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def public_offer_id(source: Any, native_listing_id: Any) -> str:
    """Return a stable source-namespaced public ID for a native listing."""
    identity = [
        source_key(source),
        str(native_listing_id if native_listing_id is not None else "").strip(),
    ]
    return canonical_sha256(identity)


def source_identity_keys(value: Any) -> frozenset[str]:
    return canonical_source_identity_keys(value)


def source_family(value: Any) -> str:
    return canonical_source_family(value)


def _load_json_and_hash(path: Path) -> tuple[Any | None, str | None]:
    if not path.exists():
        return None, None
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def load_blocked_source_evidence(
    policy_path: Path, quarantine_path: Path,
) -> BlockedSourceEvidence:
    names: set[str] = set()
    policy, policy_sha256 = _load_json_and_hash(policy_path)
    if policy is None or policy_sha256 is None:
        raise FileNotFoundError(f"required source policy is unavailable: {policy_path}")
    payload = policy
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), dict):
        raise ValueError(f"invalid source policy: {policy_path}")
    for name, record in payload["sources"].items():
        if not isinstance(record, dict):
            raise ValueError(f"invalid source policy record: {name}")
        if record.get("mode") != "active":
            names.add(str(name).strip())
    quarantine, quarantine_sha256 = _load_json_and_hash(quarantine_path)
    if quarantine is not None:
        payload = quarantine
        if not isinstance(payload, dict):
            raise ValueError(f"invalid quarantine manifest: {quarantine_path}")
        names.update(str(name).strip() for name in payload if str(name).strip())
    normalized = tuple(sorted({source_key(name) for name in names if source_key(name)}))
    return BlockedSourceEvidence(
        keys=frozenset(normalized),
        policy_sha256=policy_sha256,
        quarantine_sha256=quarantine_sha256,
        keys_sha256=canonical_sha256(list(normalized)),
        sources=normalized,
    )


def load_blocked_source_keys(policy_path: Path, quarantine_path: Path) -> frozenset[str]:
    return load_blocked_source_evidence(policy_path, quarantine_path).keys


def normalize_https_url(value: Any) -> str:
    raw = str(value or "").strip()
    match = re.match(r"(?i)^https://[^\s<>\"']+", raw)
    if not match or len(match.group(0)) > 2_048 or "\\" in match.group(0):
        return ""
    try:
        parsed = urlsplit(match.group(0))
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if port is not None and port != 443:
            host = f"{host}:{port}"
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except (UnicodeError, ValueError):
        return ""


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalized_fuel(value: Any, title: Any) -> str:
    text = normalized_text(f"{value or ''} {title or ''}")
    if (
        DIESEL_PATTERN.search(text)
        or ELECTRIC_PATTERN.search(text)
        or PLUGIN_PATTERN.search(text)
        or GAS_PATTERN.search(text)
    ):
        return ""
    if HYBRID_PATTERN.search(text):
        return "hybrid"
    if PETROL_PATTERN.search(text):
        return "petrol"
    return ""


def percentile_type7(values: list[int], probability: float) -> float:
    if not values:
        raise ValueError("empty percentile input")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])


def peer_dispersion(values: list[int], median_value: float) -> float:
    if median_value <= 0:
        return math.inf
    deviations = [int(round(abs(value - median_value))) for value in values]
    mad = percentile_type7(deviations, 0.5)
    return (1.4826 * mad) / median_value


def bounded_sample_add(bucket: list[tuple[int, int, str]], offer: dict[str, Any], cap: int) -> None:
    sample_hash = int.from_bytes(
        hashlib.blake2b(str(offer["id"]).encode("utf-8"), digest_size=8).digest(),
        "big",
    )
    item = (sample_hash, int(offer["price"]), str(offer["country"]))
    if len(bucket) < cap:
        bucket.append(item)
        return
    worst_index = max(range(len(bucket)), key=lambda index: bucket[index][0])
    if item[0] < bucket[worst_index][0]:
        bucket[worst_index] = item


def candidate_query(cutoff: str, reference_year: int) -> tuple[str, list[Any]]:
    countries = sorted(SCHENGEN_COUNTRIES)
    placeholders = ",".join("?" for _ in countries)
    query = f"""
        SELECT
            source,
            source_listing_id,
            source_url,
            title,
            make_model,
            country,
            price_eur,
            year,
            mileage_km,
            fuel,
            seller_type,
            last_seen_at,
            CASE WHEN json_valid(raw_json) THEN json_extract(raw_json, '$.listing_id') END,
            CASE WHEN json_valid(raw_json) THEN json_extract(raw_json, '$.auction_end_at') END,
            CASE WHEN json_valid(raw_json) THEN json_extract(raw_json, '$.sale_term_code') END
        FROM offers INDEXED BY idx_offers_last_seen
        WHERE last_seen_at >= ?
          AND year BETWEEN ? AND ?
          AND price_eur BETWEEN ? AND ?
          AND mileage_km BETWEEN 0 AND ?
          AND country IN ({placeholders})
        ORDER BY last_seen_at, id
    """
    parameters: list[Any] = [
        cutoff,
        reference_year - 3,
        reference_year,
        MIN_PRICE_EUR,
        MAX_PRICE_EUR,
        MAX_MILEAGE_KM,
        *countries,
    ]
    return query, parameters


def eligible_rows(
    connection: sqlite3.Connection,
    *,
    query: str,
    parameters: list[Any],
    blocked_source_keys: frozenset[str],
    evidence: ScanEvidence | None = None,
) -> Iterator[dict[str, Any]]:
    seen_identities: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()
    for raw in connection.execute(query, parameters):
        if evidence is not None:
            evidence.scanned_recent_rows += 1
        (
            source, source_listing_id, raw_url, title, model, country, price,
            year, mileage, fuel, seller, last_seen, raw_listing_id, auction_end,
            sale_term_code,
        ) = raw
        source = " ".join(str(source or "").split())
        title = " ".join(str(title or "").split())
        model = str(model or "").strip()
        country = str(country or "").strip().upper()
        listing_id = str(
            raw_listing_id
            if raw_listing_id is not None and str(raw_listing_id).strip()
            else source_listing_id or ""
        ).strip()
        identity_error = False
        try:
            source, listing_id = canonical_source_identity(source, listing_id)
        except IdentityError:
            identity_error = True
        identity = (source_key(source), listing_id)
        url = normalize_https_url(raw_url)
        text = normalized_text(f"{title} {model}")
        fuel_label = normalized_fuel(fuel, title)
        reason = ""
        if identity_error:
            reason = "identity_normalization"
        elif not listing_id or not model or model.casefold().startswith("unprofiled"):
            reason = "identity_or_model"
        elif not title or not source:
            reason = "missing_title_or_source"
        elif not url:
            reason = "unsafe_url"
        elif source_identity_keys(source).intersection(blocked_source_keys):
            reason = "blocked_source"
        elif str(sale_term_code or "").strip() or str(auction_end or "").strip():
            reason = "auction_bid"
        elif any(pattern.search(text) for pattern in SEMANTIC_PRICE_PATTERNS):
            reason = "non_vehicle_price"
        elif (
            condition_exclusion_reason(title, model)
            or RISK_PATTERN.search(text)
            or GHOST_PATTERN.search(text)
        ):
            reason = "risk_or_ghost"
        elif not fuel_label:
            reason = "unsupported_fuel"
        elif source_key(source) == "l'argus":
            match = LARGUS_FACT_PATTERN.search(url)
            if match is None or int(match.group(1)) != int(year) or int(match.group(2)) != int(mileage):
                reason = "source_fact_mismatch"
        if not reason and identity in seen_identities:
            reason = "identity_duplicate"
        elif not reason and url in seen_urls:
            reason = "url_duplicate"
        if reason:
            if evidence is not None:
                evidence.rejected[reason] += 1
            continue
        seen_identities.add(identity)
        seen_urls.add(url)
        offer = {
            "id": public_offer_id(source, listing_id),
            "model": model,
            "title": title,
            "source": source,
            "source_family": source_family(source),
            "url": url,
            "price": int(price),
            "year": int(year),
            "mileage": int(mileage),
            "fuel": fuel_label,
            "country": country,
            "seller": str(seller or "").strip(),
            "last_seen_at": str(last_seen),
        }
        offer["cohort"] = (
            model.casefold(), int(year), fuel_label, int(mileage) // MILEAGE_BAND_KM,
        )
        if evidence is not None:
            evidence.eligible_rows += 1
            evidence.sources[source] += 1
            evidence.countries[country] += 1
            evidence.digest.update(
                json.dumps(
                    offer, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
            )
            evidence.digest.update(b"\n")
        yield offer


def build_peer_stats(
    samples: dict[tuple[str, int, str, int], dict[str, list[tuple[int, int, str]]]],
    *,
    min_count: int,
    min_sources: int,
    min_countries: int,
    max_dispersion: float,
) -> dict[tuple[tuple[str, int, str, int], str], dict[str, Any]]:
    result: dict[tuple[tuple[str, int, str, int], str], dict[str, Any]] = {}
    for cohort, by_family in samples.items():
        for excluded_family in by_family:
            peer_families = {
                family: rows for family, rows in by_family.items()
                if family != excluded_family and rows
            }
            prices = [item[1] for rows in peer_families.values() for item in rows]
            countries = {item[2] for rows in peer_families.values() for item in rows}
            if (
                len(prices) < min_count
                or len(peer_families) < min_sources
                or len(countries) < min_countries
            ):
                continue
            p50 = percentile_type7(prices, 0.5)
            dispersion = peer_dispersion(prices, p50)
            if dispersion > max_dispersion:
                continue
            result[(cohort, excluded_family)] = {
                "lower_quartile": int(round(percentile_type7(prices, 0.25))),
                "median": int(round(p50)),
                "count": len(prices),
                "sources": len(peer_families),
                "countries": len(countries),
                "dispersion": round(dispersion, 4),
            }
    return result


def verbose_rank_key(offer: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -int(offer["conservative_discount_bps"]),
        -int(offer["savings_vs_lower_quartile_eur"]),
        -int(offer["median_discount_bps"]),
        -int(offer["peer_source_count"]),
        -int(offer["peer_count"]),
        -int(offer["year"]),
        int(offer["mileage"]),
        int(offer["price"]),
        str(offer["id"]),
    )


def compact_offer(offer: dict[str, Any], verification: dict[str, int]) -> dict[str, Any]:
    return {
        "id": offer["id"], "m": offer["model"], "t": offer["title"],
        "p": offer["price"], "q1": offer["peer_lower_quartile_eur"],
        "mp": offer["peer_median_eur"], "sv": offer["savings_vs_lower_quartile_eur"],
        "sp": round(offer["conservative_discount_bps"] / 100, 2),
        "dp": round(offer["median_discount_bps"] / 100, 2),
        "pn": offer["peer_count"], "ps": offer["peer_source_count"],
        "pc": offer["peer_country_count"], "y": offer["year"],
        "km": offer["mileage"], "f": offer["fuel"], "c": offer["country"],
        "s": offer["source"], "u": offer["url"], "ls": offer["last_seen_at"],
        "v": verification.get(offer["url"], 0),
    }


def validate_compact_offers(
    offers: list[dict[str, Any]], *, require_unverified: bool = False,
) -> None:
    expected_keys = set(COMPACT_OFFER_TYPES)
    for index, offer in enumerate(offers):
        if not isinstance(offer, dict) or set(offer) != expected_keys:
            raise RuntimeError(f"compact offer {index} does not have the exact v7 fields")
        if FORBIDDEN_ECONOMICS_FIELDS.intersection(offer):
            raise RuntimeError(f"compact offer {index} contains unsupported economics")
        for key, expected_type in COMPACT_OFFER_TYPES.items():
            if type(offer[key]) is not expected_type:
                raise RuntimeError(
                    f"compact offer {index} field {key} has an invalid v7 type"
                )
        if not re.fullmatch(r"[0-9a-f]{64}", offer["id"]):
            raise RuntimeError(f"compact offer {index} has an invalid public ID")
        if not math.isfinite(offer["sp"]) or not math.isfinite(offer["dp"]):
            raise RuntimeError(f"compact offer {index} has a non-finite percentage")
        if offer["v"] not in {-1, 0, 1} or (require_unverified and offer["v"] != 0):
            raise RuntimeError(f"compact offer {index} has an invalid verification state")


def canonical_offer_fields_sha256(offers: list[dict[str, Any]]) -> str:
    """Seal ordered provisional v7 offers as canonical newline-delimited JSON."""
    validate_compact_offers(offers, require_unverified=True)
    digest = hashlib.sha256()
    for offer in offers:
        digest.update(
            json.dumps(
                offer,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def normalized_host(value: Any) -> str:
    try:
        host = (urlsplit(str(value or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def browser_evidence_expected(result: dict[str, Any]) -> bool:
    """Mirror validator browser eligibility after merging attempted results."""
    if "direct_reason" in result:
        return True
    paruvendu_protection_redirect = (
        result.get("reason") == "protection_redirect"
        and normalized_host(result.get("url")) == "paruvendu.fr"
        and normalized_host(result.get("final_url")) == "paruvendu.fr"
    )
    return (
        result.get("status") == "unknown"
        and str(result.get("url") or "").startswith("http")
        and not autoscout24_non_detail_url(result.get("url"))
        and not paruvendu_protection_redirect
    )


def load_validation(
    path: Path,
    expected_timestamp: str,
    *,
    expected_algorithm: str,
    expected_snapshot_sha256: str,
    expected_offer_fields_sha256: str,
    expected_urls: list[str],
    expected_ranked_candidate_rows: int,
) -> tuple[dict[str, int], dict[str, Any]]:
    if not path.exists():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or payload.get("input_updated_at") != expected_timestamp
        or payload.get("input_algorithm") != expected_algorithm
        or payload.get("input_snapshot_sha256") != expected_snapshot_sha256
        or payload.get("input_offer_fields_sha256") != expected_offer_fields_sha256
    ):
        return {}, {}
    states = {"verified": 1, "dead": -1, "unknown": 0}
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != len(expected_urls):
        return {}, {}
    if len(set(expected_urls)) != len(expected_urls):
        raise RuntimeError("provisional board contains duplicate URLs")
    seen_urls: set[str] = set()
    verification: dict[str, int] = {}
    status_counts: Counter[str] = Counter()
    for position, result in enumerate(results):
        if not isinstance(result, dict):
            return {}, {}
        url = result.get("url")
        status = result.get("status")
        if (
            type(url) is not str
            or url != expected_urls[position]
            or url in seen_urls
            or type(result.get("board_rank")) is not int
            or result["board_rank"] != position + 1
            or (
                "direct_reason" in result
                and (
                    not isinstance(result["direct_reason"], str)
                    or not url.startswith("http")
                )
            )
            or type(status) is not str
            or status not in states
            or (status == "verified" and autoscout24_non_detail_url(url))
        ):
            return {}, {}
        seen_urls.add(url)
        verification[url] = states[status]
        status_counts[status] += 1
    if seen_urls != set(expected_urls):
        return {}, {}
    verified_target = payload.get("verified_target")
    target_reached = payload.get("target_reached")
    pool_exhausted = payload.get("pool_exhausted")
    ranked_pool_count = payload.get("ranked_pool_count")
    direct_attempted_count = payload.get("direct_attempted_count")
    browser_target_count = payload.get("browser_target_count")
    browser_attempted_count = payload.get("browser_attempted_count")
    browser_target_ranks = payload.get("browser_target_ranks")
    browser_attempted_ranks = payload.get("browser_attempted_ranks")
    selection_frontier_rank = payload.get("selection_frontier_rank")
    browser_frontier_target_count = payload.get("browser_frontier_target_count")
    browser_frontier_attempted_count = payload.get("browser_frontier_attempted_count")
    browser_frontier_complete = payload.get("browser_frontier_complete")
    ranked_candidate_count = payload.get("ranked_candidate_count")
    ranked_universe_exhausted = payload.get("ranked_universe_exhausted")
    full_input_coverage = payload.get("full_input_coverage")
    expected_universe_exhausted = expected_ranked_candidate_rows <= len(results)
    rank_lists_valid = (
        isinstance(browser_target_ranks, list)
        and isinstance(browser_attempted_ranks, list)
        and all(type(rank) is int for rank in browser_target_ranks)
        and all(type(rank) is int for rank in browser_attempted_ranks)
        and browser_target_ranks == sorted(set(browser_target_ranks))
        and browser_attempted_ranks == sorted(set(browser_attempted_ranks))
        and all(1 <= rank <= len(results) for rank in browser_target_ranks)
        and all(1 <= rank <= len(results) for rank in browser_attempted_ranks)
    )
    if not rank_lists_valid:
        return {}, {}
    target_rank_set = set(browser_target_ranks)
    attempted_rank_set = set(browser_attempted_ranks)
    evidenced_target_ranks = [
        position
        for position, result in enumerate(results, start=1)
        if isinstance(result, dict) and browser_evidence_expected(result)
    ]
    result_attempted_ranks = {
        position
        for position, result in enumerate(results, start=1)
        if isinstance(result, dict) and "direct_reason" in result
    }
    expected_target_ranks = (
        evidenced_target_ranks[:browser_target_count]
        if type(browser_target_count) is int and browser_target_count >= 0
        else []
    )
    verified_ranks = [
        position
        for position, result in enumerate(results, start=1)
        if isinstance(result, dict) and result.get("status") == "verified"
    ]
    expected_frontier = (
        verified_ranks[verified_target - 1]
        if type(verified_target) is int
        and verified_target > 0
        and len(verified_ranks) >= verified_target
        else None
    )
    expected_frontier_target_ranks = (
        [rank for rank in browser_target_ranks if rank <= expected_frontier]
        if isinstance(browser_target_ranks, list) and expected_frontier is not None
        else []
    )
    expected_frontier_attempted = sum(
        rank in attempted_rank_set for rank in expected_frontier_target_ranks
    )
    expected_frontier_complete = (
        expected_frontier is not None
        and expected_frontier_attempted == len(expected_frontier_target_ranks)
        and all(
            not browser_evidence_expected(result) or "direct_reason" in result
            for position, result in enumerate(results, start=1)
            if isinstance(result, dict)
            and position <= expected_frontier
        )
    )
    if (
        type(ranked_pool_count) is not int
        or ranked_pool_count != len(results)
        or type(verified_target) is not int
        or verified_target < 1
        or type(direct_attempted_count) is not int
        or direct_attempted_count != len(results)
        or type(browser_target_count) is not int
        or type(browser_attempted_count) is not int
        or browser_target_count != len(browser_target_ranks)
        or browser_attempted_count != len(browser_attempted_ranks)
        or not attempted_rank_set.issubset(target_rank_set)
        or browser_target_ranks != expected_target_ranks
        or attempted_rank_set != result_attempted_ranks
        or type(target_reached) is not bool
        or type(pool_exhausted) is not bool
        or target_reached != (status_counts["verified"] >= verified_target)
        or not (target_reached or pool_exhausted)
        or type(browser_frontier_complete) is not bool
        or (target_reached and browser_frontier_complete is not True)
        or (
            expected_frontier is not None
            and type(selection_frontier_rank) is not int
        )
        or selection_frontier_rank != expected_frontier
        or type(browser_frontier_target_count) is not int
        or browser_frontier_target_count != len(expected_frontier_target_ranks)
        or type(browser_frontier_attempted_count) is not int
        or browser_frontier_attempted_count != expected_frontier_attempted
        or browser_frontier_complete != expected_frontier_complete
        or (
            pool_exhausted
            and (
                browser_attempted_count != browser_target_count
                or target_rank_set != set(evidenced_target_ranks)
            )
        )
        or type(ranked_candidate_count) is not int
        or ranked_candidate_count != expected_ranked_candidate_rows
        or type(ranked_universe_exhausted) is not bool
        or ranked_universe_exhausted != expected_universe_exhausted
        or full_input_coverage is not True
        or (pool_exhausted and not ranked_universe_exhausted)
    ):
        return {}, {}
    return verification, {
        "schema_version": 1,
        "generated_at": payload.get("generated_at"),
        "checked": len(results),
        "counts": {status: status_counts[status] for status in states},
        "ranked_pool_count": ranked_pool_count,
        "verified_target": verified_target,
        "direct_attempted_count": direct_attempted_count,
        "browser_target_count": browser_target_count,
        "browser_attempted_count": browser_attempted_count,
        "browser_target_ranks": browser_target_ranks,
        "browser_attempted_ranks": browser_attempted_ranks,
        "selection_frontier_rank": selection_frontier_rank,
        "browser_frontier_target_count": browser_frontier_target_count,
        "browser_frontier_attempted_count": browser_frontier_attempted_count,
        "browser_frontier_complete": browser_frontier_complete,
        "target_reached": target_reached,
        "pool_exhausted": pool_exhausted,
        "ranked_candidate_count": ranked_candidate_count,
        "ranked_universe_exhausted": ranked_universe_exhausted,
        "full_input_coverage": full_input_coverage,
        "input_updated_at": expected_timestamp,
        "input_algorithm": expected_algorithm,
        "input_snapshot_sha256": expected_snapshot_sha256,
        "input_offer_fields_sha256": expected_offer_fields_sha256,
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def build(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        universe_count, maximum_last_seen = connection.execute(
            "SELECT COUNT(*), MAX(last_seen_at) FROM offers"
        ).fetchone()
        latest = parse_utc(maximum_last_seen)
        now = datetime.now(UTC)
        if latest is None or latest > now + timedelta(minutes=5) or now - latest > timedelta(hours=24):
            raise RuntimeError("universe has no current, trustworthy last-seen watermark")
        cutoff = latest - timedelta(hours=args.max_observation_age_hours)
        query, parameters = candidate_query(cutoff.isoformat(), latest.year)
        blocked_evidence = load_blocked_source_evidence(
            args.source_policy, args.quarantine_manifest,
        )
        blocked = blocked_evidence.keys
        evidence = ScanEvidence()
        samples: dict[
            tuple[str, int, str, int], dict[str, list[tuple[int, int, str]]]
        ] = defaultdict(lambda: defaultdict(list))
        for offer in eligible_rows(
            connection, query=query, parameters=parameters,
            blocked_source_keys=blocked, evidence=evidence,
        ):
            bounded_sample_add(
                samples[offer["cohort"]][offer["source_family"]],
                offer,
                args.source_sample_cap,
            )
        peer_stats = build_peer_stats(
            samples,
            min_count=args.min_peer_count,
            min_sources=args.min_peer_sources,
            min_countries=args.min_peer_countries,
            max_dispersion=args.max_dispersion,
        )
        ranked_count = 0
        anomaly_low_count = 0

        def ranked_rows() -> Iterator[dict[str, Any]]:
            nonlocal ranked_count, anomaly_low_count
            for offer in eligible_rows(
                connection, query=query, parameters=parameters,
                blocked_source_keys=blocked,
            ):
                stats = peer_stats.get((offer["cohort"], offer["source_family"]))
                if stats is None or offer["price"] >= stats["lower_quartile"]:
                    continue
                if offer["price"] < stats["median"] * args.min_median_ratio:
                    anomaly_low_count += 1
                    continue
                savings = stats["lower_quartile"] - offer["price"]
                conservative_bps = int(round(10_000 * savings / stats["lower_quartile"]))
                median_bps = int(round(10_000 * (stats["median"] - offer["price"]) / stats["median"]))
                if savings <= 0 or conservative_bps <= 0:
                    continue
                ranked_count += 1
                yield {
                    **{
                        key: value for key, value in offer.items()
                        if key not in {"cohort", "source_family"}
                    },
                    "peer_lower_quartile_eur": stats["lower_quartile"],
                    "peer_median_eur": stats["median"],
                    "savings_vs_lower_quartile_eur": savings,
                    "conservative_discount_bps": conservative_bps,
                    "median_discount_bps": median_bps,
                    "peer_count": stats["count"],
                    "peer_source_count": stats["sources"],
                    "peer_country_count": stats["countries"],
                    "peer_dispersion": stats["dispersion"],
                }

        top = heapq.nsmallest(args.top_n, ranked_rows(), key=verbose_rank_key)
        if not top:
            raise RuntimeError("no observed-value candidates survived the stable snapshot")
        data_timestamp = latest.isoformat()
        snapshot_eligible_sha256 = evidence.digest.hexdigest()
        provisional_compacted = [compact_offer(offer, {}) for offer in top]
        offer_fields_sha256 = canonical_offer_fields_sha256(provisional_compacted)
        verification, validation_meta = load_validation(
            args.validation_report,
            data_timestamp,
            expected_algorithm=ALGORITHM,
            expected_snapshot_sha256=snapshot_eligible_sha256,
            expected_offer_fields_sha256=offer_fields_sha256,
            expected_urls=[offer["u"] for offer in provisional_compacted],
            expected_ranked_candidate_rows=ranked_count,
        )
        compacted = [compact_offer(offer, verification) for offer in top]
        validate_compact_offers(compacted)
        built_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        shared = {
            "schema_version": 2,
            "algorithm": ALGORITHM,
            "generated_at": data_timestamp,
            "data_generated_at_utc": data_timestamp,
            "board_built_at_utc": built_at,
            "universe_unique_offers": int(universe_count),
            "observation_cutoff_utc": cutoff.isoformat(),
            "max_observation_age_hours": args.max_observation_age_hours,
            "snapshot_eligible_sha256": snapshot_eligible_sha256,
            "offer_fields_sha256": offer_fields_sha256,
            "source_policy_sha256": blocked_evidence.policy_sha256,
            "quarantine_manifest_sha256": blocked_evidence.quarantine_sha256,
            "blocked_source_keys_sha256": blocked_evidence.keys_sha256,
            "blocked_source_key_count": len(blocked_evidence.keys),
            "policy_blocked_sources": list(blocked_evidence.sources),
            "scanned_recent_rows": evidence.scanned_recent_rows,
            "eligible_observed_rows": evidence.eligible_rows,
            "ranked_candidate_rows": ranked_count,
            "saved_top_rows": len(top),
            "ranking_complete": True,
            "outside_saved_better_than_cutoff": 0,
            "anomalous_low_prices_excluded": anomaly_low_count,
            "peer_method": {
                "cohort": "model_key + registration_year + fuel + 25000km_band",
                "candidate_source_family_excluded": True,
                "source_family_sample_cap": args.source_sample_cap,
                "lower_bound": "type7_p25_advertised_price_eur",
                "center": "type7_p50_advertised_price_eur",
                "minimum_peer_count": args.min_peer_count,
                "minimum_peer_sources": args.min_peer_sources,
                "minimum_peer_countries": args.min_peer_countries,
                "maximum_normalized_mad": args.max_dispersion,
                "minimum_candidate_to_median_ratio": args.min_median_ratio,
            },
            "unsupported_economics_published": 0,
            "methodology_ar": (
                "ترتيب سعري مقارن فقط: سعر الإعلان مقابل الربع الأدنى ووسيط عروض "
                "أوروبية حديثة مماثلة. لا يتضمن ربحًا أو سعر بيع أو جمارك في الجزائر."
            ),
            "rejected_counts": dict(evidence.rejected.most_common()),
            "source_counts": dict(sorted(evidence.sources.items())),
            "country_counts": dict(sorted(evidence.countries.items())),
            "connected_source_count": len(evidence.sources),
            "connected_country_count": len(evidence.countries),
            "displayed_source_count": len({offer["source"] for offer in top}),
            "displayed_country_count": len({offer["country"] for offer in top}),
            "validation": validation_meta,
            "live_verified_offer_count": sum(item["v"] == 1 for item in compacted),
        }
        ranked = {
            **shared,
            "total_all": int(universe_count),
            "qualified": ranked_count,
            "shown": len(top),
            "offers": top,
        }
        board = {
            **shared,
            "updated_utc": data_timestamp,
            "count": len(compacted),
            "scope": "schengen_observed_peer_market",
            "schengen_country_total": len(SCHENGEN_COUNTRIES),
            "offers": compacted,
        }
        return ranked, board
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("/home/krt/car_deal_finder/universe_offers.sqlite"))
    parser.add_argument("--ranked-output", type=Path, default=Path("/home/krt/car_deal_finder/top_offers.json"))
    parser.add_argument("--board-output", type=Path, default=Path("/home/krt/car_deal_finder/mobile_site_local/board.json"))
    parser.add_argument("--validation-report", type=Path, default=Path("/home/krt/car_deal_finder/top400_validation.json"))
    parser.add_argument("--source-policy", type=Path, default=Path("/home/krt/car_deal_finder/schengen_source_policy.json"))
    parser.add_argument("--quarantine-manifest", type=Path, default=Path("/data/car_deal_sonar_export/current/quarantined_sources.json"))
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--max-observation-age-hours", type=int, default=DEFAULT_MAX_OBSERVATION_AGE_HOURS)
    parser.add_argument("--min-peer-count", type=int, default=DEFAULT_MIN_PEER_COUNT)
    parser.add_argument("--min-peer-sources", type=int, default=DEFAULT_MIN_PEER_SOURCES)
    parser.add_argument("--min-peer-countries", type=int, default=DEFAULT_MIN_PEER_COUNTRIES)
    parser.add_argument("--source-sample-cap", type=int, default=DEFAULT_SOURCE_SAMPLE_CAP)
    parser.add_argument("--max-dispersion", type=float, default=DEFAULT_MAX_DISPERSION)
    parser.add_argument("--min-median-ratio", type=float, default=DEFAULT_MIN_MEDIAN_RATIO)
    parser.add_argument("--capability-check", action="store_true")
    args = parser.parse_args()
    if args.capability_check:
        print(f"OBSERVED_VALUE_RANKER_READY algorithm={ALGORITHM}")
        raise SystemExit(0)
    if not args.database.is_file():
        parser.error(f"database is unavailable: {args.database}")
    if not (1 <= args.top_n <= MAX_TOP_N):
        parser.error(f"--top-n must be between 1 and {MAX_TOP_N}")
    if not (1 <= args.max_observation_age_hours <= 168):
        parser.error("--max-observation-age-hours must be 1..168")
    if args.min_peer_count < 8 or args.min_peer_sources < 2 or args.min_peer_countries < 2:
        parser.error("peer evidence thresholds are too small")
    if not (5 <= args.source_sample_cap <= 100):
        parser.error("--source-sample-cap must be 5..100")
    if not (0 < args.max_dispersion <= 0.5 and 0.5 <= args.min_median_ratio < 1):
        parser.error("invalid robustness bounds")
    return args


def main() -> int:
    args = parse_args()
    ranked, board = build(args)
    atomic_json(args.ranked_output, ranked)
    atomic_json(args.board_output, board)
    print(
        "OBSERVED_VALUE_BOARD_PASS "
        f"universe={ranked['total_all']} recent={ranked['scanned_recent_rows']} "
        f"eligible={ranked['eligible_observed_rows']} ranked={ranked['qualified']} "
        f"saved={ranked['shown']} verified={board['live_verified_offer_count']} "
        f"timestamp={ranked['generated_at']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
