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
import json
import os
import re
import shutil
import sys
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
) -> list[dict[str, Any]]:
    from patchright.async_api import async_playwright

    targets = [
        (index, item)
        for index, item in enumerate(results)
        if item.get("status") == "unknown"
        and str(item.get("url") or "").startswith("http")
    ]
    if limit > 0:
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
) -> list[dict[str, Any]]:
    return asyncio.run(
        _browser_verify_unknowns(
            results, limit=limit, workers=workers, timeout_sec=timeout_sec
        )
    )


def load_offer_list(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload, {}
    offers = payload.get("offers", payload.get("data", []))
    if not isinstance(offers, list):
        raise ValueError(f"input contains no offers list: {path}")
    return offers, payload


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


def verify_offers(
    offers: list[dict[str, Any]],
    *,
    limit: int,
    workers: int,
    timeout_sec: int,
    id_index: dict[str, str] | None = None,
    checker: Callable[[str, int], dict[str, Any]] = check_url,
) -> list[dict[str, Any]]:
    selected = offers[: max(0, limit)] if limit else offers
    normalized = [
        normalize_offer(offer, rank, id_index or {})
        for rank, offer in enumerate(selected, start=1)
    ]
    results: list[dict[str, Any] | None] = [None] * len(normalized)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(checker, offer["url"], timeout_sec): index
            for index, offer in enumerate(normalized)
        }
        for future in as_completed(futures):
            index = futures[future]
            results[index] = {**normalized[index], **future.result()}

    return [result for result in results if result is not None]


def build_report(
    *,
    input_path: Path,
    input_payload: dict[str, Any],
    results: list[dict[str, Any]],
    requested_limit: int,
) -> dict[str, Any]:
    statuses = ("verified", "dead", "unknown")
    counts = {status: sum(item["status"] == status for item in results) for status in statuses}
    reason_counts = Counter(str(item.get("reason") or "unspecified") for item in results)
    source_status: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        source_status[str(item.get("source") or "Unknown")][str(item.get("status") or "unknown")] += 1
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "input": str(input_path),
        "input_updated_at": input_payload.get("updated_utc") or input_payload.get("generated_at"),
        "requested_limit": requested_limit,
        "checked": len(results),
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
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    offers, input_payload = load_offer_list(args.input)
    if not offers:
        raise RuntimeError(f"refusing to validate an empty board: {args.input}")

    id_index = load_id_index(args.id_index)
    results = verify_offers(
        offers,
        limit=args.limit,
        workers=args.workers,
        timeout_sec=args.timeout_sec,
        id_index=id_index,
    )
    if args.browser_fallback:
        try:
            results = browser_verify_unknowns(
                results,
                limit=args.browser_limit,
                workers=args.browser_workers,
                timeout_sec=args.browser_timeout_sec,
            )
        except Exception as exc:
            print(
                "browser fallback unavailable; preserving unknown results: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    report = build_report(
        input_path=args.input,
        input_payload=input_payload,
        results=results,
        requested_limit=args.limit,
    )
    atomic_json_write(args.output_json, report)
    counts = report["counts"]
    print(
        f"VERIFIED={counts['verified']} DEAD={counts['dead']} "
        f"UNKNOWN={counts['unknown']} RESULTS={args.output_json}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
