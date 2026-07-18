#!/usr/bin/env python3
"""Authenticated VPS client for the radar-control Cloudflare Worker."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_API = "https://sonardeals-radar-control.krtabd120.workers.dev"
DEFAULT_TOKEN = Path("/etc/sonardeals-radar/internal-token")
METRIC_FIELDS = (
    "universe_unique_offers", "qualified_universe_offers",
    "ranked_offer_count", "published_offer_count", "verified_live_count",
    "connected_country_count", "connected_source_count", "generation_id",
)


class ControlError(RuntimeError):
    pass


def request_json(
    api: str,
    token_path: Path,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    retries: int = 4,
) -> tuple[int, dict[str, Any] | None]:
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ControlError("internal token is empty")
    body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        api.rstrip("/") + endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "SonarDeals-Radar-VPS/1.0",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            if error.code == 204:
                return 204, None
            if error.code not in {429, 500, 502, 503, 504} or attempt + 1 >= retries:
                detail = raw.decode("utf-8", "replace")[:500]
                raise ControlError(f"control HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt + 1 >= retries:
                raise ControlError(f"control request failed: {error}") from error
        time.sleep(min(2 ** attempt, 8))
    raise ControlError("control request exhausted retries")


def claim(api: str, token: Path) -> dict[str, Any] | None:
    status, value = request_json(api, token, "/internal/claim")
    if status == 204:
        return None
    if status != 200 or not value or not value.get("ok"):
        raise ControlError(f"invalid claim response: status={status}")
    return value


def update(api: str, token: Path, payload: dict[str, Any]) -> dict[str, Any]:
    status, value = request_json(api, token, "/internal/update", payload)
    if status != 200 or not value or not value.get("ok"):
        raise ControlError(f"invalid update response: status={status}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("claim")
    update_parser = commands.add_parser("update")
    update_parser.add_argument("--job-id", required=True)
    update_parser.add_argument("--status", required=True)
    update_parser.add_argument("--phase", required=True)
    update_parser.add_argument("--message", required=True)
    update_parser.add_argument("--mode", choices=("smart", "full"))
    update_parser.add_argument("--error-code")
    update_parser.add_argument("--metrics-file", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "claim":
        result = claim(args.api, args.token)
        if result:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    payload: dict[str, Any] = {
        "job_id": args.job_id,
        "status": args.status,
        "phase": args.phase,
        "message": args.message,
    }
    if args.mode:
        payload["mode"] = args.mode
    if args.error_code:
        payload["error_code"] = args.error_code
    if args.status == "ok":
        from datetime import UTC, datetime
        payload["completed_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if args.metrics_file and args.metrics_file.exists():
        metrics = json.loads(args.metrics_file.read_text(encoding="utf-8"))
        for field in METRIC_FIELDS:
            if field in metrics:
                payload[field] = metrics[field]
    result = update(args.api, args.token, payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
