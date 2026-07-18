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
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


MAGIC = b"DZAR1"
PBKDF2_ITERATIONS = 310_000
ALGORITHM_VERSION = "schengen-effective-profit-v2"
DEFAULT_ROOT = Path("/home/krt/car_deal_finder")
DEFAULT_SITE = Path("/srv/sonardeals-radar/site")
DEFAULT_PIN = Path("/etc/sonardeals-radar/pin")
DEFAULT_INDEX = Path("/opt/sonardeals-radar/dashboard/index.html")
DEFAULT_AUDIT = Path("/var/lib/sonardeals-radar/latest_selection_manifest.json")

LEASE_MARKERS = (
    "leasingsübernahme", "leasingübernahme", "leasingübertragung",
    "leasing", "übernahme", "monthly payment", "finance payment",
    "credit restant", "mensualité", "mensualite", "financement",
    "cesja najmu", "cesja leasingu", "cesja umowy leasingu",
    "odstępne", "odstepne", "rata miesięczna", "rata miesieczna",
    "лизинг", "lizing", "takeover", "flex lease",
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
    return parsed.scheme == "https" and bool(parsed.netloc) and len(str(value)) <= 2048


def eligible_offer(offer: dict[str, Any]) -> bool:
    title = f"{offer.get('t', '')} {offer.get('m', '')}".casefold()
    if any(marker in title for marker in LEASE_MARKERS):
        return False
    price = number(offer.get("p"))
    profit = number(offer.get("pr"))
    roi = number(offer.get("roi"))
    credibility = integer(offer.get("cr"))
    return (
        bool(str(offer.get("c") or "").strip())
        and bool(str(offer.get("s") or "").strip())
        and valid_https_url(offer.get("u"))
        and 4_000 <= price <= 45_000
        and 0 < profit <= 25_000
        and 0 < roi <= 120
        and 30 <= credibility <= 100
        and integer(offer.get("v")) != -1
    )


def rank_key(offer: dict[str, Any]) -> tuple[Any, ...]:
    """Lower tuple is better; this is the public dashboard's ranking contract."""
    return (
        1 if integer(offer.get("a")) else 0,
        -number(offer.get("ep") or offer.get("pr")),
        -integer(offer.get("cr")),
        -number(offer.get("pr")),
        -number(offer.get("er") or offer.get("roi")),
        number(offer.get("p")),
        number(offer.get("km")),
        canonical_id(offer),
    )


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
    if top_n <= 0 or top_n >= len(candidates):
        return list(candidates)
    selected: set[int] = set()
    countries: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    for index, offer in enumerate(candidates):
        country = str(offer.get("c") or "").upper()
        source = str(offer.get("s") or "")
        if countries[country] < per_country_min or sources[source] < per_source_min:
            selected.add(index)
            countries[country] += 1
            sources[source] += 1
    for index in range(len(candidates)):
        if len(selected) >= max(top_n, len(selected)):
            break
        selected.add(index)
    return [candidates[index] for index in sorted(selected)]


def digest_ids(offers: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for offer in offers:
        digest.update(canonical_id(offer).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            "universe_last_seen_at": last_seen,
        }
    finally:
        connection.close()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def build_payload(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    board = load_json(args.board)
    offers = board.get("offers")
    if not isinstance(offers, list) or not offers:
        raise RuntimeError("refusing to publish an empty board")
    candidates = normalized_candidates(offers)
    if not candidates:
        raise RuntimeError("no eligible offers survived publication checks")
    selected = select_offers(
        candidates, args.top_n, args.per_country_min, args.per_source_min
    )
    ranked_meta = load_json(args.ranked_meta) if args.ranked_meta.exists() else {}
    metrics = universe_metrics(args.database)
    candidate_hash = digest_ids(candidates)
    selected_hash = digest_ids(selected)
    generation_id = candidate_hash[:16]

    payload = {key: value for key, value in board.items() if key != "offers"}
    payload.update(
        {
            "published_at_utc": utc_now(),
            "count": len(selected),
            "universe_unique_offers": metrics["universe_unique_offers"],
            "universe_last_seen_at": metrics["universe_last_seen_at"],
            "ranked_offer_count": int(ranked_meta.get("total_all") or len(offers)),
            "qualified_universe_offers": len(candidates),
            "published_offer_count": len(selected),
            "verified_live_count": sum(integer(offer.get("v")) == 1 for offer in selected),
            "selection_universe_count": len(candidates),
            "selection_algorithm": ALGORITHM_VERSION,
            "selection_candidate_sha256": candidate_hash,
            "selected_ids_sha256": selected_hash,
            "generation_id": generation_id,
            "selection": {
                "top_n": args.top_n,
                "per_country_min": args.per_country_min,
                "per_source_min": args.per_source_min,
                "ranking": "non-auction, effective-profit, credibility, profit, effective-roi",
            },
            "offers": selected,
        }
    )
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
        "connected_country_count": payload.get("connected_country_count", 0),
        "connected_source_count": payload.get("connected_source_count", 0),
        "data_generated_at_utc": payload.get("data_generated_at_utc"),
    }
    return payload, manifest


def prepare(args: argparse.Namespace) -> None:
    if not args.pin.is_file():
        raise RuntimeError(f"PIN secret is unavailable: {args.pin}")
    pin = args.pin.read_text(encoding="utf-8").strip()
    if len(pin) < 8:
        raise RuntimeError("dashboard secret is unexpectedly short")
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


def publish(args: argparse.Namespace) -> None:
    if not (args.site / ".git").is_dir():
        raise RuntimeError(f"publication directory is not a git checkout: {args.site}")
    (args.site / "board.json").unlink(missing_ok=True)
    run_git(args.site, "rm", "--cached", "--ignore-unmatch", "board.json", check=False)
    run_git(args.site, "add", "--", ".gitignore", "index.html", "data.enc")
    staged = {
        line.strip()
        for line in run_git(args.site, "diff", "--cached", "--name-only").stdout.splitlines()
        if line.strip()
    }
    allowed = {".gitignore", "index.html", "data.enc", "board.json"}
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
    parser.add_argument("--audit-manifest", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--top-n", type=int, default=10_000)
    parser.add_argument("--per-country-min", type=int, default=20)
    parser.add_argument("--per-source-min", type=int, default=5)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--push-only", action="store_true")
    args = parser.parse_args()
    args.board = args.root / "mobile_site_local" / "board.json"
    args.database = args.root / "universe_offers.sqlite"
    args.ranked_meta = args.root / "top_offers.json"
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
