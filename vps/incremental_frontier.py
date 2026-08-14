#!/usr/bin/env python3
"""Fail-closed frontier state for strict-newest incremental source lanes.

This module performs no network access and does not schedule work.  A caller must
provide an explicitly allowlisted ``(source, partition, sort-contract hash)`` and
an ordered page iterator.  Raw native IDs are remembered even when a listing is
filtered out downstream, so two fully known pages are meaningful evidence that
the source frontier has been reached.

Frontier rows live in the same SQLite database as the target offers.  The caller
can therefore commit observed offers, the frontier, and the run receipt in one
transaction.  A state digest and optimistic revision check make corruption,
contract drift, and concurrent writers fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping


SCHEMA_VERSION = 1
STOP_AFTER_KNOWN_PAGES = 2
MAX_ALLOWED_PAGES = 1_000
MAX_FRONTIER_IDS = 1_000_000
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
KEY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")


class FrontierError(RuntimeError):
    """Base class for fail-closed incremental-frontier errors."""


class ContractError(FrontierError):
    """The caller did not supply an allowlisted strict-newest contract."""


class ContractDriftError(FrontierError):
    """A source/partition already has state under a different sort contract."""


class StateCorruptionError(FrontierError):
    """Persisted frontier metadata and rows do not agree."""


class PageContractError(FrontierError):
    """A source page is incomplete, unordered, or otherwise unsafe."""


class FrontierBoundaryNotReached(PageContractError):
    """The bounded probe ended without reaching known history or source end."""


class ConcurrentFrontierUpdate(FrontierError):
    """Another writer advanced the frontier after the read snapshot."""


@dataclass(frozen=True, order=True)
class ContractKey:
    source_key: str
    partition_key: str
    sort_contract_sha256: str


@dataclass(frozen=True)
class SourceContract:
    """Caller-owned capability declaration for one strict-newest partition."""

    source_key: str
    partition_key: str
    sort_contract_sha256: str
    max_pages: int
    frontier_cap: int
    strict_newest: bool = True
    stop_after_known_pages: int = STOP_AFTER_KNOWN_PAGES

    @property
    def key(self) -> ContractKey:
        return ContractKey(
            self.source_key,
            self.partition_key,
            self.sort_contract_sha256,
        )

    def validate(self, allowlist: frozenset[ContractKey]) -> None:
        if (
            not KEY_PART.fullmatch(self.source_key)
            or not KEY_PART.fullmatch(self.partition_key)
            or not HEX_64.fullmatch(self.sort_contract_sha256)
        ):
            raise ContractError("incremental contract identity is invalid")
        if self.strict_newest is not True:
            raise ContractError("incremental source is not declared strict-newest")
        if self.key not in allowlist:
            raise ContractError("incremental source contract is not allowlisted")
        if self.stop_after_known_pages != STOP_AFTER_KNOWN_PAGES:
            raise ContractError("stop-at-known policy must require exactly two pages")
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= MAX_ALLOWED_PAGES:
            raise ContractError("max_pages is outside the bounded policy")
        if (
            type(self.frontier_cap) is not int
            or not 1 <= self.frontier_cap <= MAX_FRONTIER_IDS
        ):
            raise ContractError("frontier_cap is outside the bounded policy")


@dataclass(frozen=True)
class FrontierEntry:
    native_id: str
    sort_value: int
    source_listing_id: str | None
    seen_sequence: int


@dataclass(frozen=True)
class FrontierSnapshot:
    contract: SourceContract
    revision: int | None
    newest_sort_value: int | None
    next_sequence: int
    entries: Mapping[str, FrontierEntry]
    state_sha256: str | None


@dataclass(frozen=True)
class PageItem:
    """One item in the source's strict total order.

    ``sort_value`` must be unique within the partition and strictly decrease
    across the page stream.  An adapter whose API has timestamp ties must
    derive a contract-bound composite integer matching the API's documented
    stable tie-break order; a timestamp alone is not sufficient.
    """

    native_id: str
    sort_value: int
    offer: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SourcePage:
    number: int
    items: tuple[PageItem, ...]


@dataclass(frozen=True)
class FrontierPlan:
    snapshot: FrontierSnapshot
    processed_pages: int
    raw_item_count: int
    new_native_ids: tuple[str, ...]
    observed_items: tuple[PageItem, ...]
    resulting_entries: Mapping[str, FrontierEntry]
    next_sequence: int
    stop_reason: str
    input_sha256: str


FRONTIER_SCHEMA = """
CREATE TABLE IF NOT EXISTS radar_incremental_frontiers (
  source_key TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  sort_contract_sha256 TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  newest_sort_value INTEGER,
  next_sequence INTEGER NOT NULL,
  frontier_count INTEGER NOT NULL,
  frontier_sha256 TEXT NOT NULL,
  last_run_id TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY (source_key, partition_key, sort_contract_sha256)
);
CREATE TABLE IF NOT EXISTS radar_incremental_frontier_ids (
  source_key TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  sort_contract_sha256 TEXT NOT NULL,
  native_id TEXT NOT NULL,
  sort_value INTEGER NOT NULL,
  source_listing_id TEXT,
  seen_sequence INTEGER NOT NULL,
  PRIMARY KEY (source_key, partition_key, sort_contract_sha256, native_id),
  FOREIGN KEY (source_key, partition_key, sort_contract_sha256)
    REFERENCES radar_incremental_frontiers
      (source_key, partition_key, sort_contract_sha256)
    ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_radar_incremental_frontier_sequence
  ON radar_incremental_frontier_ids
    (source_key, partition_key, sort_contract_sha256, seen_sequence);
"""


def ensure_frontier_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(FRONTIER_SCHEMA)


def _canonical_entries_sha256(entries: Mapping[str, FrontierEntry]) -> str:
    digest = hashlib.sha256()
    for native_id in sorted(entries):
        entry = entries[native_id]
        payload = [
            entry.native_id,
            entry.sort_value,
            entry.source_listing_id,
            entry.seen_sequence,
        ]
        digest.update(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _contract_params(contract: SourceContract) -> tuple[str, str, str]:
    return (
        contract.source_key,
        contract.partition_key,
        contract.sort_contract_sha256,
    )


def _is_canonical_listing_id(value: object) -> bool:
    """Match ingest normalization before using an offer identity as a key."""

    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 100_000
        and value == " ".join(value.split())
    )


def load_frontier(
    connection: sqlite3.Connection,
    contract: SourceContract,
    allowlist: frozenset[ContractKey],
) -> FrontierSnapshot:
    """Load and verify one frontier without mutating the target database."""

    contract.validate(allowlist)
    hashes = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT sort_contract_sha256
              FROM radar_incremental_frontiers
             WHERE source_key=? AND partition_key=?
            """,
            (contract.source_key, contract.partition_key),
        )
    }
    if hashes and hashes != {contract.sort_contract_sha256}:
        raise ContractDriftError(
            "source partition already has a different sort-contract hash"
        )

    meta = connection.execute(
        """
        SELECT schema_version, revision, newest_sort_value, next_sequence,
               frontier_count, frontier_sha256
          FROM radar_incremental_frontiers
         WHERE source_key=? AND partition_key=? AND sort_contract_sha256=?
        """,
        _contract_params(contract),
    ).fetchone()
    rows = connection.execute(
        """
        SELECT native_id, sort_value, source_listing_id, seen_sequence
          FROM radar_incremental_frontier_ids
         WHERE source_key=? AND partition_key=? AND sort_contract_sha256=?
         ORDER BY native_id
        """,
        _contract_params(contract),
    ).fetchall()
    if meta is None:
        if rows:
            raise StateCorruptionError("frontier IDs exist without frontier metadata")
        return FrontierSnapshot(contract, None, None, 1, {}, None)

    schema_version, revision, newest, next_sequence, expected_count, expected_hash = meta
    if (
        schema_version != SCHEMA_VERSION
        or type(revision) is not int
        or revision < 1
        or (newest is not None and type(newest) is not int)
        or type(next_sequence) is not int
        or next_sequence < 1
        or type(expected_count) is not int
        or expected_count < 0
        or not isinstance(expected_hash, str)
        or not HEX_64.fullmatch(expected_hash)
    ):
        raise StateCorruptionError("frontier metadata is invalid")

    entries: dict[str, FrontierEntry] = {}
    for native_id, sort_value, source_listing_id, seen_sequence in rows:
        if (
            not isinstance(native_id, str)
            or not native_id
            or len(native_id) > 500
            or type(sort_value) is not int
            or not 0 <= sort_value <= 9_223_372_036_854_775_807
            or (
                source_listing_id is not None
                and not _is_canonical_listing_id(source_listing_id)
            )
            or type(seen_sequence) is not int
            or not 1 <= seen_sequence <= 9_223_372_036_854_775_807
        ):
            raise StateCorruptionError("frontier row is invalid")
        entries[native_id] = FrontierEntry(
            native_id, sort_value, source_listing_id, seen_sequence
        )

    actual_hash = _canonical_entries_sha256(entries)
    actual_newest = max((entry.sort_value for entry in entries.values()), default=None)
    actual_next = max((entry.seen_sequence for entry in entries.values()), default=0) + 1
    if (
        len(entries) != expected_count
        or len({entry.sort_value for entry in entries.values()}) != len(entries)
        or len({entry.seen_sequence for entry in entries.values()}) != len(entries)
        or len({
            entry.source_listing_id
            for entry in entries.values()
            if entry.source_listing_id is not None
        }) != sum(
            entry.source_listing_id is not None for entry in entries.values()
        )
        or actual_hash != expected_hash
        or actual_newest != newest
        or next_sequence != actual_next
        or len(entries) > contract.frontier_cap
    ):
        raise StateCorruptionError("frontier digest, count, or watermark is invalid")
    return FrontierSnapshot(
        contract,
        revision,
        newest,
        next_sequence,
        entries,
        expected_hash,
    )


def _coerce_item(raw: PageItem | Mapping[str, Any]) -> PageItem:
    if isinstance(raw, PageItem):
        item = raw
    elif isinstance(raw, Mapping):
        item = PageItem(
            native_id=raw.get("native_id"),  # type: ignore[arg-type]
            sort_value=raw.get("sort_value"),  # type: ignore[arg-type]
            offer=raw.get("offer"),  # type: ignore[arg-type]
        )
    else:
        raise PageContractError("page item is not an object")
    if (
        not isinstance(item.native_id, str)
        or not item.native_id
        or len(item.native_id) > 500
        or type(item.sort_value) is not int
        or item.sort_value < 0
        or item.sort_value > 9_223_372_036_854_775_807
        or (item.offer is not None and not isinstance(item.offer, Mapping))
    ):
        raise PageContractError("page item identity, sort value, or offer is invalid")
    return item


def _coerce_page(raw: SourcePage | Mapping[str, Any]) -> SourcePage:
    if isinstance(raw, SourcePage):
        page = raw
    elif isinstance(raw, Mapping):
        items = raw.get("items")
        if not isinstance(items, (list, tuple)):
            raise PageContractError("source page items are invalid")
        page = SourcePage(
            number=raw.get("number"),  # type: ignore[arg-type]
            items=tuple(_coerce_item(item) for item in items),
        )
    else:
        raise PageContractError("source page is not an object")
    if type(page.number) is not int or page.number < 1:
        raise PageContractError("source page number is invalid")
    return SourcePage(page.number, tuple(_coerce_item(item) for item in page.items))


def _processed_input_sha256(pages: list[SourcePage]) -> str:
    canonical: list[dict[str, Any]] = []
    for page in pages:
        canonical.append(
            {
                "number": page.number,
                "items": [
                    {
                        "native_id": item.native_id,
                        "sort_value": item.sort_value,
                        "offer": item.offer,
                    }
                    for item in page.items
                ],
            }
        )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_frontier(
    snapshot: FrontierSnapshot,
    pages: Iterable[SourcePage | Mapping[str, Any]],
) -> FrontierPlan:
    """Consume a bounded page stream and stop at two fully pre-run-known pages.

    An iterator ending before a terminal empty page, a caught-up frontier, or the
    configured page cap is treated as an incomplete fetch and fails closed.
    """

    iterator: Iterator[SourcePage | Mapping[str, Any]] = iter(pages)
    baseline = snapshot.entries
    resulting = dict(baseline)
    sequence = snapshot.next_sequence
    processed: list[SourcePage] = []
    observed_items: list[PageItem] = []
    new_ids: list[str] = []
    new_id_set: set[str] = set()
    processed_ids: set[str] = set()
    sort_owners = {
        entry.sort_value: entry.native_id for entry in baseline.values()
    }
    listing_owners = {
        entry.source_listing_id: entry.native_id
        for entry in baseline.values()
        if entry.source_listing_id is not None
    }
    known_streak = 0
    previous_sort: int | None = None
    stop_reason = ""

    for expected_page in range(1, snapshot.contract.max_pages + 1):
        try:
            page = _coerce_page(next(iterator))
        except StopIteration as error:
            raise PageContractError("source page stream ended before a safe stop") from error
        if page.number != expected_page:
            raise PageContractError("source pages are not consecutive from page one")
        processed.append(page)
        if not page.items:
            stop_reason = "source_exhausted"
            break

        page_has_previously_unknown = False
        page_ids: set[str] = set()
        for item in page.items:
            if previous_sort is not None and item.sort_value >= previous_sort:
                raise PageContractError(
                    "strict-newest sort values are not a unique decreasing order"
                )
            previous_sort = item.sort_value
            if item.native_id in page_ids:
                raise PageContractError("a source page repeats a native ID")
            page_ids.add(item.native_id)
            if item.native_id in processed_ids:
                raise PageContractError("source pages overlap on a native ID")
            processed_ids.add(item.native_id)

            sort_owner = sort_owners.get(item.sort_value)
            if sort_owner is not None and sort_owner != item.native_id:
                raise PageContractError(
                    "a stable sort value maps to multiple native IDs"
                )
            sort_owners[item.sort_value] = item.native_id

            old = baseline.get(item.native_id)
            if old is not None:
                if old.sort_value != item.sort_value:
                    raise PageContractError("a known native ID changed its stable sort value")
                incoming_listing_id = None
                if item.offer is not None:
                    incoming_listing_id = item.offer.get("source_listing_id")
                    if not _is_canonical_listing_id(incoming_listing_id):
                        raise PageContractError("accepted offer identity is invalid")
                if (
                    old.source_listing_id is not None
                    and incoming_listing_id is not None
                    and old.source_listing_id != incoming_listing_id
                ):
                    raise PageContractError("a native ID changed its offer identity")
            else:
                page_has_previously_unknown = True
                if item.native_id not in new_id_set:
                    new_id_set.add(item.native_id)
                    new_ids.append(item.native_id)

            current = resulting.get(item.native_id)
            listing_id = current.source_listing_id if current is not None else None
            if item.offer is not None:
                incoming = item.offer.get("source_listing_id")
                if not _is_canonical_listing_id(incoming):
                    raise PageContractError("accepted offer identity is invalid")
                if listing_id is not None and listing_id != incoming:
                    raise PageContractError("a native ID maps to multiple offer identities")
                listing_owner = listing_owners.get(incoming)
                if listing_owner is not None and listing_owner != item.native_id:
                    raise PageContractError(
                        "an offer identity maps to multiple native IDs"
                    )
                listing_owners[incoming] = item.native_id
                listing_id = incoming
            resulting[item.native_id] = FrontierEntry(
                item.native_id,
                item.sort_value,
                listing_id,
                sequence,
            )
            sequence += 1
            observed_items.append(item)

        known_streak = 0 if page_has_previously_unknown else known_streak + 1
        if known_streak >= STOP_AFTER_KNOWN_PAGES:
            stop_reason = "known_frontier_reached"
            break
    else:
        # IDs seen in a capped partial bootstrap are not a proven frontier.  If
        # persisted, the next run could mistake those pages for old history and
        # stop before an as-yet-unseen deeper page.  Fail the run atomically.
        raise FrontierBoundaryNotReached(
            "known frontier or source end was not reached within max_pages"
        )

    if not stop_reason:
        raise PageContractError("source page stream did not produce a safe stop")

    if len(resulting) > snapshot.contract.frontier_cap:
        # sort_value is the contract's stable strict-newest key.  Processing
        # order runs newest-to-oldest, so seen_sequence alone would retain the
        # oldest tail of this run and discard the page-one IDs needed for a
        # fast known-frontier stop on the next run.
        keep = sorted(
            resulting.values(),
            key=lambda entry: entry.sort_value,
            reverse=True,
        )[: snapshot.contract.frontier_cap]
        resulting = {entry.native_id: entry for entry in keep}
        # Pruning may remove the row that held the largest sequence.  Compact
        # only the next watermark to the surviving maximum so the exact state
        # invariant remains checkable without changing any retained entry.
        sequence = max(
            (entry.seen_sequence for entry in resulting.values()), default=0
        ) + 1

    return FrontierPlan(
        snapshot=snapshot,
        processed_pages=len(processed),
        raw_item_count=len(observed_items),
        new_native_ids=tuple(new_ids),
        observed_items=tuple(observed_items),
        resulting_entries=resulting,
        next_sequence=sequence,
        stop_reason=stop_reason,
        input_sha256=_processed_input_sha256(processed),
    )


def assert_snapshot_current(
    connection: sqlite3.Connection,
    snapshot: FrontierSnapshot,
    allowlist: frozenset[ContractKey],
) -> None:
    current = load_frontier(connection, snapshot.contract, allowlist)
    if (
        current.revision != snapshot.revision
        or current.state_sha256 != snapshot.state_sha256
    ):
        raise ConcurrentFrontierUpdate("incremental frontier changed during the run")


def persist_frontier(
    connection: sqlite3.Connection,
    plan: FrontierPlan,
    *,
    run_id: str,
    updated_at_utc: str,
) -> int:
    """Write a planned frontier inside the caller's open transaction."""

    contract = plan.snapshot.contract
    params = _contract_params(contract)
    revision = (plan.snapshot.revision or 0) + 1
    entries = plan.resulting_entries
    state_hash = _canonical_entries_sha256(entries)
    newest = max((entry.sort_value for entry in entries.values()), default=None)

    connection.execute(
        """
        INSERT INTO radar_incremental_frontiers (
          source_key, partition_key, sort_contract_sha256, schema_version,
          revision, newest_sort_value, next_sequence, frontier_count,
          frontier_sha256, last_run_id, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key, partition_key, sort_contract_sha256) DO UPDATE SET
          schema_version=excluded.schema_version,
          revision=excluded.revision,
          newest_sort_value=excluded.newest_sort_value,
          next_sequence=excluded.next_sequence,
          frontier_count=excluded.frontier_count,
          frontier_sha256=excluded.frontier_sha256,
          last_run_id=excluded.last_run_id,
          updated_at_utc=excluded.updated_at_utc
        """,
        (
            *params,
            SCHEMA_VERSION,
            revision,
            newest,
            plan.next_sequence,
            len(entries),
            state_hash,
            run_id,
            updated_at_utc,
        ),
    )
    connection.execute(
        """
        DELETE FROM radar_incremental_frontier_ids
         WHERE source_key=? AND partition_key=? AND sort_contract_sha256=?
        """,
        params,
    )
    connection.executemany(
        """
        INSERT INTO radar_incremental_frontier_ids (
          source_key, partition_key, sort_contract_sha256, native_id,
          sort_value, source_listing_id, seen_sequence
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                *params,
                entry.native_id,
                entry.sort_value,
                entry.source_listing_id,
                entry.seen_sequence,
            )
            for entry in entries.values()
        ],
    )
    return revision
