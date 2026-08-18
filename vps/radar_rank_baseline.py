#!/usr/bin/env python3
"""Export and validate an immutable Radar ranking baseline.

The baseline is deliberately a local auxiliary artifact.  This module has no
network, publisher, database, timer, or email integration.  It reconstructs the
exact accepted public selection from generation-bound source evidence and emits
a deterministic, content-addressed JSON file plus an optional atomic pointer to
the latest structurally accepted generation.  Artifact integrity and freshness
are deliberately separate: historical accepted evidence remains verifiable
after its original validity window closes, but callers must explicitly require
freshness before using it for a fresh-only decision.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Iterable

try:
    from . import build_observed_value_board as builder
    from . import publish_radar_dashboard as publisher
    from . import source_identity
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_observed_value_board as builder
    import publish_radar_dashboard as publisher
    import source_identity


CONTRACT = "radar-rank-baseline-v2"
LATEST_ACCEPTED_POINTER_CONTRACT = "radar-rank-baseline-latest-accepted-v2"
SCHEMA_VERSION = 2
MAX_SELECTION = 10_000
MIN_SELECTION = 50
MAX_RANKED_POOL = 100_000
MILEAGE_BAND_KM = 25_000
VALIDITY = timedelta(hours=8)
FUTURE_SKEW = timedelta(minutes=5)
HEX_16 = re.compile(r"^[0-9a-f]{16}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
STORE_LOCK_FILENAME = ".radar-rank-baseline.lock"

RANK_TUPLE_FIELDS = (
    "-conservative_discount_bps",
    "-savings_eur",
    "-median_discount_bps",
    "-peer_source_count",
    "-peer_count",
    "-year",
    "mileage_km",
    "price_eur",
    "public_offer_id",
)
RANKING_CONTRACT = {
    "name": "schengen-observed-peer-value-full-tuple-v1",
    "algorithm": publisher.ALGORITHM_VERSION,
    "ordering": list(RANK_TUPLE_FIELDS),
    "lower_tuple_is_better": True,
    "selection_limit": MAX_SELECTION,
    "minimum_usable_selection": MIN_SELECTION,
    "mileage_band_km": MILEAGE_BAND_KM,
}

TOP_FIELDS = frozenset(
    {
        "contract", "schema_version", "algorithm", "generation_id",
        "data_generated_at_utc", "valid_until_utc", "ranking_contract",
        "source_family_contract", "proof", "hashes", "code_provenance",
        "cutoffs", "peer_stats", "published_selection",
        "artifact_payload_sha256",
    }
)
LATEST_ACCEPTED_POINTER_FIELDS = frozenset(
    {
        "contract", "schema_version", "generation_id",
        "data_generated_at_utc", "valid_until_utc", "artifact_file",
        "artifact_sha256", "artifact_payload_sha256",
        "source_board_sha256", "publication_manifest_sha256",
        "live_convergence_audit_sha256", "selected_fields_sha256",
        "ranking_contract_sha256", "pointer_payload_sha256",
    }
)
PROOF_FIELDS = frozenset(
    {
        "published_verified_count", "verified_target", "target_reached",
        "pool_exhausted", "ranked_pool_count", "ranked_universe_exhausted",
        "full_input_coverage", "direct_attempted_count",
        "browser_target_count", "browser_attempted_count",
        "selection_horizon_rank", "selection_audit_pass",
        "live_convergence_pass",
    }
)
HASH_FIELDS = frozenset(
    {
        "ranked_board_sha256", "source_board_sha256",
        "validation_report_sha256", "publication_manifest_sha256",
        "selection_audit_sha256", "live_convergence_audit_sha256",
        "snapshot_sha256", "offer_fields_sha256", "source_policy_sha256",
        "quarantine_manifest_sha256", "blocked_source_keys_sha256",
        "candidate_ids_sha256", "selected_ids_sha256",
        "candidate_fields_sha256", "selected_fields_sha256",
        "ranking_contract_sha256", "source_family_contract_sha256",
        "peer_method_sha256", "peer_stats_sha256",
        "published_selection_sha256",
    }
)
CODE_PROVENANCE_FIELDS = frozenset(
    {
        "builder_code_sha256", "publisher_code_sha256",
        "exporter_code_sha256",
    }
)
CUTOFF_FIELDS = frozenset({"rank_50", "rank_horizon"})
CUTOFF_ROW_FIELDS = frozenset({"rank", "public_offer_id", "rank_tuple"})
PEER_FIELDS = frozenset(
    {
        "model", "year", "fuel", "mileage_band_start_km",
        "excluded_source_family", "lower_quartile_eur", "median_eur",
        "peer_count", "peer_source_count", "peer_country_count",
        "peer_dispersion_bps",
    }
)
SELECTION_FIELDS = frozenset(
    {"rank", "public_offer_id", "normalized_url", "rank_tuple", "compact_payload"}
)

RANKED_BOARD_FIELDS = frozenset(
    {
        "algorithm", "anomalous_low_prices_excluded", "blocked_source_key_count",
        "blocked_source_keys_sha256", "board_built_at_utc",
        "connected_country_count", "connected_source_count", "country_counts",
        "data_generated_at_utc", "displayed_country_count",
        "displayed_source_count", "eligible_observed_rows", "generated_at",
        "live_verified_offer_count", "max_observation_age_hours",
        "methodology_ar", "observation_cutoff_utc", "offer_fields_sha256",
        "offers", "outside_saved_better_than_cutoff", "peer_method",
        "policy_blocked_sources", "qualified", "quarantine_manifest_sha256",
        "ranked_candidate_rows", "ranking_complete", "rejected_counts",
        "saved_top_rows", "scanned_recent_rows", "schema_version", "shown",
        "snapshot_eligible_sha256", "source_counts", "source_policy_sha256",
        "total_all", "universe_unique_offers",
        "unsupported_economics_published", "validation",
    }
)
SOURCE_BOARD_FIELDS = frozenset(
    {
        "algorithm", "anomalous_low_prices_excluded", "blocked_source_key_count",
        "blocked_source_keys_sha256", "board_built_at_utc",
        "connected_country_count", "connected_source_count", "count",
        "country_counts", "data_generated_at_utc", "displayed_country_count",
        "displayed_source_count", "eligible_observed_rows", "generated_at",
        "live_verified_offer_count", "max_observation_age_hours",
        "methodology_ar", "observation_cutoff_utc", "offer_fields_sha256",
        "offers", "outside_saved_better_than_cutoff", "peer_method",
        "policy_blocked_sources", "quarantine_manifest_sha256",
        "ranked_candidate_rows", "ranking_complete", "rejected_counts",
        "saved_top_rows", "scanned_recent_rows", "schema_version",
        "schengen_country_total", "scope", "snapshot_eligible_sha256",
        "source_counts", "source_policy_sha256", "universe_unique_offers",
        "unsupported_economics_published", "updated_utc", "validation",
    }
)
VALIDATION_FIELDS = frozenset(
    {
        "browser_attempted_count", "browser_attempted_ranks",
        "browser_frontier_attempted_count", "browser_frontier_complete",
        "browser_frontier_target_count", "browser_target_count",
        "browser_target_ranks", "checked", "counts", "dead_listing_ids",
        "dead_urls", "direct_attempted_count", "full_input_coverage",
        "generated_at", "input", "input_algorithm",
        "input_offer_fields_sha256", "input_snapshot_sha256",
        "input_updated_at", "pool_exhausted", "ranked_candidate_count",
        "ranked_pool_count", "ranked_universe_exhausted", "reason_counts",
        "requested_limit", "results", "schema_version",
        "selection_frontier_rank", "source_status_counts", "target_reached",
        "unknown_listing_ids", "unknown_urls", "verified_listing_ids",
        "verified_target",
    }
)
VALIDATION_SUMMARY_FIELDS = frozenset(
    {
        "browser_attempted_count", "browser_attempted_ranks",
        "browser_frontier_attempted_count", "browser_frontier_complete",
        "browser_frontier_target_count", "browser_target_count",
        "browser_target_ranks", "checked", "counts", "direct_attempted_count",
        "full_input_coverage", "generated_at", "input_algorithm",
        "input_offer_fields_sha256", "input_snapshot_sha256",
        "input_updated_at", "pool_exhausted", "ranked_candidate_count",
        "ranked_pool_count", "ranked_universe_exhausted", "schema_version",
        "selection_frontier_rank", "target_reached", "verified_target",
    }
)
VALIDATION_RESULT_REQUIRED = frozenset(
    {"board_rank", "country", "listing_id", "reason", "source", "status", "title", "url"}
)
VALIDATION_RESULT_ALLOWED = VALIDATION_RESULT_REQUIRED | frozenset(
    {"direct_reason", "final_url", "http_status"}
)
MANIFEST_FIELDS = frozenset(
    {
        "algorithm", "candidate_fields_sha256", "candidate_ids_sha256",
        "connected_country_count", "connected_source_count",
        "data_generated_at_utc", "generation_id", "prepared_at",
        "published_offer_count", "qualified_universe_offers",
        "ranked_offer_count", "schema_version", "selected_fields_sha256",
        "selected_ids_sha256", "selection_input_counts",
        "snapshot_eligible_sha256", "source_board", "source_board_sha256",
        "universe_database", "universe_unique_offers", "verified_live_count",
    }
)
SELECTION_AUDIT_FIELDS = frozenset(
    {
        "algorithm", "candidate_fields_sha256", "candidate_ids_sha256",
        "cesja_count", "confirmed_dead_count",
        "confirmed_dead_or_lease_like_published", "connected_country_count",
        "connected_source_count", "coverage_quota_substitutions",
        "data_generated_at_utc", "exact_order_match",
        "exact_source_fields_match", "full_ranked_input_offers",
        "generation_id", "lease_like_count", "negative_control_pass",
        "outside_better_than_cutoff", "positive_control_pass",
        "published_offer_count", "qualified_universe_offers",
        "ranking_observed_partition_complete", "ranking_qualified_offers",
        "ranking_saved_observed_offers", "ranking_saved_offers", "result",
        "risk_listing_count", "risk_negative_control_pass",
        "same_generation_verified_only", "schema_version",
        "selected_fields_sha256", "selected_ids_sha256",
        "selection_input_counts", "snapshot_eligible_sha256",
        "source_board_sha256", "strict_global_top_n", "unique_ids",
        "unique_urls", "universe_unique_offers",
        "unsupported_economics_published", "verified_live_count",
    }
)
LIVE_AUDIT_FIELDS = frozenset(
    {
        "algorithm", "attempts", "candidate_fields_sha256",
        "candidate_ids_sha256", "cesja_count", "confirmed_dead_count",
        "confirmed_dead_or_lease_like_published", "connected_country_count",
        "connected_source_count", "coverage_quota_substitutions",
        "data_generated_at_utc", "deadline_sec", "elapsed_sec",
        "exact_order_match", "exact_source_fields_match", "expected_generation",
        "full_ranked_input_offers", "generation_id", "lease_like_count",
        "negative_control_pass", "network_errors", "observed_generation",
        "outside_better_than_cutoff", "positive_control_pass",
        "published_offer_count", "qualified_universe_offers",
        "ranking_observed_partition_complete", "ranking_qualified_offers",
        "ranking_saved_observed_offers", "ranking_saved_offers", "result",
        "risk_listing_count", "risk_negative_control_pass",
        "same_generation_verified_only", "schema_version",
        "selected_fields_sha256", "selected_ids_sha256",
        "snapshot_eligible_sha256", "strict_global_top_n", "unique_ids",
        "unique_urls", "universe_unique_offers",
        "unsupported_economics_published", "verified_live_count",
    }
)


class BaselineError(RuntimeError):
    """The baseline or one of its generation-bound inputs is invalid."""


@dataclass(frozen=True)
class BaselineStoreLock:
    """An exclusive lock bound to one canonical artifact-directory inode."""

    artifact_dir: Path
    artifact_dir_fd: int
    artifact_device: int
    artifact_inode: int
    lock_path: Path
    lock_parent_fd: int
    lock_fd: int
    lock_device: int
    lock_inode: int

    def assert_current(self) -> None:
        """Fail closed if either directory entry was substituted after open."""
        directory = os.lstat(self.artifact_dir)
        opened_directory = os.fstat(self.artifact_dir_fd)
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_dev != self.artifact_device
            or directory.st_ino != self.artifact_inode
            or opened_directory.st_dev != self.artifact_device
            or opened_directory.st_ino != self.artifact_inode
        ):
            raise BaselineError("baseline artifact directory changed while locked")
        lock_entry = os.stat(
            self.lock_path.name,
            dir_fd=self.lock_parent_fd,
            follow_symlinks=False,
        )
        opened_lock = os.fstat(self.lock_fd)
        for observed in (lock_entry, opened_lock):
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_uid != os.geteuid()
                or observed.st_dev != self.lock_device
                or observed.st_ino != self.lock_inode
            ):
                raise BaselineError("baseline store lock changed while held")


def _open_canonical_directory(
    path: Path, label: str, *, create: bool = False,
) -> tuple[Path, int, os.stat_result]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if create:
        absolute.mkdir(parents=True, exist_ok=True)
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise BaselineError(f"{label} is unavailable") from exc
    if resolved != absolute:
        raise BaselineError(f"{label} contains a symlink or path alias")
    metadata = os.lstat(absolute)
    if not stat.S_ISDIR(metadata.st_mode):
        raise BaselineError(f"{label} is not a directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(absolute, flags)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != metadata.st_dev
        or opened.st_ino != metadata.st_ino
    ):
        os.close(descriptor)
        raise BaselineError(f"{label} changed while opening")
    return absolute, descriptor, opened


def canonical_store_lock_path(artifact_dir: Path) -> Path:
    """Return the single lock path used by exporter and retention."""
    absolute = Path(os.path.abspath(os.fspath(artifact_dir)))
    return absolute.parent / STORE_LOCK_FILENAME


@contextmanager
def exclusive_store_lock(
    artifact_dir: Path, *, create_artifact_dir: bool = False,
) -> Iterable[BaselineStoreLock]:
    """Hold the canonical exclusive store lock and stable directory descriptors."""
    directory_fd = -1
    parent_fd = -1
    lock_fd = -1
    created = False
    try:
        directory, directory_fd, directory_metadata = _open_canonical_directory(
            artifact_dir,
            "baseline artifact directory",
            create=create_artifact_dir,
        )
        parent, parent_fd, _ = _open_canonical_directory(
            directory.parent, "baseline lock parent directory"
        )
        lock_path = parent / STORE_LOCK_FILENAME
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            lock_fd = os.open(
                lock_path.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            created = True
        except FileExistsError:
            lock_fd = os.open(lock_path.name, flags, dir_fd=parent_fd)
        lock_metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_nlink != 1
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            or lock_metadata.st_uid != os.geteuid()
            or lock_metadata.st_size != 0
        ):
            raise BaselineError("baseline store lock is not a private empty regular file")
        if created:
            os.fsync(lock_fd)
            fsync_directory(parent)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        held = BaselineStoreLock(
            artifact_dir=directory,
            artifact_dir_fd=directory_fd,
            artifact_device=directory_metadata.st_dev,
            artifact_inode=directory_metadata.st_ino,
            lock_path=lock_path,
            lock_parent_fd=parent_fd,
            lock_fd=lock_fd,
            lock_device=lock_metadata.st_dev,
            lock_inode=lock_metadata.st_ino,
        )
        held.assert_current()
        yield held
    except OSError as exc:
        raise BaselineError(f"cannot acquire canonical baseline store lock: {exc}") from exc
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BaselineError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise BaselineError(f"non-finite JSON number is forbidden: {value}")


def loads_strict(raw: bytes, label: str = "JSON") -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"{label} is not strict UTF-8 JSON") from exc


def load_object(path: Path, fields: frozenset[str], label: str) -> dict[str, Any]:
    value = loads_strict(path.read_bytes(), label)
    if not isinstance(value, dict):
        raise BaselineError(f"{label} is not an object")
    require_fields(value, fields, label)
    return value


def require_fields(value: dict[str, Any], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        missing = sorted(fields - set(value))
        unknown = sorted(set(value) - fields)
        raise BaselineError(f"{label} fields differ: missing={missing} unknown={unknown}")


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BaselineError("value cannot be encoded as canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_code_provenance() -> dict[str, str]:
    """Return hashes for the code bytes used by this process.

    These hashes describe provenance; they are not part of timeless structural
    validity.  A caller that needs to reproduce an artifact with the exact same
    implementation can opt into :func:`require_current_code_compatibility`.
    """
    return {
        "builder_code_sha256": file_sha256(Path(builder.__file__).resolve()),
        "publisher_code_sha256": file_sha256(Path(publisher.__file__).resolve()),
        "exporter_code_sha256": file_sha256(Path(__file__).resolve()),
    }


def require_hash(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or HEX_64.fullmatch(value) is None:
        raise BaselineError(f"{label} is not a lowercase SHA-256")
    return value


def require_int(value: Any, label: str, *, low: int = 0, high: int = 100_000_000) -> int:
    if type(value) is not int or not low <= value <= high:
        raise BaselineError(f"{label} is outside its integer bounds")
    return value


def parse_utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise BaselineError(f"{label} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineError(f"{label} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise BaselineError(f"{label} has no timezone")
    return parsed.astimezone(UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def source_family_contract() -> dict[str, Any]:
    return {
        "name": "observed-peer-source-family-v1",
        "autoscout24_rule": "autoscout24.ch-isolated;all-other-autoscout24-shared",
        "pl_listing_mirrors": sorted(source_identity.PL_MIRROR_SOURCE_KEYS),
        "it_listing_mirrors": sorted(source_identity.IT_MIRROR_SOURCE_KEYS),
        "be_listing_mirrors": sorted(source_identity.BE_MIRROR_SOURCE_KEYS),
        "fallback": "normalized-source-key",
    }


def rank_tuple(offer: dict[str, Any]) -> list[int | str]:
    value = publisher.rank_key(offer)
    result: list[int | str] = []
    for index, item in enumerate(value):
        if index == len(value) - 1:
            if not isinstance(item, str):
                raise BaselineError("rank tuple public ID is not text")
            result.append(item)
        else:
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
                raise BaselineError("rank tuple contains a non-finite number")
            if int(item) != item:
                raise BaselineError("rank tuple contains a non-integral number")
            result.append(int(item))
    return result


def _validate_validation(
    report: dict[str, Any],
    summary: dict[str, Any],
    raw_offers: list[dict[str, Any]],
    *,
    algorithm: str,
    data_generated_at: str,
    snapshot_sha256: str,
    offer_fields_sha256: str,
) -> list[int]:
    require_fields(summary, VALIDATION_SUMMARY_FIELDS, "embedded validation")
    for key in VALIDATION_SUMMARY_FIELDS:
        if report.get(key) != summary.get(key):
            raise BaselineError(f"validation report differs from embedded summary at {key}")
    count = len(raw_offers)
    counts = report.get("counts")
    if not isinstance(counts, dict) or set(counts) != {"verified", "dead", "unknown"}:
        raise BaselineError("validation status counts are invalid")
    for key in counts:
        require_int(counts[key], f"validation counts.{key}", high=MAX_RANKED_POOL)
    if sum(counts.values()) != count:
        raise BaselineError("validation status counts do not cover the ranked pool")
    if (
        report.get("schema_version") != 1
        or report.get("input_algorithm") != algorithm
        or report.get("input_updated_at") != data_generated_at
        or report.get("input_snapshot_sha256") != snapshot_sha256
        or report.get("input_offer_fields_sha256") != offer_fields_sha256
        or report.get("checked") != count
        or report.get("ranked_candidate_count") != count
        or report.get("ranked_pool_count") != count
        or report.get("target_reached") != (counts["verified"] >= report.get("verified_target", -1))
        or not (report.get("target_reached") is True or report.get("pool_exhausted") is True)
    ):
        raise BaselineError("validation generation/proof binding is invalid")
    require_int(report.get("verified_target"), "verified_target", low=MIN_SELECTION, high=MAX_SELECTION)
    direct_count = require_int(report.get("direct_attempted_count"), "direct_attempted_count", high=count)
    browser_target = report.get("browser_target_ranks")
    browser_attempted = report.get("browser_attempted_ranks")
    if (
        not isinstance(browser_target, list)
        or not isinstance(browser_attempted, list)
        or browser_target != sorted(set(browser_target))
        or browser_attempted != sorted(set(browser_attempted))
        or any(type(rank) is not int or not 1 <= rank <= count for rank in browser_target + browser_attempted)
        or report.get("browser_target_count") != len(browser_target)
        or report.get("browser_attempted_count") != len(browser_attempted)
    ):
        raise BaselineError("browser validation frontier is invalid")
    if report.get("pool_exhausted") is True and (
        report.get("full_input_coverage") is not True
        or report.get("ranked_universe_exhausted") is not True
        or direct_count != count
        or browser_attempted != browser_target
    ):
        raise BaselineError("pool exhaustion is not proven")
    results = report.get("results")
    if not isinstance(results, list) or len(results) != count:
        raise BaselineError("validation results do not cover the ranked pool")
    states: list[int] = []
    state_names: list[str] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for index, (result, offer) in enumerate(zip(results, raw_offers, strict=True), 1):
        if not isinstance(result, dict):
            raise BaselineError(f"validation result {index} is not an object")
        if not VALIDATION_RESULT_REQUIRED.issubset(result) or not set(result).issubset(VALIDATION_RESULT_ALLOWED):
            raise BaselineError(f"validation result {index} fields are invalid")
        status = result.get("status")
        if status not in {"verified", "dead", "unknown"}:
            raise BaselineError(f"validation result {index} status is invalid")
        if (
            result.get("board_rank") != index
            or result.get("listing_id") != offer.get("id")
            or result.get("url") != offer.get("url")
            or result.get("source") != offer.get("source")
            or result.get("country") != offer.get("country")
            or result.get("title") != offer.get("title")
        ):
            raise BaselineError(f"validation result {index} is not bound to its ranked row")
        identity = str(result["listing_id"])
        url = str(result["url"])
        if identity in seen_ids or url in seen_urls:
            raise BaselineError("validation results contain duplicate identity or URL")
        seen_ids.add(identity)
        seen_urls.add(url)
        state_names.append(status)
        states.append({"verified": 1, "dead": -1, "unknown": 0}[status])
    if Counter(state_names) != Counter(counts):
        raise BaselineError("validation results differ from status counts")
    for state, id_key, url_key in (
        ("verified", "verified_listing_ids", None),
        ("dead", "dead_listing_ids", "dead_urls"),
        ("unknown", "unknown_listing_ids", "unknown_urls"),
    ):
        expected_ids = [str(item["listing_id"]) for item in results if item["status"] == state]
        if report.get(id_key) != expected_ids:
            raise BaselineError(f"validation {id_key} is inconsistent")
        if url_key is not None:
            expected_urls = [str(item["url"]) for item in results if item["status"] == state]
            if report.get(url_key) != expected_urls:
                raise BaselineError(f"validation {url_key} is inconsistent")
    return states


def _validate_acceptance_receipts(
    manifest: dict[str, Any],
    audit: dict[str, Any],
    live: dict[str, Any],
    *,
    generation_id: str,
    algorithm: str,
    data_generated_at: str,
    source_board_sha256: str,
    snapshot_sha256: str,
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    ranked_count: int,
) -> None:
    if manifest.get("schema_version") != 1:
        raise BaselineError("publication manifest schema is unsupported")
    if not isinstance(manifest.get("selection_input_counts"), dict):
        raise BaselineError("publication selection counts are invalid")
    candidate_ids = publisher.digest_ids(candidates)
    selected_ids = publisher.digest_ids(selected)
    candidate_fields = publisher.digest_fields(candidates)
    selected_fields = publisher.digest_fields(selected)
    expected_generation = hashlib.sha256(
        (
            f"{algorithm}\n{data_generated_at}\n"
            f"{candidate_fields}\n{selected_fields}\n"
        ).encode("utf-8")
    ).hexdigest()[:16]
    if expected_generation != generation_id:
        raise BaselineError("generation ID does not bind the exact selection")
    manifest_expected = {
        "algorithm": algorithm,
        "generation_id": generation_id,
        "data_generated_at_utc": data_generated_at,
        "source_board_sha256": source_board_sha256,
        "snapshot_eligible_sha256": snapshot_sha256,
        "ranked_offer_count": ranked_count,
        "qualified_universe_offers": len(candidates),
        "published_offer_count": len(selected),
        "verified_live_count": len(selected),
        "candidate_ids_sha256": candidate_ids,
        "selected_ids_sha256": selected_ids,
        "candidate_fields_sha256": candidate_fields,
        "selected_fields_sha256": selected_fields,
    }
    for key, value in manifest_expected.items():
        if manifest.get(key) != value:
            raise BaselineError(f"publication manifest mismatch at {key}")
    prepared = parse_utc(manifest.get("prepared_at"), "publication prepared_at")
    if prepared < parse_utc(data_generated_at, "data timestamp"):
        raise BaselineError("publication predates its data")
    common = (
        "algorithm", "generation_id", "data_generated_at_utc",
        "snapshot_eligible_sha256", "candidate_ids_sha256",
        "candidate_fields_sha256", "selected_ids_sha256",
        "selected_fields_sha256", "qualified_universe_offers",
        "published_offer_count", "verified_live_count", "universe_unique_offers",
        "connected_country_count", "connected_source_count",
    )
    for receipt, label in ((audit, "selection audit"), (live, "live audit")):
        if receipt.get("schema_version") != 1:
            raise BaselineError(f"{label} schema is unsupported")
        for key in common:
            if receipt.get(key) != manifest.get(key):
                raise BaselineError(f"{label} mismatch at {key}")
        for key in (
            "exact_order_match", "exact_source_fields_match", "strict_global_top_n",
            "ranking_observed_partition_complete", "unique_ids", "unique_urls",
            "negative_control_pass", "risk_negative_control_pass",
            "positive_control_pass", "same_generation_verified_only",
        ):
            if receipt.get(key) is not True:
                raise BaselineError(f"{label} did not pass {key}")
        for key in (
            "coverage_quota_substitutions", "cesja_count", "lease_like_count",
            "risk_listing_count", "confirmed_dead_count",
            "confirmed_dead_or_lease_like_published", "outside_better_than_cutoff",
            "unsupported_economics_published",
        ):
            if receipt.get(key) != 0:
                raise BaselineError(f"{label} has nonzero {key}")
    if audit.get("result") != "BEST_SELECTION_AUDIT_PASS":
        raise BaselineError("local best-selection audit did not pass")
    if audit.get("source_board_sha256") != source_board_sha256:
        raise BaselineError("selection audit is not bound to the source board")
    if live.get("result") != "LIVE_GENERATION_AUDIT_PASS":
        raise BaselineError("live convergence audit did not pass")
    if live.get("expected_generation") != generation_id or live.get("observed_generation") != generation_id:
        raise BaselineError("live convergence generation mismatch")
    if live.get("network_errors") != 0:
        raise BaselineError("live convergence audit recorded network errors")


def _peer_stats(raw_offers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for index, offer in enumerate(raw_offers, 1):
        if not isinstance(offer, dict) or set(offer) != publisher.RAW_OFFER_FIELDS:
            raise BaselineError(f"ranked offer {index} does not have the exact v7 fields")
        key = (
            str(offer["model"]).casefold(),
            int(offer["year"]),
            str(offer["fuel"]),
            (int(offer["mileage"]) // MILEAGE_BAND_KM) * MILEAGE_BAND_KM,
            builder.source_family(offer["source"]),
        )
        dispersion = offer["peer_dispersion"]
        if isinstance(dispersion, bool) or not isinstance(dispersion, (int, float)) or not math.isfinite(dispersion):
            raise BaselineError(f"ranked offer {index} peer dispersion is invalid")
        value = (
            int(offer["peer_lower_quartile_eur"]),
            int(offer["peer_median_eur"]),
            int(offer["peer_count"]),
            int(offer["peer_source_count"]),
            int(offer["peer_country_count"]),
            int(round(float(dispersion) * 10_000)),
        )
        prior = by_key.setdefault(key, value)
        if prior != value:
            raise BaselineError(f"conflicting peer statistics for cohort {key}")
    return [
        {
            "model": key[0],
            "year": key[1],
            "fuel": key[2],
            "mileage_band_start_km": key[3],
            "excluded_source_family": key[4],
            "lower_quartile_eur": value[0],
            "median_eur": value[1],
            "peer_count": value[2],
            "peer_source_count": value[3],
            "peer_country_count": value[4],
            "peer_dispersion_bps": value[5],
        }
        for key, value in sorted(by_key.items())
    ]


def build_baseline(
    *,
    ranked_board_path: Path,
    source_board_path: Path,
    validation_report_path: Path,
    publication_manifest_path: Path,
    selection_audit_path: Path,
    live_audit_path: Path,
    source_policy_path: Path,
    quarantine_manifest_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    ranked = load_object(ranked_board_path, RANKED_BOARD_FIELDS, "ranked board")
    source = load_object(source_board_path, SOURCE_BOARD_FIELDS, "source board")
    validation = load_object(validation_report_path, VALIDATION_FIELDS, "validation report")
    manifest = load_object(publication_manifest_path, MANIFEST_FIELDS, "publication manifest")
    audit = load_object(selection_audit_path, SELECTION_AUDIT_FIELDS, "selection audit")
    live = load_object(live_audit_path, LIVE_AUDIT_FIELDS, "live convergence audit")

    if ranked.get("schema_version") != 2 or source.get("schema_version") != 2:
        raise BaselineError("ranked/source board schema is unsupported")
    algorithm = ranked.get("algorithm")
    if algorithm != publisher.ALGORITHM_VERSION or source.get("algorithm") != algorithm:
        raise BaselineError("ranked/source algorithm mismatch")
    data_generated_at = ranked.get("data_generated_at_utc")
    generated = parse_utc(data_generated_at, "data_generated_at_utc")
    if (
        ranked.get("generated_at") != data_generated_at
        or source.get("generated_at") != data_generated_at
        or source.get("updated_utc") != data_generated_at
    ):
        raise BaselineError("ranked/source data timestamp mismatch")
    validation_generated = parse_utc(validation.get("generated_at"), "validation generated_at")
    if validation_generated < generated:
        raise BaselineError("validation predates its ranked data")
    valid_until = min(generated + VALIDITY, validation_generated + VALIDITY)

    shared = (
        "algorithm", "data_generated_at_utc", "snapshot_eligible_sha256",
        "offer_fields_sha256", "source_policy_sha256",
        "quarantine_manifest_sha256", "blocked_source_keys_sha256",
        "blocked_source_key_count", "policy_blocked_sources", "peer_method",
        "ranking_complete", "outside_saved_better_than_cutoff",
        "unsupported_economics_published", "universe_unique_offers",
        "saved_top_rows", "scanned_recent_rows", "eligible_observed_rows",
        "ranked_candidate_rows", "validation",
    )
    for key in shared:
        if ranked.get(key) != source.get(key):
            raise BaselineError(f"ranked/source board mismatch at {key}")
    raw_offers = ranked.get("offers")
    compact_offers = source.get("offers")
    if (
        not isinstance(raw_offers, list)
        or not isinstance(compact_offers, list)
        or not MIN_SELECTION <= len(raw_offers) <= MAX_RANKED_POOL
        or len(raw_offers) != len(compact_offers)
        or ranked.get("shown") != len(raw_offers)
        or source.get("count") != len(compact_offers)
        or ranked.get("qualified") != len(raw_offers)
    ):
        raise BaselineError("ranked/source row coverage is invalid")
    if (
        ranked.get("ranking_complete") is not True
        or ranked.get("outside_saved_better_than_cutoff") != 0
        or ranked.get("unsupported_economics_published") != 0
    ):
        raise BaselineError("ranked board does not prove a complete safe ordering")

    policy_sha = file_sha256(source_policy_path)
    quarantine_sha = file_sha256(quarantine_manifest_path) if quarantine_manifest_path else None
    blocked = ranked.get("policy_blocked_sources")
    if (
        policy_sha != ranked.get("source_policy_sha256")
        or quarantine_sha != ranked.get("quarantine_manifest_sha256")
        or not isinstance(blocked, list)
        or blocked != sorted(set(blocked))
        or ranked.get("blocked_source_key_count") != len(blocked)
        or ranked.get("blocked_source_keys_sha256") != canonical_sha256(blocked)
    ):
        raise BaselineError("source-policy/quarantine evidence is invalid")
    snapshot_sha = require_hash(ranked.get("snapshot_eligible_sha256"), "snapshot hash")
    offer_fields_sha = require_hash(ranked.get("offer_fields_sha256"), "offer-fields hash")
    states = _validate_validation(
        validation,
        ranked["validation"],
        raw_offers,
        algorithm=algorithm,
        data_generated_at=data_generated_at,
        snapshot_sha256=str(snapshot_sha),
        offer_fields_sha256=str(offer_fields_sha),
    )
    projected: list[dict[str, Any]] = []
    last_key: tuple[Any, ...] | None = None
    for index, (raw, compact, state) in enumerate(zip(raw_offers, compact_offers, states, strict=True), 1):
        if not isinstance(raw, dict) or not isinstance(compact, dict):
            raise BaselineError(f"ranked/source row {index} is not an object")
        try:
            expected = publisher.compact_ranked_offer(raw, state)
            current_key = builder.verbose_rank_key(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineError(f"ranked row {index} cannot be projected") from exc
        if compact != expected:
            raise BaselineError(f"source row {index} differs from ranked/validation evidence")
        if last_key is not None and current_key < last_key:
            raise BaselineError(f"ranked row {index} is out of order")
        last_key = current_key
        projected.append(expected)
    provisional = [{**item, "v": 0} for item in projected]
    if builder.canonical_offer_fields_sha256(provisional) != offer_fields_sha:
        raise BaselineError("offer-fields hash does not bind the complete source board")

    candidates = publisher.normalized_candidates(projected)
    published_count = require_int(
        manifest.get("published_offer_count"), "published_offer_count",
        low=MIN_SELECTION, high=MAX_SELECTION,
    )
    if published_count > len(candidates):
        raise BaselineError("publication count exceeds verified candidates")
    if len(candidates) > MAX_SELECTION and published_count != MAX_SELECTION:
        raise BaselineError("publication did not seal the 10,000-row horizon")
    if len(candidates) <= MAX_SELECTION and published_count != len(candidates):
        raise BaselineError("sub-10,000 publication omits a verified candidate")
    selected = candidates[:published_count]
    generation_id = manifest.get("generation_id")
    if not isinstance(generation_id, str) or HEX_16.fullmatch(generation_id) is None:
        raise BaselineError("generation ID is invalid")
    source_board_sha = file_sha256(source_board_path)
    _validate_acceptance_receipts(
        manifest,
        audit,
        live,
        generation_id=generation_id,
        algorithm=algorithm,
        data_generated_at=data_generated_at,
        source_board_sha256=source_board_sha,
        snapshot_sha256=str(snapshot_sha),
        candidates=candidates,
        selected=selected,
        ranked_count=len(raw_offers),
    )

    proof = {
        "published_verified_count": len(selected),
        "verified_target": validation["verified_target"],
        "target_reached": validation["target_reached"],
        "pool_exhausted": validation["pool_exhausted"],
        "ranked_pool_count": validation["ranked_pool_count"],
        "ranked_universe_exhausted": validation["ranked_universe_exhausted"],
        "full_input_coverage": validation["full_input_coverage"],
        "direct_attempted_count": validation["direct_attempted_count"],
        "browser_target_count": validation["browser_target_count"],
        "browser_attempted_count": validation["browser_attempted_count"],
        "selection_horizon_rank": len(selected),
        "selection_audit_pass": True,
        "live_convergence_pass": True,
    }
    if len(selected) < MAX_SELECTION and (
        proof["pool_exhausted"] is not True
        or proof["ranked_universe_exhausted"] is not True
        or proof["full_input_coverage"] is not True
        or proof["direct_attempted_count"] != proof["ranked_pool_count"]
    ):
        raise BaselineError("sub-10,000 baseline lacks complete pool-exhaustion proof")

    raw_by_id = {str(raw["id"]): raw for raw in raw_offers}
    selection: list[dict[str, Any]] = []
    for rank, compact in enumerate(selected, 1):
        identity = publisher.canonical_id(compact)
        raw = raw_by_id.get(identity)
        if raw is None:
            raise BaselineError(f"published offer {identity} has no ranked evidence")
        normalized_url = builder.normalize_https_url(compact["u"])
        if not normalized_url:
            raise BaselineError(f"published offer {identity} URL cannot be normalized")
        selection.append(
            {
                "rank": rank,
                "public_offer_id": identity,
                "normalized_url": normalized_url,
                "rank_tuple": rank_tuple(compact),
                "compact_payload": compact,
            }
        )
    peers = _peer_stats(raw_offers)
    peer_lookup = {
        (
            item["model"], item["year"], item["fuel"],
            item["mileage_band_start_km"], item["excluded_source_family"],
        ): item
        for item in peers
    }
    for row in selection:
        compact = row["compact_payload"]
        key = (
            str(compact["m"]).casefold(), compact["y"], compact["f"],
            (compact["km"] // MILEAGE_BAND_KM) * MILEAGE_BAND_KM,
            builder.source_family(compact["s"]),
        )
        peer = peer_lookup.get(key)
        if peer is None or any(
            peer[field] != compact[compact_field]
            for field, compact_field in (
                ("lower_quartile_eur", "q1"), ("median_eur", "mp"),
                ("peer_count", "pn"), ("peer_source_count", "ps"),
                ("peer_country_count", "pc"),
            )
        ):
            raise BaselineError(f"published rank {row['rank']} has no exact peer-stat row")

    ranking_contract = dict(RANKING_CONTRACT)
    family_contract = source_family_contract()
    hashes = {
        "ranked_board_sha256": file_sha256(ranked_board_path),
        "source_board_sha256": source_board_sha,
        "validation_report_sha256": file_sha256(validation_report_path),
        "publication_manifest_sha256": file_sha256(publication_manifest_path),
        "selection_audit_sha256": file_sha256(selection_audit_path),
        "live_convergence_audit_sha256": file_sha256(live_audit_path),
        "snapshot_sha256": snapshot_sha,
        "offer_fields_sha256": offer_fields_sha,
        "source_policy_sha256": policy_sha,
        "quarantine_manifest_sha256": quarantine_sha,
        "blocked_source_keys_sha256": ranked["blocked_source_keys_sha256"],
        "candidate_ids_sha256": manifest["candidate_ids_sha256"],
        "selected_ids_sha256": manifest["selected_ids_sha256"],
        "candidate_fields_sha256": manifest["candidate_fields_sha256"],
        "selected_fields_sha256": manifest["selected_fields_sha256"],
        "ranking_contract_sha256": canonical_sha256(ranking_contract),
        "source_family_contract_sha256": canonical_sha256(family_contract),
        "peer_method_sha256": canonical_sha256(ranked["peer_method"]),
        "peer_stats_sha256": canonical_sha256(peers),
        "published_selection_sha256": canonical_sha256(selection),
    }
    cutoff = lambda row: {
        "rank": row["rank"],
        "public_offer_id": row["public_offer_id"],
        "rank_tuple": row["rank_tuple"],
    }
    payload: dict[str, Any] = {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "algorithm": algorithm,
        "generation_id": generation_id,
        "data_generated_at_utc": format_utc(generated),
        "valid_until_utc": format_utc(valid_until),
        "ranking_contract": ranking_contract,
        "source_family_contract": family_contract,
        "proof": proof,
        "hashes": hashes,
        "code_provenance": current_code_provenance(),
        "cutoffs": {
            "rank_50": cutoff(selection[49]),
            "rank_horizon": cutoff(selection[-1]),
        },
        "peer_stats": peers,
        "published_selection": selection,
    }
    payload["artifact_payload_sha256"] = canonical_sha256(payload)
    require_exportable_baseline(payload, now=now)
    return payload


def validate_baseline_structure(baseline: dict[str, Any]) -> dict[str, Any]:
    """Validate immutable artifact structure without making a freshness claim."""
    if not isinstance(baseline, dict):
        raise BaselineError("baseline is not an object")
    require_fields(baseline, TOP_FIELDS, "baseline")
    if (
        baseline.get("contract") != CONTRACT
        or baseline.get("schema_version") != SCHEMA_VERSION
        or baseline.get("algorithm") != publisher.ALGORITHM_VERSION
        or not isinstance(baseline.get("generation_id"), str)
        or HEX_16.fullmatch(baseline["generation_id"]) is None
    ):
        raise BaselineError("baseline contract identity is invalid")
    unsigned = {key: value for key, value in baseline.items() if key != "artifact_payload_sha256"}
    if baseline.get("artifact_payload_sha256") != canonical_sha256(unsigned):
        raise BaselineError("baseline internal payload hash mismatch")
    generated = parse_utc(baseline.get("data_generated_at_utc"), "baseline data timestamp")
    valid_until = parse_utc(baseline.get("valid_until_utc"), "baseline valid-until")
    if baseline["data_generated_at_utc"] != format_utc(generated) or baseline["valid_until_utc"] != format_utc(valid_until):
        raise BaselineError("baseline timestamps are not canonical UTC")
    if valid_until <= generated or valid_until > generated + VALIDITY:
        raise BaselineError("baseline validity window is invalid")
    if baseline.get("ranking_contract") != RANKING_CONTRACT:
        raise BaselineError("ranking contract differs from this validator")
    if baseline.get("source_family_contract") != source_family_contract():
        raise BaselineError("source-family contract differs from this validator")

    proof = baseline.get("proof")
    hashes = baseline.get("hashes")
    code_provenance = baseline.get("code_provenance")
    cutoffs = baseline.get("cutoffs")
    peers = baseline.get("peer_stats")
    selection = baseline.get("published_selection")
    if (
        not isinstance(proof, dict)
        or not isinstance(hashes, dict)
        or not isinstance(code_provenance, dict)
        or not isinstance(cutoffs, dict)
    ):
        raise BaselineError("baseline nested contracts are invalid")
    require_fields(proof, PROOF_FIELDS, "baseline proof")
    require_fields(hashes, HASH_FIELDS, "baseline hashes")
    require_fields(code_provenance, CODE_PROVENANCE_FIELDS, "baseline code provenance")
    require_fields(cutoffs, CUTOFF_FIELDS, "baseline cutoffs")
    for key, value in hashes.items():
        require_hash(value, f"baseline hashes.{key}", nullable=(key == "quarantine_manifest_sha256"))
    for key, value in code_provenance.items():
        require_hash(value, f"baseline code_provenance.{key}")
    if hashes["ranking_contract_sha256"] != canonical_sha256(RANKING_CONTRACT):
        raise BaselineError("ranking contract hash mismatch")
    if hashes["source_family_contract_sha256"] != canonical_sha256(source_family_contract()):
        raise BaselineError("source-family contract hash mismatch")

    count = require_int(
        proof.get("published_verified_count"), "published_verified_count",
        low=MIN_SELECTION, high=MAX_SELECTION,
    )
    if (
        proof.get("selection_horizon_rank") != count
        or proof.get("selection_audit_pass") is not True
        or proof.get("live_convergence_pass") is not True
        or not isinstance(selection, list)
        or len(selection) != count
    ):
        raise BaselineError("baseline selection proof/count is invalid")
    ranked_pool = require_int(proof.get("ranked_pool_count"), "ranked_pool_count", low=count, high=MAX_RANKED_POOL)
    require_int(proof.get("verified_target"), "verified_target", low=MIN_SELECTION, high=MAX_SELECTION)
    require_int(proof.get("direct_attempted_count"), "direct_attempted_count", high=ranked_pool)
    require_int(proof.get("browser_target_count"), "browser_target_count", high=ranked_pool)
    require_int(proof.get("browser_attempted_count"), "browser_attempted_count", high=ranked_pool)
    if count < MAX_SELECTION and (
        proof.get("pool_exhausted") is not True
        or proof.get("ranked_universe_exhausted") is not True
        or proof.get("full_input_coverage") is not True
        or proof.get("direct_attempted_count") != ranked_pool
        or proof.get("browser_attempted_count") != proof.get("browser_target_count")
    ):
        raise BaselineError("sub-10,000 baseline lacks pool-exhaustion proof")
    if proof.get("target_reached") is not (count >= proof["verified_target"]):
        raise BaselineError("target-reached proof differs from the selection count")

    if not isinstance(peers, list) or not peers:
        raise BaselineError("baseline has no peer statistics")
    peer_keys: list[tuple[Any, ...]] = []
    peer_lookup: dict[tuple[Any, ...], dict[str, Any]] = {}
    for index, item in enumerate(peers, 1):
        if not isinstance(item, dict):
            raise BaselineError(f"peer statistic {index} is not an object")
        require_fields(item, PEER_FIELDS, f"peer statistic {index}")
        if (
            not isinstance(item["model"], str) or not item["model"]
            or item["model"] != item["model"].casefold()
            or not isinstance(item["fuel"], str) or item["fuel"] not in {"petrol", "hybrid"}
            or not isinstance(item["excluded_source_family"], str)
            or not item["excluded_source_family"]
        ):
            raise BaselineError(f"peer statistic {index} identity is invalid")
        for field, low, high in (
            ("year", 1900, 2200), ("mileage_band_start_km", 0, 1_000_000),
            ("lower_quartile_eur", 1, 1_000_000), ("median_eur", 1, 1_000_000),
            ("peer_count", 1, 1_000_000), ("peer_source_count", 1, 100_000),
            ("peer_country_count", 1, 1_000), ("peer_dispersion_bps", 0, 10_000),
        ):
            require_int(item[field], f"peer statistic {index}.{field}", low=low, high=high)
        if item["mileage_band_start_km"] % MILEAGE_BAND_KM or item["median_eur"] < item["lower_quartile_eur"]:
            raise BaselineError(f"peer statistic {index} values are invalid")
        key = (
            item["model"], item["year"], item["fuel"],
            item["mileage_band_start_km"], item["excluded_source_family"],
        )
        peer_keys.append(key)
        peer_lookup[key] = item
    if peer_keys != sorted(set(peer_keys)):
        raise BaselineError("peer statistics are not uniquely canonical-sorted")
    if hashes["peer_stats_sha256"] != canonical_sha256(peers):
        raise BaselineError("peer-statistics hash mismatch")

    ids: set[str] = set()
    urls: set[str] = set()
    prior_tuple: tuple[Any, ...] | None = None
    compact_selection: list[dict[str, Any]] = []
    for rank, row in enumerate(selection, 1):
        if not isinstance(row, dict):
            raise BaselineError(f"selection row {rank} is not an object")
        require_fields(row, SELECTION_FIELDS, f"selection row {rank}")
        compact = row.get("compact_payload")
        if not isinstance(compact, dict):
            raise BaselineError(f"selection row {rank} compact payload is invalid")
        try:
            eligible = publisher.eligible_offer(compact)
        except (TypeError, ValueError, ZeroDivisionError):
            eligible = False
        identity = publisher.canonical_id(compact)
        normalized_url = builder.normalize_https_url(compact.get("u"))
        expected_tuple = rank_tuple(compact)
        if (
            row.get("rank") != rank
            or not eligible
            or row.get("public_offer_id") != identity
            or row.get("normalized_url") != normalized_url
            or row.get("rank_tuple") != expected_tuple
            or HEX_64.fullmatch(identity) is None
        ):
            raise BaselineError(f"selection row {rank} contract is invalid")
        tuple_value = tuple(expected_tuple)
        if prior_tuple is not None and tuple_value < prior_tuple:
            raise BaselineError(f"selection row {rank} is out of rank order")
        prior_tuple = tuple_value
        if identity in ids or normalized_url in urls:
            raise BaselineError("selection contains duplicate ID or URL")
        ids.add(identity)
        urls.add(normalized_url)
        peer_key = (
            str(compact["m"]).casefold(), compact["y"], compact["f"],
            (compact["km"] // MILEAGE_BAND_KM) * MILEAGE_BAND_KM,
            builder.source_family(compact["s"]),
        )
        peer = peer_lookup.get(peer_key)
        if peer is None or any(
            peer[field] != compact[compact_field]
            for field, compact_field in (
                ("lower_quartile_eur", "q1"), ("median_eur", "mp"),
                ("peer_count", "pn"), ("peer_source_count", "ps"),
                ("peer_country_count", "pc"),
            )
        ):
            raise BaselineError(f"selection row {rank} peer statistics mismatch")
        compact_selection.append(compact)
    if hashes["published_selection_sha256"] != canonical_sha256(selection):
        raise BaselineError("published-selection hash mismatch")
    if hashes["selected_ids_sha256"] != publisher.digest_ids(compact_selection):
        raise BaselineError("selected-ID hash mismatch")
    if hashes["selected_fields_sha256"] != publisher.digest_fields(compact_selection):
        raise BaselineError("selected-fields hash mismatch")
    expected_generation = hashlib.sha256(
        (
            f"{baseline['algorithm']}\n{baseline['data_generated_at_utc']}\n"
            f"{hashes['candidate_fields_sha256']}\n{hashes['selected_fields_sha256']}\n"
        ).encode("utf-8")
    ).hexdigest()[:16]
    if baseline["generation_id"] != expected_generation:
        raise BaselineError("generation ID does not bind baseline hashes")
    for name, row in cutoffs.items():
        if not isinstance(row, dict):
            raise BaselineError(f"cutoff {name} is not an object")
        require_fields(row, CUTOFF_ROW_FIELDS, f"cutoff {name}")
    expected_50 = {key: selection[49][key] for key in CUTOFF_ROW_FIELDS}
    expected_horizon = {key: selection[-1][key] for key in CUTOFF_ROW_FIELDS}
    if cutoffs["rank_50"] != expected_50 or cutoffs["rank_horizon"] != expected_horizon:
        raise BaselineError("rank cutoffs do not bind the selection")
    return {
        "result": "RADAR_RANK_BASELINE_V2_STRUCTURE_PASS",
        "generation_id": baseline["generation_id"],
        "published_verified_count": count,
        "peer_stat_count": len(peers),
        "data_generated_at_utc": baseline["data_generated_at_utc"],
        "valid_until_utc": baseline["valid_until_utc"],
        "artifact_payload_sha256": baseline["artifact_payload_sha256"],
    }


def assess_freshness_window(
    data_generated_at_utc: Any,
    valid_until_utc: Any,
    *,
    now: datetime | None = None,
    label: str = "baseline",
) -> dict[str, Any]:
    """Assess a canonical sealed time window with an exact five-minute skew."""
    generated = parse_utc(data_generated_at_utc, f"{label} data timestamp")
    valid_until = parse_utc(valid_until_utc, f"{label} valid-until")
    if data_generated_at_utc != format_utc(generated) or valid_until_utc != format_utc(valid_until):
        raise BaselineError(f"{label} timestamps are not canonical UTC")
    if valid_until <= generated or valid_until > generated + VALIDITY:
        raise BaselineError(f"{label} validity window is invalid")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current < generated - FUTURE_SKEW:
        status = "future"
    elif current >= valid_until:
        status = "expired"
    else:
        status = "fresh"
    return {
        "freshness_status": status,
        "fresh": status == "fresh",
        "evaluated_at_utc": format_utc(current),
        "data_generated_at_utc": format_utc(generated),
        "valid_until_utc": format_utc(valid_until),
        "age_seconds": math.floor((current - generated).total_seconds()),
    }


def assess_baseline_freshness(
    baseline: dict[str, Any], *, now: datetime | None = None,
) -> dict[str, Any]:
    """Report freshness without changing historical structural validity."""
    validate_baseline_structure(baseline)
    return assess_freshness_window(
        baseline["data_generated_at_utc"], baseline["valid_until_utc"], now=now
    )


def require_exportable_baseline(
    baseline: dict[str, Any], *, now: datetime | None = None,
) -> dict[str, Any]:
    """Accept fresh or expired evidence, but reject clock-skew poisoning."""
    structure = validate_baseline_structure(baseline)
    freshness = assess_baseline_freshness(baseline, now=now)
    if freshness["freshness_status"] == "future":
        raise BaselineError("baseline generation is too far in the future")
    return {
        **structure,
        **freshness,
        "result": "RADAR_RANK_BASELINE_V2_EXPORTABLE",
    }


def require_fresh_baseline(
    baseline: dict[str, Any], *, now: datetime | None = None,
) -> dict[str, Any]:
    """Validate structure and reject any artifact that is not fresh now."""
    structure = validate_baseline_structure(baseline)
    freshness = assess_baseline_freshness(baseline, now=now)
    if freshness["freshness_status"] == "future":
        raise BaselineError("baseline is timestamped too far in the future")
    if freshness["freshness_status"] == "expired":
        raise BaselineError("baseline is expired")
    return {**structure, **freshness, "result": "RADAR_RANK_BASELINE_V2_FRESH_PASS"}


def assess_code_provenance(
    baseline: dict[str, Any],
    *,
    artifact_path: Path | None = None,
    trusted_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Assess declared code hashes only after optional external byte anchoring.

    The artifact's own seal cannot make its code-hash declarations trusted.  A
    caller must supply both the artifact path and a SHA-256 obtained from a
    separately trusted source before this function can report compatibility.
    """
    structure = validate_baseline_structure(baseline)
    observed = baseline["code_provenance"]
    current = current_code_provenance()
    mismatches = sorted(
        key for key in CODE_PROVENANCE_FIELDS if observed[key] != current[key]
    )
    if artifact_path is None and trusted_artifact_sha256 is None:
        return {
            **structure,
            "result": "RADAR_RANK_BASELINE_V2_CODE_PROVENANCE_DECLARED",
            "code_provenance_status": "declared_unanchored",
        }
    if artifact_path is None or trusted_artifact_sha256 is None:
        raise BaselineError("trusted code compatibility requires a complete artifact anchor")
    trusted = require_hash(trusted_artifact_sha256, "trusted artifact SHA-256")
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise BaselineError("anchored baseline artifact is not a regular file")
    actual = file_sha256(artifact_path)
    if actual != trusted:
        raise BaselineError("baseline artifact differs from its trusted SHA-256 anchor")
    anchored = load_object(artifact_path, TOP_FIELDS, "anchored baseline artifact")
    validate_baseline_structure(anchored)
    if canonical_bytes(anchored) != canonical_bytes(baseline):
        raise BaselineError("trusted artifact anchor does not bind this baseline")
    return {
        **structure,
        "result": "RADAR_RANK_BASELINE_V2_CODE_PROVENANCE_ANCHORED",
        "code_provenance_status": (
            "compatible_anchored" if not mismatches else "historical_anchored"
        ),
        "trusted_artifact_sha256": trusted,
    }


def require_current_code_compatibility(
    baseline: dict[str, Any],
    *,
    artifact_path: Path | None = None,
    trusted_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Require an external byte anchor and exact current code provenance."""
    assessment = assess_code_provenance(
        baseline,
        artifact_path=artifact_path,
        trusted_artifact_sha256=trusted_artifact_sha256,
    )
    if assessment["code_provenance_status"] == "declared_unanchored":
        raise BaselineError("current code compatibility requires a trusted artifact anchor")
    if assessment["code_provenance_status"] != "compatible_anchored":
        observed = baseline["code_provenance"]
        current = current_code_provenance()
        mismatches = sorted(
            key for key in CODE_PROVENANCE_FIELDS if observed[key] != current[key]
        )
        raise BaselineError(f"current code provenance mismatch: {mismatches}")
    return {
        **assessment,
        "result": "RADAR_RANK_BASELINE_V2_CODE_COMPATIBLE_ANCHORED",
    }


def artifact_bytes(baseline: dict[str, Any]) -> bytes:
    return canonical_bytes(baseline) + b"\n"


def reconstruction_payload(baseline: dict[str, Any]) -> dict[str, Any]:
    """Return evidence-bound bytes while excluding historical code provenance."""
    return {
        key: value for key, value in baseline.items()
        if key not in {"artifact_payload_sha256", "code_provenance"}
    }


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_content_addressed(output_dir: Path, baseline: dict[str, Any]) -> tuple[Path, str]:
    data = artifact_bytes(baseline)
    digest = hashlib.sha256(data).hexdigest()
    target = output_dir / f"{CONTRACT}.{digest}.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise BaselineError("content-addressed target is not a regular file")
        if target.read_bytes() != data:
            raise BaselineError("content-addressed target exists with different bytes")
        return target, digest
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_dir, prefix=f".{CONTRACT}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        temporary = None
        fsync_directory(output_dir)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target, digest


def build_latest_accepted_pointer(
    baseline: dict[str, Any], artifact_path: Path, artifact_sha256: str,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    require_exportable_baseline(baseline, now=now)
    require_hash(artifact_sha256, "artifact SHA-256")
    expected_name = f"{CONTRACT}.{artifact_sha256}.json"
    if artifact_path.name != expected_name:
        raise BaselineError("artifact filename is not content-addressed")
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise BaselineError("baseline artifact is not a regular file")
    if file_sha256(artifact_path) != artifact_sha256:
        raise BaselineError("baseline artifact hash mismatch")
    hashes = baseline["hashes"]
    core = {
        "contract": LATEST_ACCEPTED_POINTER_CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "generation_id": baseline["generation_id"],
        "data_generated_at_utc": baseline["data_generated_at_utc"],
        "valid_until_utc": baseline["valid_until_utc"],
        "artifact_file": artifact_path.name,
        "artifact_sha256": artifact_sha256,
        "artifact_payload_sha256": baseline["artifact_payload_sha256"],
        "source_board_sha256": hashes["source_board_sha256"],
        "publication_manifest_sha256": hashes["publication_manifest_sha256"],
        "live_convergence_audit_sha256": hashes["live_convergence_audit_sha256"],
        "selected_fields_sha256": hashes["selected_fields_sha256"],
        "ranking_contract_sha256": hashes["ranking_contract_sha256"],
    }
    return {**core, "pointer_payload_sha256": canonical_sha256(core)}


def validate_latest_accepted_pointer_envelope(pointer: dict[str, Any]) -> str:
    """Validate the sealed pointer itself and return its artifact SHA-256."""
    if not isinstance(pointer, dict):
        raise BaselineError("latest-accepted pointer is not an object")
    require_fields(pointer, LATEST_ACCEPTED_POINTER_FIELDS, "latest-accepted pointer")
    if (
        pointer["contract"] != LATEST_ACCEPTED_POINTER_CONTRACT
        or pointer["schema_version"] != SCHEMA_VERSION
    ):
        raise BaselineError("latest-accepted pointer contract is unsupported")
    core = {
        key: value for key, value in pointer.items()
        if key != "pointer_payload_sha256"
    }
    if pointer["pointer_payload_sha256"] != canonical_sha256(core):
        raise BaselineError("latest-accepted pointer payload hash mismatch")
    artifact_sha256 = require_hash(pointer["artifact_sha256"], "pointer artifact SHA-256")
    expected_name = f"{CONTRACT}.{artifact_sha256}.json"
    if pointer["artifact_file"] != expected_name:
        raise BaselineError("latest-accepted pointer artifact filename is invalid")
    return str(artifact_sha256)


def validate_latest_accepted_anchor(
    pointer: dict[str, Any],
    baseline: dict[str, Any],
    artifact_sha256: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate that a trusted pointer anchors these exact artifact bytes."""
    pointer_sha256 = validate_latest_accepted_pointer_envelope(pointer)
    actual_sha256 = require_hash(artifact_sha256, "observed artifact SHA-256")
    if pointer_sha256 != actual_sha256:
        raise BaselineError("latest-accepted pointer artifact SHA-256 mismatch")
    result = require_exportable_baseline(baseline, now=now)
    freshness = assess_baseline_freshness(baseline, now=now)
    hashes = baseline["hashes"]
    expected = {
        "generation_id": baseline["generation_id"],
        "data_generated_at_utc": baseline["data_generated_at_utc"],
        "valid_until_utc": baseline["valid_until_utc"],
        "artifact_payload_sha256": baseline["artifact_payload_sha256"],
        "source_board_sha256": hashes["source_board_sha256"],
        "publication_manifest_sha256": hashes["publication_manifest_sha256"],
        "live_convergence_audit_sha256": hashes["live_convergence_audit_sha256"],
        "selected_fields_sha256": hashes["selected_fields_sha256"],
        "ranking_contract_sha256": hashes["ranking_contract_sha256"],
    }
    for key, value in expected.items():
        if pointer[key] != value:
            raise BaselineError(f"latest-accepted pointer mismatch at {key}")
    return {
        **result,
        **freshness,
        "result": "RADAR_RANK_BASELINE_LATEST_ACCEPTED_V2_PASS",
        "artifact_sha256": actual_sha256,
        "pointer_payload_sha256": pointer["pointer_payload_sha256"],
    }


def validate_latest_accepted_pointer(
    pointer: dict[str, Any], output_dir: Path, *, now: datetime | None = None,
) -> dict[str, Any]:
    artifact_sha256 = validate_latest_accepted_pointer_envelope(pointer)
    artifact_path = output_dir / pointer["artifact_file"]
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise BaselineError("latest-accepted pointer artifact is unavailable")
    if file_sha256(artifact_path) != artifact_sha256:
        raise BaselineError("latest-accepted pointer artifact hash mismatch")
    baseline = load_object(artifact_path, TOP_FIELDS, "latest-accepted baseline artifact")
    return validate_latest_accepted_anchor(
        pointer, baseline, artifact_sha256, now=now
    )


def write_latest_accepted_pointer(
    path: Path, pointer: dict[str, Any], *, now: datetime | None = None,
) -> bool:
    require_fields(pointer, LATEST_ACCEPTED_POINTER_FIELDS, "latest-accepted pointer")
    if (
        pointer["contract"] != LATEST_ACCEPTED_POINTER_CONTRACT
        or pointer["schema_version"] != SCHEMA_VERSION
    ):
        raise BaselineError("refusing to write an unsupported latest-accepted pointer")
    core = {
        key: value for key, value in pointer.items()
        if key != "pointer_payload_sha256"
    }
    if pointer["pointer_payload_sha256"] != canonical_sha256(core):
        raise BaselineError("refusing to write an unsealed latest-accepted pointer")
    freshness = assess_freshness_window(
        pointer["data_generated_at_utc"],
        pointer["valid_until_utc"],
        now=now,
        label="latest-accepted pointer",
    )
    if freshness["freshness_status"] == "future":
        raise BaselineError("refusing a future latest-accepted pointer")
    data = canonical_bytes(pointer) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise BaselineError("latest-accepted pointer target is not a regular file")
        prior_raw = path.read_bytes()
        if prior_raw == data:
            return False
        prior = loads_strict(prior_raw, "existing latest-accepted pointer")
        if not isinstance(prior, dict):
            raise BaselineError("existing latest-accepted pointer is not an object")
        require_fields(prior, LATEST_ACCEPTED_POINTER_FIELDS, "existing latest-accepted pointer")
        if (
            prior["contract"] != LATEST_ACCEPTED_POINTER_CONTRACT
            or prior["schema_version"] != SCHEMA_VERSION
        ):
            raise BaselineError("existing latest-accepted pointer contract is unsupported")
        prior_core = {
            key: value for key, value in prior.items()
            if key != "pointer_payload_sha256"
        }
        if prior["pointer_payload_sha256"] != canonical_sha256(prior_core):
            raise BaselineError("existing latest-accepted pointer payload hash mismatch")
        prior_time = parse_utc(
            prior["data_generated_at_utc"], "existing pointer data timestamp"
        )
        candidate_time = parse_utc(
            pointer["data_generated_at_utc"], "candidate pointer data timestamp"
        )
        if candidate_time < prior_time:
            raise BaselineError("refusing to regress the latest-accepted baseline pointer")
        if candidate_time == prior_time:
            if pointer["generation_id"] != prior["generation_id"]:
                raise BaselineError("conflicting generation at latest-accepted pointer timestamp")
            raise BaselineError("latest-accepted generation differs from its prior seal")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
        fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def export_accepted_baseline(
    output_dir: Path,
    expected: dict[str, Any],
    *,
    latest_accepted_manifest: Path | None = None,
    now: datetime | None = None,
) -> tuple[
    dict[str, Any], Path, str, dict[str, Any] | None, bool | None, dict[str, Any]
]:
    """Write one accepted export while excluding concurrent retention."""
    exportability = require_exportable_baseline(expected, now=now)
    with exclusive_store_lock(output_dir, create_artifact_dir=True) as held:
        held.assert_current()
        path, digest = write_content_addressed(held.artifact_dir, expected)
        held.assert_current()
        pointer: dict[str, Any] | None = None
        pointer_updated: bool | None = None
        if latest_accepted_manifest is not None:
            pointer = build_latest_accepted_pointer(
                expected, path, digest, now=now
            )
            validate_latest_accepted_pointer(pointer, held.artifact_dir, now=now)
            held.assert_current()
            pointer_updated = write_latest_accepted_pointer(
                latest_accepted_manifest, pointer, now=now
            )
            held.assert_current()
        lock_evidence = {
            "path": str(held.lock_path),
            "device": held.lock_device,
            "inode": held.lock_inode,
            "exclusive": True,
        }
    return (
        exportability,
        path,
        digest,
        pointer,
        pointer_updated,
        lock_evidence,
    )


def _input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ranked-board", type=Path, required=True)
    parser.add_argument("--source-board", type=Path, required=True)
    parser.add_argument("--validation-report", type=Path, required=True)
    parser.add_argument("--publication-manifest", type=Path, required=True)
    parser.add_argument("--selection-audit", type=Path, required=True)
    parser.add_argument("--live-audit", type=Path, required=True)
    parser.add_argument("--source-policy", type=Path, required=True)
    parser.add_argument("--quarantine-manifest", type=Path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="build an accepted content-addressed baseline")
    _input_arguments(export)
    export.add_argument("--output-dir", type=Path, required=True)
    export.add_argument("--latest-accepted-manifest", type=Path)
    validate = commands.add_parser("validate", help="validate an artifact against all source evidence")
    _input_arguments(validate)
    validate.add_argument("--artifact", type=Path, required=True)
    anchor = validate.add_mutually_exclusive_group()
    anchor.add_argument("--trusted-artifact-sha256")
    anchor.add_argument("--trusted-latest-accepted-manifest", type=Path)
    validate.add_argument("--require-current-code-compatibility", action="store_true")
    return parser.parse_args(argv)


def _build_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return build_baseline(
        ranked_board_path=args.ranked_board,
        source_board_path=args.source_board,
        validation_report_path=args.validation_report,
        publication_manifest_path=args.publication_manifest,
        selection_audit_path=args.selection_audit,
        live_audit_path=args.live_audit,
        source_policy_path=args.source_policy,
        quarantine_manifest_path=args.quarantine_manifest,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        expected = _build_from_args(args)
        if args.command == "export":
            (
                exportability,
                path,
                digest,
                pointer,
                pointer_updated,
                lock_evidence,
            ) = export_accepted_baseline(
                args.output_dir,
                expected,
                latest_accepted_manifest=args.latest_accepted_manifest,
            )
            structure = validate_baseline_structure(expected)
            freshness = {
                key: exportability[key]
                for key in (
                    "freshness_status", "fresh", "evaluated_at_utc",
                    "data_generated_at_utc", "valid_until_utc", "age_seconds",
                )
            }
            print(json.dumps({
                **freshness,
                "result": "RADAR_RANK_BASELINE_V2_ACCEPTED",
                "structural_validation": structure["result"],
                "generation_id": structure["generation_id"],
                "published_verified_count": structure["published_verified_count"],
                "peer_stat_count": structure["peer_stat_count"],
                "artifact_payload_sha256": structure["artifact_payload_sha256"],
                "artifact_path": str(path),
                "artifact_sha256": digest,
                "latest_accepted_manifest_path": (
                    str(args.latest_accepted_manifest)
                    if args.latest_accepted_manifest is not None else None
                ),
                "latest_accepted_pointer_payload_sha256": (
                    pointer["pointer_payload_sha256"] if pointer is not None else None
                ),
                "latest_accepted_updated": pointer_updated,
                "store_lock": lock_evidence,
                "incremental_advancement": False,
            }, sort_keys=True))
            return 0
        observed = load_object(args.artifact, TOP_FIELDS, "baseline artifact")
        structure = validate_baseline_structure(observed)
        freshness = assess_baseline_freshness(observed)
        if canonical_bytes(reconstruction_payload(observed)) != canonical_bytes(
            reconstruction_payload(expected)
        ):
            raise BaselineError("artifact differs from the exact source-evidence reconstruction")
        artifact_sha256 = file_sha256(args.artifact)
        trusted_sha256: str | None = None
        anchor_status = "unanchored"
        if args.trusted_artifact_sha256 is not None:
            trusted_sha256 = str(
                require_hash(
                    args.trusted_artifact_sha256, "trusted artifact SHA-256"
                )
            )
            anchor_status = "trusted_sha256"
        elif args.trusted_latest_accepted_manifest is not None:
            pointer = load_object(
                args.trusted_latest_accepted_manifest,
                LATEST_ACCEPTED_POINTER_FIELDS,
                "trusted latest-accepted pointer",
            )
            validate_latest_accepted_anchor(
                pointer, observed, artifact_sha256
            )
            trusted_sha256 = pointer["artifact_sha256"]
            anchor_status = "latest_accepted_pointer"
        provenance = assess_code_provenance(
            observed,
            artifact_path=args.artifact if trusted_sha256 is not None else None,
            trusted_artifact_sha256=trusted_sha256,
        )
        if args.require_current_code_compatibility:
            require_current_code_compatibility(
                observed,
                artifact_path=args.artifact if trusted_sha256 is not None else None,
                trusted_artifact_sha256=trusted_sha256,
            )
        result = {
            **structure,
            **freshness,
            "result": "RADAR_RANK_BASELINE_V2_RECONSTRUCTION_PASS",
            "artifact_sha256": artifact_sha256,
            "artifact_anchor_status": anchor_status,
            "code_provenance_status": provenance["code_provenance_status"],
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BaselineError, OSError) as exc:
        print(f"RADAR_RANK_BASELINE_V2_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
