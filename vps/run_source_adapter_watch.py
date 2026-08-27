#!/usr/bin/env python3
"""Run the generated source adapters and merge their output into the watch lane.

Every canonical source has a generated launcher under ``source_launchers_118``.
This bridge discovers operator-supplied adapter configurations, runs the matching
launchers, and writes one normal monitored-auction input for ``auction_refresh``.
Sources without a configuration stay visible in ``source_reports`` with zero rows;
they do not prevent configured sources from refreshing.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from auction_registry import auction_source_by_key


UTC = dt.timezone.utc
SCHEMA_VERSION = 1
WATCH_LANE = "official_auction_watch"
RESEARCH_LANE = "source_acquisition_research"
PRICE_KINDS = frozenset({
    "current_bid", "starting_bid", "minimum_bid", "guide_price", "sealed_bid",
    "hidden", "unknown", "minimum_offer", "base_price",
})
EUR_PRICE_KINDS = frozenset({
    "current_bid", "starting_bid", "minimum_bid", "guide_price",
})


class AdapterWatchError(RuntimeError):
    """Raised for a malformed local adapter package or configuration."""


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def utc_now() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AdapterWatchError(f"cannot read JSON file: {path.name}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def source_profiles(launchers_dir: Path) -> list[dict[str, str]]:
    manifest_path = launchers_dir / "source_launcher_manifest.json"
    manifest = load_json(manifest_path)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("canonical_identity_count") != 118
        or not isinstance(manifest.get("sources"), list)
        or len(manifest["sources"]) != 118
    ):
        raise AdapterWatchError("source launcher manifest must account for 118 sources")
    profiles: list[dict[str, str]] = []
    identities: set[str] = set()
    source_keys: set[str] = set()
    for raw in manifest["sources"]:
        if not isinstance(raw, dict):
            raise AdapterWatchError("source launcher manifest has a malformed source")
        identity = clean(raw.get("canonical_identity"))
        source_key = clean(raw.get("connector_path"))
        launcher_name = clean(raw.get("launcher"))
        # The manifest's ``connector_path`` describes bespoke connectors.  The
        # generated launcher itself discovers its registry key from the ledger;
        # obtain it here from the authoritative ledger below instead.
        if not identity or not launcher_name or Path(launcher_name).name != launcher_name:
            raise AdapterWatchError("source launcher manifest has an unsafe launcher")
        launcher = launchers_dir / launcher_name
        if launcher.suffix != ".py" or not launcher.is_file():
            raise AdapterWatchError(f"source launcher is missing: {launcher_name}")
        if identity in identities:
            raise AdapterWatchError("source launcher manifest has duplicate identities")
        identities.add(identity)
        profiles.append({
            "identity": identity,
            "launcher": str(launcher),
            "connector_path": source_key,
        })

    ledger = load_json(launchers_dir / "source_completion_ledger.json")
    raw_sources = ledger.get("sources") if isinstance(ledger, dict) else None
    if not isinstance(raw_sources, list) or len(raw_sources) != 118:
        raise AdapterWatchError("source completion ledger must account for 118 sources")
    ledger_keys: dict[str, str] = {}
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise AdapterWatchError("source completion ledger has a malformed source")
        identity = clean(raw.get("canonical_identity"))
        keys = raw.get("registry_keys")
        if not identity or not isinstance(keys, list) or len(keys) != 1:
            raise AdapterWatchError("each source must map to exactly one registry key")
        key = clean(keys[0])
        if not key or identity in ledger_keys or key in source_keys:
            raise AdapterWatchError("source completion ledger has duplicate source mappings")
        if auction_source_by_key(key) is None:
            raise AdapterWatchError(f"source key is absent from the auction registry: {key}")
        ledger_keys[identity] = key
        source_keys.add(key)
    if set(ledger_keys) != identities:
        raise AdapterWatchError("launcher manifest and ledger identities do not match")
    for profile in profiles:
        profile["source_key"] = ledger_keys[profile["identity"]]
    return sorted(profiles, key=lambda profile: profile["identity"])


def configured_sources(config_dir: Path, identities: set[str]) -> tuple[dict[str, tuple[Path, dict[str, Any]]], set[str]]:
    configured: dict[str, tuple[Path, dict[str, Any]]] = {}
    duplicate_identities: set[str] = set()
    if not config_dir.is_dir():
        return configured, duplicate_identities
    for path in sorted(config_dir.glob("*.json")):
        try:
            raw = load_json(path)
        except AdapterWatchError:
            continue
        identity = clean(raw.get("canonical_identity")) if isinstance(raw, dict) else ""
        if identity not in identities:
            continue
        if identity in configured:
            duplicate_identities.add(identity)
            continue
        configured[identity] = (path, raw)
    for identity in duplicate_identities:
        configured.pop(identity, None)
    return configured, duplicate_identities


def safe_feed_file(raw: Any, *, config_path: Path, feed_root: Path) -> Path:
    value = clean(raw)
    if not value:
        raise AdapterWatchError("file-mode adapter requires execution.feed_file")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(feed_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise AdapterWatchError("feed file is outside the configured feed root") from exc
    if not resolved.is_file():
        raise AdapterWatchError("configured feed file is not a regular file")
    return resolved


def execution_command(
    profile: dict[str, str],
    *,
    config_path: Path,
    config: dict[str, Any],
    feed_root: Path,
    output: Path,
) -> list[str]:
    execution = config.get("execution")
    if execution is None:
        execution = {}
    if not isinstance(execution, dict):
        raise AdapterWatchError("adapter execution must be an object")
    mode = clean(execution.get("mode") or "network").lower()
    command = [sys.executable, profile["launcher"], "--config", str(config_path), "--out", str(output)]
    if mode == "network":
        return [*command, "--network"]
    if mode == "file":
        return [*command, "--feed-file", str(safe_feed_file(execution.get("feed_file"), config_path=config_path, feed_root=feed_root))]
    raise AdapterWatchError("adapter execution mode must be network or file")


def number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not (result > 0 and result < float("inf")):
        return None
    return int(result) if result.is_integer() else result


def watch_row(raw: dict[str, Any], *, source_key: str, observed_at: str) -> dict[str, Any]:
    registry = auction_source_by_key(source_key)
    if registry is None:
        raise AdapterWatchError("configured source is absent from the registry")
    amount = number(raw.get("price_amount"))
    currency = clean(raw.get("price_currency")).upper() or "EUR"
    raw_kind = clean(raw.get("price_kind")).lower() or "unknown"
    if raw_kind not in PRICE_KINDS:
        raw_kind = "unknown"
    price_kind = raw_kind
    price_eur: int | float | None = None
    if currency == "EUR" and amount is not None and raw_kind in EUR_PRICE_KINDS:
        price_eur = amount
    elif raw_kind in EUR_PRICE_KINDS:
        # A non-EUR feed price must never be represented as EUR.  Keep the
        # original amount/currency but label its EUR semantic as unknown.
        price_kind = "unknown"
    if price_kind == "hidden":
        amount = None
    return {
        "id": clean(raw.get("id")),
        "source": source_key,
        "source_key": source_key,
        "url": clean(raw.get("url")),
        "title": clean(raw.get("title")),
        "model": clean(raw.get("title")),
        "country": registry.country.upper(),
        "category": clean(raw.get("category")).lower() or "unknown",
        "category_raw": clean(raw.get("category")).lower(),
        "year": raw.get("year"),
        "mileage": raw.get("mileage"),
        "fuel": clean(raw.get("fuel")).lower(),
        "location": clean(raw.get("location")),
        "price_eur": price_eur,
        "price_kind": price_kind,
        "price_currency": currency,
        "price_amount": amount,
        "price_label": "configured source feed",
        "bid_visibility": "source feed",
        "canonical_end_utc": raw.get("end_at_utc") or None,
        "sale_end_utc": raw.get("end_at_utc") or None,
        "last_seen_at": observed_at,
        "eligibility_status": "review_required",
        "eligibility_reason": "تم الجلب من موصل المصدر المهيأ؛ حالة العرض تُراجع داخل واجهة المزادات.",
        "access_sale_note": "Configured source feed.",
        "raw_evidence_ref": f"source-adapter:{source_key}",
        "adapter_authorized": True,
    }


def run_profile(
    profile: dict[str, str],
    *,
    config_path: Path,
    config: dict[str, Any],
    feed_root: Path,
    work_dir: Path,
    timeout_seconds: int,
    observed_at: str,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    source_key = profile["source_key"]
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{source_key}-", dir=work_dir) as temporary:
            output = Path(temporary) / "adapter-output.json"
            command = execution_command(
                profile,
                config_path=config_path,
                config=config,
                feed_root=feed_root,
                output=output,
            )
            completed = subprocess.run(
                command,
                cwd=Path(profile["launcher"]).parent,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise AdapterWatchError(f"launcher exited with status {completed.returncode}")
            document = load_json(output)
    except subprocess.TimeoutExpired:
        return source_key, [], {
            "status": "error", "connector_status": "error", "normalized_rows": 0,
            "error": f"adapter timed out after {timeout_seconds}s",
        }
    except (AdapterWatchError, OSError, ValueError):
        return source_key, [], {
            "status": "error", "connector_status": "error", "normalized_rows": 0,
            "error": "configured adapter failed",
        }
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("lane") != RESEARCH_LANE
        or document.get("canonical_identity") != profile["identity"]
        or document.get("row_count") != len(document.get("rows") if isinstance(document.get("rows"), list) else [])
        or not isinstance(document.get("rows"), list)
    ):
        return source_key, [], {
            "status": "error", "connector_status": "error", "normalized_rows": 0,
            "error": "configured adapter returned an invalid snapshot",
        }
    try:
        rows = [watch_row(row, source_key=source_key, observed_at=observed_at) for row in document["rows"]]
    except (AdapterWatchError, TypeError, ValueError):
        return source_key, [], {
            "status": "error", "connector_status": "error", "normalized_rows": 0,
            "error": "configured adapter returned an invalid row",
        }
    return source_key, rows, {
        "status": "ok",
        "connector_status": "ok",
        "configured": True,
        "declared": document["row_count"],
        "visited": document["row_count"],
        "normalized_rows": len(rows),
    }


def build_watch(
    *,
    profiles: list[dict[str, str]],
    config_dir: Path,
    feed_root: Path,
    work_dir: Path,
    timeout_seconds: int,
    workers: int,
    execute: bool,
) -> dict[str, Any]:
    observed_at = utc_now()
    identities = {profile["identity"] for profile in profiles}
    configured, duplicates = configured_sources(config_dir, identities)
    source_reports: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    selected: list[tuple[dict[str, str], Path, dict[str, Any]]] = []
    for profile in profiles:
        identity = profile["identity"]
        source_key = profile["source_key"]
        if identity in duplicates:
            source_reports[source_key] = {
                "status": "error", "connector_status": "error", "normalized_rows": 0,
                "error": "multiple configurations for this source",
            }
        elif identity not in configured:
            source_reports[source_key] = {
                "status": "partial", "connector_status": "awaiting_config",
                "normalized_rows": 0, "configured": False,
            }
        else:
            config_path, config = configured[identity]
            if not isinstance(config, dict):
                source_reports[source_key] = {
                    "status": "error", "connector_status": "error", "normalized_rows": 0,
                    "error": "invalid source configuration",
                }
            elif execute:
                selected.append((profile, config_path, config))
            else:
                source_reports[source_key] = {
                    "status": "partial", "connector_status": "planned",
                    "normalized_rows": 0, "configured": True,
                }
    if execute and selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    run_profile,
                    profile,
                    config_path=config_path,
                    config=config,
                    feed_root=feed_root,
                    work_dir=work_dir,
                    timeout_seconds=timeout_seconds,
                    observed_at=observed_at,
                )
                for profile, config_path, config in selected
            ]
            for future in concurrent.futures.as_completed(futures):
                source_key, source_rows, report = future.result()
                source_reports[source_key] = report
                rows.extend(source_rows)
    rows.sort(key=lambda row: (str(row["source_key"]), str(row["id"])))
    summary: dict[str, int] = {}
    for report in source_reports.values():
        status = str(report.get("status") or "error")
        summary[status] = summary.get(status, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "lane": WATCH_LANE,
        "generated_at_utc": observed_at,
        "row_count": len(rows),
        "source_reports": source_reports,
        "adapter_summary": {
            "canonical_source_count": len(profiles),
            "configured_source_count": len(configured),
            "executed": execute,
            "status_counts": dict(sorted(summary.items())),
        },
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Connect configured generated source adapters to the auction watch"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--launchers-dir", type=Path,
        default=Path(__file__).resolve().with_name("source_launchers_118"),
    )
    parser.add_argument(
        "--config-dir", type=Path,
        default=Path("/etc/sonardeals-radar/auction-source-feeds"),
    )
    parser.add_argument(
        "--feed-root", type=Path,
        default=Path("/var/lib/sonardeals-radar/authorized-feeds"),
    )
    parser.add_argument(
        "--work-dir", type=Path,
        default=Path("/var/lib/sonardeals-radar/runtime/source-adapter-work"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 5 <= args.timeout_seconds <= 900:
        raise SystemExit("--timeout-seconds must be between 5 and 900")
    if not 1 <= args.workers <= 16:
        raise SystemExit("--workers must be between 1 and 16")
    try:
        profiles = source_profiles(args.launchers_dir)
        watch = build_watch(
            profiles=profiles,
            config_dir=args.config_dir,
            feed_root=args.feed_root,
            work_dir=args.work_dir,
            timeout_seconds=args.timeout_seconds,
            workers=args.workers,
            execute=not args.dry_run,
        )
        atomic_write_json(args.out, watch)
    except AdapterWatchError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        "result": "SOURCE_ADAPTER_WATCH_PASS",
        "row_count": watch["row_count"],
        "summary": watch["adapter_summary"],
        "output": str(args.out),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
