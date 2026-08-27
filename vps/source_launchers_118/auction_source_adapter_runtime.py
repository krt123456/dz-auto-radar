#!/usr/bin/env python3
"""Fail-closed runtime for generated per-source acquisition launchers.

The runtime does not invent endpoints or selectors.  Existing source-specific
connectors remain the preferred path.  For sources that require an operator-
authorized feed, this module supplies a strict JSON/JSONL/CSV adapter with
finite page enumeration, stable-ID reconciliation, host pinning, and a small
normalized research envelope.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


UTC = dt.timezone.utc
SCHEMA_VERSION = 1
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,159}$")
SAFE_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
SAFE_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
SUPPORTED_FORMATS = {"json", "jsonl", "csv"}
SUPPORTED_PAGINATION = {"single", "page"}
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_ROWS = 1_000_000
CANONICAL_FIELDS = (
    "id",
    "url",
    "title",
    "country",
    "category",
    "year",
    "mileage",
    "fuel",
    "price_amount",
    "price_currency",
    "price_kind",
    "end_at_utc",
    "location",
)


class AdapterError(ValueError):
    """A feed or mapping cannot prove a complete safe snapshot."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot read JSON {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def profile_index(ledger: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(ledger, dict) or ledger.get("schema_version") != 1:
        raise AdapterError("source completion ledger schema_version must be 1")
    sources = ledger.get("sources")
    if not isinstance(sources, list) or len(sources) != 118:
        raise AdapterError("source completion ledger must contain 118 identities")
    output: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise AdapterError("source completion profile must be an object")
        identity = clean(source.get("canonical_identity"))
        if not identity or identity in output:
            raise AdapterError("source completion ledger has an empty or duplicate identity")
        output[identity] = source
    return output


def profile_hosts(profile: Mapping[str, Any]) -> set[str]:
    values: list[str] = []
    values.extend(str(value) for value in profile.get("display_urls") or [])
    catalogue = profile.get("catalogue") or {}
    if catalogue.get("url"):
        values.append(str(catalogue["url"]))
    values.extend(str(value) for value in catalogue.get("evidence_urls") or [])
    hosts: set[str] = set()
    for value in values:
        candidate = value if "://" in value else f"https://{value}"
        try:
            parsed = urllib.parse.urlparse(candidate)
        except ValueError:
            continue
        if parsed.hostname:
            hosts.add(parsed.hostname.casefold().removeprefix("www."))
    return hosts


def validate_config(
    config: Any, *, identity: str, profile: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise AdapterError("adapter config schema_version must be 1")
    if clean(config.get("canonical_identity")) != identity:
        raise AdapterError("adapter config canonical_identity mismatch")
    keys = [clean(value) for value in profile.get("registry_keys") or []]
    source_key = clean(config.get("source_key"))
    if not SAFE_KEY.fullmatch(source_key) or source_key not in keys:
        raise AdapterError("adapter config source_key is not mapped to this identity")
    feed = config.get("feed")
    mapping = config.get("mapping")
    if not isinstance(feed, dict) or not isinstance(mapping, dict):
        raise AdapterError("adapter config requires feed and mapping objects")
    feed_format = clean(feed.get("format")).lower()
    if feed_format not in SUPPORTED_FORMATS:
        raise AdapterError(f"unsupported feed format: {feed_format!r}")
    pagination = feed.get("pagination")
    if not isinstance(pagination, dict):
        raise AdapterError("feed.pagination must be an object")
    pagination_type = clean(pagination.get("type")).lower()
    if pagination_type not in SUPPORTED_PAGINATION:
        raise AdapterError(f"unsupported pagination type: {pagination_type!r}")
    if feed_format != "json" and pagination_type != "single":
        raise AdapterError("CSV and JSONL feeds must be complete single snapshots")
    for field in ("id", "url", "title"):
        if field not in mapping:
            raise AdapterError(f"mapping is missing required field: {field}")
    unknown = set(mapping) - set(CANONICAL_FIELDS)
    if unknown:
        raise AdapterError(f"mapping has unsupported canonical fields: {sorted(unknown)}")
    allowed_hosts = profile_hosts(profile)
    extra_hosts = feed.get("authorized_hosts") or []
    if not isinstance(extra_hosts, list) or any(not isinstance(value, str) for value in extra_hosts):
        raise AdapterError("feed.authorized_hosts must be a string list")
    if extra_hosts and not clean(feed.get("authorization_assertion")):
        raise AdapterError("extra authorized feed hosts require authorization_assertion")
    for value in extra_hosts:
        host = value.casefold().removeprefix("www.")
        if not host or "/" in host or ":" in host:
            raise AdapterError(f"invalid authorized feed host: {value!r}")
        allowed_hosts.add(host)
    if not allowed_hosts:
        raise AdapterError("profile has no official host boundary")
    validated = json.loads(json.dumps(config, ensure_ascii=False))
    validated["_allowed_hosts"] = sorted(allowed_hosts)
    return validated


def value_at(value: Any, path: Any, *, required: bool = False) -> Any:
    if path in (None, "", []):
        return value
    parts = path if isinstance(path, list) else str(path).split(".")
    current = value
    for raw in parts:
        if isinstance(current, dict):
            if raw not in current:
                if required:
                    raise AdapterError(f"required path is missing: {'.'.join(map(str, parts))}")
                return None
            current = current[raw]
        elif isinstance(current, list) and str(raw).isdigit():
            index = int(raw)
            if index >= len(current):
                if required:
                    raise AdapterError(f"required list path is missing: {'.'.join(map(str, parts))}")
                return None
            current = current[index]
        else:
            if required:
                raise AdapterError(f"required path is not traversable: {'.'.join(map(str, parts))}")
            return None
    return current


def mapped_value(item: Mapping[str, Any], specification: Any) -> Any:
    if isinstance(specification, str) or isinstance(specification, list):
        return value_at(item, specification)
    if not isinstance(specification, dict):
        raise AdapterError("field mapping must be a path, path list, or mapping object")
    if "static" in specification:
        return specification["static"]
    paths = specification.get("paths")
    if paths is None:
        return value_at(item, specification.get("path"))
    if not isinstance(paths, list):
        raise AdapterError("mapping paths must be a list")
    for path in paths:
        candidate = value_at(item, path)
        if candidate not in (None, ""):
            return candidate
    return specification.get("default")


def positive_number(value: Any) -> int | float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, str):
        candidate = re.sub(r"[^0-9,.-]", "", value.strip())
        if "," in candidate and "." in candidate:
            candidate = (
                candidate.replace(".", "").replace(",", ".")
                if candidate.rfind(",") > candidate.rfind(".")
                else candidate.replace(",", "")
            )
        elif "," in candidate:
            tail = candidate.rsplit(",", 1)[-1]
            candidate = candidate.replace(",", ".") if len(tail) <= 2 else candidate.replace(",", "")
        value = candidate
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number) if number.is_integer() else number


def optional_integer(value: Any, *, minimum: int = 0) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= minimum else None


def official_url(value: Any, allowed_hosts: set[str]) -> str:
    candidate = clean(value)
    try:
        parsed = urllib.parse.urlparse(candidate)
        port = parsed.port
    except ValueError as exc:
        raise AdapterError(f"invalid source URL: {candidate!r}") from exc
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)
    ):
        raise AdapterError(f"row URL is outside the pinned official hosts: {candidate!r}")
    return candidate


def normalize_item(
    item: Any,
    *,
    mapping: Mapping[str, Any],
    source_key: str,
    allowed_hosts: set[str],
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise AdapterError("feed item is not an object")
    raw = {field: mapped_value(item, mapping[field]) for field in mapping}
    native_id = clean(raw.get("id"))
    title = clean(raw.get("title"))
    if not native_id or not title:
        raise AdapterError("feed item is missing a stable ID or title")
    row_id = native_id if native_id.startswith(f"{source_key}:") else f"{source_key}:{native_id}"
    if len(row_id) > 500:
        raise AdapterError("feed item ID is unreasonably long")
    currency = clean(raw.get("price_currency")).upper()
    if currency and re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise AdapterError(f"feed item has invalid currency: {currency!r}")
    return {
        "id": row_id,
        "source_key": source_key,
        "url": official_url(raw.get("url"), allowed_hosts),
        "title": title,
        "country": clean(raw.get("country")).upper(),
        "category": clean(raw.get("category")).lower() or "unknown",
        "year": optional_integer(raw.get("year"), minimum=1900),
        "mileage": optional_integer(raw.get("mileage"), minimum=0),
        "fuel": clean(raw.get("fuel")).lower(),
        "price_amount": positive_number(raw.get("price_amount")),
        "price_currency": currency,
        "price_kind": clean(raw.get("price_kind")).lower() or "unknown",
        "end_at_utc": clean(raw.get("end_at_utc")) or None,
        "location": clean(raw.get("location")),
    }


def decode_items(body: bytes, *, feed_format: str, items_path: Any) -> tuple[list[Any], Any]:
    text = body.decode("utf-8-sig", errors="strict")
    if feed_format == "json":
        document = json.loads(text)
        items = value_at(document, items_path, required=True)
        if not isinstance(items, list):
            raise AdapterError("configured JSON items_path does not resolve to a list")
        return items, document
    if feed_format == "jsonl":
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
        return items, items
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise AdapterError("CSV feed has no header")
    items = list(reader)
    return items, items


def read_limited(response: Any) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise AdapterError(f"feed response exceeds {MAX_RESPONSE_BYTES} bytes")
    return body


def request_headers(feed: Mapping[str, Any]) -> dict[str, str]:
    raw = feed.get("headers") or {}
    if not isinstance(raw, dict):
        raise AdapterError("feed.headers must be an object")
    output = {"User-Agent": "SonarDeals-Authorized-Feed/1.0"}
    for name, value in raw.items():
        if not SAFE_HEADER.fullmatch(str(name)):
            raise AdapterError(f"invalid HTTP header name: {name!r}")
        if isinstance(value, str):
            if str(name).casefold() in {"authorization", "cookie", "proxy-authorization"}:
                raise AdapterError(f"secret header must be loaded from an environment variable: {name}")
            output[str(name)] = value
            continue
        if not isinstance(value, dict) or not SAFE_ENV.fullmatch(clean(value.get("env"))):
            raise AdapterError(f"header {name!r} must be a string or environment reference")
        environment_name = clean(value["env"])
        secret = os.environ.get(environment_name)
        if not secret:
            raise AdapterError(f"required environment variable is missing: {environment_name}")
        prefix = str(value.get("prefix") or "")
        output[str(name)] = prefix + secret
    return output


def pinned_feed_url(feed: Mapping[str, Any], allowed_hosts: set[str]) -> str:
    url = clean(feed.get("url"))
    if not url:
        raise AdapterError("feed.url is required for network mode")
    return official_url(url, allowed_hosts)


def request_page(
    feed: Mapping[str, Any],
    *,
    allowed_hosts: set[str],
    page: int | None,
    pagination: Mapping[str, Any],
) -> bytes:
    url = pinned_feed_url(feed, allowed_hosts)
    method = clean(feed.get("method") or "GET").upper()
    if method not in {"GET", "POST"}:
        raise AdapterError("authorized feed method must be GET or POST")
    query = feed.get("query") or {}
    body = feed.get("json_body") or {}
    if not isinstance(query, dict) or not isinstance(body, dict):
        raise AdapterError("feed query and json_body must be objects")
    query = dict(query)
    body = dict(body)
    if page is not None:
        page_parameter = clean(pagination.get("page_parameter"))
        if not page_parameter:
            raise AdapterError("page pagination requires page_parameter")
        target = clean(pagination.get("parameter_location") or "query")
        if target == "query":
            query[page_parameter] = page
        elif target == "body":
            body[page_parameter] = page
        else:
            raise AdapterError("parameter_location must be query or body")
        size_parameter = clean(pagination.get("page_size_parameter"))
        page_size = pagination.get("page_size")
        if size_parameter:
            if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 100_000:
                raise AdapterError("page_size must be a safe positive integer")
            (query if target == "query" else body)[size_parameter] = page_size
    if query:
        parsed = urllib.parse.urlparse(url)
        existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        encoded = urllib.parse.urlencode([*existing, *query.items()], doseq=True)
        url = urllib.parse.urlunparse(parsed._replace(query=encoded))
    payload = None
    headers = request_headers(feed)
    if method == "POST":
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    timeout = feed.get("timeout_seconds", 30)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 5 <= timeout <= 120:
        raise AdapterError("feed timeout_seconds must be between 5 and 120")
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        final = urllib.parse.urlparse(response.geturl()).hostname or ""
        final = final.casefold().removeprefix("www.")
        if not any(final == allowed or final.endswith(f".{allowed}") for allowed in allowed_hosts):
            raise AdapterError(f"authorized feed redirected outside pinned hosts: {final}")
        return read_limited(response)


def enumerate_feed(
    config: Mapping[str, Any],
    *,
    feed_file: Path | None,
    network: bool,
) -> tuple[list[Any], int, dict[str, Any]]:
    feed = config["feed"]
    pagination = feed["pagination"]
    pagination_type = clean(pagination.get("type")).lower()
    feed_format = clean(feed.get("format")).lower()
    items_path = pagination.get("items_path")
    total_path = pagination.get("total_path")
    if feed_file is not None:
        if network or pagination_type != "single":
            raise AdapterError("--feed-file requires single-snapshot mode and no --network")
        try:
            body = feed_file.read_bytes()
        except OSError as exc:
            raise AdapterError(f"cannot read feed file {feed_file}: {exc}") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise AdapterError("feed file exceeds the response safety limit")
        items, document = decode_items(body, feed_format=feed_format, items_path=items_path)
        declared = value_at(document, total_path, required=bool(total_path)) if total_path else len(items)
        pages = 1
        body_hashes = [hashlib.sha256(body).hexdigest()]
    else:
        if not network:
            raise AdapterError("network feed execution requires --network")
        allowed_hosts = set(config["_allowed_hosts"])
        if pagination_type == "single":
            body = request_page(feed, allowed_hosts=allowed_hosts, page=None, pagination=pagination)
            items, document = decode_items(body, feed_format=feed_format, items_path=items_path)
            declared = value_at(document, total_path, required=bool(total_path)) if total_path else len(items)
            pages = 1
            body_hashes = [hashlib.sha256(body).hexdigest()]
        else:
            start = pagination.get("page_start", 1)
            maximum = pagination.get("max_pages", 1000)
            if (
                not isinstance(start, int) or isinstance(start, bool) or start < 0
                or not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 10_000
            ):
                raise AdapterError("page_start/max_pages safety limits are invalid")
            items = []
            declared = None
            pages = 0
            body_hashes = []
            for page in range(start, start + maximum):
                body = request_page(feed, allowed_hosts=allowed_hosts, page=page, pagination=pagination)
                page_items, document = decode_items(body, feed_format=feed_format, items_path=items_path)
                body_hashes.append(hashlib.sha256(body).hexdigest())
                pages += 1
                current_total = value_at(document, total_path, required=True)
                if declared is None:
                    declared = current_total
                elif current_total != declared:
                    raise AdapterError("feed declared total changed during pagination")
                items.extend(page_items)
                if not page_items or len(items) >= int(declared):
                    break
            else:
                raise AdapterError("feed pagination reached max_pages without a terminal")
    if isinstance(declared, bool) or not isinstance(declared, int) or not 0 <= declared <= MAX_ROWS:
        raise AdapterError(f"feed declared total is invalid: {declared!r}")
    if len(items) != declared:
        raise AdapterError(f"feed completeness mismatch: declared={declared} visited={len(items)}")
    return items, declared, {"pages": pages, "response_sha256": body_hashes}


def build_research_output(
    *,
    identity: str,
    profile: Mapping[str, Any],
    config: Mapping[str, Any],
    feed_file: Path | None,
    network: bool,
) -> dict[str, Any]:
    items, declared, evidence = enumerate_feed(config, feed_file=feed_file, network=network)
    source_key = config["source_key"]
    allowed_hosts = set(config["_allowed_hosts"])
    rows = [
        normalize_item(
            item,
            mapping=config["mapping"],
            source_key=source_key,
            allowed_hosts=allowed_hosts,
        )
        for item in items
    ]
    ids = [row["id"] for row in rows]
    urls = [row["url"] for row in rows]
    if len(ids) != len(set(ids)):
        raise AdapterError("feed contains duplicate stable IDs")
    if len(urls) != len(set(urls)):
        raise AdapterError("feed contains duplicate official URLs")
    if len(rows) != declared:
        raise AdapterError("normalized rows do not reconcile to the declared feed total")
    generated = dt.datetime.now(UTC).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "lane": "source_acquisition_research",
        "research_only": True,
        "canonical_identity": identity,
        "generated_at_utc": generated,
        "row_count": len(rows),
        "rows": rows,
        "source_reports": {
            source_key: {
                "status": "ok",
                "connector_kind": "operator_authorized_feed_adapter",
                "declared": declared,
                "discovered": declared,
                "visited": len(items),
                "normalized_rows": len(rows),
                "duplicates": 0,
                "dropped": 0,
                "pages": evidence["pages"],
                "response_sha256": evidence["response_sha256"],
                "invariants": {
                    "declared_equals_visited": True,
                    "visited_equals_normalized": True,
                    "stable_ids_unique": True,
                    "official_urls_unique": True,
                    "no_silent_drops": True,
                },
            }
        },
        "profile_status": {
            "connector": (profile.get("connector") or {}).get("status"),
            "overall": profile.get("overall_status"),
        },
    }


def default_ledger_path(script_path: Path) -> Path:
    candidates = (
        script_path.parent / "source_completion_ledger.json",
        script_path.parent.parent / "source_completion_ledger.json",
        Path(os.environ.get("SONARDEALS_SOURCE_COMPLETION_LEDGER", "")),
    )
    for candidate in candidates:
        if str(candidate) and candidate.is_file():
            return candidate
    raise AdapterError("source_completion_ledger.json was not found beside the launcher")


def main_for_source(identity: str, argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=f"Fail-closed acquisition launcher for {identity}"
    )
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--feed-file", type=Path)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    ledger_path = args.ledger or default_ledger_path(Path(__file__).resolve())
    profiles = profile_index(load_json(ledger_path))
    if identity not in profiles:
        raise SystemExit(f"canonical identity is not in the authoritative ledger: {identity}")
    profile = profiles[identity]
    if args.describe or not any((args.config, args.feed_file, args.network, args.out)):
        print(json.dumps({
            "canonical_identity": identity,
            "batch": profile.get("batch"),
            "platforms": profile.get("platforms"),
            "registry_keys": profile.get("registry_keys"),
            "catalogue": profile.get("catalogue"),
            "access": profile.get("access"),
            "connector": profile.get("connector"),
            "enumeration": profile.get("enumeration"),
            "integration": profile.get("integration"),
            "overall_status": profile.get("overall_status"),
            "official_hosts": sorted(profile_hosts(profile)),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.config is None or args.out is None:
        parser.error("feed execution requires --config and --out")
    if args.feed_file is None and not args.network:
        parser.error("choose --feed-file or --network")
    if args.feed_file is not None and args.network:
        parser.error("--feed-file and --network are mutually exclusive")
    config = validate_config(load_json(args.config), identity=identity, profile=profile)
    output = build_research_output(
        identity=identity,
        profile=profile,
        config=config,
        feed_file=args.feed_file,
        network=args.network,
    )
    atomic_write_json(args.out, output)
    print(json.dumps({
        "result": "SOURCE_ACQUISITION_RESEARCH_PASS",
        "canonical_identity": identity,
        "row_count": output["row_count"],
        "output": str(args.out),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit("run a generated per-source launcher instead of this module directly")
