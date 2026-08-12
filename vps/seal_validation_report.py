#!/usr/bin/env python3
"""Bind a complete URL-validation report to one observed-value v7 board."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit


ALGORITHM = "schengen-observed-peer-value-v7-live-verified"
READY_MARKER = f"VALIDATION_SEALER_READY algorithm={ALGORITHM}"
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_URL_BYTES = 2_048
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ContractError(ValueError):
    """Raised when an input does not satisfy the sealing contract."""


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
        raise ContractError(f"cannot stat {label}: {path}: {exc}") from exc
    if size > MAX_JSON_BYTES:
        raise ContractError(f"{label} exceeds {MAX_JSON_BYTES} bytes")
    try:
        raw = path.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot parse {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def safe_https_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        if len(value.encode("utf-8")) > MAX_URL_BYTES:
            return False
    except UnicodeError:
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return False
    if any(character in value for character in "\\\\<>\"'"):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            return False
        parsed.hostname.encode("idna").decode("ascii")
        parsed.port
    except (UnicodeError, ValueError):
        return False
    return True


def offer_url(offer: dict[str, Any], position: int) -> str:
    compact = offer.get("u")
    verbose = offer.get("url")
    if compact is not None and verbose is not None and compact != verbose:
        raise ContractError(f"board offer {position} has conflicting u and url fields")
    value = compact if compact is not None else verbose
    if not safe_https_url(value):
        raise ContractError(f"board offer {position} has an unsafe HTTPS URL")
    return value


def canonical_offer_fields_sha256(offers: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    try:
        for raw_offer in offers:
            # Verification is an output of this report, not part of its input
            # identity.  Normalize it so retrying the same snapshot cannot
            # bind a newly sealed report to stale, nonzero verdicts from the
            # preceding board build.
            offer = {**raw_offer, "v": 0} if "v" in raw_offer else raw_offer
            digest.update(
                json.dumps(
                    offer,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            digest.update(b"\n")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError(f"board offers are not canonical JSON: {exc}") from exc
    return digest.hexdigest()


def validate_contract(
    board: dict[str, Any], validation: dict[str, Any]
) -> tuple[str, str]:
    if type(board.get("schema_version")) is not int or board["schema_version"] != 2:
        raise ContractError("board schema_version must be 2")
    if board.get("algorithm") != ALGORITHM:
        raise ContractError(f"board algorithm must be {ALGORITHM}")

    timestamp = board.get("data_generated_at_utc")
    if not isinstance(timestamp, str) or not timestamp:
        raise ContractError("board data_generated_at_utc must be a nonempty string")

    snapshot = board.get("snapshot_eligible_sha256")
    if not isinstance(snapshot, str) or SHA256_PATTERN.fullmatch(snapshot) is None:
        raise ContractError("board snapshot_eligible_sha256 must be 64 hexadecimal characters")

    offers = board.get("offers")
    if not isinstance(offers, list) or not offers:
        raise ContractError("board offers must be a nonempty array")
    urls: list[str] = []
    for position, offer in enumerate(offers):
        if not isinstance(offer, dict):
            raise ContractError(f"board offer {position} must be an object")
        urls.append(offer_url(offer, position))
    if len(set(urls)) != len(urls):
        raise ContractError("board offer URLs must be unique")

    if type(validation.get("schema_version")) is not int or validation["schema_version"] != 1:
        raise ContractError("validation schema_version must be 1")
    if validation.get("input_updated_at") != timestamp:
        raise ContractError("validation input_updated_at does not match board data_generated_at_utc")

    results = validation.get("results")
    if not isinstance(results, list):
        raise ContractError("validation results must be an array")
    checked = validation.get("checked")
    if type(checked) is not int or checked != len(urls):
        raise ContractError("validation checked must equal the board offer count")
    if len(results) != len(urls):
        raise ContractError("validation results must contain one entry per board URL")

    result_urls: list[str] = []
    for position, result in enumerate(results):
        if not isinstance(result, dict):
            raise ContractError(f"validation result {position} must be an object")
        url = result.get("url")
        if not isinstance(url, str):
            raise ContractError(f"validation result {position} has no string URL")
        result_urls.append(url)
    if len(set(result_urls)) != len(result_urls):
        raise ContractError("validation result URLs must be unique")
    if set(result_urls) != set(urls):
        missing = len(set(urls) - set(result_urls))
        extra = len(set(result_urls) - set(urls))
        raise ContractError(
            f"validation URLs do not exactly cover the board (missing={missing}, extra={extra})"
        )

    return snapshot, canonical_offer_fields_sha256(offers)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError(f"validation cannot be encoded as JSON: {exc}") from exc

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
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def seal(board_path: Path, validation_path: Path) -> dict[str, Any]:
    board = load_json_object(board_path, "board")
    validation = load_json_object(validation_path, "validation")
    snapshot, offers_digest = validate_contract(board, validation)
    validation["input_algorithm"] = ALGORITHM
    validation["input_snapshot_sha256"] = snapshot
    validation["input_offer_fields_sha256"] = offers_digest
    atomic_write_json(validation_path, validation)
    return validation


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=Path)
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--capability-check", action="store_true")
    args = parser.parse_args(argv)
    if not args.capability_check and (args.board is None or args.validation is None):
        parser.error("--board and --validation are required unless --capability-check is used")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.capability_check:
        print(READY_MARKER)
        return 0
    try:
        seal(args.board, args.validation)
    except (ContractError, OSError) as exc:
        print(f"VALIDATION_SEAL_FAILED: {exc}", file=sys.stderr)
        return 2
    print("VALIDATION_SEAL_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
