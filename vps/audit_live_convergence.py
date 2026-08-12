#!/usr/bin/env python3
"""Wait for an asynchronous Pages deployment, then audit its exact generation.

The remote payload is checked in two stages.  First its encrypted envelope and
self-bound generation metadata are validated.  An internally valid older
generation is deployment lag, not evidence of a ranking defect.  Only after the
remote generation equals the locally audited target do we compare every ID and
field against the current source board.  Generation-bound metadata that may
legitimately move meanwhile, such as the cumulative universe counter, is checked
against the sealed preparation manifest and its successful local audit rather
than a later SQLite observation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import audit_best_selection as selection_audit


EXIT_INVALID = 65
EXIT_DIVERGENCE = 66
EXIT_RETRY_EXHAUSTED = 75
HEX_16 = re.compile(r"^[0-9a-f]{16}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
MAX_DEADLINE_SEC = 86_400.0
MAX_BACKOFF_SEC = 3_600.0
MAX_REQUEST_TIMEOUT_SEC = 300.0
MAX_NETWORK_ERRORS = 1_000
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
DEFAULT_SELECTION_MANIFEST = Path(
    "/var/lib/sonardeals-radar/latest_selection_manifest.json"
)
DEFAULT_SELECTION_AUDIT = Path(
    "/var/lib/sonardeals-radar/latest_selection_audit.json"
)
SAFE_REMOTE_INVALID_REASONS = frozenset(
    {
        "algorithm_invalid",
        "contract_invalid",
        "candidate_digest_invalid",
        "candidate_field_digest_invalid",
        "data_timestamp_invalid",
        "decrypt_or_envelope_invalid",
        "expected_generation_invalid",
        "generation_invalid",
        "generation_unbound",
        "offers_invalid",
        "payload_empty",
        "payload_too_large",
        "request_bounds_invalid",
        "selection_digest_invalid",
        "selection_field_digest_invalid",
        "selection_field_digest_unbound",
        "selection_digest_unbound",
        "snapshot_digest_invalid",
    }
)
PASS_UINT_FIELDS = (
    "universe_unique_offers",
    "qualified_universe_offers",
    "published_offer_count",
    "verified_live_count",
    "coverage_quota_substitutions",
    "full_ranked_input_offers",
    "ranking_qualified_offers",
    "ranking_saved_offers",
    "ranking_saved_observed_offers",
    "cesja_count",
    "lease_like_count",
    "risk_listing_count",
    "confirmed_dead_count",
    "outside_better_than_cutoff",
    "confirmed_dead_or_lease_like_published",
    "unsupported_economics_published",
    "connected_country_count",
    "connected_source_count",
)
PASS_TRUE_FIELDS = (
    "exact_order_match",
    "exact_source_fields_match",
    "strict_global_top_n",
    "ranking_observed_partition_complete",
    "unique_ids",
    "unique_urls",
    "negative_control_pass",
    "risk_negative_control_pass",
    "positive_control_pass",
    "same_generation_verified_only",
)


class RemoteInvalid(Exception):
    """The endpoint responded, but not with a trustworthy radar artifact."""


class RemoteNetworkError(Exception):
    """The endpoint could not be read within the bounded transport policy."""


class ClockIntegrityError(Exception):
    """A monotonic clock sample was invalid or moved backwards."""


@dataclass(frozen=True)
class ProbeResult:
    state: str
    report: dict[str, Any]


@dataclass(frozen=True)
class SealedGenerationEvidence:
    generation_id: str
    universe_unique_offers: int
    candidate_ids_sha256: str
    selected_ids_sha256: str
    candidate_fields_sha256: str
    selected_fields_sha256: str
    snapshot_eligible_sha256: str
    data_generated_at_utc: str
    pass_report: dict[str, Any]


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("sealed evidence is not an object")
    return value


def load_sealed_generation_evidence(
    *,
    manifest_path: Path,
    audit_path: Path,
    expected_generation: str,
) -> SealedGenerationEvidence:
    """Load a mutually bound preparation manifest and successful local audit."""
    if not isinstance(expected_generation, str) or not HEX_16.fullmatch(
        expected_generation
    ):
        raise ValueError("expected generation invalid")
    manifest = load_json_object(manifest_path)
    audit = load_json_object(audit_path)
    if manifest.get("schema_version") != 1:
        raise ValueError("manifest schema invalid")
    if audit.get("result") != "BEST_SELECTION_AUDIT_PASS":
        raise ValueError("local audit did not pass")
    if (
        manifest.get("generation_id") != expected_generation
        or audit.get("generation_id") != expected_generation
    ):
        raise ValueError("sealed evidence generation mismatch")
    if (
        manifest.get("algorithm") != selection_audit.ALGORITHM
        or audit.get("algorithm") != selection_audit.ALGORITHM
    ):
        raise ValueError("sealed evidence algorithm mismatch")

    candidate_digest = manifest.get("candidate_ids_sha256")
    selected_digest = manifest.get("selected_ids_sha256")
    if (
        not isinstance(candidate_digest, str)
        or not HEX_64.fullmatch(candidate_digest)
        or audit.get("candidate_ids_sha256") != candidate_digest
        or not isinstance(selected_digest, str)
        or not HEX_64.fullmatch(selected_digest)
        or audit.get("selected_ids_sha256") != selected_digest
    ):
        raise ValueError("sealed evidence digest mismatch")
    candidate_fields_digest = manifest.get("candidate_fields_sha256")
    selected_fields_digest = manifest.get("selected_fields_sha256")
    snapshot_digest = manifest.get("snapshot_eligible_sha256")
    data_generated_at = manifest.get("data_generated_at_utc")
    if (
        not isinstance(candidate_fields_digest, str)
        or not HEX_64.fullmatch(candidate_fields_digest)
        or audit.get("candidate_fields_sha256") != candidate_fields_digest
        or not isinstance(selected_fields_digest, str)
        or not HEX_64.fullmatch(selected_fields_digest)
        or audit.get("selected_fields_sha256") != selected_fields_digest
        or not isinstance(snapshot_digest, str)
        or not HEX_64.fullmatch(snapshot_digest)
        or audit.get("snapshot_eligible_sha256") != snapshot_digest
        or not isinstance(data_generated_at, str)
        or not data_generated_at.strip()
        or len(data_generated_at) > 128
        or audit.get("data_generated_at_utc") != data_generated_at
    ):
        raise ValueError("sealed field evidence mismatch")
    bound_generation = hashlib.sha256(
        (
            f"{selection_audit.ALGORITHM}\n"
            f"{data_generated_at}\n"
            f"{candidate_fields_digest}\n"
            f"{selected_fields_digest}\n"
        ).encode("utf-8")
    ).hexdigest()[:16]
    if bound_generation != expected_generation:
        raise ValueError("sealed evidence generation is unbound")

    manifest_count = selection_audit.bounded_uint(
        manifest.get("universe_unique_offers"),
        "manifest universe_unique_offers",
    )
    audit_count = selection_audit.bounded_uint(
        audit.get("universe_unique_offers"),
        "audit universe_unique_offers",
    )
    if manifest_count != audit_count:
        raise ValueError("sealed universe counters disagree")
    return SealedGenerationEvidence(
        generation_id=expected_generation,
        universe_unique_offers=manifest_count,
        candidate_ids_sha256=candidate_digest,
        selected_ids_sha256=selected_digest,
        candidate_fields_sha256=candidate_fields_digest,
        selected_fields_sha256=selected_fields_digest,
        snapshot_eligible_sha256=snapshot_digest,
        data_generated_at_utc=data_generated_at,
        pass_report=sanitized_pass_report(
            audit,
            expected_generation=expected_generation,
        ),
    )


def validate_payload_against_sealed_evidence(
    payload: dict[str, Any],
    evidence: SealedGenerationEvidence,
) -> None:
    if payload.get("generation_id") != evidence.generation_id:
        raise AssertionError("payload generation does not match sealed evidence")
    if (
        payload.get("selection_candidate_sha256")
        != evidence.candidate_ids_sha256
        or payload.get("selected_ids_sha256") != evidence.selected_ids_sha256
        or payload.get("selection_candidate_fields_sha256")
        != evidence.candidate_fields_sha256
        or payload.get("selected_fields_sha256") != evidence.selected_fields_sha256
        or payload.get("data_generated_at_utc") != evidence.data_generated_at_utc
        or payload.get("snapshot_eligible_sha256") != evidence.snapshot_eligible_sha256
    ):
        raise AssertionError("payload selection digests do not match sealed evidence")
    remote_count = selection_audit.bounded_uint(
        payload.get("universe_unique_offers"),
        "remote universe_unique_offers",
    )
    if remote_count != evidence.universe_unique_offers:
        raise AssertionError("payload universe counter does not match sealed evidence")
    if (
        payload.get("schema_version") != selection_audit.CONTRACT_SCHEMA_VERSION
        or payload.get("algorithm") != selection_audit.ALGORITHM
        or payload.get("unsupported_economics_published") != 0
        or not isinstance(payload.get("selection"), dict)
        or payload["selection"].get("algeria_economics_included") is not False
    ):
        raise AssertionError("payload contract metadata differs from sealed evidence")
    for field in (
        "qualified_universe_offers", "published_offer_count", "verified_live_count",
        "connected_country_count", "connected_source_count",
    ):
        if payload.get(field) != evidence.pass_report.get(field):
            raise AssertionError("payload counters differ from sealed evidence")


def cache_busted_url(url: str, expected_generation: str, attempt: int) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(
        [
            ("sonardeals_generation", expected_generation),
            ("sonardeals_audit_attempt", str(attempt)),
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_blob(url: str, timeout_sec: float, max_payload_bytes: int) -> bytes:
    if (
        isinstance(timeout_sec, bool)
        or not isinstance(timeout_sec, (int, float))
        or not math.isfinite(float(timeout_sec))
        or not 0 < float(timeout_sec) <= MAX_REQUEST_TIMEOUT_SEC
        or type(max_payload_bytes) is not int
        or not 0 < max_payload_bytes <= MAX_PAYLOAD_BYTES
    ):
        raise RemoteInvalid("request_bounds_invalid")
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            blob = response.read(max_payload_bytes + 1)
    except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as error:
        raise RemoteNetworkError(type(error).__name__) from error
    if len(blob) > max_payload_bytes:
        raise RemoteInvalid("payload_too_large")
    if not blob:
        raise RemoteInvalid("payload_empty")
    return blob


def validated_generation(payload: dict[str, Any]) -> str:
    algorithm = payload.get("selection_algorithm")
    candidate_hash = payload.get("selection_candidate_sha256")
    selected_hash = payload.get("selected_ids_sha256")
    candidate_fields_hash = payload.get("selection_candidate_fields_sha256")
    selected_fields_hash = payload.get("selected_fields_sha256")
    data_generated_at = payload.get("data_generated_at_utc")
    generation = payload.get("generation_id")
    offers = payload.get("offers")
    snapshot_digest = payload.get("snapshot_eligible_sha256")
    if (
        payload.get("schema_version") != selection_audit.CONTRACT_SCHEMA_VERSION
        or payload.get("algorithm") != selection_audit.ALGORITHM
        or payload.get("unsupported_economics_published") != 0
    ):
        raise RemoteInvalid("contract_invalid")
    if algorithm != selection_audit.ALGORITHM:
        raise RemoteInvalid("algorithm_invalid")
    if not isinstance(candidate_hash, str) or not HEX_64.fullmatch(candidate_hash):
        raise RemoteInvalid("candidate_digest_invalid")
    if not isinstance(selected_hash, str) or not HEX_64.fullmatch(selected_hash):
        raise RemoteInvalid("selection_digest_invalid")
    if not isinstance(candidate_fields_hash, str) or not HEX_64.fullmatch(candidate_fields_hash):
        raise RemoteInvalid("candidate_field_digest_invalid")
    if not isinstance(selected_fields_hash, str) or not HEX_64.fullmatch(selected_fields_hash):
        raise RemoteInvalid("selection_field_digest_invalid")
    if (
        not isinstance(data_generated_at, str)
        or not data_generated_at.strip()
        or len(data_generated_at) > 128
    ):
        raise RemoteInvalid("data_timestamp_invalid")
    if not isinstance(generation, str) or not HEX_16.fullmatch(generation):
        raise RemoteInvalid("generation_invalid")
    if not isinstance(offers, list) or not offers:
        raise RemoteInvalid("offers_invalid")
    if not isinstance(snapshot_digest, str) or not HEX_64.fullmatch(snapshot_digest):
        raise RemoteInvalid("snapshot_digest_invalid")
    if selection_audit.digest(offers) != selected_hash:
        raise RemoteInvalid("selection_digest_unbound")
    if selection_audit.digest_fields(offers) != selected_fields_hash:
        raise RemoteInvalid("selection_field_digest_unbound")
    bound_generation = hashlib.sha256(
        (
            f"{algorithm}\n{data_generated_at}\n"
            f"{candidate_fields_hash}\n{selected_fields_hash}\n"
        ).encode("utf-8")
    ).hexdigest()[:16]
    if generation != bound_generation:
        raise RemoteInvalid("generation_unbound")
    return generation


def sanitized_pass_report(
    report: dict[str, Any],
    *,
    expected_generation: str,
) -> dict[str, Any]:
    """Construct the complete PASS receipt from a strict allowlist."""
    if not isinstance(report, dict):
        raise ValueError("pass report invalid")
    if report.get("result") != "BEST_SELECTION_AUDIT_PASS":
        raise ValueError("pass result invalid")
    if report.get("generation_id") != expected_generation:
        raise ValueError("pass generation invalid")
    if report.get("algorithm") != selection_audit.ALGORITHM:
        raise ValueError("pass algorithm invalid")
    for field in (
        "candidate_ids_sha256", "selected_ids_sha256",
        "candidate_fields_sha256", "selected_fields_sha256",
        "snapshot_eligible_sha256",
    ):
        value = report.get(field)
        if not isinstance(value, str) or not HEX_64.fullmatch(value):
            raise ValueError("pass digest invalid")
    safe: dict[str, Any] = {
        "result": "LIVE_GENERATION_AUDIT_PASS",
        "generation_id": expected_generation,
        "algorithm": selection_audit.ALGORITHM,
        "candidate_ids_sha256": report["candidate_ids_sha256"],
        "selected_ids_sha256": report["selected_ids_sha256"],
        "candidate_fields_sha256": report["candidate_fields_sha256"],
        "selected_fields_sha256": report["selected_fields_sha256"],
        "snapshot_eligible_sha256": report["snapshot_eligible_sha256"],
        "data_generated_at_utc": report["data_generated_at_utc"],
    }
    for field in PASS_UINT_FIELDS:
        upper = (
            selection_audit.MAX_CONNECTED_COUNT
            if field in {"connected_country_count", "connected_source_count"}
            else selection_audit.MAX_RECEIPT_COUNT
        )
        safe[field] = selection_audit.bounded_uint(
            report.get(field),
            field,
            upper=upper,
        )
    for field in PASS_TRUE_FIELDS:
        if report.get(field) is not True:
            raise ValueError("pass boolean invalid")
        safe[field] = True
    for field in (
        "coverage_quota_substitutions",
        "cesja_count",
        "lease_like_count",
        "risk_listing_count",
        "confirmed_dead_count",
        "outside_better_than_cutoff",
        "confirmed_dead_or_lease_like_published",
        "unsupported_economics_published",
    ):
        if safe[field] != 0:
            raise ValueError("pass zero invariant invalid")
    safe["expected_generation"] = expected_generation
    safe["observed_generation"] = expected_generation
    return safe


def safe_pending_report(
    report: dict[str, Any],
    *,
    expected_generation: str,
) -> dict[str, Any] | None:
    observed = report.get("observed_generation") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or report.get("result") != "DEPLOYMENT_PENDING"
        or report.get("expected_generation") != expected_generation
        or not isinstance(observed, str)
        or not HEX_16.fullmatch(observed)
        or observed == expected_generation
    ):
        return None
    return {
        "result": "DEPLOYMENT_PENDING",
        "expected_generation": expected_generation,
        "observed_generation": observed,
    }


def finite_number(value: Any, *, lower: float, upper: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and lower <= float(value) <= upper
    )


def valid_wait_bounds(
    *,
    expected_generation: str,
    deadline_sec: Any,
    initial_backoff_sec: Any,
    max_backoff_sec: Any,
    max_network_errors: Any,
) -> bool:
    return (
        isinstance(expected_generation, str)
        and bool(HEX_16.fullmatch(expected_generation))
        and finite_number(deadline_sec, lower=0.000001, upper=MAX_DEADLINE_SEC)
        and finite_number(initial_backoff_sec, lower=0.000001, upper=MAX_BACKOFF_SEC)
        and finite_number(max_backoff_sec, lower=0.000001, upper=MAX_BACKOFF_SEC)
        and float(max_backoff_sec) >= float(initial_backoff_sec)
        and type(max_network_errors) is int
        and 0 <= max_network_errors <= MAX_NETWORK_ERRORS
    )


def classify_blob(
    *,
    blob: bytes,
    pin: str,
    expected_generation: str,
    root: Path,
    top_n: int,
    per_country_min: int,
    per_source_min: int,
    sealed_evidence: SealedGenerationEvidence,
) -> ProbeResult:
    del root, top_n, per_country_min, per_source_min
    if not isinstance(expected_generation, str) or not HEX_16.fullmatch(expected_generation):
        raise RemoteInvalid("expected_generation_invalid")
    try:
        payload = selection_audit.decrypt_blob(blob, pin)
    except Exception as error:  # cryptography and gzip/json expose several types
        raise RemoteInvalid("decrypt_or_envelope_invalid") from error
    observed_generation = validated_generation(payload)
    if observed_generation != expected_generation:
        # Deliberately do not report offer IDs, ranking indices, or field diffs.
        return ProbeResult(
            "pending",
            {
                "result": "DEPLOYMENT_PENDING",
                "expected_generation": expected_generation,
                "observed_generation": observed_generation,
            },
        )
    try:
        validate_payload_against_sealed_evidence(payload, sealed_evidence)
    except (AssertionError, KeyError, TypeError, ValueError):
        return ProbeResult(
            "fatal",
            {
                "result": "SAME_GENERATION_DIVERGENCE",
                "expected_generation": expected_generation,
                "observed_generation": observed_generation,
                "failure_class": "audit_divergence",
            },
        )
    try:
        safe_report = sanitized_pass_report(
            {
                **sealed_evidence.pass_report,
                "result": "BEST_SELECTION_AUDIT_PASS",
            },
            expected_generation=expected_generation,
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        return ProbeResult(
            "fatal",
            {
                "result": "SAME_GENERATION_DIVERGENCE",
                "expected_generation": expected_generation,
                "observed_generation": observed_generation,
                "failure_class": "pass_schema_invalid",
            },
        )
    return ProbeResult("pass", safe_report)


def wait_for_convergence(
    *,
    expected_generation: str,
    probe: Callable[[int, float], ProbeResult],
    deadline_sec: float,
    initial_backoff_sec: float,
    max_backoff_sec: float,
    max_network_errors: int,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, Any]]:
    attempt = 0
    network_errors = 0
    last_pending: dict[str, Any] | None = None
    last_clock: float | None = None
    started: float | None = None

    if not valid_wait_bounds(
        expected_generation=expected_generation,
        deadline_sec=deadline_sec,
        initial_backoff_sec=initial_backoff_sec,
        max_backoff_sec=max_backoff_sec,
        max_network_errors=max_network_errors,
    ):
        return EXIT_INVALID, {
            "result": "WAIT_BOUNDS_INVALID",
            "attempts": 0,
            "network_errors": 0,
        }

    deadline_value = float(deadline_sec)
    initial_backoff_value = float(initial_backoff_sec)
    max_backoff_value = float(max_backoff_sec)

    def sample_clock() -> float:
        nonlocal last_clock
        raw = clock()
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or float(raw) < 0
        ):
            raise ClockIntegrityError
        value = float(raw)
        if last_clock is not None and value < last_clock:
            raise ClockIntegrityError
        last_clock = value
        return value

    def clock_failure() -> tuple[int, dict[str, Any]]:
        elapsed = 0.0
        if started is not None and last_clock is not None:
            elapsed = max(0.0, last_clock - started)
        return EXIT_INVALID, {
            "result": "CLOCK_INTEGRITY_FAILURE",
            "expected_generation": expected_generation,
            "attempts": attempt,
            "network_errors": network_errors,
            "deadline_sec": deadline_value,
            "elapsed_sec": round(elapsed, 6),
        }

    try:
        started = sample_clock()
    except ClockIntegrityError:
        return clock_failure()
    deadline = started + deadline_value
    if not math.isfinite(deadline):
        return clock_failure()
    backoff = initial_backoff_value

    def terminal(
        code: int,
        report: dict[str, Any],
        *,
        now: float | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if now is None:
            try:
                now = sample_clock()
            except ClockIntegrityError:
                return clock_failure()
        final = dict(report)
        final.update(
            {
                "attempts": attempt,
                "network_errors": network_errors,
                "deadline_sec": deadline_value,
                "elapsed_sec": round(max(0.0, now - started), 6),
            }
        )
        return code, final

    def deadline_expired(now: float) -> tuple[int, dict[str, Any]]:
        if last_pending is not None:
            return terminal(
                EXIT_RETRY_EXHAUSTED,
                {**last_pending, "result": "DEPLOYMENT_PENDING_TIMEOUT"},
                now=now,
            )
        return terminal(
            EXIT_RETRY_EXHAUSTED,
            {
                "result": "NETWORK_DEADLINE_EXCEEDED",
                "expected_generation": expected_generation,
            },
            now=now,
        )

    while True:
        try:
            now = sample_clock()
        except ClockIntegrityError:
            return clock_failure()
        remaining = deadline - now
        if remaining <= 0:
            return deadline_expired(now)
        attempt += 1
        try:
            result = probe(attempt, remaining)
        except RemoteNetworkError:
            network_errors += 1
            if network_errors > max_network_errors:
                return terminal(
                    EXIT_RETRY_EXHAUSTED,
                    {
                        "result": "NETWORK_RETRY_LIMIT",
                        "expected_generation": expected_generation,
                    },
                )
        except RemoteInvalid as error:
            reason = str(error)
            if reason not in SAFE_REMOTE_INVALID_REASONS:
                reason = "remote_invalid"
            return terminal(
                EXIT_INVALID,
                {
                    "result": "REMOTE_PAYLOAD_INVALID",
                    "expected_generation": expected_generation,
                    "reason": reason,
                },
            )
        else:
            # A response that completes after the deadline cannot extend the
            # bounded wait. A response completing exactly at the boundary is
            # accepted, but a pending result at that boundary times out below.
            try:
                now = sample_clock()
            except ClockIntegrityError:
                return clock_failure()
            if now > deadline:
                return deadline_expired(now)
            if result.state == "pass":
                try:
                    safe_pass = sanitized_pass_report(
                        {
                            **result.report,
                            "result": "BEST_SELECTION_AUDIT_PASS",
                        },
                        expected_generation=expected_generation,
                    )
                except (AssertionError, KeyError, TypeError, ValueError):
                    return terminal(
                        EXIT_INVALID,
                        {
                            "result": "PROBE_CONTRACT_INVALID",
                            "expected_generation": expected_generation,
                        },
                        now=now,
                    )
                return terminal(0, safe_pass, now=now)
            if result.state == "fatal":
                observed = (
                    result.report.get("observed_generation")
                    if isinstance(result.report, dict)
                    else None
                )
                if (
                    not isinstance(result.report, dict)
                    or result.report.get("result") != "SAME_GENERATION_DIVERGENCE"
                    or result.report.get("expected_generation") != expected_generation
                    or observed != expected_generation
                ):
                    return terminal(
                        EXIT_INVALID,
                        {
                            "result": "PROBE_CONTRACT_INVALID",
                            "expected_generation": expected_generation,
                        },
                        now=now,
                    )
                return terminal(
                    EXIT_DIVERGENCE,
                    {
                        "result": "SAME_GENERATION_DIVERGENCE",
                        "expected_generation": expected_generation,
                        "observed_generation": expected_generation,
                        "failure_class": "audit_divergence",
                    },
                    now=now,
                )
            if result.state != "pending":
                return terminal(
                    EXIT_INVALID,
                    {
                        "result": "PROBE_CONTRACT_INVALID",
                        "expected_generation": expected_generation,
                    },
                    now=now,
                )
            last_pending = safe_pending_report(
                result.report,
                expected_generation=expected_generation,
            )
            if last_pending is None:
                return terminal(
                    EXIT_INVALID,
                    {
                        "result": "PROBE_CONTRACT_INVALID",
                        "expected_generation": expected_generation,
                    },
                    now=now,
                )

        try:
            now = sample_clock()
        except ClockIntegrityError:
            return clock_failure()
        remaining = deadline - now
        if remaining <= 0:
            return deadline_expired(now)
        delay = min(backoff, max_backoff_value, remaining)
        sleeper(delay)
        backoff = min(max_backoff_value, backoff * 2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/home/krt/car_deal_finder"))
    parser.add_argument("--pin", type=Path, default=Path("/etc/sonardeals-radar/pin"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-url", required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument(
        "--selection-manifest",
        type=Path,
        default=DEFAULT_SELECTION_MANIFEST,
    )
    parser.add_argument(
        "--selection-audit",
        type=Path,
        default=DEFAULT_SELECTION_AUDIT,
    )
    parser.add_argument("--top-n", type=int, default=10_000)
    parser.add_argument("--per-country-min", type=int, default=20)
    parser.add_argument("--per-source-min", type=int, default=5)
    parser.add_argument("--deadline-sec", type=float, default=1_800)
    parser.add_argument("--request-timeout-sec", type=float, default=30)
    parser.add_argument("--initial-backoff-sec", type=float, default=5)
    parser.add_argument("--max-backoff-sec", type=float, default=60)
    parser.add_argument("--max-network-errors", type=int, default=8)
    parser.add_argument("--max-payload-bytes", type=int, default=64 * 1024 * 1024)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not HEX_16.fullmatch(args.expected_generation):
        raise SystemExit("expected generation must be 16 lowercase hexadecimal characters")
    if (
        not valid_wait_bounds(
            expected_generation=args.expected_generation,
            deadline_sec=args.deadline_sec,
            initial_backoff_sec=args.initial_backoff_sec,
            max_backoff_sec=args.max_backoff_sec,
            max_network_errors=args.max_network_errors,
        )
        or not finite_number(
            args.request_timeout_sec,
            lower=0.000001,
            upper=MAX_REQUEST_TIMEOUT_SEC,
        )
        or type(args.max_payload_bytes) is not int
        or not 0 < args.max_payload_bytes <= MAX_PAYLOAD_BYTES
    ):
        raise SystemExit("invalid convergence bounds")
    pin = selection_audit.load_dashboard_pin(args.pin)
    try:
        sealed_evidence = load_sealed_generation_evidence(
            manifest_path=args.selection_manifest,
            audit_path=args.selection_audit,
            expected_generation=args.expected_generation,
        )
    except (AssertionError, OSError, TypeError, ValueError) as error:
        raise SystemExit("same-generation sealed evidence is invalid") from error

    def probe(attempt: int, remaining: float) -> ProbeResult:
        timeout = min(args.request_timeout_sec, max(0.1, remaining or 0.1))
        blob = fetch_blob(
            cache_busted_url(args.data_url, args.expected_generation, attempt),
            timeout,
            args.max_payload_bytes,
        )
        return classify_blob(
            blob=blob,
            pin=pin,
            expected_generation=args.expected_generation,
            root=args.root,
            top_n=args.top_n,
            per_country_min=args.per_country_min,
            per_source_min=args.per_source_min,
            sealed_evidence=sealed_evidence,
        )

    exit_code, report = wait_for_convergence(
        expected_generation=args.expected_generation,
        probe=probe,
        deadline_sec=args.deadline_sec,
        initial_backoff_sec=args.initial_backoff_sec,
        max_backoff_sec=args.max_backoff_sec,
        max_network_errors=args.max_network_errors,
    )
    selection_audit.atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
