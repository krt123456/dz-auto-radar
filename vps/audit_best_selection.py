#!/usr/bin/env python3
"""Independently verify the encrypted dashboard selection before publication."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


MAGIC = b"DZAR1"
ITERATIONS = 310_000
ALGORITHM = "schengen-strict-global-economics-v6-live-verified"
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
        1 if bool(offer.get("auction")) else 0,
        -integer(offer.get("effective_profit") or offer.get("profit")),
        -integer(offer.get("credibility")),
        -integer(offer.get("profit")),
        -round(num(offer.get("effective_roi") or offer.get("roi")), 1),
        integer(offer.get("price")), integer(offer.get("mileage")),
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
        bool(offer.get("estimated")), num(offer.get("price")),
        -integer(offer.get("credibility")),
        -num(offer.get("profit")), ranked_offer_id(offer),
    )


def validation_excluded_urls(root: Path, data_generated_at: Any) -> set[str]:
    path = root / "top400_validation.json"
    if not path.exists() or not data_generated_at:
        return set()
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("input_updated_at") != data_generated_at:
        return set()
    return {
        str(item.get("url") or "") for item in value.get("results", [])
        if isinstance(item, dict) and item.get("status") != "verified"
    }


def full_ranked_candidates(
    offers: list[dict[str, Any]], board: dict[str, Any], dead_urls: set[str],
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
    }
    for raw in offers:
        if not isinstance(raw, dict):
            continue
        title = f"{raw.get('title', '')} {raw.get('model', '')}"
        reason = semantic_price_reason(title)
        if reason is not None:
            counts["semantic_price_rejections"] += 1
            continue
        lowered = title.lower()
        url = str(raw.get("url") or "").strip()
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        country = str(raw.get("country") or "").strip().upper()
        source = str(raw.get("source") or "").strip()
        if (
            any(tell in lowered for tell in EV_TELLS)
            or country not in SCHENGEN_COUNTRIES
            or not source
            or source_identity_keys(source).intersection(blocked)
            or bool(raw.get("estimated"))
            or raw.get("eligible", True) is False
            or not (4_000 <= num(raw.get("price")) <= 45_000)
            or not (0 < num(raw.get("profit")) <= 25_000)
            or not (0 < num(raw.get("roi")) <= 120)
            or not (30 <= integer(raw.get("credibility")) <= 100)
            or parsed.scheme != "https" or not parsed.netloc or len(url) > 2048
            or url in dead_urls
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
    crosspost_winners: dict[tuple[Any, ...], dict[str, Any]] = {}
    for offer in accepted:
        fingerprint = ranked_crosspost_fingerprint(offer)
        current = crosspost_winners.get(fingerprint)
        if current is None or ranked_crosspost_winner_key(offer) < ranked_crosspost_winner_key(current):
            crosspost_winners[fingerprint] = offer
    deduplicated = list(crosspost_winners.values())
    counts["semantic_crosspost_duplicates_removed_from_saved_ranking"] = (
        len(accepted) - len(deduplicated)
    )
    return sorted(deduplicated, key=ranked_key), counts


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
    title = f"{offer.get('t', '')} {offer.get('m', '')}".casefold()
    parsed = urlparse(str(offer.get("u") or ""))
    return (
        semantic_price_reason(title) is None
        and parsed.scheme == "https" and bool(parsed.netloc)
        and bool(str(offer.get("c") or "").strip())
        and bool(str(offer.get("s") or "").strip())
        and 4_000 <= num(offer.get("p")) <= 45_000
        and 0 < num(offer.get("pr")) <= 25_000
        and 0 < num(offer.get("roi")) <= 120
        and 30 <= integer(offer.get("cr")) <= 100
        and integer(offer.get("e")) == 0
        and integer(offer.get("v")) == 1
    )


def key(offer: dict[str, Any]) -> tuple[Any, ...]:
    return (
        1 if integer(offer.get("a")) else 0,
        -num(offer.get("ep") or offer.get("pr")),
        -integer(offer.get("cr")),
        -num(offer.get("pr")),
        -num(offer.get("er") or offer.get("roi")),
        num(offer.get("p")), num(offer.get("km")), offer_id(offer),
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
    candidates = candidate_list(board.get("offers", []))
    expected = expected_selection(
        candidates, top_n, per_country_min, per_source_min
    )
    published = payload.get("offers")
    if not isinstance(published, list) or not published:
        raise AssertionError("encrypted publication contains no offers")

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
            f"{ALGORITHM}\n{board.get('data_generated_at_utc')}\n"
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
    confirmed_dead_count = sum(integer(offer.get("v")) == -1 for offer in published)
    unverified_count = sum(integer(offer.get("v")) != 1 for offer in published)
    if cesja_count or lease_like_count or confirmed_dead_count or unverified_count:
        raise AssertionError(
            "semantic-price/live publication gate failed: "
            f"cesja={cesja_count} lease_like={lease_like_count} "
            f"dead={confirmed_dead_count} unverified={unverified_count}"
        )
    if any(not eligible(offer) for offer in published):
        raise AssertionError("publication includes an ineligible/dead/lease-like offer")

    negative_control = {
        "id": "negative-control-cesja", "t": "Range Rover Velar P400 Cesja!",
        "m": "velar", "u": "https://example.invalid/cesja", "c": "PL",
        "s": "negative-control", "p": 4_001, "pr": 25_000, "ep": 25_000,
        "roi": 120, "er": 120, "cr": 100, "e": 0, "v": 1, "a": 0,
    }
    negative_control_pass = not eligible(negative_control)
    allowed_control = {
        **negative_control,
        "id": "allowed-control-sinistrata", "t": "Alfa Romeo Giulia sinistrata",
        "u": "https://example.invalid/sinistrata", "pr": 1_000, "ep": 1_000,
        "roi": 10, "er": 10,
    }
    allowed_substring_control_pass = eligible(allowed_control)
    if not negative_control_pass or not allowed_substring_control_pass:
        raise AssertionError("semantic-price negative/allowed controls failed")

    if sealed_universe_count is None:
        # The pre-publication audit deliberately checks the current SQLite.
        # Post-publication callers may instead provide the count sealed by the
        # same-generation manifest and successful local audit so that later,
        # additive ingestion cannot create a false live divergence.
        database = sqlite3.connect(
            f"file:{root / 'universe_offers.sqlite'}?mode=ro", uri=True
        )
        try:
            universe_count = bounded_uint(
                int(database.execute("SELECT COUNT(*) FROM offers").fetchone()[0]),
                "local universe count",
            )
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

    effective_path = root / "effective_planet_dashboard.json"
    effective_counts: dict[str, Any] = {}
    if effective_path.exists():
        effective = json.loads(effective_path.read_text(encoding="utf-8"))
        effective_universe = bounded_uint(
            effective.get("universe_unique_offers"),
            "effective dashboard universe_unique_offers",
        )
        if effective_universe != universe_count:
            raise AssertionError("effective dashboard does not cover the sealed universe")
        effective_counts = {
            "qualified_after_age_fuel_legal_filters": bounded_uint(
                effective.get("qualified_universe_planets", 0),
                "qualified_universe_planets",
            ),
            "economics_qualified_ranked": bounded_uint(
                effective.get("qualified_effective_roi_planets", 0),
                "qualified_effective_roi_planets",
            ),
        }

    ranked_payload = json.loads((root / "top_offers.json").read_text())
    ranked_offers = ranked_payload.get("offers", [])
    if not isinstance(ranked_offers, list) or len(ranked_offers) < len(candidates):
        raise AssertionError("ranked source does not cover the final candidate pool")
    first_estimated = next(
        (index for index, offer in enumerate(ranked_offers) if offer.get("estimated")),
        len(ranked_offers),
    )
    if any(not offer.get("estimated") for offer in ranked_offers[first_estimated:]):
        raise AssertionError("real and estimated ranking partitions are interleaved")
    declared_non_estimated = bounded_uint(
        ranked_payload.get("qualified_non_estimated", 0),
        "qualified_non_estimated",
    )
    real_partition_complete = ranked_payload.get("non_estimated_partition_complete") is True
    if not real_partition_complete or declared_non_estimated != first_estimated:
        raise AssertionError(
            "ranking artifact cannot prove that every real analysed offer was retained"
        )
    shown_count = bounded_uint(ranked_payload.get("shown", 0), "shown")
    if shown_count != len(ranked_offers):
        raise AssertionError("ranking saved-count metadata does not match the artifact")

    dead_urls = validation_excluded_urls(root, board.get("data_generated_at_utc"))
    full_candidates, full_counts = full_ranked_candidates(
        ranked_offers, board, dead_urls,
    )
    full_candidate_ids = [ranked_offer_id(offer) for offer in full_candidates]
    board_candidate_ids = [offer_id(offer) for offer in candidates]
    if full_candidate_ids != board_candidate_ids:
        mismatch = next(
            (
                index for index, pair in enumerate(zip(full_candidate_ids, board_candidate_ids))
                if pair[0] != pair[1]
            ),
            min(len(full_candidate_ids), len(board_candidate_ids)),
        )
        raise AssertionError(
            f"board is not the independently recomputed full ranked universe at index {mismatch}"
        )
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
    real_saved_count = bounded_uint(
        first_estimated,
        "ranking_saved_non_estimated_offers",
    )
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
        "result": "BEST_SELECTION_AUDIT_PASS",
        "generation_id": payload.get("generation_id"),
        "algorithm": ALGORITHM,
        "universe_unique_offers": universe_count,
        "qualified_universe_offers": qualified_count,
        "published_offer_count": published_count,
        "verified_live_count": verified_count,
        "candidate_ids_sha256": digest(candidates),
        "selected_ids_sha256": digest(expected),
        "exact_order_match": True,
        "exact_source_fields_match": True,
        "strict_global_top_n": True,
        "coverage_quota_substitutions": 0,
        "full_ranked_input_offers": full_ranked_count,
        "ranking_qualified_offers": ranking_qualified_count,
        "ranking_saved_offers": ranking_saved_count,
        "ranking_saved_non_estimated_offers": real_saved_count,
        "ranking_non_estimated_partition_complete": real_partition_complete,
        "selection_input_counts": {
            "universe_unique_after_source_identity_dedupe": universe_count,
            "ranked_rows_before_final_filter": full_ranked_count,
            **effective_counts,
            **(ranked_payload.get("input_counts") or {}),
            **full_counts,
            "publication_candidates_after_all_filters_and_dedupe": len(full_candidates),
        },
        "unique_ids": True,
        "unique_urls": True,
        "cesja_count": cesja_count,
        "lease_like_count": lease_like_count,
        "confirmed_dead_count": confirmed_dead_count,
        "confirmed_dead_or_lease_like_published": confirmed_dead_count + lease_like_count,
        "same_generation_verified_only": True,
        "outside_better_than_cutoff": outside_better_than_cutoff,
        "negative_control_pass": negative_control_pass,
        "allowed_substring_control_pass": allowed_substring_control_pass,
        "estimated_economics_published": 0,
        "connected_country_count": connected_country_count,
        "connected_source_count": connected_source_count,
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
