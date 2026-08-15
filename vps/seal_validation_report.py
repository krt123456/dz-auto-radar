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


def normalized_host(value: Any) -> str:
    try:
        host = (urlsplit(str(value or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def browser_evidence_expected(result: dict[str, Any]) -> bool:
    """Mirror validator browser eligibility after merging attempted results."""
    if "direct_reason" in result:
        return True
    paruvendu_protection_redirect = (
        result.get("reason") == "protection_redirect"
        and normalized_host(result.get("url")) == "paruvendu.fr"
        and normalized_host(result.get("final_url")) == "paruvendu.fr"
    )
    return (
        result.get("status") == "unknown"
        and str(result.get("url") or "").startswith("http")
        and not paruvendu_protection_redirect
    )


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


def strict_rank_list(value: Any, *, label: str, maximum: int) -> list[int]:
    if (
        not isinstance(value, list)
        or any(type(rank) is not int or not (1 <= rank <= maximum) for rank in value)
        or value != sorted(set(value))
    ):
        raise ContractError(f"{label} must be unique ordered board ranks")
    return value


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
        if (
            type(result.get("board_rank")) is not int
            or result["board_rank"] != position + 1
            or url != urls[position]
            or (
                "direct_reason" in result
                and (
                    not isinstance(result["direct_reason"], str)
                    or not url.startswith("http")
                )
            )
        ):
            raise ContractError(
                f"validation result {position} does not match its board rank"
            )
        result_urls.append(url)
    if len(set(result_urls)) != len(result_urls):
        raise ContractError("validation result URLs must be unique")
    if set(result_urls) != set(urls):
        missing = len(set(urls) - set(result_urls))
        extra = len(set(result_urls) - set(urls))
        raise ContractError(
            f"validation URLs do not exactly cover the board (missing={missing}, extra={extra})"
        )

    counts = validation.get("counts")
    statuses = ("verified", "dead", "unknown")
    if (
        not isinstance(counts, dict)
        or set(counts) != set(statuses)
        or any(type(counts.get(status)) is not int or counts[status] < 0 for status in statuses)
        or sum(counts.values()) != len(results)
        or any(
            counts[status] != sum(result.get("status") == status for result in results)
            for status in statuses
        )
    ):
        raise ContractError("validation status counts do not match results")
    target = validation.get("verified_target")
    target_reached = validation.get("target_reached")
    pool_exhausted = validation.get("pool_exhausted")
    ranked_candidate_count = validation.get("ranked_candidate_count")
    ranked_universe_exhausted = validation.get("ranked_universe_exhausted")
    full_input_coverage = validation.get("full_input_coverage")
    direct_attempted = validation.get("direct_attempted_count")
    browser_targets = validation.get("browser_target_count")
    browser_attempted = validation.get("browser_attempted_count")
    target_ranks = strict_rank_list(
        validation.get("browser_target_ranks"),
        label="browser_target_ranks",
        maximum=len(results),
    )
    attempted_ranks = strict_rank_list(
        validation.get("browser_attempted_ranks"),
        label="browser_attempted_ranks",
        maximum=len(results),
    )
    frontier_rank = validation.get("selection_frontier_rank")
    frontier_targets = validation.get("browser_frontier_target_count")
    frontier_attempted = validation.get("browser_frontier_attempted_count")
    frontier_complete = validation.get("browser_frontier_complete")
    target_rank_set = set(target_ranks)
    attempted_rank_set = set(attempted_ranks)
    result_attempted_ranks = {
        position
        for position, result in enumerate(results, start=1)
        if "direct_reason" in result
    }
    evidenced_target_ranks = [
        position
        for position, result in enumerate(results, start=1)
        if browser_evidence_expected(result)
    ]
    expected_target_ranks = (
        evidenced_target_ranks[:browser_targets]
        if type(browser_targets) is int and browser_targets >= 0
        else []
    )
    verified_ranks = [
        position
        for position, result in enumerate(results, start=1)
        if result.get("status") == "verified"
    ]
    expected_frontier = (
        verified_ranks[target - 1]
        if type(target) is int and target > 0 and len(verified_ranks) >= target
        else None
    )
    expected_frontier_target_ranks = (
        [rank for rank in target_ranks if rank <= expected_frontier]
        if expected_frontier is not None
        else []
    )
    expected_frontier_attempted = sum(
        rank in attempted_rank_set for rank in expected_frontier_target_ranks
    )
    expected_frontier_complete = (
        expected_frontier is not None
        and expected_frontier_attempted == len(expected_frontier_target_ranks)
        and all(
            "direct_reason" in result
            for position, result in enumerate(results, start=1)
            if position <= expected_frontier
            and result.get("status") == "unknown"
            and str(result.get("url") or "").startswith("http")
        )
    )
    if (
        type(validation.get("ranked_pool_count")) is not int
        or validation["ranked_pool_count"] != len(results)
        or type(target) is not int
        or target < 1
        or type(direct_attempted) is not int
        or direct_attempted != len(results)
        or type(browser_targets) is not int
        or type(browser_attempted) is not int
        or browser_targets != len(target_ranks)
        or browser_attempted != len(attempted_ranks)
        or not attempted_rank_set.issubset(target_rank_set)
        or target_ranks != expected_target_ranks
        or attempted_rank_set != result_attempted_ranks
        or type(target_reached) is not bool
        or type(pool_exhausted) is not bool
        or target_reached != (counts["verified"] >= target)
        or not (target_reached or pool_exhausted)
        or (target_reached and not expected_frontier_complete)
        or (expected_frontier is not None and type(frontier_rank) is not int)
        or frontier_rank != expected_frontier
        or type(frontier_targets) is not int
        or frontier_targets != len(expected_frontier_target_ranks)
        or type(frontier_attempted) is not int
        or frontier_attempted != expected_frontier_attempted
        or type(frontier_complete) is not bool
        or frontier_complete != expected_frontier_complete
    ):
        raise ContractError("validation target/exhaustion evidence is invalid")
    saved_top_rows = board.get("saved_top_rows")
    board_ranked_count = board.get("ranked_candidate_rows")
    expected_universe_exhausted = (
        board.get("ranking_complete") is True
        and type(saved_top_rows) is int
        and saved_top_rows == len(results)
        and type(board_ranked_count) is int
        and board_ranked_count <= saved_top_rows
    )
    if (
        full_input_coverage is not True
        or type(ranked_candidate_count) is not int
        or ranked_candidate_count != board_ranked_count
        or type(ranked_universe_exhausted) is not bool
        or ranked_universe_exhausted != expected_universe_exhausted
        or (pool_exhausted and not ranked_universe_exhausted)
    ):
        raise ContractError("validation does not prove full ranked-universe coverage")
    if pool_exhausted and (
        attempted_ranks != target_ranks
        or target_rank_set != set(evidenced_target_ranks)
        or any(
            browser_evidence_expected(result) and "direct_reason" not in result
            for result in results
        )
    ):
        raise ContractError("pool exhaustion leaves browser-eligible unknowns unattempted")

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
