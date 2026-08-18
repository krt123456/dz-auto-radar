#!/usr/bin/env python3
"""Independently verify the encrypted dashboard selection before publication."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from .source_identity import autoscout24_non_detail_url
except ImportError:
    from source_identity import autoscout24_non_detail_url


MAGIC = b"DZAR1"
ITERATIONS = 310_000
ALGORITHM = "schengen-observed-peer-value-v7-live-verified"
CONTRACT_SCHEMA_VERSION = 2
MAX_RECEIPT_COUNT = 100_000_000
MAX_CONNECTED_COUNT = 100_000
SCHENGEN_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
        "FR", "GR", "HR", "HU", "IS", "IT", "LI", "LT", "LU", "LV",
        "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
    }
)
EV_TELLS = (
    "kwh", "ioniq", "ev6", "ev9", "ev3", "ev4", "enyaq", "e-tron",
    "id.3", "id.4", "id.5", "id.7", "tesla", "model 3", "model y",
    "polestar", "mg4", "vinfast", " born", "spring", "leaf", "zoe",
    "electric", "elektri", "électri", "електри",
)
UNSUPPORTED_POWERTRAIN_PATTERN = re.compile(
    r"\b(?:diesel|dizel|tdi|hdi|dci|cdi|crdi|jtd|multijet|ecoblue|bluedci|bluehdi|"
    r"d-4d|tdci|electric|electrique|elektrisch|elektro|elettrica|ev|bev|tesla|leaf|"
    r"zoe|ioniq|enyaq|polestar|vinfast|e-tron|etron|id[ .-]?[3457]|plug[ -]?in|phev|"
    r"gte|ehybrid|e-hybrid|recharge|p400e|t8|lpg|gpl|cng|gnc|tgi)\b"
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
FORBIDDEN_ECONOMICS_FIELDS = frozenset(
    {
        "pr", "ep", "prd", "roi", "er", "rd", "ld", "cd", "ci", "cb", "cr",
        "profit", "effective_profit", "profit_dzd", "effective_profit_dzd",
        "landed_cost", "landed_cost_dzd", "resale", "resale_dzd",
        "customs", "customs_dzd", "algerian_price", "algerian_price_dzd",
        "confidence",
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
VALIDATION_STATES = {"verified": 1, "dead": -1, "unknown": 0}


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


def source_key(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def source_identity_keys(value: Any) -> frozenset[str]:
    key_value = source_key(value)
    identities = {key_value} if key_value else set()
    if "autoscout24" in key_value and key_value not in {
        "autoscout24.ch", "autoscout24.ch liechtenstein",
    }:
        identities.add("autoscout24")
    return frozenset(identities)


def ranked_offer_id(offer: dict[str, Any]) -> str:
    explicit = str(offer.get("id") or "").strip()
    if explicit:
        return explicit
    material = f"{offer.get('source', '')}\0{offer.get('url', '')}".encode()
    return "url_" + hashlib.sha256(material).hexdigest()[:24]


def ranked_key(offer: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -integer(offer.get("conservative_discount_bps")),
        -integer(offer.get("savings_vs_lower_quartile_eur")),
        -integer(offer.get("median_discount_bps")),
        -integer(offer.get("peer_source_count")),
        -integer(offer.get("peer_count")),
        -integer(offer.get("year")),
        integer(offer.get("mileage")), integer(offer.get("price")),
        ranked_offer_id(offer),
    )


def ranked_crosspost_fingerprint(offer: dict[str, Any]) -> tuple[Any, ...]:
    title = re.sub(
        r"[\W_]+", " ", normalized_semantic_text(offer.get("title"))
    ).strip()
    model = re.sub(
        r"[\W_]+", " ", normalized_semantic_text(offer.get("model"))
    ).strip()
    if not title:
        return ("id", ranked_offer_id(offer))
    return (
        title[:120], model[:80], str(offer.get("country") or "").upper(),
        integer(offer.get("year")), int(num(offer.get("price")) // 250),
        int(num(offer.get("mileage")) // 1000),
        int(num(offer.get("engine_cc")) // 100),
    )


def ranked_crosspost_winner_key(offer: dict[str, Any]) -> tuple[Any, ...]:
    return (
        num(offer.get("price")), -integer(offer.get("peer_source_count")),
        -integer(offer.get("peer_count")), ranked_offer_id(offer),
    )


def provisional_offer_fields_digest(offers: list[dict[str, Any]]) -> str:
    value = hashlib.sha256()
    for raw in offers:
        if not isinstance(raw, dict):
            raise AssertionError("board offer is not an object")
        offer = {**raw, "v": 0}
        value.update(
            json.dumps(
                offer, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        )
        value.update(b"\n")
    return value.hexdigest()


def validation_states(root: Path, board: dict[str, Any]) -> dict[str, int]:
    path = root / "top400_validation.json"
    if not path.is_file():
        raise AssertionError("generation-bound validation report is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("validation report is not an object")
    board_offers = board.get("offers")
    if not isinstance(board_offers, list) or not board_offers:
        raise AssertionError("source board contains no offers")
    if any(not isinstance(item, dict) for item in board_offers):
        raise AssertionError("source board offer is not an object")
    expected_urls = [str(item.get("u") or "") for item in board_offers]
    results = value.get("results")
    if (
        value.get("schema_version") != 1
        or value.get("input_updated_at") != board.get("data_generated_at_utc")
        or value.get("input_algorithm") != ALGORITHM
        or value.get("input_snapshot_sha256") != board.get("snapshot_eligible_sha256")
        or value.get("input_offer_fields_sha256") != provisional_offer_fields_digest(board_offers)
        or type(value.get("checked")) is not int
        or value.get("checked") != len(expected_urls)
        or not isinstance(results, list)
        or len(results) != len(expected_urls)
        or len(set(expected_urls)) != len(expected_urls)
    ):
        raise AssertionError("validation report is not bound to the board snapshot")
    states: dict[str, int] = {}
    for item in results:
        if not isinstance(item, dict):
            raise AssertionError("validation result is not an object")
        url = str(item.get("url") or "")
        status = item.get("status")
        if url in states or status not in VALIDATION_STATES:
            raise AssertionError("validation result URL/status is invalid")
        states[url] = VALIDATION_STATES[str(status)]
    if set(states) != set(expected_urls):
        raise AssertionError("validation results do not exactly cover the board")
    return states


def full_ranked_candidates(
    offers: list[dict[str, Any]], board: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    blocked = frozenset(
        source_key(name) for name in board.get("policy_blocked_sources", [])
        if str(name).strip()
    )
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    accepted: list[dict[str, Any]] = []
    counts = {
        "ranked_saved_input": len(offers),
        "eligible_before_publication_dedupe": 0,
        "publication_identity_duplicates_removed": 0,
        "semantic_price_rejections": 0,
        "risk_rejections": 0,
    }
    for raw in offers:
        if not isinstance(raw, dict) or set(raw) != RAW_OFFER_FIELDS:
            continue
        title = f"{raw.get('title', '')} {raw.get('model', '')}"
        reason = semantic_price_reason(title)
        if reason is not None:
            counts["semantic_price_rejections"] += 1
            continue
        if RISK_PATTERN.search(normalized_semantic_text(title)):
            counts["risk_rejections"] += 1
            continue
        normalized_title = normalized_semantic_text(title)
        url = str(raw.get("url") or "").strip()
        country = str(raw.get("country") or "").strip().upper()
        source = str(raw.get("source") or "").strip()
        peer_median = integer(raw.get("peer_median_eur"))
        price = integer(raw.get("price"))
        lower_quartile = integer(raw.get("peer_lower_quartile_eur"))
        savings = integer(raw.get("savings_vs_lower_quartile_eur"))
        conservative_bps = integer(raw.get("conservative_discount_bps"))
        median_bps = integer(raw.get("median_discount_bps"))
        dispersion = num(raw.get("peer_dispersion"))
        if (
            UNSUPPORTED_POWERTRAIN_PATTERN.search(normalized_title) is not None
            or country not in SCHENGEN_COUNTRIES
            or not source
            or source_identity_keys(source).intersection(blocked)
            or FORBIDDEN_ECONOMICS_FIELDS.intersection(raw)
            or str(raw.get("fuel")) not in {"petrol", "hybrid"}
            or not valid_timestamp(raw.get("last_seen_at"))
            or not (4_000 <= price <= 45_000)
            or lower_quartile <= price
            or peer_median < lower_quartile
            or savings != lower_quartile - price
            or conservative_bps <= 0
            or median_bps < conservative_bps
            or conservative_bps != integer(10_000 * savings / lower_quartile)
            or median_bps != integer(10_000 * (peer_median - price) / peer_median)
            or integer(raw.get("peer_count")) < 20
            or integer(raw.get("peer_source_count")) < 3
            or integer(raw.get("peer_country_count")) < 2
            or not (0 <= dispersion <= 0.35)
            or price < peer_median * 0.60
            or not valid_https_url(url)
        ):
            continue
        counts["eligible_before_publication_dedupe"] += 1
        identity = ranked_offer_id(raw)
        if identity in seen_ids or url in seen_urls:
            counts["publication_identity_duplicates_removed"] += 1
            continue
        seen_ids.add(identity)
        seen_urls.add(url)
        accepted.append(raw)
    counts["semantic_crosspost_duplicates_removed_from_saved_ranking"] = 0
    return sorted(accepted, key=ranked_key), counts


def compact_ranked_offer(offer: dict[str, Any], verification: int) -> dict[str, Any]:
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


def num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def integer(value: Any) -> int:
    return int(round(num(value)))


def bounded_uint(
    value: Any,
    field: str,
    *,
    upper: int = MAX_RECEIPT_COUNT,
) -> int:
    """Return a strict bounded integer without coercing remote metadata."""
    if type(value) is not int or value < 0 or value > upper:
        raise AssertionError(f"{field} is not a bounded integer")
    return value


def load_dashboard_pin(path: Path) -> str:
    """Accept both legacy plain-text PINs and JSON secret objects."""
    raw = path.read_text(encoding="utf-8").strip()
    try:
        secret = json.loads(raw)
    except json.JSONDecodeError:
        pin = raw
    else:
        pin = str(secret.get("pin") or "").strip() if isinstance(secret, dict) else raw
    if len(pin) < 8:
        raise AssertionError("dashboard secret is unexpectedly short")
    return pin


def offer_id(offer: dict[str, Any]) -> str:
    explicit = str(offer.get("id") or "").strip()
    if explicit:
        return explicit
    material = f"{offer.get('s', '')}\0{offer.get('u', '')}".encode()
    return "url_" + hashlib.sha256(material).hexdigest()[:24]


def eligible(offer: dict[str, Any]) -> bool:
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
    price = integer(offer.get("p"))
    lower_quartile = integer(offer.get("q1"))
    peer_median = integer(offer.get("mp"))
    savings = integer(offer.get("sv"))
    conservative_discount = num(offer.get("sp"))
    median_discount = num(offer.get("dp"))
    return (
        semantic_price_reason(title) is None
        and RISK_PATTERN.search(normalized_semantic_text(title)) is None
        and valid_https_url(offer.get("u"))
        and not autoscout24_non_detail_url(offer.get("u"))
        and valid_timestamp(offer.get("ls"))
        and str(offer.get("c")).upper() in SCHENGEN_COUNTRIES
        and offer.get("f") in {"petrol", "hybrid"}
        and 4_000 <= price <= 45_000
        and lower_quartile > price
        and peer_median >= lower_quartile
        and savings == lower_quartile - price
        and 0 < conservative_discount <= median_discount < 100
        and integer(10_000 * savings / lower_quartile) == integer(100 * conservative_discount)
        and integer(10_000 * (peer_median - price) / peer_median) == integer(100 * median_discount)
        and integer(offer.get("pn")) >= 20
        and integer(offer.get("ps")) >= 3
        and integer(offer.get("pc")) >= 2
        and integer(offer.get("v")) == 1
    )


def key(offer: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -integer(num(offer.get("sp")) * 100),
        -integer(offer.get("sv")),
        -integer(num(offer.get("dp")) * 100),
        -integer(offer.get("ps")),
        -integer(offer.get("pn")),
        -integer(offer.get("y")),
        integer(offer.get("km")), integer(offer.get("p")), offer_id(offer),
    )


def candidate_list(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    result = []
    for raw in offers:
        if not isinstance(raw, dict) or not eligible(raw):
            continue
        item = dict(raw)
        item["id"] = offer_id(item)
        url = str(item.get("u") or "")
        if item["id"] in seen_ids or url in seen_urls:
            continue
        seen_ids.add(item["id"])
        seen_urls.add(url)
        result.append(item)
    return sorted(result, key=key)


def expected_selection(
    candidates: list[dict[str, Any]], top_n: int,
    per_country_min: int, per_source_min: int,
) -> list[dict[str, Any]]:
    del per_country_min, per_source_min
    if top_n <= 0:
        return list(candidates)
    return list(candidates[:top_n])


def digest(offers: list[dict[str, Any]]) -> str:
    value = hashlib.sha256()
    for offer in offers:
        value.update(offer_id(offer).encode())
        value.update(b"\n")
    return value.hexdigest()


def digest_fields(offers: list[dict[str, Any]]) -> str:
    value = hashlib.sha256()
    for offer in offers:
        value.update(
            json.dumps(
                offer, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        value.update(b"\n")
    return value.hexdigest()


def decrypt_blob(blob: bytes, pin: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if not blob.startswith(MAGIC) or len(blob) < 50:
        raise AssertionError("invalid encrypted payload envelope")
    salt, nonce, ciphertext = blob[5:21], blob[21:33], blob[33:]
    key_bytes = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS,
    ).derive(pin.encode())
    raw = AESGCM(key_bytes).decrypt(nonce, ciphertext, None)
    payload = json.loads(gzip.decompress(raw))
    if not isinstance(payload, dict):
        raise AssertionError("decrypted payload is not an object")
    return payload


def load_encrypted_payload(path: Path, pin: str, data_url: str | None) -> dict[str, Any]:
    if data_url:
        request = Request(
            data_url,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        with urlopen(request, timeout=30) as response:
            blob = response.read()
    else:
        blob = path.read_bytes()
    return decrypt_blob(blob, pin)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def audit_payload(
    *,
    root: Path,
    payload: dict[str, Any],
    top_n: int = 10_000,
    per_country_min: int = 20,
    per_source_min: int = 5,
    sealed_universe_count: int | None = None,
) -> dict[str, Any]:
    """Perform the exact, generation-specific ranking and field audit.

    Remote callers must first establish that ``payload.generation_id`` is the
    target generation.  Keeping that check outside this function prevents an
    older, internally valid Pages artifact from being mislabeled as a ranking
    regression against a newer local board.
    """
    board = json.loads((root / "mobile_site_local" / "board.json").read_text())
    if (
        not isinstance(board, dict)
        or board.get("schema_version") != CONTRACT_SCHEMA_VERSION
        or board.get("algorithm") != ALGORITHM
        or board.get("unsupported_economics_published") != 0
    ):
        raise AssertionError("source board contract is unsupported")
    blocked_sources = board.get("policy_blocked_sources")
    source_policy_sha256 = optional_sha256_file(
        root / "schengen_source_policy.json"
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
        raise AssertionError("source-policy evidence is invalid or stale")
    data_generated_at = board.get("data_generated_at_utc")
    snapshot_digest = board.get("snapshot_eligible_sha256")
    if (
        not valid_timestamp(data_generated_at)
        or board.get("generated_at") != data_generated_at
        or board.get("updated_utc") != data_generated_at
        or not isinstance(snapshot_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_digest)
    ):
        raise AssertionError("source board has no generation-bound data timestamp")
    board_offers = board.get("offers")
    if not isinstance(board_offers, list) or not board_offers:
        raise AssertionError("source board contains no offers")
    candidates = candidate_list(board.get("offers", []))
    expected = expected_selection(
        candidates, top_n, per_country_min, per_source_min
    )
    published = payload.get("offers")
    if not isinstance(published, list) or not published:
        raise AssertionError("encrypted publication contains no offers")
    if (
        payload.get("schema_version") != CONTRACT_SCHEMA_VERSION
        or payload.get("algorithm") != ALGORITHM
        or payload.get("unsupported_economics_published") != 0
        or payload.get("snapshot_eligible_sha256") != snapshot_digest
        or not isinstance(payload.get("selection"), dict)
        or payload["selection"].get("algeria_economics_included") is not False
    ):
        raise AssertionError("encrypted publication contract is unsupported")

    expected_ids = [offer_id(offer) for offer in expected]
    published_ids = [offer_id(offer) for offer in published]
    if published_ids != expected_ids:
        mismatch = next(
            (index for index, pair in enumerate(zip(expected_ids, published_ids)) if pair[0] != pair[1]),
            min(len(expected_ids), len(published_ids)),
        )
        raise AssertionError(f"published selection diverges from full ranking at index {mismatch}")
    if published != expected:
        mismatch = next(
            (
                index for index, pair in enumerate(zip(expected, published))
                if pair[0] != pair[1]
            ),
            min(len(expected), len(published)),
        )
        raise AssertionError(
            f"published offer fields diverge from source board at index {mismatch}"
        )
    if payload.get("selection_algorithm") != ALGORITHM:
        raise AssertionError("selection algorithm version mismatch")
    if payload.get("data_generated_at_utc") != data_generated_at:
        raise AssertionError("payload data timestamp differs from source board")
    if payload.get("selection_candidate_sha256") != digest(candidates):
        raise AssertionError("candidate-universe digest mismatch")
    if payload.get("selected_ids_sha256") != digest(expected):
        raise AssertionError("published-selection digest mismatch")
    if payload.get("selection_candidate_fields_sha256") != digest_fields(candidates):
        raise AssertionError("candidate-field digest mismatch")
    if payload.get("selected_fields_sha256") != digest_fields(expected):
        raise AssertionError("selected-field digest mismatch")
    expected_generation = hashlib.sha256(
        (
            f"{ALGORITHM}\n{data_generated_at}\n"
            f"{digest_fields(candidates)}\n{digest_fields(expected)}\n"
        ).encode("utf-8")
    ).hexdigest()[:16]
    if payload.get("generation_id") != expected_generation:
        raise AssertionError("generation identity does not bind algorithm and selection")
    if len(set(published_ids)) != len(published_ids):
        raise AssertionError("duplicate listing IDs in publication")
    urls = [str(offer.get("u") or "") for offer in published]
    if len(set(urls)) != len(urls):
        raise AssertionError("duplicate listing URLs in publication")
    cesja_count = sum(
        bool(re.search(r"\bcesja\b", normalized_semantic_text(
            f"{offer.get('t', '')} {offer.get('m', '')}"
        )))
        for offer in published
    )
    lease_like_count = sum(
        semantic_price_reason(f"{offer.get('t', '')} {offer.get('m', '')}") is not None
        for offer in published
    )
    risk_count = sum(
        RISK_PATTERN.search(normalized_semantic_text(
            f"{offer.get('t', '')} {offer.get('m', '')}"
        )) is not None
        for offer in published
    )
    confirmed_dead_count = sum(integer(offer.get("v")) == -1 for offer in published)
    unverified_count = sum(integer(offer.get("v")) != 1 for offer in published)
    if cesja_count or lease_like_count or risk_count or confirmed_dead_count or unverified_count:
        raise AssertionError(
            "semantic-price/risk/live publication gate failed: "
            f"cesja={cesja_count} lease_like={lease_like_count} risk={risk_count} "
            f"dead={confirmed_dead_count} unverified={unverified_count}"
        )
    if any(not eligible(offer) for offer in published):
        raise AssertionError("publication includes an ineligible/dead/lease-like offer")

    negative_control = {
        "id": "negative-control-cesja", "t": "Range Rover Velar P400 Cesja!",
        "m": "velar", "u": "https://example.invalid/cesja", "c": "PL",
        "s": "negative-control", "p": 10_000, "q1": 12_000, "mp": 13_000,
        "sv": 2_000, "sp": 16.67, "dp": 23.08, "pn": 30, "ps": 4,
        "pc": 3, "y": 2025, "km": 20_000, "f": "petrol", "v": 1,
        "ls": "2026-08-11T00:00:00+00:00",
    }
    negative_control_pass = not eligible(negative_control)
    risk_control = {
        **negative_control,
        "id": "negative-control-sinistrata", "t": "Alfa Romeo Giulia sinistrata",
        "u": "https://example.invalid/sinistrata",
    }
    positive_control = {
        **negative_control,
        "id": "positive-control-observed-value", "t": "Renault Clio TCe 90",
        "u": "https://example.invalid/observed-value",
    }
    risk_negative_control_pass = not eligible(risk_control)
    positive_control_pass = eligible(positive_control)
    if not negative_control_pass or not risk_negative_control_pass or not positive_control_pass:
        raise AssertionError("observed-value eligibility controls failed")

    if sealed_universe_count is None:
        # The pre-publication audit deliberately checks the current SQLite.
        # Post-publication callers may instead provide the count sealed by the
        # same-generation manifest and successful local audit so that later,
        # additive ingestion cannot create a false live divergence.
        database = sqlite3.connect(
            f"file:{root / 'universe_offers.sqlite'}?mode=ro", uri=True
        )
        try:
            database.execute("PRAGMA query_only=ON")
            database.execute("BEGIN")
            raw_count, database_last_seen = database.execute(
                "SELECT COUNT(*), MAX(last_seen_at) FROM offers"
            ).fetchone()
            universe_count = bounded_uint(
                int(raw_count),
                "local universe count",
            )
            if canonical_timestamp(database_last_seen) != data_generated_at:
                raise AssertionError("board timestamp differs from the current universe")
        finally:
            database.close()
    else:
        universe_count = bounded_uint(
            sealed_universe_count,
            "sealed universe count",
        )
    remote_universe_count = bounded_uint(
        payload.get("universe_unique_offers"),
        "universe_unique_offers",
    )
    if universe_count != remote_universe_count:
        raise AssertionError("published universe counter does not match SQLite")
    if bounded_uint(
        board.get("universe_unique_offers"), "board universe_unique_offers"
    ) != universe_count:
        raise AssertionError("board universe counter does not match sealed evidence")

    ranked_payload = json.loads((root / "top_offers.json").read_text())
    if (
        not isinstance(ranked_payload, dict)
        or ranked_payload.get("schema_version") != CONTRACT_SCHEMA_VERSION
        or ranked_payload.get("algorithm") != ALGORITHM
        or ranked_payload.get("generated_at") != data_generated_at
        or ranked_payload.get("snapshot_eligible_sha256") != snapshot_digest
        or any(
            ranked_payload.get(key) != board.get(key)
            for key in (
                "offer_fields_sha256", "source_policy_sha256",
                "quarantine_manifest_sha256", "blocked_source_keys_sha256",
                "blocked_source_key_count", "policy_blocked_sources",
            )
        )
    ):
        raise AssertionError("ranked source contract does not match the board")
    ranked_offers = ranked_payload.get("offers", [])
    if not isinstance(ranked_offers, list) or len(ranked_offers) < len(candidates):
        raise AssertionError("ranked source does not cover the final candidate pool")
    observed_partition_complete = ranked_payload.get("ranking_complete") is True
    if not observed_partition_complete:
        raise AssertionError("observed ranking does not declare a complete snapshot scan")
    if bounded_uint(
        ranked_payload.get("outside_saved_better_than_cutoff"),
        "outside_saved_better_than_cutoff",
    ) != 0:
        raise AssertionError("ranked source cannot prove the saved top cutline")
    if bounded_uint(
        ranked_payload.get("unsupported_economics_published"),
        "unsupported_economics_published",
    ) != 0:
        raise AssertionError("ranked source contains unsupported economics")
    shown_count = bounded_uint(ranked_payload.get("shown", 0), "shown")
    if shown_count != len(ranked_offers):
        raise AssertionError("ranking saved-count metadata does not match the artifact")
    if bounded_uint(
        ranked_payload.get("total_all"), "ranked universe count"
    ) != universe_count:
        raise AssertionError("ranked universe counter does not match sealed evidence")

    verification = validation_states(root, board)
    full_ranked, full_counts = full_ranked_candidates(ranked_offers, board)
    projected_board = [
        compact_ranked_offer(offer, verification.get(str(offer.get("url") or ""), 0))
        for offer in full_ranked
    ]
    if projected_board != board_offers:
        mismatch = next(
            (
                index for index, pair in enumerate(zip(projected_board, board_offers))
                if pair[0] != pair[1]
            ),
            min(len(projected_board), len(board_offers)),
        )
        raise AssertionError(
            f"board fields diverge from ranked evidence at index {mismatch}"
        )
    full_candidates = [
        offer for offer in full_ranked
        if verification.get(str(offer.get("url") or ""), 0) == 1
    ]
    full_candidate_ids = [ranked_offer_id(offer) for offer in full_candidates]
    board_candidate_ids = [offer_id(offer) for offer in candidates]
    if full_candidate_ids != board_candidate_ids:
        raise AssertionError("verified board order diverges from ranked evidence")
    full_expected = full_candidates if top_n <= 0 else full_candidates[:top_n]
    full_expected_ids = [ranked_offer_id(offer) for offer in full_expected]
    published_set = set(published_ids)
    cutoff = key(published[-1])
    outside_better_than_cutoff = sum(
        ranked_offer_id(offer) not in published_set and ranked_key(offer) < cutoff
        for offer in full_candidates
    )
    if outside_better_than_cutoff != 0:
        raise AssertionError(
            f"{outside_better_than_cutoff} excluded offers beat the publication cutoff"
        )
    if published_ids != full_expected_ids:
        mismatch = next(
            (
                index for index, pair in enumerate(zip(full_expected_ids, published_ids))
                if pair[0] != pair[1]
            ),
            min(len(full_expected_ids), len(published_ids)),
        )
        raise AssertionError(f"global-top order mismatch at index {mismatch}")

    qualified_count = bounded_uint(len(candidates), "qualified_universe_offers")
    published_count = bounded_uint(len(published), "published_offer_count")
    verified_count = bounded_uint(
        sum(integer(offer.get("v")) == 1 for offer in published),
        "verified_live_count",
    )
    if verified_count != len(published):
        raise AssertionError("every published offer must be same-generation verified")
    full_ranked_count = bounded_uint(
        ranked_payload.get("total_all", 0),
        "full_ranked_input_offers",
    )
    ranking_qualified_count = bounded_uint(
        ranked_payload.get("qualified", 0),
        "ranking_qualified_offers",
    )
    ranking_saved_count = bounded_uint(len(ranked_offers), "ranking_saved_offers")
    observed_saved_count = bounded_uint(len(ranked_offers), "ranking_saved_observed_offers")
    connected_country_count = bounded_uint(
        payload.get("connected_country_count", 0),
        "connected_country_count",
        upper=MAX_CONNECTED_COUNT,
    )
    connected_source_count = bounded_uint(
        payload.get("connected_source_count", 0),
        "connected_source_count",
        upper=MAX_CONNECTED_COUNT,
    )

    return {
        "schema_version": 1,
        "result": "BEST_SELECTION_AUDIT_PASS",
        "generation_id": payload.get("generation_id"),
        "algorithm": ALGORITHM,
        "universe_unique_offers": universe_count,
        "qualified_universe_offers": qualified_count,
        "published_offer_count": published_count,
        "verified_live_count": verified_count,
        "candidate_ids_sha256": digest(candidates),
        "selected_ids_sha256": digest(expected),
        "candidate_fields_sha256": digest_fields(candidates),
        "selected_fields_sha256": digest_fields(expected),
        "data_generated_at_utc": data_generated_at,
        "exact_order_match": True,
        "exact_source_fields_match": True,
        "strict_global_top_n": True,
        "coverage_quota_substitutions": 0,
        "full_ranked_input_offers": full_ranked_count,
        "ranking_qualified_offers": ranking_qualified_count,
        "ranking_saved_offers": ranking_saved_count,
        "ranking_saved_observed_offers": observed_saved_count,
        "ranking_observed_partition_complete": observed_partition_complete,
        "snapshot_eligible_sha256": snapshot_digest,
        "source_board_sha256": sha256_file(root / "mobile_site_local" / "board.json"),
        "selection_input_counts": {
            "universe_unique_after_source_identity_dedupe": universe_count,
            "recent_snapshot_rows_scanned": bounded_uint(
                ranked_payload.get("scanned_recent_rows", 0),
                "scanned_recent_rows",
            ),
            "eligible_observed_rows": bounded_uint(
                ranked_payload.get("eligible_observed_rows", 0),
                "eligible_observed_rows",
            ),
            "peer_ranked_candidates": ranking_qualified_count,
            **full_counts,
            "publication_candidates_after_all_filters_and_dedupe": len(full_candidates),
        },
        "unique_ids": True,
        "unique_urls": True,
        "cesja_count": cesja_count,
        "lease_like_count": lease_like_count,
        "risk_listing_count": risk_count,
        "confirmed_dead_count": confirmed_dead_count,
        "confirmed_dead_or_lease_like_published": confirmed_dead_count + lease_like_count,
        "same_generation_verified_only": True,
        "outside_better_than_cutoff": outside_better_than_cutoff,
        "negative_control_pass": negative_control_pass,
        "risk_negative_control_pass": risk_negative_control_pass,
        "positive_control_pass": positive_control_pass,
        "unsupported_economics_published": 0,
        "connected_country_count": connected_country_count,
        "connected_source_count": connected_source_count,
    }
    auction_fields = audit_auction_lane(payload, data_generated_at, report["generation_id"])
    report.update(auction_fields)
    return report


def audit_auction_lane(
    payload: dict[str, Any],
    data_generated_at: str,
    generation_id: str,
) -> dict[str, Any]:
    """Verify the optional generation-bound auction lane in the payload.

    Absent lane => all None (old payloads and lane-less publishes pass).
    Present lane => every binding field must match the same-generation payload.
    """
    lane = payload.get("auction_lane")
    if lane is None:
        return {
            "auction_lane_count": None,
            "auction_lane_sha256": None,
            "auction_lane_registry_digest": None,
        }
    if not isinstance(lane, dict):
        raise AssertionError("auction lane in payload is not an object")
    if lane.get("schema_version") != 1 or lane.get("lane") != "auction":
        raise AssertionError("auction lane contract metadata is invalid")
    if lane.get("bound_generation_id") != generation_id:
        raise AssertionError("auction lane is not bound to this generation")
    if lane.get("bound_data_generated_at_utc") != data_generated_at:
        raise AssertionError("auction lane is not bound to this data snapshot")
    rows = lane.get("rows")
    if not isinstance(rows, list) or lane.get("lane_count") != len(rows):
        raise AssertionError("auction lane count does not match its rows")
    registry_digest = lane.get("registry_digest")
    if not isinstance(registry_digest, str) or len(registry_digest) < 8:
        raise AssertionError("auction lane registry digest is invalid")
    return {
        "auction_lane_count": lane.get("lane_count"),
        "auction_lane_sha256": canonical_json_sha256(lane),
        "auction_lane_registry_digest": registry_digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/home/krt/car_deal_finder"))
    parser.add_argument("--site", type=Path, default=Path("/srv/sonardeals-radar/site"))
    parser.add_argument("--pin", type=Path, default=Path("/etc/sonardeals-radar/pin"))
    parser.add_argument(
        "--output", type=Path,
        default=Path("/var/lib/sonardeals-radar/latest_selection_audit.json"),
    )
    parser.add_argument("--top-n", type=int, default=10_000)
    parser.add_argument("--per-country-min", type=int, default=20)
    parser.add_argument("--per-source-min", type=int, default=5)
    parser.add_argument(
        "--data-url",
        help="Audit a deployed encrypted payload instead of the local site file.",
    )
    args = parser.parse_args()

    payload = load_encrypted_payload(
        args.site / "data.enc", load_dashboard_pin(args.pin), args.data_url
    )
    report = audit_payload(
        root=args.root,
        payload=payload,
        top_n=args.top_n,
        per_country_min=args.per_country_min,
        per_source_min=args.per_source_min,
    )
    atomic_json(args.output, report)
    print("BEST_SELECTION_AUDIT_PASS " + json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
