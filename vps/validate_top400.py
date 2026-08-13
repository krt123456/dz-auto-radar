#!/usr/bin/env python3
"""Verify the highest-ranked offers actually published on the local board.

The validator is deliberately observational: it writes a machine-readable report
and never edits the board, ranked data, or live inventory.  A separate removal
step may consume only results whose status is ``dead``.  Blocked, rate-limited,
challenged, malformed, and unreachable pages remain ``unknown``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import importlib.metadata
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listing_availability as lifecycle


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BOARD = BASE_DIR / "mobile_site_local" / "board.json"
DEFAULT_ID_INDEX = BASE_DIR / "top_offers.json"
DEFAULT_OUTPUT = BASE_DIR / "top400_validation.json"
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_CONTRACT = "sonardeals-top400-validation-checkpoint-v1"
DEFAULT_CHECKPOINT_BATCH_SIZE = 100
DEFAULT_CHECKPOINT_INTERVAL_SEC = 30.0
DEFAULT_CHECKPOINT_MAX_AGE_SEC = 6 * 60 * 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.8,fr;q=0.7,de;q=0.6",
}

# Definitive listing-expiry markers only.  Ambiguous access/protection pages are
# classified separately and must never turn into a false dead/verified result.
DEAD_MARKERS = (
    "no longer available",
    "this listing has been removed",
    "this ad has been removed",
    "listing has expired",
    "listing expired",
    "ad expired",
    "nie jest już dostępne",
    "nie jest już dostepne",
    "ogłoszenie zostało usunięte",
    "ogłoszenie zostało przeniesione do archiwum",
    "oferta nieaktualna",
    "nicht mehr verfügbar",
    "anzeige wurde beendet",
    "annonce n'est plus disponible",
    "annonce introuvable",
    "annonce supprimée",
    "ya no está disponible",
    "anuncio retirado",
    "não está disponível",
    "já não está disponível",
    "annuncio non più disponibile",
    "annuncio è stato rimosso",
    "nu mai este disponibil",
    "anunțul a fost șters",
    "više nije dostupno",
    "oglas je uklonjen",
    "niet meer beschikbaar",
)

# High-signal challenge/block pages that sometimes return HTTP 200.
PROTECTION_MARKERS = (
    ("cloudflare_challenge", "<title>just a moment"),
    ("cloudflare_challenge", "cf-chl-"),
    ("cloudflare_challenge", "/cdn-cgi/challenge-platform/"),
    ("cloudflare_challenge", 'id="challenge-running"'),
    ("human_verification", "verify you are human"),
    ("human_verification", "verification required to continue"),
    ("access_denied", "attention required! | cloudflare"),
    ("access_denied", "the request was blocked"),
    ("access_denied", "your access to this site has been blocked"),
    ("bot_protection", "unusual traffic from your computer network"),
    ("bot_protection", "are you a robot"),
)

UNKNOWN_HTTP_STATUSES = {401, 403, 407, 418, 423, 429, 451, 503}
MAX_BODY_BYTES = 300_000
MIN_VERIFIABLE_BODY_CHARS = 200
BROWSER_IDENTITY_STOPWORDS = frozenset(
    {
        "auto", "car", "cars", "voiture", "occasion", "gebrauchtwagen",
        "benzine", "benzina", "petrol", "hybrid", "hybride", "automatic",
        "automatique", "manual", "diesel", "mhev", "phev", "tsi", "tce",
        "tfsi", "turbo", "cv", "kw", "ch", "dsg", "puretech", "allure",
        "active", "style", "edition", "sport", "line", "pack", "plus",
        "sale", "vendo", "sprzedam", "salon", "camera", "navi",
    }
)
BROWSER_EXPIRED_QUERY_KEYS = frozenset(
    {"fromexpiredadid", "expiredadid", "expiredlistingid"}
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def protection_reason(text: str, final_url: str) -> str | None:
    lowered = text.lower()
    for reason, marker in PROTECTION_MARKERS:
        if marker in lowered:
            return reason
    path = urlparse(final_url).path.lower()
    if any(part in path for part in ("/captcha", "/challenge", "/access-denied", "/blocked")):
        return "protection_redirect"
    return None


def dead_marker(text: str) -> str | None:
    normalized = normalize_text(text)
    for marker in DEAD_MARKERS:
        if marker in normalized:
            return marker
    return None


def read_response_text(response: requests.Response, limit: int = MAX_BODY_BYTES) -> str:
    """Read a bounded body while remaining compatible with simple test doubles."""
    if not hasattr(response, "iter_content"):
        return str(getattr(response, "text", ""))[:limit]

    body = bytearray()
    for chunk in response.iter_content(chunk_size=16_384):
        if not chunk:
            continue
        remaining = limit - len(body)
        body.extend(chunk[:remaining])
        if len(body) >= limit:
            break
    encoding = response.encoding or "utf-8"
    return bytes(body).decode(encoding, errors="replace")


def check_url(
    url: str,
    timeout_sec: int = 10,
    request_get: Callable[..., requests.Response] = requests.get,
) -> dict[str, Any]:
    """Return a truthful ``verified``/``dead``/``unknown`` URL classification."""
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "status": "unknown",
            "http_status": None,
            "final_url": "",
            "reason": "missing_or_invalid_url",
        }

    response: requests.Response | None = None
    try:
        response = request_get(
            url,
            headers=HEADERS,
            timeout=timeout_sec,
            allow_redirects=True,
            stream=True,
        )
        status = int(response.status_code)
        final_url = str(getattr(response, "url", "") or url)

        if status in {404, 410}:
            return {
                "status": "dead",
                "http_status": status,
                "final_url": final_url,
                "reason": f"http_{status}",
            }
        if status in UNKNOWN_HTTP_STATUSES or status >= 400:
            return {
                "status": "unknown",
                "http_status": status,
                "final_url": final_url,
                "reason": f"http_{status}",
            }
        if status != 200:
            return {
                "status": "unknown",
                "http_status": status,
                "final_url": final_url,
                "reason": f"unexpected_http_{status}",
            }

        text = read_response_text(response)
        challenge = protection_reason(text, final_url)
        if challenge:
            return {
                "status": "unknown",
                "http_status": status,
                "final_url": final_url,
                "reason": challenge,
            }
        marker = dead_marker(text)
        if marker:
            return {
                "status": "dead",
                "http_status": status,
                "final_url": final_url,
                "reason": f"dead_marker:{marker}",
            }
        if len(text.strip()) < MIN_VERIFIABLE_BODY_CHARS:
            return {
                "status": "unknown",
                "http_status": status,
                "final_url": final_url,
                "reason": "insufficient_content",
            }
        availability, expires_at = lifecycle.structured_lifecycle(text)
        if availability is not None or expires_at is not None:
            observed = datetime.now(UTC)
            decision = lifecycle.decide_lifecycle(
                availability=availability,
                expires_at=expires_at,
                as_of=observed,
                valid_until=observed + timedelta(hours=lifecycle.PUBLIC_VALIDITY_HOURS),
                identity_proven=False,
            )
            return {
                "status": decision.status,
                "http_status": status,
                "final_url": final_url,
                "reason": f"structured:{decision.reason}",
            }
        return {
            "status": "unknown",
            "http_status": status,
            "final_url": final_url,
            "reason": "http_200_listing_identity_unproven",
        }
    except requests.RequestException as exc:
        return {
            "status": "unknown",
            "http_status": None,
            "final_url": "",
            "reason": f"request_error:{type(exc).__name__}",
        }
    finally:
        if response is not None:
            response.close()


def normalized_browser_text(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_marks).split())


def browser_identity_tokens(title: object) -> list[str]:
    tokens: list[str] = []
    for token in normalized_browser_text(title).split():
        if token in BROWSER_IDENTITY_STOPWORDS or len(token) < 2:
            continue
        if token.isdigit() and (len(token) == 4 or len(token) < 3):
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens[:12]


def normalized_host(value: str) -> str:
    host = (urlparse(value).hostname or "").casefold()
    return host[4:] if host.startswith("www.") else host


def normalized_path(value: str) -> str:
    path = re.sub(r"/{2,}", "/", urlparse(value).path or "/")
    return path.rstrip("/").casefold() or "/"


def classify_browser_page(
    offer: dict[str, Any],
    *,
    http_status: int | None,
    final_url: str,
    page_title: str,
    body_text: str,
) -> dict[str, Any]:
    """Classify a rendered page without promoting a search/error page as live."""
    original_url = str(offer.get("url") or "")
    combined = f"{page_title}\n{body_text}"
    if http_status in {404, 410}:
        return {
            "status": "dead", "http_status": http_status,
            "final_url": final_url, "reason": f"browser_http_{http_status}",
        }
    if http_status is None or http_status >= 400:
        return {
            "status": "unknown", "http_status": http_status,
            "final_url": final_url, "reason": f"browser_http_{http_status or 'none'}",
        }

    challenge = protection_reason(combined, final_url)
    if challenge or "just a moment" in page_title.casefold():
        return {
            "status": "unknown", "http_status": http_status,
            "final_url": final_url, "reason": f"browser_{challenge or 'challenge'}",
        }
    marker = dead_marker(combined)
    if marker:
        return {
            "status": "dead", "http_status": http_status,
            "final_url": final_url, "reason": f"browser_dead_marker:{marker}",
        }

    query_keys = {key.casefold() for key in parse_qs(urlparse(final_url).query)}
    if query_keys.intersection(BROWSER_EXPIRED_QUERY_KEYS):
        return {
            "status": "dead", "http_status": http_status,
            "final_url": final_url, "reason": "browser_expired_listing_redirect",
        }
    if normalized_host(original_url) != normalized_host(final_url):
        return {
            "status": "unknown", "http_status": http_status,
            "final_url": final_url, "reason": "browser_cross_host_redirect",
        }
    if normalized_path(original_url) != normalized_path(final_url):
        return {
            "status": "unknown", "http_status": http_status,
            "final_url": final_url, "reason": "browser_detail_path_changed",
        }
    if len(body_text.strip()) < MIN_VERIFIABLE_BODY_CHARS:
        return {
            "status": "unknown", "http_status": http_status,
            "final_url": final_url, "reason": "browser_insufficient_content",
        }

    rendered = f" {normalized_browser_text(combined)} "
    tokens = browser_identity_tokens(offer.get("title"))
    matched = [token for token in tokens if f" {token} " in rendered]
    minimum_matches = 2 if len(tokens) >= 2 else 1
    if len(matched) < minimum_matches:
        return {
            "status": "unknown", "http_status": http_status,
            "final_url": final_url, "reason": "browser_listing_identity_unproven",
        }
    return {
        "status": "verified", "http_status": http_status,
        "final_url": final_url, "reason": "browser_rendered_detail_identity",
    }


def browser_executable() -> str | None:
    for candidate in (
        "/usr/bin/google-chrome",
        "/opt/google/chrome/chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium")


async def _browser_verify_unknowns(
    results: list[dict[str, Any]],
    *,
    limit: int,
    workers: int,
    timeout_sec: int,
    target_ranks: list[int] | None = None,
    completed_ranks: set[int] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    from patchright.async_api import async_playwright

    frozen_targets = set(target_ranks) if target_ranks is not None else None
    already_completed = completed_ranks or set()
    targets = [
        (index, item)
        for index, item in enumerate(results)
        if item.get("status") == "unknown"
        and str(item.get("url") or "").startswith("http")
        and (frozen_targets is None or item.get("board_rank") in frozen_targets)
        and item.get("board_rank") not in already_completed
    ]
    if frozen_targets is None and limit > 0:
        targets = targets[:limit]
    if not targets:
        return results

    executable = browser_executable()
    if not executable:
        raise RuntimeError("no Chrome/Chromium executable is available for browser fallback")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            executable_path=executable,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        semaphore = asyncio.Semaphore(max(1, workers))

        async def verify_one(index: int, offer: dict[str, Any]) -> None:
            async with semaphore:
                page = await context.new_page()
                try:
                    response = await page.goto(
                        offer["url"],
                        wait_until="domcontentloaded",
                        timeout=max(1, timeout_sec) * 1000,
                    )
                    await page.wait_for_timeout(750)
                    try:
                        body = await page.locator("body").inner_text(timeout=5_000)
                    except Exception:
                        body = ""
                    classification = classify_browser_page(
                        offer,
                        http_status=response.status if response else None,
                        final_url=page.url,
                        page_title=await page.title(),
                        body_text=body[:MAX_BODY_BYTES],
                    )
                except Exception as exc:
                    classification = {
                        "status": "unknown",
                        "http_status": None,
                        "final_url": "",
                        "reason": f"browser_error:{type(exc).__name__}",
                    }
                finally:
                    await page.close()
                results[index] = {
                    **offer,
                    "direct_reason": offer.get("reason", ""),
                    **classification,
                }
                if on_result is not None:
                    on_result(results[index])

        await asyncio.gather(*(verify_one(index, offer) for index, offer in targets))
        await context.close()
        await browser.close()
    return results


def browser_verify_unknowns(
    results: list[dict[str, Any]],
    *,
    limit: int,
    workers: int,
    timeout_sec: int,
    target_ranks: list[int] | None = None,
    completed_ranks: set[int] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    return asyncio.run(
        _browser_verify_unknowns(
            results,
            limit=limit,
            workers=workers,
            timeout_sec=timeout_sec,
            target_ranks=target_ranks,
            completed_ranks=completed_ranks,
            on_result=on_result,
        )
    )


def decode_offer_list(raw: bytes, path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(raw.decode("utf-8"))
    if isinstance(payload, list):
        return payload, {}
    offers = payload.get("offers", payload.get("data", []))
    if not isinstance(offers, list):
        raise ValueError(f"input contains no offers list: {path}")
    return offers, payload


def load_offer_list(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return decode_offer_list(path.read_bytes(), path)


def load_id_index(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    offers, _ = load_offer_list(path)
    index: dict[str, str] = {}
    for offer in offers:
        url = str(offer.get("url") or offer.get("u") or offer.get("source_url") or "").strip()
        listing_id = str(offer.get("id") or offer.get("listing_id") or "").strip()
        if url and listing_id:
            index.setdefault(url, listing_id)
    return index


def normalize_offer(raw: dict[str, Any], rank: int, id_index: dict[str, str]) -> dict[str, Any]:
    url = str(raw.get("url") or raw.get("u") or raw.get("source_url") or "").strip()
    listing_id = str(raw.get("id") or raw.get("listing_id") or id_index.get(url, "")).strip()
    return {
        "board_rank": rank,
        "listing_id": listing_id,
        "source": str(raw.get("source") or raw.get("s") or ""),
        "country": str(raw.get("country") or raw.get("c") or ""),
        "title": str(raw.get("title") or raw.get("t") or ""),
        "url": url,
    }


def normalize_offers(
    offers: list[dict[str, Any]],
    *,
    limit: int,
    id_index: dict[str, str],
) -> list[dict[str, Any]]:
    selected = offers[: max(0, limit)] if limit else offers
    return [
        normalize_offer(offer, rank, id_index)
        for rank, offer in enumerate(selected, start=1)
    ]


def package_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def executable_identity(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    candidate = Path(path).resolve()
    try:
        metadata = candidate.stat()
    except OSError:
        return {"path": str(candidate), "available": False}
    return {
        "path": str(candidate),
        "available": True,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "sha256": sha256_file(candidate),
    }


def build_checkpoint_identity(
    *,
    args: argparse.Namespace,
    normalized: list[dict[str, Any]],
    input_updated_at: Any,
    input_file_sha256: str,
    id_index_file_sha256: str | None,
) -> dict[str, Any]:
    browser_enabled = bool(args.browser_fallback)
    semantic_input = {
        "input_updated_at": input_updated_at,
        "selected_offers": normalized,
    }
    return {
        "contract": CHECKPOINT_CONTRACT,
        "input": {
            "path": str(args.input.resolve()),
            "file_sha256": input_file_sha256,
            "semantic_content_sha256": sha256_bytes(canonical_json_bytes(semantic_input)),
            "input_updated_at": input_updated_at,
            "selected_offer_content_sha256": sha256_bytes(
                canonical_json_bytes(normalized)
            ),
            "selected_offer_count": len(normalized),
            "id_index_path": str(args.id_index.resolve()) if args.id_index else None,
            "id_index_file_sha256": id_index_file_sha256,
        },
        "validator": {
            "source_sha256": sha256_file(Path(__file__).resolve()),
            "lifecycle_source_sha256": sha256_file(Path(lifecycle.__file__).resolve()),
            "lifecycle_policy_version": lifecycle.POLICY_VERSION,
            "lifecycle_public_validity_hours": lifecycle.PUBLIC_VALIDITY_HOURS,
            "python": sys.version,
            "python_cache_tag": sys.implementation.cache_tag,
            "requests_version": package_version("requests"),
            "patchright_version": package_version("patchright") if browser_enabled else None,
            "browser": executable_identity(browser_executable()) if browser_enabled else None,
        },
        "config": {
            "limit": args.limit,
            "verified_target": getattr(args, "verified_target", 0),
            "workers": args.workers,
            "timeout_sec": args.timeout_sec,
            "browser_fallback": browser_enabled,
            "browser_limit": args.browser_limit,
            "browser_workers": args.browser_workers,
            "browser_timeout_sec": args.browser_timeout_sec,
            "checkpoint_batch_size": args.checkpoint_batch_size,
            "checkpoint_interval_sec": args.checkpoint_interval_sec,
            "checkpoint_max_age_sec": args.checkpoint_max_age_sec,
        },
    }


def select_browser_target_ranks(
    direct_results: list[dict[str, Any]], limit: int
) -> list[int]:
    ranks = [
        int(item["board_rank"])
        for item in direct_results
        if item.get("status") == "unknown"
        and str(item.get("url") or "").startswith("http")
    ]
    return ranks[:limit] if limit > 0 else ranks


class CheckpointError(RuntimeError):
    pass


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CheckpointError(f"checkpoint contains duplicate JSON key: {key}")
        value[key] = item
    return value


def reject_non_finite_json(value: str) -> None:
    raise CheckpointError(f"checkpoint contains non-finite JSON number: {value}")


class CheckpointStore:
    def __init__(
        self,
        path: Path,
        *,
        identity: dict[str, Any],
        normalized: list[dict[str, Any]],
        source_diagnostics: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.identity = identity
        self.identity_sha256 = sha256_bytes(canonical_json_bytes(identity))
        self.normalized_by_rank = {
            int(item["board_rank"]): item for item in normalized
        }
        self.source_diagnostics = source_diagnostics or {}

    def quarantine(self, reason: str) -> Path:
        safe_reason = re.sub(r"[^a-z0-9_-]+", "-", reason.casefold()).strip("-") or "invalid"
        destination = self.path.with_name(
            f"{self.path.name}.{safe_reason}.{time.time_ns()}.{os.getpid()}.quarantine"
        )
        os.replace(self.path, destination)
        fsync_parent(self.path)
        print(
            f"checkpoint {reason}; quarantined as {destination}",
            file=sys.stderr,
        )
        return destination

    def _validated_result(
        self,
        raw: Any,
        *,
        browser: bool,
        direct_by_rank: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise CheckpointError("checkpoint result is not an object")
        rank = raw.get("board_rank")
        if type(rank) is not int or rank not in self.normalized_by_rank:
            raise CheckpointError("checkpoint result has an invalid board rank")
        expected = self.normalized_by_rank[rank]
        for field in ("board_rank", "listing_id", "source", "country", "title", "url"):
            if raw.get(field) != expected[field]:
                raise CheckpointError(
                    f"checkpoint result rank {rank} does not match selected input"
                )
        if raw.get("status") not in {"verified", "dead", "unknown"}:
            raise CheckpointError(f"checkpoint result rank {rank} has an invalid status")
        http_status = raw.get("http_status")
        if http_status is not None and type(http_status) is not int:
            raise CheckpointError(f"checkpoint result rank {rank} has an invalid HTTP status")
        if not isinstance(raw.get("final_url"), str) or not isinstance(raw.get("reason"), str):
            raise CheckpointError(f"checkpoint result rank {rank} has invalid evidence")
        if browser:
            direct = direct_by_rank.get(rank)
            if direct is None or raw.get("direct_reason") != direct.get("reason", ""):
                raise CheckpointError(
                    f"checkpoint browser result rank {rank} has invalid direct evidence"
                )
        elif "direct_reason" in raw:
            raise CheckpointError(
                f"checkpoint direct result rank {rank} contains browser evidence"
            )
        # Re-encode now so non-finite or otherwise non-canonical values fail closed.
        canonical_json_bytes(raw)
        return raw

    def _validate(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CheckpointError("checkpoint root is not an object")
        supplied_checksum = payload.get("checkpoint_sha256")
        unsigned = {key: value for key, value in payload.items() if key != "checkpoint_sha256"}
        expected_checksum = sha256_bytes(canonical_json_bytes(unsigned))
        if not isinstance(supplied_checksum, str) or not hmac.compare_digest(
            supplied_checksum, expected_checksum
        ):
            raise CheckpointError("checkpoint checksum mismatch")
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError("checkpoint schema mismatch")
        if payload.get("contract") != CHECKPOINT_CONTRACT:
            raise CheckpointError("checkpoint contract mismatch")
        if payload.get("identity_sha256") != self.identity_sha256:
            raise CheckpointError("checkpoint identity mismatch")
        if payload.get("identity") != self.identity:
            raise CheckpointError("checkpoint identity mismatch")
        if payload.get("stage") not in {"direct", "browser", "complete"}:
            raise CheckpointError("checkpoint stage is invalid")
        if not isinstance(payload.get("run_started_at"), str) or not payload["run_started_at"]:
            raise CheckpointError("checkpoint run timestamp is invalid")
        try:
            started_at = datetime.fromisoformat(
                payload["run_started_at"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise CheckpointError("checkpoint run timestamp is invalid") from exc
        if started_at.tzinfo is None:
            raise CheckpointError("checkpoint run timestamp is invalid")
        age = (datetime.now(UTC) - started_at).total_seconds()
        max_age = int(self.identity["config"]["checkpoint_max_age_sec"])
        if age < -300 or age > max_age:
            raise CheckpointError("checkpoint has expired or is future-dated")

        direct_by_rank: dict[int, dict[str, Any]] = {}
        previous_rank = 0
        direct_results = payload.get("direct_results")
        if not isinstance(direct_results, list):
            raise CheckpointError("checkpoint direct results are invalid")
        for raw in direct_results:
            result = self._validated_result(
                raw, browser=False, direct_by_rank=direct_by_rank
            )
            rank = result["board_rank"]
            if rank <= previous_rank:
                raise CheckpointError("checkpoint direct ranks are duplicate or unordered")
            direct_by_rank[rank] = result
            previous_rank = rank

        targets = payload.get("browser_target_ranks")
        if (
            not isinstance(targets, list)
            or any(type(rank) is not int for rank in targets)
            or targets != sorted(set(targets))
        ):
            raise CheckpointError("checkpoint browser target ranks are invalid")

        browser_by_rank: dict[int, dict[str, Any]] = {}
        previous_rank = 0
        browser_results = payload.get("browser_results")
        if not isinstance(browser_results, list):
            raise CheckpointError("checkpoint browser results are invalid")
        for raw in browser_results:
            result = self._validated_result(
                raw, browser=True, direct_by_rank=direct_by_rank
            )
            rank = result["board_rank"]
            if rank <= previous_rank or rank not in targets:
                raise CheckpointError("checkpoint browser ranks are duplicate or unexpected")
            browser_by_rank[rank] = result
            previous_rank = rank

        stage = payload["stage"]
        all_ranks = set(self.normalized_by_rank)
        if stage == "direct":
            if targets or browser_by_rank:
                raise CheckpointError("direct checkpoint contains browser state")
        else:
            if set(direct_by_rank) != all_ranks:
                raise CheckpointError("checkpoint direct ranks are incomplete")
            expected_targets = (
                select_browser_target_ranks(
                    list(direct_by_rank.values()),
                    int(self.identity["config"]["browser_limit"]),
                )
                if self.identity["config"]["browser_fallback"]
                else []
            )
            if targets != expected_targets:
                raise CheckpointError("checkpoint browser targets do not match direct results")
            if stage == "complete" and set(browser_by_rank) != set(targets):
                raise CheckpointError("complete checkpoint has incomplete browser coverage")
        return {
            "stage": stage,
            "run_started_at": payload["run_started_at"],
            "direct_by_rank": direct_by_rank,
            "browser_target_ranks": targets,
            "browser_by_rank": browser_by_rank,
        }

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            if self.path.stat().st_size > 128 * 1024 * 1024:
                raise CheckpointError("checkpoint is too large")
            payload = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=unique_json_object,
                parse_constant=reject_non_finite_json,
            )
            return self._validate(payload)
        except (CheckpointError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
            self.quarantine("invalid-or-mismatched")
            raise CheckpointError(f"checkpoint rejected after quarantine: {exc}") from exc

    def save(
        self,
        *,
        stage: str,
        run_started_at: str,
        direct_by_rank: dict[int, dict[str, Any]],
        browser_target_ranks: list[int],
        browser_by_rank: dict[int, dict[str, Any]],
    ) -> None:
        unsigned = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "contract": CHECKPOINT_CONTRACT,
            "identity": self.identity,
            "identity_sha256": self.identity_sha256,
            "stage": stage,
            "run_started_at": run_started_at,
            "checkpointed_at": utc_now(),
            "source_diagnostics": self.source_diagnostics,
            "direct_results": [direct_by_rank[rank] for rank in sorted(direct_by_rank)],
            "browser_target_ranks": browser_target_ranks,
            "browser_results": [browser_by_rank[rank] for rank in sorted(browser_by_rank)],
        }
        payload = {
            **unsigned,
            "checkpoint_sha256": sha256_bytes(canonical_json_bytes(unsigned)),
        }
        atomic_json_write(self.path, payload)

    def remove_completed(self) -> None:
        state = self.load()
        if state is None or state["stage"] != "complete":
            raise CheckpointError("refusing to remove a non-complete checkpoint")
        self.path.unlink()
        fsync_parent(self.path)


class CheckpointCadence:
    def __init__(
        self,
        *,
        batch_size: int,
        interval_sec: float,
        save: Callable[[], None],
    ) -> None:
        self.batch_size = batch_size
        self.interval_sec = interval_sec
        self.save = save
        self.pending = 0
        self.last_saved = time.monotonic()

    def changed(self) -> None:
        self.pending += 1
        now = time.monotonic()
        if self.pending >= self.batch_size or now - self.last_saved >= self.interval_sec:
            self.flush()

    def flush(self) -> None:
        if self.pending:
            self.save()
            self.pending = 0
            self.last_saved = time.monotonic()


def verify_offers(
    offers: list[dict[str, Any]],
    *,
    limit: int,
    workers: int,
    timeout_sec: int,
    id_index: dict[str, str] | None = None,
    checker: Callable[[str, int], dict[str, Any]] = check_url,
    existing_results: dict[int, dict[str, Any]] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    selected = offers[: max(0, limit)] if limit else offers
    normalized = [
        normalize_offer(offer, rank, id_index or {})
        for rank, offer in enumerate(selected, start=1)
    ]
    results: list[dict[str, Any] | None] = [None] * len(normalized)
    for rank, result in (existing_results or {}).items():
        if 1 <= rank <= len(results):
            results[rank - 1] = result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(checker, offer["url"], timeout_sec): index
            for index, offer in enumerate(normalized)
            if results[index] is None
        }
        for future in as_completed(futures):
            index = futures[future]
            results[index] = {**normalized[index], **future.result()}
            if on_result is not None:
                on_result(results[index])

    return [result for result in results if result is not None]


def build_report(
    *,
    input_path: Path,
    input_payload: dict[str, Any],
    results: list[dict[str, Any]],
    requested_limit: int,
    generated_at: str | None = None,
    verified_target: int = 0,
    direct_attempted_count: int | None = None,
    browser_target_count: int = 0,
    browser_attempted_count: int = 0,
    target_reached: bool = False,
    pool_exhausted: bool = False,
    ranked_candidate_count: int | None = None,
    ranked_universe_exhausted: bool = False,
    full_input_coverage: bool = True,
) -> dict[str, Any]:
    statuses = ("verified", "dead", "unknown")
    counts = {status: sum(item["status"] == status for item in results) for status in statuses}
    reason_counts = Counter(str(item.get("reason") or "unspecified") for item in results)
    source_status: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        source_status[str(item.get("source") or "Unknown")][str(item.get("status") or "unknown")] += 1
    return {
        "schema_version": 1,
        "generated_at": generated_at or utc_now(),
        "input": str(input_path),
        "input_updated_at": input_payload.get("updated_utc") or input_payload.get("generated_at"),
        "requested_limit": requested_limit,
        "checked": len(results),
        "ranked_pool_count": len(results),
        "verified_target": verified_target,
        "direct_attempted_count": (
            len(results) if direct_attempted_count is None else direct_attempted_count
        ),
        "browser_target_count": browser_target_count,
        "browser_attempted_count": browser_attempted_count,
        "target_reached": target_reached,
        "pool_exhausted": pool_exhausted,
        "ranked_candidate_count": (
            len(results) if ranked_candidate_count is None else ranked_candidate_count
        ),
        "ranked_universe_exhausted": ranked_universe_exhausted,
        "full_input_coverage": full_input_coverage,
        "counts": counts,
        "reason_counts": dict(reason_counts.most_common()),
        "source_status_counts": {
            source: dict(status_counts)
            for source, status_counts in sorted(
                source_status.items(),
                key=lambda pair: (-sum(pair[1].values()), pair[0]),
            )
        },
        "verified_listing_ids": [
            item["listing_id"] for item in results if item["status"] == "verified" and item["listing_id"]
        ],
        "dead_listing_ids": [
            item["listing_id"] for item in results if item["status"] == "dead" and item["listing_id"]
        ],
        "unknown_listing_ids": [
            item["listing_id"] for item in results if item["status"] == "unknown" and item["listing_id"]
        ],
        "dead_urls": [item["url"] for item in results if item["status"] == "dead"],
        "unknown_urls": [item["url"] for item in results if item["status"] == "unknown"],
        "results": results,
    }


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        fsync_parent(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_BOARD)
    parser.add_argument("--id-index", type=Path, default=DEFAULT_ID_INDEX)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout-sec", type=int, default=10)
    parser.add_argument("--browser-fallback", action="store_true")
    parser.add_argument("--browser-limit", type=int, default=2_000)
    parser.add_argument("--browser-workers", type=int, default=6)
    parser.add_argument("--browser-timeout-sec", type=int, default=30)
    parser.add_argument("--verified-target", type=int, default=10_000)
    parser.add_argument("--checkpoint", "--checkpoint-json", type=Path)
    parser.add_argument(
        "--discard-checkpoint",
        action="store_true",
        help="quarantine any existing checkpoint and start a fresh validation",
    )
    parser.add_argument(
        "--checkpoint-batch-size", type=int, default=DEFAULT_CHECKPOINT_BATCH_SIZE
    )
    parser.add_argument(
        "--checkpoint-interval-sec", type=float, default=DEFAULT_CHECKPOINT_INTERVAL_SEC
    )
    parser.add_argument(
        "--checkpoint-max-age-sec", type=int, default=DEFAULT_CHECKPOINT_MAX_AGE_SEC
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.workers < 1 or args.timeout_sec < 1:
        raise RuntimeError("limit must be nonnegative and direct worker/time values positive")
    if (
        args.browser_limit < 0
        or args.browser_workers < 1
        or args.browser_timeout_sec < 1
        or args.checkpoint_batch_size < 1
        or args.checkpoint_interval_sec <= 0
        or args.checkpoint_max_age_sec < 1
        or args.verified_target < 1
    ):
        raise RuntimeError("browser/checkpoint limits are invalid")
    args.checkpoint = args.checkpoint or args.output_json.with_name(
        f"{args.output_json.name}.checkpoint.json"
    )
    protected_paths = {args.input.resolve(), args.output_json.resolve()}
    if args.id_index:
        protected_paths.add(args.id_index.resolve())
    if args.checkpoint.resolve() in protected_paths:
        raise RuntimeError("checkpoint path must be distinct from input, ID index, and output")

    input_raw = args.input.read_bytes()
    offers, input_payload = decode_offer_list(input_raw, args.input)
    if not offers:
        raise RuntimeError(f"refusing to validate an empty board: {args.input}")

    id_index_raw = (
        args.id_index.read_bytes() if args.id_index and args.id_index.exists() else None
    )
    if id_index_raw is None:
        id_index = {}
    else:
        index_offers, _ = decode_offer_list(id_index_raw, args.id_index)
        id_index = {}
        for offer in index_offers:
            url = str(offer.get("url") or offer.get("u") or offer.get("source_url") or "").strip()
            listing_id = str(offer.get("id") or offer.get("listing_id") or "").strip()
            if url and listing_id:
                id_index.setdefault(url, listing_id)
    normalized = normalize_offers(offers, limit=args.limit, id_index=id_index)
    if not normalized:
        raise RuntimeError("refusing to validate an empty selected board")
    identity = build_checkpoint_identity(
        args=args,
        normalized=normalized,
        input_updated_at=input_payload.get("updated_utc")
        or input_payload.get("generated_at"),
        input_file_sha256=sha256_bytes(input_raw),
        id_index_file_sha256=(
            sha256_bytes(id_index_raw) if id_index_raw is not None else None
        ),
    )
    store = CheckpointStore(
        args.checkpoint,
        identity=identity,
        normalized=normalized,
        source_diagnostics={
            "input_file_sha256": sha256_bytes(input_raw),
            "id_index_file_sha256": (
                sha256_bytes(id_index_raw) if id_index_raw is not None else None
            ),
        },
    )
    if args.discard_checkpoint and args.checkpoint.exists():
        store.quarantine("explicitly-discarded")
    state = store.load()
    if state is None:
        state = {
            "stage": "direct",
            "run_started_at": utc_now(),
            "direct_by_rank": {},
            "browser_target_ranks": [],
            "browser_by_rank": {},
        }

    def save_state() -> None:
        store.save(**state)

    save_state()
    cadence = CheckpointCadence(
        batch_size=args.checkpoint_batch_size,
        interval_sec=args.checkpoint_interval_sec,
        save=save_state,
    )

    if state["stage"] == "direct":
        def direct_completed(result: dict[str, Any]) -> None:
            state["direct_by_rank"][result["board_rank"]] = result
            cadence.changed()

        try:
            direct_results = verify_offers(
                offers,
                limit=args.limit,
                workers=args.workers,
                timeout_sec=args.timeout_sec,
                id_index=id_index,
                existing_results=state["direct_by_rank"],
                on_result=direct_completed,
            )
        except BaseException:
            cadence.flush()
            raise
        if len(direct_results) != len(normalized):
            raise CheckpointError("direct validation did not produce exactly one result per rank")
        state["direct_by_rank"] = {
            item["board_rank"]: item for item in direct_results
        }
        state["browser_target_ranks"] = (
            select_browser_target_ranks(direct_results, args.browser_limit)
            if args.browser_fallback
            else []
        )
        state["stage"] = "browser"
        save_state()
        cadence = CheckpointCadence(
            batch_size=args.checkpoint_batch_size,
            interval_sec=args.checkpoint_interval_sec,
            save=save_state,
        )

    results = [
        state["browser_by_rank"].get(rank, state["direct_by_rank"][rank])
        for rank in sorted(state["direct_by_rank"])
    ]
    if state["stage"] == "browser" and args.browser_fallback:
        def browser_completed(result: dict[str, Any]) -> None:
            state["browser_by_rank"][result["board_rank"]] = result
            cadence.changed()

        try:
            results = browser_verify_unknowns(
                results,
                limit=args.browser_limit,
                workers=args.browser_workers,
                timeout_sec=args.browser_timeout_sec,
                target_ranks=state["browser_target_ranks"],
                completed_ranks=set(state["browser_by_rank"]),
                on_result=browser_completed,
            )
        except BaseException as exc:
            cadence.flush()
            print(
                "browser fallback incomplete; preserving checkpoint for resume: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise
    if len(results) != len(normalized) or [item["board_rank"] for item in results] != list(
        range(1, len(normalized) + 1)
    ):
        raise CheckpointError("final validation ranks are duplicate, missing, or unordered")
    all_browser_targets = select_browser_target_ranks(
        list(state["direct_by_rank"].values()), 0
    )
    browser_coverage_complete = set(state["browser_by_rank"]) == set(
        state["browser_target_ranks"]
    )
    target_reached = sum(item["status"] == "verified" for item in results) >= args.verified_target
    saved_top_rows = input_payload.get("saved_top_rows")
    ranked_candidate_count = input_payload.get("ranked_candidate_rows")
    full_input_coverage = (
        len(normalized) == len(offers)
        and type(saved_top_rows) is int
        and saved_top_rows == len(offers)
    )
    ranked_universe_exhausted = (
        full_input_coverage
        and input_payload.get("ranking_complete") is True
        and type(ranked_candidate_count) is int
        and ranked_candidate_count <= saved_top_rows
    )
    pool_exhausted = (
        browser_coverage_complete
        and ranked_universe_exhausted
        and (
            (args.browser_fallback and state["browser_target_ranks"] == all_browser_targets)
            or (not args.browser_fallback and not all_browser_targets)
        )
    )
    if not target_reached and not pool_exhausted:
        cadence.flush()
        raise CheckpointError(
            "verified target was not reached and the ranked pool was not exhausted"
        )
    state["stage"] = "complete"
    save_state()
    report = build_report(
        input_path=args.input,
        input_payload=input_payload,
        results=results,
        requested_limit=args.limit,
        generated_at=state["run_started_at"],
        verified_target=args.verified_target,
        direct_attempted_count=len(state["direct_by_rank"]),
        browser_target_count=len(state["browser_target_ranks"]),
        browser_attempted_count=len(state["browser_by_rank"]),
        target_reached=target_reached,
        pool_exhausted=pool_exhausted,
        ranked_candidate_count=(
            ranked_candidate_count if type(ranked_candidate_count) is int else -1
        ),
        ranked_universe_exhausted=ranked_universe_exhausted,
        full_input_coverage=full_input_coverage,
    )
    atomic_json_write(args.output_json, report)
    store.remove_completed()
    counts = report["counts"]
    print(
        f"VERIFIED={counts['verified']} DEAD={counts['dead']} "
        f"UNKNOWN={counts['unknown']} RESULTS={args.output_json}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
