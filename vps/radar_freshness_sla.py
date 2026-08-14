#!/usr/bin/env python3
"""Fail-closed, offline freshness assessment for one published Radar generation.

The monitor deliberately consumes four explicit local evidence files.  It does
not fetch the public site or infer freshness from file mtimes.  A caller must
first capture the decrypted public payload, URL-validation report, publication
manifest, and successful live-convergence receipt for the same generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


EXPECTED_PUBLISHED_OFFERS = 10_000
HEALTHY_BEFORE_SECONDS = 4 * 60 * 60
FALLBACK_AT_SECONDS = 5 * 60 * 60
BREACH_AT_SECONDS = 6 * 60 * 60
MAX_JSON_BYTES = 256 * 1024 * 1024
MAX_TIMESTAMP_BYTES = 128
HEX_16 = frozenset("0123456789abcdef")
HEX_64 = HEX_16

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_FALLBACK = 3
EXIT_BREACH = 4

HASH_FIELDS = {
    "candidate_ids": (
        "selection_candidate_sha256",
        "candidate_ids_sha256",
    ),
    "selected_ids": ("selected_ids_sha256", "selected_ids_sha256"),
    "candidate_fields": (
        "selection_candidate_fields_sha256",
        "candidate_fields_sha256",
    ),
    "selected_fields": (
        "selected_fields_sha256",
        "selected_fields_sha256",
    ),
    "snapshot": ("snapshot_eligible_sha256", "snapshot_eligible_sha256"),
}


class ContractError(ValueError):
    """An evidence file is absent, malformed, stale, or mutually inconsistent."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON number: {value}")


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ContractError(f"cannot stat {label}: {exc}") from exc
    if not 0 < size <= MAX_JSON_BYTES:
        raise ContractError(f"{label} size is outside the accepted bounds")
    try:
        value = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot parse {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def required_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > MAX_TIMESTAMP_BYTES
    ):
        raise ContractError(f"{label} must be a bounded nonempty string")
    return value


def required_hex(value: Any, length: int, label: str) -> str:
    text = required_text(value, label)
    alphabet = HEX_16 if length == 16 else HEX_64
    if len(text) != length or any(character not in alphabet for character in text):
        raise ContractError(f"{label} must be {length} lowercase hexadecimal characters")
    return text


def parse_timestamp(value: Any, label: str) -> tuple[str, datetime]:
    raw = required_text(value, label)
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise ContractError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{label} must include a UTC offset")
    normalized = parsed.astimezone(timezone.utc)
    return raw, normalized


def canonical_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def require_schema(value: dict[str, Any], expected: int, label: str) -> None:
    if type(value.get("schema_version")) is not int or value["schema_version"] != expected:
        raise ContractError(f"{label} schema_version must be {expected}")


def require_exact_count(value: dict[str, Any], label: str) -> None:
    if "published_offer_count" not in value:
        raise ContractError(f"{label}.published_offer_count is required")
    for field in ("published_offer_count", "verified_live_count", "count"):
        if field not in value:
            continue
        count = value[field]
        if type(count) is not int or count != EXPECTED_PUBLISHED_OFFERS:
            raise ContractError(
                f"{label}.{field} must equal {EXPECTED_PUBLISHED_OFFERS}"
            )
    if "offers" in value:
        offers = value["offers"]
        if not isinstance(offers, list) or len(offers) != EXPECTED_PUBLISHED_OFFERS:
            raise ContractError(
                f"{label}.offers must contain exactly {EXPECTED_PUBLISHED_OFFERS} entries"
            )


def require_optional_exact_counts(value: dict[str, Any], label: str) -> None:
    for field in ("published_offer_count", "verified_live_count", "count"):
        if field in value and (
            type(value[field]) is not int
            or value[field] != EXPECTED_PUBLISHED_OFFERS
        ):
            raise ContractError(
                f"{label}.{field} must equal {EXPECTED_PUBLISHED_OFFERS}"
            )


def classify_age(age_seconds: int) -> str:
    if age_seconds >= BREACH_AT_SECONDS:
        return "breach"
    if age_seconds >= FALLBACK_AT_SECONDS:
        return "fallback"
    if age_seconds >= HEALTHY_BEFORE_SECONDS:
        return "warn"
    return "healthy"


def age_report(timestamp: datetime, now: datetime) -> dict[str, Any]:
    seconds = int((now - timestamp).total_seconds())
    if seconds < 0:
        raise ContractError("evidence timestamp is in the future")
    return {
        "timestamp_utc": canonical_timestamp(timestamp),
        "age_seconds": seconds,
        "age_hours": round(seconds / 3600, 6),
        "status": classify_age(seconds),
    }


def _artifact_hashes(
    value: dict[str, Any], label: str, *, payload_names: bool
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    index = 0 if payload_names else 1
    for logical_name, names in HASH_FIELDS.items():
        field = names[index]
        hashes[logical_name] = required_hex(value.get(field), 64, f"{label}.{field}")
    return hashes


def evaluate(
    *,
    payload: dict[str, Any],
    validation: dict[str, Any],
    publication: dict[str, Any],
    convergence: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ContractError("now must include a UTC offset")
    now = now.astimezone(timezone.utc)

    require_schema(payload, 2, "payload")
    require_schema(validation, 1, "validation")
    require_schema(publication, 1, "publication")
    require_exact_count(payload, "payload")
    require_exact_count(publication, "publication")
    require_exact_count(convergence, "convergence")
    require_optional_exact_counts(validation, "validation")

    if convergence.get("result") != "LIVE_GENERATION_AUDIT_PASS":
        raise ContractError("convergence did not record LIVE_GENERATION_AUDIT_PASS")

    generation = required_hex(payload.get("generation_id"), 16, "payload.generation_id")
    for label, value in (("publication", publication), ("convergence", convergence)):
        if required_hex(value.get("generation_id"), 16, f"{label}.generation_id") != generation:
            raise ContractError(f"{label} generation does not match the public payload")
    if "generation_id" in validation and validation["generation_id"] != generation:
        raise ContractError("validation generation does not match the public payload")

    algorithm = required_text(payload.get("selection_algorithm"), "payload.selection_algorithm")
    if payload.get("algorithm") != algorithm:
        raise ContractError("payload algorithm fields disagree")
    for label, value in (("publication", publication), ("convergence", convergence)):
        if value.get("algorithm") != algorithm:
            raise ContractError(f"{label} algorithm does not match the public payload")
    if validation.get("input_algorithm") != algorithm:
        raise ContractError("validation algorithm does not match the public payload")

    payload_hashes = _artifact_hashes(payload, "payload", payload_names=True)
    publication_hashes = _artifact_hashes(
        publication, "publication", payload_names=False
    )
    convergence_hashes = _artifact_hashes(
        convergence, "convergence", payload_names=False
    )
    if publication_hashes != payload_hashes or convergence_hashes != payload_hashes:
        raise ContractError("generation hashes do not converge across evidence")

    offer_fields_hash = required_hex(
        payload.get("offer_fields_sha256"), 64, "payload.offer_fields_sha256"
    )
    if required_hex(
        validation.get("input_offer_fields_sha256"),
        64,
        "validation.input_offer_fields_sha256",
    ) != offer_fields_hash:
        raise ContractError("validation offer hash does not match the public payload")
    if required_hex(
        validation.get("input_snapshot_sha256"),
        64,
        "validation.input_snapshot_sha256",
    ) != payload_hashes["snapshot"]:
        raise ContractError("validation snapshot hash does not match the public payload")

    data_raw, data_at = parse_timestamp(
        payload.get("data_generated_at_utc"), "payload.data_generated_at_utc"
    )
    payload_published_raw, payload_published_at = parse_timestamp(
        payload.get("published_at_utc"), "payload.published_at_utc"
    )
    validation_input_raw, _ = parse_timestamp(
        validation.get("input_updated_at"), "validation.input_updated_at"
    )
    _, validation_at = parse_timestamp(
        validation.get("generated_at"), "validation.generated_at"
    )
    publication_data_raw, _ = parse_timestamp(
        publication.get("data_generated_at_utc"),
        "publication.data_generated_at_utc",
    )
    _, publication_at = parse_timestamp(
        publication.get("prepared_at"), "publication.prepared_at"
    )
    convergence_data_raw, _ = parse_timestamp(
        convergence.get("data_generated_at_utc"),
        "convergence.data_generated_at_utc",
    )
    if not (
        validation_input_raw
        == publication_data_raw
        == convergence_data_raw
        == data_raw
    ):
        raise ContractError("data generation timestamps do not converge")
    if publication_at != payload_published_at:
        raise ContractError("publication timestamps do not converge")
    if "universe_last_seen_at" in payload:
        universe_raw, _ = parse_timestamp(
            payload["universe_last_seen_at"], "payload.universe_last_seen_at"
        )
        if universe_raw != data_raw:
            raise ContractError("payload universe observation does not match generation time")
    if not data_at <= validation_at <= publication_at <= now:
        raise ContractError("evidence timestamps violate data/validation/publication order")

    bound_generation = hashlib.sha256(
        (
            f"{algorithm}\n{data_raw}\n"
            f"{payload_hashes['candidate_fields']}\n"
            f"{payload_hashes['selected_fields']}\n"
        ).encode("utf-8")
    ).hexdigest()[:16]
    if bound_generation != generation:
        raise ContractError("generation_id is not bound to its algorithm, time, and hashes")

    ages = {
        "data_observed": age_report(data_at, now),
        "validation": age_report(validation_at, now),
        "publication": age_report(publication_at, now),
    }
    severity = {"healthy": 0, "warn": 1, "fallback": 2, "breach": 3}
    status = max(
        (entry["status"] for entry in ages.values()), key=severity.__getitem__
    )
    return {
        "schema_version": 1,
        "result": f"RADAR_FRESHNESS_SLA_{status.upper()}",
        "status": status,
        "now_utc": canonical_timestamp(now),
        "generation_id": generation,
        "published_offer_count": EXPECTED_PUBLISHED_OFFERS,
        "thresholds_hours": {
            "warn_at": 4,
            "fallback_at": 5,
            "breach_at": 6,
        },
        "ages": ages,
        "checks": {
            "exact_published_offer_count": True,
            "generation_bound": True,
            "generation_converged": True,
            "hashes_converged": True,
            "live_convergence_passed": True,
            "timestamps_ordered": True,
        },
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--convergence", type=Path, required=True)
    parser.add_argument(
        "--now",
        help="timezone-aware ISO-8601 assessment time (defaults to the current UTC time)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    now_raw = args.now or datetime.now(timezone.utc).isoformat()
    try:
        _, now = parse_timestamp(now_raw, "now")
        report = evaluate(
            payload=load_json_object(args.payload, "payload"),
            validation=load_json_object(args.validation, "validation"),
            publication=load_json_object(args.publication, "publication"),
            convergence=load_json_object(args.convergence, "convergence"),
            now=now,
        )
    except (ContractError, OSError, TypeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "result": "RADAR_FRESHNESS_SLA_INVALID",
            "status": "invalid",
            "error": str(exc),
        }
        exit_code = EXIT_INVALID
    else:
        exit_code = {
            "healthy": EXIT_OK,
            "warn": EXIT_OK,
            "fallback": EXIT_FALLBACK,
            "breach": EXIT_BREACH,
        }[report["status"]]
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
