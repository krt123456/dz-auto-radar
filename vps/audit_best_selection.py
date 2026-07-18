#!/usr/bin/env python3
"""Independently verify the encrypted dashboard selection before publication."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MAGIC = b"DZAR1"
ITERATIONS = 310_000
ALGORITHM = "schengen-verified-economics-v3"
LEASE_MARKERS = (
    "leasingsübernahme", "leasingübernahme", "leasingübertragung",
    "leasing", "übernahme", "monthly payment", "finance payment",
    "credit restant", "mensualité", "mensualite", "financement",
    "cesja najmu", "cesja leasingu", "cesja umowy leasingu",
    "odstępne", "odstepne", "rata miesięczna", "rata miesieczna",
    "лизинг", "lizing", "takeover", "flex lease",
)


def num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def integer(value: Any) -> int:
    return int(round(num(value)))


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
        not any(marker in title for marker in LEASE_MARKERS)
        and parsed.scheme == "https" and bool(parsed.netloc)
        and bool(str(offer.get("c") or "").strip())
        and bool(str(offer.get("s") or "").strip())
        and 4_000 <= num(offer.get("p")) <= 45_000
        and 0 < num(offer.get("pr")) <= 25_000
        and 0 < num(offer.get("roi")) <= 120
        and 30 <= integer(offer.get("cr")) <= 100
        and integer(offer.get("e")) == 0
        and integer(offer.get("v")) != -1
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
    if top_n <= 0 or top_n >= len(candidates):
        return list(candidates)
    selected: set[int] = set()
    country_count: Counter[str] = Counter()
    source_count: Counter[str] = Counter()
    for index, offer in enumerate(candidates):
        country = str(offer.get("c") or "").upper()
        source = str(offer.get("s") or "")
        if country_count[country] < per_country_min or source_count[source] < per_source_min:
            selected.add(index)
            country_count[country] += 1
            source_count[source] += 1
    for index in range(len(candidates)):
        if len(selected) >= max(top_n, len(selected)):
            break
        selected.add(index)
    return [candidates[index] for index in sorted(selected)]


def digest(offers: list[dict[str, Any]]) -> str:
    value = hashlib.sha256()
    for offer in offers:
        value.update(offer_id(offer).encode())
        value.update(b"\n")
    return value.hexdigest()


def decrypt(path: Path, pin: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    blob = path.read_bytes()
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
    args = parser.parse_args()

    board = json.loads((args.root / "mobile_site_local" / "board.json").read_text())
    payload = decrypt(args.site / "data.enc", args.pin.read_text().strip())
    candidates = candidate_list(board.get("offers", []))
    expected = expected_selection(
        candidates, args.top_n, args.per_country_min, args.per_source_min
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
    if payload.get("selection_algorithm") != ALGORITHM:
        raise AssertionError("selection algorithm version mismatch")
    if payload.get("selection_candidate_sha256") != digest(candidates):
        raise AssertionError("candidate-universe digest mismatch")
    if payload.get("selected_ids_sha256") != digest(expected):
        raise AssertionError("published-selection digest mismatch")
    if len(set(published_ids)) != len(published_ids):
        raise AssertionError("duplicate listing IDs in publication")
    urls = [str(offer.get("u") or "") for offer in published]
    if len(set(urls)) != len(urls):
        raise AssertionError("duplicate listing URLs in publication")
    if any(not eligible(offer) for offer in published):
        raise AssertionError("publication includes an ineligible/dead/lease-like offer")

    database = sqlite3.connect(f"file:{args.root / 'universe_offers.sqlite'}?mode=ro", uri=True)
    try:
        universe_count = int(database.execute("SELECT COUNT(*) FROM offers").fetchone()[0])
    finally:
        database.close()
    if universe_count != int(payload.get("universe_unique_offers") or -1):
        raise AssertionError("published universe counter does not match SQLite")

    report = {
        "result": "BEST_SELECTION_AUDIT_PASS",
        "generation_id": payload.get("generation_id"),
        "algorithm": ALGORITHM,
        "universe_unique_offers": universe_count,
        "qualified_universe_offers": len(candidates),
        "published_offer_count": len(published),
        "verified_live_count": sum(integer(offer.get("v")) == 1 for offer in published),
        "candidate_ids_sha256": digest(candidates),
        "selected_ids_sha256": digest(expected),
        "exact_order_match": True,
        "unique_ids": True,
        "unique_urls": True,
        "confirmed_dead_or_lease_like_published": 0,
        "estimated_economics_published": 0,
        "connected_country_count": payload.get("connected_country_count", 0),
        "connected_source_count": payload.get("connected_source_count", 0),
    }
    atomic_json(args.output, report)
    print("BEST_SELECTION_AUDIT_PASS " + json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
