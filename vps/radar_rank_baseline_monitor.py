#!/usr/bin/env python3
"""Independently scheduled, network-dark monitor for an anchored rank baseline.

The command performs pointer/store checks and delegates full nested artifact
semantics to the canonical validator.  It emits a deterministic incident key
for degraded state but has no email, webhook, socket, subprocess, scheduler, or
delivery mechanism.  It remains unfit for installation until a protected LKG
watermark and durable incident lifecycle are added.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

try:
    from . import radar_rank_baseline as baseline
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import radar_rank_baseline as baseline


CONTRACT = "radar-rank-baseline-v2"
POINTER_CONTRACT = "radar-rank-baseline-latest-accepted-v2"
SCHEMA_VERSION = 2
RECOMMENDED_INTERVAL_SECONDS = 15 * 60
FUTURE_SKEW = timedelta(minutes=5)
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_POINTER_BYTES = 128 * 1024
HARD_MAX_ARTIFACTS = 64
HARD_MAX_BYTES = 2 * 1024 * 1024 * 1024
HEX_16 = re.compile(r"^[0-9a-f]{16}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_RE = re.compile(rf"^{re.escape(CONTRACT)}\.([0-9a-f]{{64}})\.json$")
POINTER_FIELDS = frozenset(
    {
        "contract", "schema_version", "generation_id", "data_generated_at_utc",
        "valid_until_utc", "artifact_file", "artifact_sha256",
        "artifact_payload_sha256", "source_board_sha256",
        "publication_manifest_sha256", "live_convergence_audit_sha256",
        "selected_fields_sha256", "ranking_contract_sha256",
        "pointer_payload_sha256",
    }
)
POINTER_TO_ARTIFACT_HASH = {
    "source_board_sha256": "source_board_sha256",
    "publication_manifest_sha256": "publication_manifest_sha256",
    "live_convergence_audit_sha256": "live_convergence_audit_sha256",
    "selected_fields_sha256": "selected_fields_sha256",
    "ranking_contract_sha256": "ranking_contract_sha256",
}
ARTIFACT_FIELDS = frozenset(
    {
        "contract", "schema_version", "algorithm", "generation_id",
        "data_generated_at_utc", "valid_until_utc", "ranking_contract",
        "source_family_contract", "proof", "hashes", "code_provenance",
        "cutoffs", "peer_stats", "published_selection",
        "artifact_payload_sha256",
    }
)


class MonitorError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _reject_constant(value: str) -> None:
    raise MonitorError("json_non_finite", f"non-finite JSON value: {value}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MonitorError("json_duplicate_key", "duplicate JSON key")
        result[key] = value
    return result


def loads_strict(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MonitorError("json_encoding", f"{label} is not UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except MonitorError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise MonitorError("json_invalid", f"{label} is not strict JSON") from exc


def parse_utc(value: Any, code: str, label: str) -> datetime:
    if not isinstance(value, str):
        raise MonitorError(code, f"{label} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MonitorError(code, f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise MonitorError(code, f"{label} is not UTC")
    return parsed.astimezone(UTC)


def _canonical_existing(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise MonitorError("path_missing", f"{label} is unavailable") from exc
    if resolved != absolute:
        raise MonitorError("path_alias", f"{label} contains a symlink or path alias")
    return resolved


def _require_directory(path: Path) -> Path:
    resolved = _canonical_existing(path, "artifact directory")
    if not stat.S_ISDIR(os.lstat(resolved).st_mode):
        raise MonitorError("artifact_directory_invalid", "artifact directory is invalid")
    return resolved


def _read_regular(path: Path, label: str, maximum: int) -> bytes:
    resolved = _canonical_existing(path, label)
    metadata = os.lstat(resolved)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise MonitorError("file_type_invalid", f"{label} is not a private regular file")
    if metadata.st_size > maximum:
        raise MonitorError("file_oversize", f"{label} exceeds its size limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
            or opened.st_nlink != 1
        ):
            raise MonitorError("file_race", f"{label} changed while opening")
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > maximum:
                raise MonitorError("file_oversize", f"{label} exceeds its size limit")
            blocks.append(block)
        completed = os.fstat(descriptor)
        if (
            completed.st_dev != opened.st_dev
            or completed.st_ino != opened.st_ino
            or completed.st_size != opened.st_size
            or completed.st_mtime_ns != opened.st_mtime_ns
            or completed.st_ctime_ns != opened.st_ctime_ns
            or completed.st_nlink != 1
            or stat.S_IMODE(completed.st_mode) != 0o600
        ):
            raise MonitorError("file_race", f"{label} changed while reading")
    finally:
        os.close(descriptor)
    return b"".join(blocks)


def _require_hash(value: Any, code: str, label: str) -> str:
    if not isinstance(value, str) or HEX_64.fullmatch(value) is None:
        raise MonitorError(code, f"{label} is invalid")
    return value


def _scan_store(directory: Path) -> dict[str, Any]:
    count = 0
    total_bytes = 0
    unknown_entries: list[str] = []
    seen_inodes: set[tuple[int, int]] = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.startswith(f"{CONTRACT}."):
                unknown_entries.append(entry.name)
                continue
            match = ARTIFACT_RE.fullmatch(entry.name)
            if match is None:
                raise MonitorError(
                    "artifact_namespace_invalid",
                    "artifact namespace contains a malformed name",
                )
            path = directory / entry.name
            resolved = _canonical_existing(path, "baseline artifact")
            metadata = os.lstat(resolved)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_ARTIFACT_BYTES
            ):
                raise MonitorError(
                    "artifact_inventory_invalid",
                    "artifact inventory contains an unsafe file",
                )
            identity = (metadata.st_dev, metadata.st_ino)
            if identity in seen_inodes:
                raise MonitorError(
                    "artifact_inventory_invalid",
                    "artifact inventory contains a duplicate inode",
                )
            seen_inodes.add(identity)
            count += 1
            total_bytes += metadata.st_size
    if unknown_entries:
        raise MonitorError(
            "artifact_store_unknown_entries",
            "artifact store contains entries outside the canonical namespace",
        )
    if count > HARD_MAX_ARTIFACTS or total_bytes > HARD_MAX_BYTES:
        raise MonitorError(
            "store_cap_exceeded",
            "artifact inventory exceeds its hard retention bounds",
        )
    return {
        "artifact_count": count,
        "artifact_bytes": total_bytes,
        "unknown_entries": sorted(unknown_entries),
    }


def _validate(
    *,
    artifact_dir: Path,
    pointer_path: Path,
    trusted_pointer_sha256: str,
    minimum_data_generated_at_utc: str,
    now: datetime,
) -> dict[str, Any]:
    directory = _require_directory(artifact_dir)
    inventory = _scan_store(directory)
    trusted = _require_hash(
        trusted_pointer_sha256, "trusted_pointer_invalid", "trusted pointer SHA-256"
    )
    pointer_raw = _read_regular(pointer_path, "latest-accepted pointer", MAX_POINTER_BYTES)
    if hashlib.sha256(pointer_raw).hexdigest() != trusted:
        raise MonitorError(
            "pointer_anchor_mismatch", "latest-accepted pointer differs from its trusted anchor"
        )
    pointer = loads_strict(pointer_raw, "latest-accepted pointer")
    if not isinstance(pointer, dict) or set(pointer) != POINTER_FIELDS:
        raise MonitorError("pointer_schema_invalid", "latest-accepted pointer fields differ")
    if pointer["contract"] != POINTER_CONTRACT or pointer["schema_version"] != SCHEMA_VERSION:
        raise MonitorError("pointer_contract_invalid", "latest-accepted pointer contract differs")
    if not isinstance(pointer["generation_id"], str) or HEX_16.fullmatch(pointer["generation_id"]) is None:
        raise MonitorError("pointer_generation_invalid", "pointer generation is invalid")
    for field in (
        "artifact_sha256", "artifact_payload_sha256", "source_board_sha256",
        "publication_manifest_sha256", "live_convergence_audit_sha256",
        "selected_fields_sha256", "ranking_contract_sha256",
        "pointer_payload_sha256",
    ):
        _require_hash(pointer[field], "pointer_hash_invalid", f"pointer {field}")
    pointer_core = {key: value for key, value in pointer.items() if key != "pointer_payload_sha256"}
    if pointer["pointer_payload_sha256"] != canonical_sha256(pointer_core):
        raise MonitorError("pointer_seal_mismatch", "latest-accepted pointer seal differs")
    artifact_sha256 = _require_hash(
        pointer["artifact_sha256"], "pointer_artifact_hash_invalid", "artifact SHA-256"
    )
    match = ARTIFACT_RE.fullmatch(pointer["artifact_file"])
    if match is None or match.group(1) != artifact_sha256:
        raise MonitorError("pointer_artifact_name_invalid", "pointed artifact name is invalid")
    minimum = parse_utc(
        minimum_data_generated_at_utc,
        "rollback_floor_invalid",
        "trusted minimum baseline timestamp",
    )
    generated = parse_utc(
        pointer["data_generated_at_utc"], "pointer_timestamp_invalid", "pointer timestamp"
    )
    valid_until = parse_utc(
        pointer["valid_until_utc"], "pointer_timestamp_invalid", "pointer validity"
    )
    if generated < minimum:
        raise MonitorError("pointer_rollback", "latest-accepted pointer predates its rollback floor")
    if valid_until <= generated:
        raise MonitorError("pointer_window_invalid", "pointer validity window is invalid")
    artifact_path = directory / pointer["artifact_file"]
    try:
        if artifact_path.parent.resolve(strict=True) != directory:
            raise MonitorError("artifact_path_escape", "pointed artifact escapes its directory")
    except OSError as exc:
        raise MonitorError("artifact_missing", "pointed artifact is unavailable") from exc
    artifact_raw = _read_regular(artifact_path, "pointed artifact", MAX_ARTIFACT_BYTES)
    if hashlib.sha256(artifact_raw).hexdigest() != artifact_sha256:
        raise MonitorError("artifact_hash_mismatch", "pointed artifact hash differs")
    artifact = loads_strict(artifact_raw, "pointed artifact")
    if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_FIELDS:
        raise MonitorError("artifact_schema_invalid", "pointed artifact fields differ")
    if artifact.get("contract") != CONTRACT or artifact.get("schema_version") != SCHEMA_VERSION:
        raise MonitorError("artifact_contract_invalid", "pointed artifact contract differs")
    if (
        not isinstance(artifact.get("generation_id"), str)
        or HEX_16.fullmatch(artifact["generation_id"]) is None
    ):
        raise MonitorError("artifact_generation_invalid", "artifact generation is invalid")
    payload_hash = _require_hash(
        artifact.get("artifact_payload_sha256"),
        "artifact_payload_hash_invalid",
        "artifact payload SHA-256",
    )
    unsigned = {key: value for key, value in artifact.items() if key != "artifact_payload_sha256"}
    if payload_hash != canonical_sha256(unsigned):
        raise MonitorError("artifact_payload_seal_mismatch", "artifact payload seal differs")
    try:
        baseline.validate_baseline_structure(artifact)
    except baseline.BaselineError as exc:
        raise MonitorError(
            "artifact_canonical_validation_failed",
            "pointed artifact fails canonical structural validation",
        ) from exc
    hashes = artifact.get("hashes")
    if not isinstance(hashes, dict):
        raise MonitorError("artifact_schema_invalid", "artifact hashes are unavailable")
    for artifact_key in POINTER_TO_ARTIFACT_HASH.values():
        _require_hash(
            hashes.get(artifact_key),
            "artifact_hash_field_invalid",
            f"artifact hash {artifact_key}",
        )
    expected = {
        "generation_id": artifact.get("generation_id"),
        "data_generated_at_utc": artifact.get("data_generated_at_utc"),
        "valid_until_utc": artifact.get("valid_until_utc"),
        "artifact_payload_sha256": payload_hash,
        **{
            pointer_key: hashes.get(artifact_key)
            for pointer_key, artifact_key in POINTER_TO_ARTIFACT_HASH.items()
        },
    }
    for key, value in expected.items():
        if pointer.get(key) != value:
            raise MonitorError("pointer_artifact_mismatch", f"pointer differs from artifact at {key}")
    artifact_generated = parse_utc(
        artifact.get("data_generated_at_utc"),
        "artifact_timestamp_invalid",
        "artifact timestamp",
    )
    artifact_valid_until = parse_utc(
        artifact.get("valid_until_utc"),
        "artifact_timestamp_invalid",
        "artifact validity",
    )
    if artifact_generated < minimum:
        raise MonitorError("artifact_rollback", "pointed artifact predates its rollback floor")
    if now < artifact_generated - FUTURE_SKEW:
        raise MonitorError("artifact_future", "pointed artifact is timestamped in the future")
    if now >= artifact_valid_until:
        raise MonitorError("artifact_expired", "pointed artifact is expired")
    return {
        "generation_id": pointer["generation_id"],
        "data_generated_at_utc": pointer["data_generated_at_utc"],
        "valid_until_utc": pointer["valid_until_utc"],
        "artifact_file": pointer["artifact_file"],
        "artifact_sha256": artifact_sha256,
        "pointer_sha256": trusted,
        **inventory,
    }


def evaluate(
    *,
    artifact_dir: Path,
    pointer_path: Path,
    trusted_pointer_sha256: str,
    minimum_data_generated_at_utc: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    evaluated = (now or datetime.now(UTC)).astimezone(UTC)
    base = {
        "contract": "radar-rank-baseline-monitor-v1",
        "schema_version": 1,
        "recommended_interval_seconds": RECOMMENDED_INTERVAL_SECONDS,
        "evaluated_at_utc": evaluated.isoformat(),
        "production_ready": False,
        "shared_exporter_lock_verified": False,
        "lkg_watermark_enforced": False,
    }
    try:
        evidence = _validate(
            artifact_dir=artifact_dir,
            pointer_path=pointer_path,
            trusted_pointer_sha256=trusted_pointer_sha256,
            minimum_data_generated_at_utc=minimum_data_generated_at_utc,
            now=evaluated,
        )
        return {**base, "result": "RADAR_RANK_BASELINE_MONITOR_V1_HEALTHY", "healthy": True, "incident": None, **evidence}
    except (MonitorError, OSError) as exc:
        code = exc.code if isinstance(exc, MonitorError) else "io_failure"
        context = {
            "trusted_pointer_sha256": hashlib.sha256(
                trusted_pointer_sha256.encode("utf-8", errors="surrogatepass")
            ).hexdigest(),
            "minimum_data_generated_at_utc": hashlib.sha256(
                minimum_data_generated_at_utc.encode(
                    "utf-8", errors="surrogatepass"
                )
            ).hexdigest(),
        }
        incident_core = {
            "contract": "radar-rank-baseline-incident-v1",
            "reason_code": code,
            "context_sha256": canonical_sha256(context),
        }
        incident = {
            **incident_core,
            "incident_key": canonical_sha256(incident_core),
            "delivery_attempted": False,
        }
        return {
            **base,
            "result": "RADAR_RANK_BASELINE_MONITOR_V1_DEGRADED",
            "healthy": False,
            "incident": incident,
            "stateful_incident_lifecycle": False,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--latest-accepted-manifest", type=Path, required=True)
    parser.add_argument("--trusted-pointer-sha256", required=True)
    parser.add_argument("--minimum-data-generated-at-utc", required=True)
    parser.add_argument("--now-utc")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        now = (
            parse_utc(args.now_utc, "now_invalid", "evaluation timestamp")
            if args.now_utc is not None
            else None
        )
        receipt = evaluate(
            artifact_dir=args.artifact_dir,
            pointer_path=args.latest_accepted_manifest,
            trusted_pointer_sha256=args.trusted_pointer_sha256,
            minimum_data_generated_at_utc=args.minimum_data_generated_at_utc,
            now=now,
        )
    except MonitorError as exc:
        print(f"RADAR_RANK_BASELINE_MONITOR_V1_FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt["healthy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
