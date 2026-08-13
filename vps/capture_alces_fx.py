#!/usr/bin/env python3
"""Capture and seal the ALCES EUR external-trade sell rate.

ALCES currently serves its public API from multiple replicas.  Their JSON can
differ byte-for-byte (for example, ``EURO`` versus ``Euro``) while carrying the
same rate.  This program preserves every raw response, canonicalises only the
rate-bearing fields, and publishes a config only after at least two independent
HTTPS reads agree semantically.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unicodedata
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlparse


SCHEMA_VERSION = 1
SOURCE_NAME = "ALCES External Portal"
SOURCE_URL = "https://alces.douane.gov.dz/api/public/com/main/selectFxrtList"
SOURCE_HOST = "alces.douane.gov.dz"
RATE_KIND = "external_trade_sell"
BASE_CURRENCY = "EUR"
DISPLAY_CURRENCY = "DZD"
RATE_SCALE = 100_000
RATE_QUANTUM = Decimal("0.00001")
MIN_RATE = Decimal("50")
MAX_RATE = Decimal("500")
MIN_READS = 2
MAX_READS = 8
MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_CONFIG_AGE = timedelta(hours=6)
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_EFFECTIVE_DATE_AGE = timedelta(days=10)
EXPECTED_INTERMEDIATE_PEM_SHA256 = (
    "b40358316fd9e1caaaab92c382927d2c13ef51b481eec9371a82e16c86f2a687"
)
DEFAULT_STATE = Path("/var/lib/sonardeals-radar")
DEFAULT_CONFIG = DEFAULT_STATE / "fx" / "display_currency.json"
DEFAULT_INTERMEDIATE = (
    Path(__file__).resolve().parent
    / "certs"
    / "sectigo-public-server-authentication-ca-dv-r36.pem"
)
DEFAULT_CA_PATH = Path("/etc/ssl/certs")
HEX_64 = re.compile(r"[0-9a-f]{64}")
RATE_TEXT = re.compile(r"(?:[1-9][0-9]{1,2})\.[0-9]{5}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return (
        parsed.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parsed_timestamp(value: Any) -> datetime | None:
    canonical = canonical_timestamp(value)
    return datetime.fromisoformat(canonical.replace("Z", "+00:00")) if canonical else None


def decimal_field(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise ValueError(f"ALCES {field} is not a JSON number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"ALCES {field} is invalid") from exc
    if not number.is_finite() or number != number.quantize(RATE_QUANTUM):
        raise ValueError(f"ALCES {field} has unsupported precision")
    if not MIN_RATE <= number <= MAX_RATE:
        raise ValueError(f"ALCES {field} is outside the safety range")
    return number.quantize(RATE_QUANTUM)


def normalized_name(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def parse_alces_response(raw: bytes) -> dict[str, Any]:
    """Return the canonical EUR row carried by one raw ALCES response."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("ALCES response size is invalid")
    try:
        document = json.loads(
            raw.decode("utf-8"), parse_float=Decimal, parse_int=Decimal
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("ALCES response is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("resultList"), list):
        raise ValueError("ALCES response has no resultList")
    rows = [
        row
        for row in document["resultList"]
        if isinstance(row, dict)
        and str(row.get("currCd") or "").strip().casefold() == "eur"
    ]
    if len(rows) != 1:
        raise ValueError("ALCES response must contain exactly one EUR row")
    row = rows[0]
    if normalized_name(row.get("currNm")) != "euro":
        raise ValueError("ALCES EUR row has an unexpected currency name")
    try:
        effective = datetime.strptime(str(row.get("aplyBgnDt") or ""), "%d/%m/%Y").date()
    except ValueError as exc:
        raise ValueError("ALCES EUR effective date is invalid") from exc
    buy = decimal_field(row.get("buyAmt"), "buyAmt")
    sell = decimal_field(row.get("selAmt"), "selAmt")
    if sell < buy:
        raise ValueError("ALCES EUR sell rate is below its buy rate")
    return {
        "base_currency": BASE_CURRENCY,
        "display_currency": DISPLAY_CURRENCY,
        "effective_date": effective.isoformat(),
        "buy_rate": format(buy, "f"),
        "sell_rate": format(sell, "f"),
        "rate_scale": RATE_SCALE,
        "sell_rate_scaled": int(sell * RATE_SCALE),
    }


def validate_effective_date(semantic: dict[str, Any], captured_at: datetime) -> None:
    try:
        effective = date.fromisoformat(str(semantic["effective_date"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("ALCES effective date is invalid") from exc
    captured_date = captured_at.astimezone(UTC).date()
    if effective > captured_date + timedelta(days=1):
        raise ValueError("ALCES effective date is in the future")
    if captured_date - effective > MAX_EFFECTIVE_DATE_AGE:
        raise ValueError("ALCES effective date is stale")


def resolve_public_addresses(host: str = SOURCE_HOST) -> list[str]:
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    }
    public = sorted(address for address in addresses if ipaddress.ip_address(address).is_global)
    if not public:
        raise RuntimeError("ALCES DNS returned no public IPv4 address")
    return public


def validate_tls_material(intermediate: Path, ca_path: Path) -> None:
    if not intermediate.is_file():
        raise RuntimeError(f"ALCES intermediate certificate is unavailable: {intermediate}")
    if sha256_bytes(intermediate.read_bytes()) != EXPECTED_INTERMEDIATE_PEM_SHA256:
        raise RuntimeError("ALCES intermediate certificate does not match its pinned hash")
    if not ca_path.is_dir():
        raise RuntimeError(f"system CA directory is unavailable: {ca_path}")


def fetch_alces(address: str, intermediate: Path, ca_path: Path) -> bytes:
    """Fetch one replica through curl with hostname and chain verification enabled."""
    parsed = urlparse(SOURCE_URL)
    if parsed.scheme != "https" or parsed.hostname != SOURCE_HOST or parsed.port is not None:
        raise RuntimeError("ALCES source URL safety invariant failed")
    ipaddress.ip_address(address)
    curl = shutil.which("curl")
    if curl is None:
        raise RuntimeError("curl is required for pinned-replica ALCES capture")
    result = subprocess.run(
        [
            curl,
            "--proto", "=https",
            "--proto-redir", "=https",
            "--tlsv1.2",
            "--fail",
            "--silent",
            "--show-error",
            "--connect-timeout", "10",
            "--max-time", "30",
            "--max-filesize", str(MAX_RESPONSE_BYTES),
            "--retry", "1",
            "--retry-delay", "1",
            "--retry-connrefused",
            "--cacert", str(intermediate),
            "--capath", str(ca_path),
            "--resolve", f"{SOURCE_HOST}:443:{address}",
            "--header", "Accept: application/json",
            "--header", "Cache-Control: no-cache, no-store, max-age=0",
            "--header", "Pragma: no-cache",
            SOURCE_URL,
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"verified ALCES fetch failed for {address}: {detail}")
    if not result.stdout or len(result.stdout) > MAX_RESPONSE_BYTES:
        raise RuntimeError("ALCES response size is invalid")
    return result.stdout


def build_config(
    *,
    captured_at: datetime,
    observations: Iterable[tuple[str, bytes, str]],
) -> dict[str, Any]:
    """Build a hash-sealed config from ``(replica, raw, evidence_file)`` reads."""
    if captured_at.tzinfo is None:
        raise ValueError("capture timestamp must be timezone-aware")
    captured_at = captured_at.astimezone(UTC).replace(microsecond=0)
    entries: list[dict[str, Any]] = []
    semantics: list[dict[str, Any]] = []
    for replica, raw, evidence_file in observations:
        try:
            ipaddress.ip_address(replica)
        except ValueError as exc:
            raise ValueError("evidence replica is not an IP address") from exc
        semantics.append(parse_alces_response(raw))
        entries.append(
            {
                "replica": replica,
                "evidence_file": evidence_file,
                "bytes": len(raw),
                "raw_sha256": sha256_bytes(raw),
            }
        )
    if not MIN_READS <= len(entries) <= MAX_READS:
        raise ValueError(f"ALCES capture requires {MIN_READS} to {MAX_READS} reads")
    semantic = semantics[0]
    if any(item != semantic for item in semantics[1:]):
        raise ValueError("ALCES replicas do not agree on the EUR trade rate")
    validate_effective_date(semantic, captured_at)
    core = {
        "schema_version": SCHEMA_VERSION,
        "captured_at_utc": captured_at.isoformat().replace("+00:00", "Z"),
        "source": {
            "name": SOURCE_NAME,
            "url": SOURCE_URL,
            "rate_kind": RATE_KIND,
        },
        "semantic": semantic,
        "semantic_sha256": canonical_json_sha256(semantic),
        "read_count": len(entries),
        "evidence": entries,
    }
    return {
        **core,
        "seal": {
            "algorithm": "sha256",
            "value": canonical_json_sha256(core),
        },
    }


def safe_evidence_path(config_path: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or len(relative) > 512:
        raise ValueError("evidence path is invalid")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("evidence path escapes the FX state directory")
    base = config_path.parent.resolve()
    candidate = (base / Path(*path.parts)).resolve(strict=True)
    if candidate == base or base not in candidate.parents or not candidate.is_file():
        raise ValueError("evidence path escapes the FX state directory")
    return candidate


def load_sealed_config(
    config_path: Path,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_CONFIG_AGE,
) -> dict[str, Any]:
    """Validate a fresh config and all raw evidence, then return public metadata."""
    if max_age <= timedelta(0) or max_age > timedelta(days=7):
        raise ValueError("display-currency config freshness bound is invalid")
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("display-currency config is unreadable") from exc
    required = {
        "schema_version", "captured_at_utc", "source", "semantic",
        "semantic_sha256", "read_count", "evidence", "seal",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("display-currency config fields are invalid")
    seal = value.get("seal")
    core = {key: item for key, item in value.items() if key != "seal"}
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or not isinstance(seal, dict)
        or set(seal) != {"algorithm", "value"}
        or seal.get("algorithm") != "sha256"
        or not isinstance(seal.get("value"), str)
        or not HEX_64.fullmatch(seal["value"])
        or seal["value"] != canonical_json_sha256(core)
    ):
        raise ValueError("display-currency config seal is invalid")
    captured_text = canonical_timestamp(value.get("captured_at_utc"))
    captured = parsed_timestamp(value.get("captured_at_utc"))
    if captured is None or captured_text != value.get("captured_at_utc"):
        raise ValueError("display-currency capture timestamp is invalid")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    age = current - captured
    if age < -FUTURE_TOLERANCE:
        raise ValueError("display-currency config is timestamped in the future")
    if age > max_age:
        raise ValueError("display-currency config is stale")
    if value.get("source") != {
        "name": SOURCE_NAME,
        "url": SOURCE_URL,
        "rate_kind": RATE_KIND,
    }:
        raise ValueError("display-currency source is invalid")
    semantic = value.get("semantic")
    semantic_fields = {
        "base_currency", "display_currency", "effective_date", "buy_rate",
        "sell_rate", "rate_scale", "sell_rate_scaled",
    }
    if not isinstance(semantic, dict) or set(semantic) != semantic_fields:
        raise ValueError("display-currency semantic fields are invalid")
    if (
        semantic.get("base_currency") != BASE_CURRENCY
        or semantic.get("display_currency") != DISPLAY_CURRENCY
        or semantic.get("rate_scale") != RATE_SCALE
        or not isinstance(semantic.get("buy_rate"), str)
        or not isinstance(semantic.get("sell_rate"), str)
        or not RATE_TEXT.fullmatch(semantic["buy_rate"])
        or not RATE_TEXT.fullmatch(semantic["sell_rate"])
        or type(semantic.get("sell_rate_scaled")) is not int
    ):
        raise ValueError("display-currency semantic values are invalid")
    buy = decimal_field(Decimal(semantic["buy_rate"]), "buy_rate")
    sell = decimal_field(Decimal(semantic["sell_rate"]), "sell_rate")
    if sell < buy or semantic["sell_rate_scaled"] != int(sell * RATE_SCALE):
        raise ValueError("display-currency rate scaling is invalid")
    validate_effective_date(semantic, captured)
    semantic_hash = value.get("semantic_sha256")
    if (
        not isinstance(semantic_hash, str)
        or not HEX_64.fullmatch(semantic_hash)
        or semantic_hash != canonical_json_sha256(semantic)
    ):
        raise ValueError("display-currency semantic digest is invalid")
    evidence = value.get("evidence")
    if (
        type(value.get("read_count")) is not int
        or not isinstance(evidence, list)
        or value["read_count"] != len(evidence)
        or not MIN_READS <= len(evidence) <= MAX_READS
    ):
        raise ValueError("display-currency evidence count is invalid")
    evidence_paths: set[Path] = set()
    for entry in evidence:
        if not isinstance(entry, dict) or set(entry) != {
            "replica", "evidence_file", "bytes", "raw_sha256"
        }:
            raise ValueError("display-currency evidence fields are invalid")
        try:
            ipaddress.ip_address(entry.get("replica"))
        except ValueError as exc:
            raise ValueError("display-currency evidence replica is invalid") from exc
        if (
            type(entry.get("bytes")) is not int
            or not 0 < entry["bytes"] <= MAX_RESPONSE_BYTES
            or not isinstance(entry.get("raw_sha256"), str)
            or not HEX_64.fullmatch(entry["raw_sha256"])
        ):
            raise ValueError("display-currency evidence receipt is invalid")
        path = safe_evidence_path(config_path, entry.get("evidence_file"))
        if path in evidence_paths:
            raise ValueError("display-currency evidence path is duplicated")
        evidence_paths.add(path)
        raw = path.read_bytes()
        if len(raw) != entry["bytes"] or sha256_bytes(raw) != entry["raw_sha256"]:
            raise ValueError("display-currency raw evidence digest is invalid")
        if parse_alces_response(raw) != semantic:
            raise ValueError("display-currency raw evidence is semantically inconsistent")
    return {
        "code": DISPLAY_CURRENCY,
        "base_code": BASE_CURRENCY,
        "rate_kind": RATE_KIND,
        "rate": semantic["sell_rate"],
        "rate_scale": RATE_SCALE,
        "rate_scaled": semantic["sell_rate_scaled"],
        "effective_date": semantic["effective_date"],
        "captured_at_utc": captured_text,
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "semantic_sha256": semantic_hash,
        "config_seal_sha256": seal["value"],
        "evidence_read_count": len(evidence),
        "display_only": True,
        "underlying_comparison_currency": BASE_CURRENCY,
    }


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
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


def capture(args: argparse.Namespace) -> dict[str, Any]:
    validate_tls_material(args.intermediate, args.ca_path)
    if not MIN_READS <= args.reads <= MAX_READS:
        raise ValueError(f"--reads must be between {MIN_READS} and {MAX_READS}")
    addresses = resolve_public_addresses()
    captured_at = datetime.now(UTC).replace(microsecond=0)
    raw_reads: list[tuple[str, bytes]] = []
    failures: list[str] = []
    # ALCES advertises multiple public replicas, but a replica can temporarily
    # reject an otherwise identical hostname-verified request.  Keep the
    # capture fail-closed while allowing another HTTPS read to provide the
    # requested agreement evidence; never publish a partial read set.
    for index in range(args.reads * len(addresses)):
        address = addresses[index % len(addresses)]
        try:
            raw = fetch_alces(address, args.intermediate, args.ca_path)
        except RuntimeError as exc:
            failures.append(f"{address}:{type(exc).__name__}")
            continue
        raw_reads.append((address, raw))
        if len(raw_reads) == args.reads:
            break
    if len(raw_reads) != args.reads:
        raise RuntimeError(
            "ALCES capture could not obtain the required verified reads; "
            f"successful={len(raw_reads)} required={args.reads} "
            f"failures={','.join(failures)}"
        )
    semantics = [parse_alces_response(raw) for _, raw in raw_reads]
    if any(item != semantics[0] for item in semantics[1:]):
        raise RuntimeError("ALCES replicas do not agree on the EUR trade rate")
    validate_effective_date(semantics[0], captured_at)
    semantic_hash = canonical_json_sha256(semantics[0])
    capture_id = captured_at.strftime("%Y%m%dT%H%M%SZ") + "-" + semantic_hash[:12]
    fx_root = args.config.parent
    evidence_root = fx_root / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=evidence_root, prefix=".capture-"))
    final = evidence_root / capture_id
    try:
        observations: list[tuple[str, bytes, str]] = []
        for index, (address, raw) in enumerate(raw_reads, start=1):
            name = f"read-{index:02d}.json"
            atomic_write(stage / name, raw, 0o600)
            observations.append((address, raw, f"evidence/{capture_id}/{name}"))
        if final.exists():
            raise RuntimeError(f"ALCES evidence capture already exists: {capture_id}")
        os.replace(stage, final)
        config = build_config(captured_at=captured_at, observations=observations)
        atomic_write(
            args.config,
            json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
            0o600,
        )
        public = load_sealed_config(args.config, now=captured_at)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {
        "result": "ALCES_FX_CAPTURE_PASS",
        "config": str(args.config),
        "captured_at_utc": public["captured_at_utc"],
        "effective_date": public["effective_date"],
        "rate": public["rate"],
        "read_count": public["evidence_read_count"],
        "semantic_sha256": public["semantic_sha256"],
        "config_seal_sha256": public["config_seal_sha256"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--intermediate", type=Path, default=DEFAULT_INTERMEDIATE)
    parser.add_argument("--ca-path", type=Path, default=DEFAULT_CA_PATH)
    parser.add_argument("--reads", type=int, default=MIN_READS)
    return parser.parse_args()


def main() -> int:
    report = capture(parse_args())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
