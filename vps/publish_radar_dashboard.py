#!/usr/bin/env python3
"""Prepare and publish the encrypted SonarDeals radar dashboard.

The full offer universe stays on the VPS.  Only a deterministic, audited slice
of the highest-ranked eligible offers is encrypted and pushed to GitHub Pages.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listing_availability as lifecycle
from source_identity import autoscout24_non_detail_url


MAGIC = b"DZAR1"
PBKDF2_ITERATIONS = 310_000
ALGORITHM_VERSION = "schengen-observed-peer-value-v7-live-verified"
CONTRACT_SCHEMA_VERSION = 2
PUBLICATION_VALIDATION_MAX_AGE_HOURS = 8
PUBLICATION_VALIDATION_MAX_AGE = timedelta(
    hours=PUBLICATION_VALIDATION_MAX_AGE_HOURS
)
PUBLICATION_DATA_MAX_AGE_HOURS = 8
PUBLICATION_DATA_MAX_AGE = timedelta(hours=PUBLICATION_DATA_MAX_AGE_HOURS)
PUBLICATION_DATA_FUTURE_SKEW_ALLOWANCE = timedelta(minutes=5)
DEFAULT_ROOT = Path("/home/krt/car_deal_finder")
DEFAULT_SITE = Path("/srv/sonardeals-radar/site")
DEFAULT_PIN = Path("/etc/sonardeals-radar/pin")
DEFAULT_INDEX = Path("/opt/sonardeals-radar/dashboard/index.html")
DEFAULT_FX = Path("/var/lib/sonardeals-radar/fx/display_currency.json")
DEFAULT_AUDIT = Path("/var/lib/sonardeals-radar/latest_selection_manifest.json")
DEFAULT_SELECTION_AUDIT = Path(
    "/var/lib/sonardeals-radar/latest_selection_audit.json"
)
SCHENGEN_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
        "FR", "GR", "HR", "HU", "IS", "IT", "LI", "LT", "LU", "LV",
        "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
    }
)
COMPACT_OFFER_FIELDS = frozenset(
    {
        "id", "m", "t", "p", "q1", "mp", "sv", "sp", "dp", "pn",
        "ps", "pc", "y", "km", "f", "c", "s", "u", "ls", "v",
    }
)
RAW_OFFER_FIELDS = frozenset(
    {
        "id", "model", "title", "source", "url", "price", "year",
        "mileage", "fuel", "country", "seller", "last_seen_at",
        "peer_lower_quartile_eur", "peer_median_eur",
        "savings_vs_lower_quartile_eur", "conservative_discount_bps",
        "median_discount_bps", "peer_count", "peer_source_count",
        "peer_country_count", "peer_dispersion",
    }
)
FORBIDDEN_ECONOMICS_FIELDS = frozenset(
    {
        "pr", "ep", "prd", "roi", "er", "rd", "ld", "cd", "ci", "cb", "cr",
        "profit", "effective_profit", "profit_dzd", "effective_profit_dzd",
        "landed_cost", "landed_cost_dzd", "resale", "resale_dzd",
        "customs", "customs_dzd", "algerian_price", "algerian_price_dzd",
        "confidence",
    }
)

SEMANTIC_PRICE_PATTERNS = (
    ("cesja", re.compile(r"\bcesja\b")),
    ("lease", re.compile(r"\b(?:leasing|lease|lizing|лизинг)\b")),
    ("transfer", re.compile(
        r"\b(?:lease|leasing|contract|contrat|contrato|umow\w*|najem)\b.{0,40}"
        r"\b(?:takeover|transfer|assignment|cession|cesion|cessione|subentro|preluare|ubernahme|ubertragung)\b|"
        r"\b(?:takeover|transfer|assignment|cession|cesion|cessione|subentro|preluare|ubernahme|ubertragung)\b.{0,40}"
        r"\b(?:lease|leasing|contract|contrat|contrato|umow\w*|najem)\b"
    )),
    ("deposit", re.compile(
        r"\b(?:down\s*payment|deposit|transfer\s*fee|kaucja|zaliczka|wplata\s+wlasna|"
        r"oplata\s+wstepna|anzahlung|abloese(?:gebuhr)?|kaution|acompte|apport\s+initial|"
        r"depot\s+de\s+garantie|anticipo|deposito|entrada|avans|aanbetaling|borgsom|kontantinsats)\b"
    )),
    ("instalment", re.compile(
        r"\b(?:installments?|instalments?|monthly\s+payment|finance\s+payment|monatsrate|"
        r"ratenzahlung|mensualite|cuota\s+mensual|maandtermijn|manadsavgift|manedlig\s+ydelse|"
        r"rata|raty|rataln\w*|miesieczn\w*\s+rat\w*|credit\s+restant|restschuld)\b"
    )),
    ("finance-only", re.compile(r"\b(?:financement|finanzierung|kredyt|flex\s+lease|loa|lld)\b")),
    ("takeover-fee", re.compile(r"\b(?:odstepne|takeover)\b")),
)
RISK_PATTERN = re.compile(
    r"\b(?:salvage|accident(?:ed)?|damaged|unfall|motorschaden|bastler|epave|"
    # Text is accent-folded before matching. Keep the French participles
    # explicit so endommagement/endommager do not become broad false positives.
    r"endommag(?:e|ee|es|ees)|"
    r"sinistr\w*|uszkodz\w*|powypadk\w*|pour\s+pieces|parts\s+only|non\s+runner)\b"
)


def normalized_semantic_text(value: Any) -> str:
    folded = unicodedata.normalize(
        "NFKD", str(value or "").casefold().translate(
            str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss"})
        )
    )
    return " ".join(
        "".join(ch for ch in folded if not unicodedata.combining(ch)).split()
    )


def semantic_price_reason(value: Any) -> str | None:
    text = normalized_semantic_text(value)
    return next(
        (name for name, pattern in SEMANTIC_PRICE_PATTERNS if pattern.search(text)),
        None,
    )


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def integer(value: Any) -> int:
    return int(round(number(value)))


def canonical_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def valid_timestamp(value: Any) -> bool:
    return canonical_timestamp(value) is not None


def parsed_utc(value: Any) -> datetime | None:
    canonical = canonical_timestamp(value)
    if canonical is None:
        return None
    return datetime.fromisoformat(canonical)


def require_publishable_validation(
    validation: Any, *, now: datetime | None = None,
) -> datetime:
    """Reject evidence the dashboard would already present as stale."""
    if not isinstance(validation, dict):
        raise RuntimeError("board validation evidence is invalid or incomplete")
    generated = parsed_utc(validation.get("generated_at"))
    if generated is None:
        raise RuntimeError("board validation evidence is invalid or incomplete")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("publication freshness time must be timezone-aware")
    age = current.astimezone(UTC) - generated
    if age < timedelta(0):
        raise RuntimeError("board validation evidence is timestamped in the future")
    if age >= PUBLICATION_VALIDATION_MAX_AGE:
        raise RuntimeError(
            "board validation evidence is stale; re-validate before publishing"
        )
    return generated


def require_publishable_data_timestamp(
    value: Any, *, now: datetime | None = None,
) -> datetime:
    """Reject board data the dashboard would present as stale or future-skewed."""
    generated = parsed_utc(value)
    if generated is None:
        raise RuntimeError("board data timestamp is invalid or incomplete")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("publication freshness time must be timezone-aware")
    age = current.astimezone(UTC) - generated
    if age < -PUBLICATION_DATA_FUTURE_SKEW_ALLOWANCE:
        raise RuntimeError("board data timestamp is too far in the future")
    if age >= PUBLICATION_DATA_MAX_AGE:
        raise RuntimeError("board data is stale; refresh before publishing")
    return generated


def canonical_id(offer: dict[str, Any]) -> str:
    explicit = str(offer.get("id") or "").strip()
    if explicit:
        return explicit
    material = f"{offer.get('s', '')}\0{offer.get('u', '')}".encode("utf-8")
    return "url_" + hashlib.sha256(material).hexdigest()[:24]


def valid_https_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and len(str(value)) <= 2048
    )


def eligible_offer(offer: dict[str, Any]) -> bool:
    if set(offer) != COMPACT_OFFER_FIELDS or FORBIDDEN_ECONOMICS_FIELDS.intersection(offer):
        return False
    if any(
        not isinstance(offer.get(field), str) or not str(offer[field]).strip()
        for field in ("id", "m", "t", "f", "c", "s", "u", "ls")
    ):
        return False
    if any(type(offer.get(field)) is not int for field in ("p", "q1", "mp", "sv", "pn", "ps", "pc", "y", "km", "v")):
        return False
    if any(
        isinstance(offer.get(field), bool)
        or not isinstance(offer.get(field), (int, float))
        or not math.isfinite(float(offer[field]))
        for field in ("sp", "dp")
    ):
        return False
    title = f"{offer.get('t', '')} {offer.get('m', '')}".casefold()
    if semantic_price_reason(title) is not None:
        return False
    if RISK_PATTERN.search(normalized_semantic_text(title)) is not None:
        return False
    price = integer(offer.get("p"))
    lower_quartile = integer(offer.get("q1"))
    peer_median = integer(offer.get("mp"))
    savings = integer(offer.get("sv"))
    conservative_discount = number(offer.get("sp"))
    median_discount = number(offer.get("dp"))
    peer_count = integer(offer.get("pn"))
    peer_sources = integer(offer.get("ps"))
    peer_countries = integer(offer.get("pc"))
    return (
        str(offer["c"]).upper() in SCHENGEN_COUNTRIES
        and offer["f"] in {"petrol", "hybrid"}
        and valid_https_url(offer.get("u"))
        and not autoscout24_non_detail_url(offer.get("u"))
        and valid_timestamp(offer.get("ls"))
        and 4_000 <= price <= 45_000
        and lower_quartile > price
        and peer_median >= lower_quartile
        and savings == lower_quartile - price
        and 0 < conservative_discount < 100
        and conservative_discount <= median_discount < 100
        and integer(10_000 * savings / lower_quartile) == integer(100 * conservative_discount)
        and integer(10_000 * (peer_median - price) / peer_median) == integer(100 * median_discount)
        and peer_count >= 20
        and peer_sources >= 3
        and peer_countries >= 2
        and integer(offer.get("v")) == 1
    )


def rank_key(offer: dict[str, Any]) -> tuple[Any, ...]:
    """Lower tuple is better; this is the public dashboard's ranking contract."""
    return (
        -integer(number(offer.get("sp")) * 100),
        -integer(offer.get("sv")),
        -integer(number(offer.get("dp")) * 100),
        -integer(offer.get("ps")),
        -integer(offer.get("pn")),
        -integer(offer.get("y")),
        integer(offer.get("km")),
        number(offer.get("p")),
        canonical_id(offer),
    )


def compact_ranked_offer(
    offer: dict[str, Any], verification: int,
) -> dict[str, Any]:
    """Project one exact ranked-evidence row into the public v7 contract."""
    if set(offer) != RAW_OFFER_FIELDS:
        raise RuntimeError("ranked offer does not have the exact v7 fields")
    return {
        "id": offer["id"],
        "m": offer["model"],
        "t": offer["title"],
        "p": offer["price"],
        "q1": offer["peer_lower_quartile_eur"],
        "mp": offer["peer_median_eur"],
        "sv": offer["savings_vs_lower_quartile_eur"],
        "sp": round(offer["conservative_discount_bps"] / 100, 2),
        "dp": round(offer["median_discount_bps"] / 100, 2),
        "pn": offer["peer_count"],
        "ps": offer["peer_source_count"],
        "pc": offer["peer_country_count"],
        "y": offer["year"],
        "km": offer["mileage"],
        "f": offer["fuel"],
        "c": offer["country"],
        "s": offer["source"],
        "u": offer["url"],
        "ls": offer["last_seen_at"],
        "v": verification,
    }


def normalized_candidates(offers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: set[str] = set()
    by_url: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for raw in offers:
        if not isinstance(raw, dict) or not eligible_offer(raw):
            continue
        offer = dict(raw)
        offer["id"] = canonical_id(offer)
        url = str(offer.get("u") or "").strip()
        if offer["id"] in by_id or url in by_url:
            continue
        by_id.add(offer["id"])
        by_url.add(url)
        candidates.append(offer)
    return sorted(candidates, key=rank_key)


def select_offers(
    candidates: list[dict[str, Any]],
    top_n: int,
    per_country_min: int,
    per_source_min: int,
) -> list[dict[str, Any]]:
    """Return the literal global top-N without quota-based substitutions.

    The legacy minimum arguments remain in the callable interface so older
    service invocations do not break, but coverage quotas must never displace a
    better-ranked deal from a dashboard labelled as the global best selection.
    """
    del per_country_min, per_source_min
    if top_n <= 0:
        return list(candidates)
    return list(candidates[:top_n])


def digest_ids(offers: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for offer in offers:
        digest.update(canonical_id(offer).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def digest_fields(offers: Iterable[dict[str, Any]]) -> str:
    """Bind generation identity to every public field, not IDs alone."""
    digest = hashlib.sha256()
    for offer in offers:
        digest.update(
            json.dumps(
                offer, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def optional_sha256_file(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def encrypt_payload(pin: str, payload: dict[str, Any]) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        iterations=PBKDF2_ITERATIONS,
    ).derive(pin.encode("utf-8"))
    raw = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=9,
    )
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, raw, None)


def universe_metrics(database: Path) -> dict[str, Any]:
    if not database.exists():
        return {"universe_unique_offers": 0, "universe_last_seen_at": None}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        count, last_seen = connection.execute(
            "SELECT COUNT(*), MAX(last_seen_at) FROM offers"
        ).fetchone()
        return {
            "universe_unique_offers": int(count or 0),
            "universe_last_seen_at": canonical_timestamp(last_seen),
        }
    finally:
        connection.close()


AUCTION_LANE_SCHEMA_VERSION = 1
AUCTION_LANE_REQUIRED_FIELDS = frozenset({
    "id", "source", "source_key", "registry_key", "registry_priority",
    "url", "title", "model", "country", "year", "mileage", "fuel", "seller",
    "current_bid_eur", "canonical_end_utc", "ends_soon", "first_seen_at",
    "last_seen_at", "access_sale_note", "evidence",
})
AUCTION_LANE_OPTIONAL_FIELDS = frozenset({"ouedkniss_reference"})


def validate_auction_lane(lane: Any) -> None:
    """Fail closed unless the lane is exactly the accepted auction contract.

    Raises RuntimeError (never silently drops rows, never weakens the gate).
    """
    if not isinstance(lane, dict):
        raise RuntimeError("auction lane is not an object")
    if lane.get("schema_version") != AUCTION_LANE_SCHEMA_VERSION:
        raise RuntimeError("unsupported auction lane schema")
    if lane.get("lane") != "auction":
        raise RuntimeError("auction lane field mismatch")
    registry_digest = lane.get("registry_digest")
    if not isinstance(registry_digest, str) or len(registry_digest) < 8:
        raise RuntimeError("auction lane registry digest is invalid")
    generated_at = lane.get("generated_at_utc")
    if not valid_timestamp(generated_at):
        raise RuntimeError("auction lane generation timestamp is invalid")
    rows = lane.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("auction lane rows are not a list")
    if lane.get("lane_count") != len(rows):
        raise RuntimeError("auction lane count does not match its rows")
    if rows:
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RuntimeError(f"auction lane row {index} is not an object")
            fields = set(row)
            if not AUCTION_LANE_REQUIRED_FIELDS.issubset(fields) or fields - (
                AUCTION_LANE_REQUIRED_FIELDS | AUCTION_LANE_OPTIONAL_FIELDS
            ):
                raise RuntimeError(f"auction lane row {index} does not have the accepted lane fields")
            row_id = str(row.get("id") or "").strip()
            url = str(row.get("url") or "").strip()
            if not row_id or not url:
                raise RuntimeError(f"auction lane row {index} is missing id or url")
            if row_id in seen_ids or url in seen_urls:
                raise RuntimeError(f"auction lane row {index} duplicates id or url")
            seen_ids.add(row_id)
            seen_urls.add(url)
            bid = row.get("current_bid_eur")
            if type(bid) is not int or bid <= 0:
                raise RuntimeError(f"auction lane row {index} has an invalid current bid")
            if type(row.get("ends_soon")) is not bool:
                raise RuntimeError(f"auction lane row {index} has an invalid ends_soon")
            end = parsed_utc(row.get("canonical_end_utc"))
            if end is None:
                raise RuntimeError(f"auction lane row {index} has an invalid canonical end")
            priority = row.get("registry_priority")
            if type(priority) is not int or priority < 1:
                raise RuntimeError(f"auction lane row {index} has an invalid registry priority")
            if not isinstance(row.get("evidence"), str) or not row["evidence"]:
                raise RuntimeError(f"auction lane row {index} is missing source evidence")
            reference = row.get("ouedkniss_reference")
            if reference is not None:
                if not isinstance(reference, dict):
                    raise RuntimeError(f"auction lane row {index} has an invalid Ouedkniss reference")
                if (
                    type(reference.get("average_dzd")) is not int
                    or reference["average_dzd"] <= 0
                    or type(reference.get("sample_count")) is not int
                    or reference["sample_count"] < 2
                    or reference.get("source") != "Ouedkniss"
                    or not valid_timestamp(reference.get("observed_at_utc"))
                ):
                    raise RuntimeError(f"auction lane row {index} has an invalid Ouedkniss reference")


def embed_auction_lane(
    lane: dict[str, Any],
    data_generated_at: str,
    generation_id: str,
    published_ids: set[str],
    published_urls: set[str],
) -> dict[str, Any]:
    """Embed the lane, generation-bound, after fail-closed validation."""
    validate_auction_lane(lane)
    for row in lane["rows"]:
        if str(row.get("id") or "") in published_ids or str(row.get("url") or "") in published_urls:
            raise RuntimeError("auction lane row overlaps the regular lane")
    return {
        "schema_version": AUCTION_LANE_SCHEMA_VERSION,
        "lane": "auction",
        "registry_digest": lane["registry_digest"],
        "generated_at_utc": lane["generated_at_utc"],
        "bound_generation_id": generation_id,
        "bound_data_generated_at_utc": data_generated_at,
        "lane_count": lane["lane_count"],
        "rows": lane["rows"],
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def load_dashboard_pin(path: Path) -> str:
    """Load either a legacy plain-text PIN or the current JSON secret format."""
    raw = path.read_text(encoding="utf-8").strip()
    try:
        secret = json.loads(raw)
    except json.JSONDecodeError:
        pin = raw
    else:
        pin = str(secret.get("pin") or "").strip() if isinstance(secret, dict) else raw
    if len(pin) < 8:
        raise RuntimeError("dashboard secret is unexpectedly short")
    return pin


def build_payload(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    board = load_json(args.board)
    if (
        board.get("schema_version") != CONTRACT_SCHEMA_VERSION
        or board.get("algorithm") != ALGORITHM_VERSION
        or board.get("unsupported_economics_published") != 0
    ):
        raise RuntimeError("unsupported observed-value board contract")
    snapshot_digest = board.get("snapshot_eligible_sha256")
    if not isinstance(snapshot_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_digest):
        raise RuntimeError("board snapshot digest is invalid")
    blocked_sources = board.get("policy_blocked_sources")
    source_policy_sha256 = optional_sha256_file(
        args.root / "schengen_source_policy.json"
    )
    if (
        source_policy_sha256 is None
        or
        not isinstance(blocked_sources, list)
        or any(not isinstance(item, str) or not item for item in blocked_sources)
        or blocked_sources != sorted(set(blocked_sources))
        or board.get("blocked_source_key_count") != len(blocked_sources)
        or board.get("blocked_source_keys_sha256") != canonical_json_sha256(blocked_sources)
        or board.get("source_policy_sha256")
        != source_policy_sha256
        or board.get("quarantine_manifest_sha256")
        != optional_sha256_file(Path("/data/car_deal_sonar_export/current/quarantined_sources.json"))
    ):
        raise RuntimeError("board source-policy evidence is invalid or stale")
    data_generated_at = board.get("data_generated_at_utc")
    if (
        not valid_timestamp(data_generated_at)
        or board.get("generated_at") != data_generated_at
        or board.get("updated_utc") != data_generated_at
    ):
        raise RuntimeError("board data timestamp is invalid or inconsistent")
    require_publishable_data_timestamp(data_generated_at)
    offers = board.get("offers")
    if not isinstance(offers, list) or not offers:
        raise RuntimeError("refusing to publish an empty board")
    validation = board.get("validation")
    if (
        not isinstance(validation, dict)
        or validation.get("schema_version") != 1
        or validation.get("input_updated_at") != data_generated_at
        or not valid_timestamp(validation.get("generated_at"))
        or validation.get("checked") != len(offers)
    ):
        raise RuntimeError("board validation evidence is invalid or incomplete")
    require_publishable_validation(validation)
    candidates = normalized_candidates(offers)
    if not candidates:
        raise RuntimeError("no eligible offers survived publication checks")
    selected = select_offers(
        candidates, args.top_n, args.per_country_min, args.per_source_min
    )
    if not args.ranked_meta.is_file():
        raise RuntimeError("ranked observed-value evidence is unavailable")
    ranked_meta = load_json(args.ranked_meta)
    if (
        ranked_meta.get("schema_version") != CONTRACT_SCHEMA_VERSION
        or ranked_meta.get("algorithm") != ALGORITHM_VERSION
        or ranked_meta.get("generated_at") != data_generated_at
        or ranked_meta.get("snapshot_eligible_sha256") != snapshot_digest
        or ranked_meta.get("unsupported_economics_published") != 0
        or any(
            ranked_meta.get(key) != board.get(key)
            for key in (
                "offer_fields_sha256", "source_policy_sha256",
                "quarantine_manifest_sha256", "blocked_source_keys_sha256",
                "blocked_source_key_count", "policy_blocked_sources",
            )
        )
    ):
        raise RuntimeError("ranked evidence does not match the board snapshot")
    ranked_offers = ranked_meta.get("offers")
    if (
        not isinstance(ranked_offers, list)
        or len(ranked_offers) != len(offers)
        or ranked_meta.get("shown") != len(ranked_offers)
    ):
        raise RuntimeError("ranked evidence does not cover the exact board")
    try:
        projected_board = [
            compact_ranked_offer(ranked, compact["v"])
            for ranked, compact in zip(ranked_offers, offers, strict=True)
            if isinstance(ranked, dict) and isinstance(compact, dict)
        ]
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise RuntimeError("ranked evidence cannot be projected to the board") from exc
    if len(projected_board) != len(offers) or projected_board != offers:
        raise RuntimeError("ranked offer fields do not match the board")
    metrics = universe_metrics(args.database)
    if (
        type(board.get("universe_unique_offers")) is not int
        or board["universe_unique_offers"] != metrics["universe_unique_offers"]
        or metrics["universe_last_seen_at"] != data_generated_at
        or ranked_meta.get("total_all") != board["universe_unique_offers"]
    ):
        raise RuntimeError("universe changed after the observed-value snapshot")
    candidate_hash = digest_ids(candidates)
    selected_hash = digest_ids(selected)
    candidate_fields_hash = digest_fields(candidates)
    selected_fields_hash = digest_fields(selected)
    published_ids = {str(offer.get("id") or "") for offer in selected}
    published_urls = {str(offer.get("u") or "") for offer in selected}
    auction_lane_source: dict[str, Any] | None = None
    auction_lane_input_sha256: str | None = None
    auction_lane_path = getattr(args, "auction_lane", None)
    if auction_lane_path is not None and auction_lane_path.is_file():
        try:
            lane = json.loads(auction_lane_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"auction lane file is unreadable: {exc}") from exc
        validate_auction_lane(lane)
        for row in lane["rows"]:
            if (
                str(row.get("id") or "") in published_ids
                or str(row.get("url") or "") in published_urls
            ):
                raise RuntimeError("auction lane row overlaps the regular lane")
        auction_lane_source = lane
        auction_lane_input_sha256 = canonical_json_sha256(lane)

    generation_material = (
        f"{ALGORITHM_VERSION}\n{data_generated_at}\n"
        f"{candidate_fields_hash}\n{selected_fields_hash}\n"
    )
    if auction_lane_input_sha256 is not None:
        generation_material += f"auction:{auction_lane_input_sha256}\n"
    generation_id = hashlib.sha256(generation_material.encode("utf-8")).hexdigest()[:16]

    payload = {key: value for key, value in board.items() if key != "offers"}
    payload.update(
        {
            "published_at_utc": utc_now(),
            "count": len(selected),
            "universe_unique_offers": board["universe_unique_offers"],
            "universe_last_seen_at": metrics["universe_last_seen_at"],
            "ranked_offer_count": int(ranked_meta.get("qualified") or 0),
            "qualified_universe_offers": len(candidates),
            "published_offer_count": len(selected),
            "verified_live_count": sum(integer(offer.get("v")) == 1 for offer in selected),
            "displayed_country_count": len({str(offer.get("c") or "") for offer in selected}),
            "displayed_source_count": len({str(offer.get("s") or "") for offer in selected}),
            "selection_universe_count": len(candidates),
            "selection_algorithm": ALGORITHM_VERSION,
            "selection_candidate_sha256": candidate_hash,
            "selected_ids_sha256": selected_hash,
            "selection_candidate_fields_sha256": candidate_fields_hash,
            "selected_fields_sha256": selected_fields_hash,
            "generation_id": generation_id,
            "selection": {
                "top_n": args.top_n,
                "strict_global_order": True,
                "coverage_quota_substitutions": 0,
                "ranking": (
                    "observed European peer price: lower-quartile discount, euro gap, "
                    "median discount, peer diversity, model year and mileage"
                ),
                "algeria_economics_included": False,
            },
            "selection_input_counts": {
                "universe_unique_after_source_identity_dedupe": board["universe_unique_offers"],
                "recent_snapshot_rows_scanned": int(ranked_meta.get("scanned_recent_rows") or 0),
                "eligible_observed_rows": int(ranked_meta.get("eligible_observed_rows") or 0),
                "peer_ranked_candidates": int(ranked_meta.get("qualified") or 0),
                "ranking_saved": int(ranked_meta.get("shown") or 0),
                "publication_candidates_after_all_filters_and_dedupe": len(candidates),
            },
            "offers": selected,
        }
    )
    auction_lane_block: dict[str, Any] | None = None
    auction_lane_sha256: str | None = None
    if auction_lane_source is not None:
        auction_lane_block = embed_auction_lane(
            auction_lane_source,
            data_generated_at,
            generation_id,
            published_ids,
            published_urls,
        )
        auction_lane_sha256 = canonical_json_sha256(auction_lane_block)
        payload["auction_lane"] = auction_lane_block
    manifest = {
        "schema_version": 1,
        "prepared_at": payload["published_at_utc"],
        "generation_id": generation_id,
        "algorithm": ALGORITHM_VERSION,
        "source_board": str(args.board),
        "source_board_sha256": sha256_file(args.board),
        "universe_database": str(args.database),
        "universe_unique_offers": metrics["universe_unique_offers"],
        "ranked_offer_count": payload["ranked_offer_count"],
        "qualified_universe_offers": len(candidates),
        "published_offer_count": len(selected),
        "verified_live_count": payload["verified_live_count"],
        "candidate_ids_sha256": candidate_hash,
        "selected_ids_sha256": selected_hash,
        "candidate_fields_sha256": candidate_fields_hash,
        "selected_fields_sha256": selected_fields_hash,
        "connected_country_count": payload.get("connected_country_count", 0),
        "connected_source_count": payload.get("connected_source_count", 0),
        "data_generated_at_utc": payload.get("data_generated_at_utc"),
        "snapshot_eligible_sha256": snapshot_digest,
        "selection_input_counts": payload["selection_input_counts"],
        "auction_lane_count": None if auction_lane_block is None else auction_lane_block["lane_count"],
        "auction_lane_sha256": auction_lane_sha256,
        "auction_lane_registry_digest": None if auction_lane_block is None
            else auction_lane_block["registry_digest"],
    }
    return payload, manifest


def prepare(args: argparse.Namespace) -> None:
    if not args.pin.is_file():
        raise RuntimeError(f"PIN secret is unavailable: {args.pin}")
    pin = load_dashboard_pin(args.pin)
    payload, manifest = build_payload(args)
    if not args.index.is_file():
        raise RuntimeError(f"dashboard index is unavailable: {args.index}")
    args.site.mkdir(parents=True, exist_ok=True)
    atomic_write(
        args.site / ".gitignore",
        b"board.json\n__pycache__/\n*.pyc\nworker/.dev.vars\nworker/.wrangler/\nworker/node_modules/\n",
        0o644,
    )
    atomic_write(args.site / "index.html", args.index.read_bytes(), 0o644)
    atomic_write(args.site / "data.enc", encrypt_payload(pin, payload), 0o600)
    fx_bytes: bytes | None = None
    try:
        fx_bytes = args.fx_config.read_bytes() if args.fx_config.is_file() else None
    except OSError:
        fx_bytes = None
    if fx_bytes is not None:
        if not fx_bytes.strip().startswith(b"{"):
            raise RuntimeError("fx display-currency config is not a JSON object")
        atomic_write(args.site / "display_currency.json", fx_bytes, 0o644)
    else:
        (args.site / "display_currency.json").unlink(missing_ok=True)
    atomic_write(
        args.audit_manifest,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        0o600,
    )
    (args.site / "board.json").unlink(missing_ok=True)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


def run_git(site: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = dict(
        os.environ,
        GIT_AUTHOR_NAME="SonarDeals Radar",
        GIT_AUTHOR_EMAIL="radar@sonardeals.com",
        GIT_COMMITTER_NAME="SonarDeals Radar",
        GIT_COMMITTER_EMAIL="radar@sonardeals.com",
    )
    return subprocess.run(
        ["git", "-C", str(site), *arguments], check=check,
        capture_output=True, text=True, env=environment,
    )


def is_git_checkout(site: Path) -> bool:
    metadata = site / ".git"
    if not (metadata.is_dir() or metadata.is_file()):
        return False
    result = run_git(site, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def enforce_publication_audit(args: argparse.Namespace) -> None:
    """Require the independent, same-generation audit before Git mutation."""
    manifest = load_json(args.audit_manifest)
    audit = load_json(args.selection_audit)
    board = load_json(args.board)
    if sha256_file(args.board) != manifest.get("source_board_sha256"):
        raise RuntimeError("publication manifest does not match the current board")
    require_publishable_data_timestamp(board.get("data_generated_at_utc"))
    require_publishable_validation(board.get("validation"))
    if audit.get("result") != "BEST_SELECTION_AUDIT_PASS":
        raise RuntimeError("selection audit did not pass")
    for key in (
        "generation_id", "algorithm", "data_generated_at_utc",
        "candidate_ids_sha256", "selected_ids_sha256",
        "candidate_fields_sha256", "selected_fields_sha256",
        "universe_unique_offers", "qualified_universe_offers",
        "published_offer_count", "verified_live_count",
        "snapshot_eligible_sha256",
        "source_board_sha256",
    ):
        if audit.get(key) != manifest.get(key):
            raise RuntimeError(f"selection audit does not match manifest: {key}")
    for key in ("auction_lane_count", "auction_lane_sha256", "auction_lane_registry_digest"):
        if audit.get(key) != manifest.get(key):
            raise RuntimeError(f"selection audit does not match manifest: {key}")
    if manifest.get("verified_live_count") != manifest.get("published_offer_count"):
        raise RuntimeError("publication contains links not verified in this generation")


def publish(args: argparse.Namespace) -> None:
    enforce_publication_audit(args)
    if not is_git_checkout(args.site):
        raise RuntimeError(f"publication directory is not a git checkout: {args.site}")
    (args.site / "board.json").unlink(missing_ok=True)
    run_git(args.site, "rm", "--cached", "--ignore-unmatch", "board.json", check=False)
    run_git(args.site, "add", "--", ".gitignore", "index.html", "data.enc", "display_currency.json")
    staged = {
        line.strip()
        for line in run_git(args.site, "diff", "--cached", "--name-only").stdout.splitlines()
        if line.strip()
    }
    allowed = {".gitignore", "index.html", "data.enc", "display_currency.json", "board.json"}
    unexpected = staged - allowed
    if unexpected:
        raise RuntimeError(f"refusing to publish unexpected files: {sorted(unexpected)}")
    if not staged:
        print("RADAR_PUBLISH_NO_CHANGES")
        return
    run_git(args.site, "diff", "--cached", "--check")
    manifest = load_json(args.audit_manifest)
    run_git(args.site, "commit", "-m", f"radar {manifest['generation_id']}")
    run_git(args.site, "push", "origin", "HEAD:main")
    print(f"RADAR_PUBLISH_PASS generation={manifest['generation_id']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--site", type=Path, default=DEFAULT_SITE)
    parser.add_argument("--pin", type=Path, default=DEFAULT_PIN)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--fx-config", type=Path, default=DEFAULT_FX)
    parser.add_argument("--audit-manifest", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--selection-audit", type=Path, default=DEFAULT_SELECTION_AUDIT)
    parser.add_argument("--top-n", type=int, default=10_000)
    parser.add_argument("--per-country-min", type=int, default=20)
    parser.add_argument("--per-source-min", type=int, default=5)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--push-only", action="store_true")
    parser.add_argument("--auction-lane", type=Path, default=None,
                        help="auction lane JSON; omitted when absent (toggle hidden)")
    args = parser.parse_args()
    args.board = args.root / "mobile_site_local" / "board.json"
    args.database = args.root / "universe_offers.sqlite"
    args.ranked_meta = args.root / "top_offers.json"
    if args.auction_lane is None:
        args.auction_lane = args.root / "mobile_site_local" / "auction_lane.json"
    if args.prepare_only and args.push_only:
        parser.error("--prepare-only and --push-only are mutually exclusive")
    return args


def main() -> int:
    args = parse_args()
    if not args.push_only:
        prepare(args)
    if not args.prepare_only:
        publish(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
