#!/usr/bin/env python3
"""Atomically ingest a pre-fetched strict-newest Radar page stream.

This is deliberately a dark runtime primitive: it has no network client, timer,
service activation, publisher, or email side effect.  The caller supplies both a
page stream and an independently configured contract allowlist.  Only offers
actually observed in processed pages get ``last_seen_at`` refreshed; filtered raw
IDs still advance the source frontier.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping

try:
    from .incremental_frontier import (
        ConcurrentFrontierUpdate,
        ContractKey,
        FrontierPlan,
        SourceContract,
        SourcePage,
        assert_snapshot_current,
        ensure_frontier_schema,
        load_frontier,
        persist_frontier,
        plan_frontier,
    )
except ImportError:
    from incremental_frontier import (
        ConcurrentFrontierUpdate,
        ContractKey,
        FrontierPlan,
        SourceContract,
        SourcePage,
        assert_snapshot_current,
        ensure_frontier_schema,
        load_frontier,
        persist_frontier,
        plan_frontier,
    )


RUN_SCHEMA_VERSION = 2
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class IngestError(RuntimeError):
    """An incremental run cannot be committed safely."""


class RunConflictError(IngestError):
    """A run ID or request digest was reused inconsistently."""


class StaleObservationError(IngestError):
    """An older observation would overwrite a newer stored offer."""


class SchemaError(IngestError):
    """Persisted incremental tables do not match the sealed schema."""


OFFERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_listing_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  make_model TEXT NOT NULL DEFAULT '',
  variant TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  price_eur INTEGER NOT NULL DEFAULT 0,
  raw_price TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT '',
  year INTEGER NOT NULL DEFAULT 0,
  mileage_km INTEGER NOT NULL DEFAULT 0,
  fuel TEXT NOT NULL DEFAULT '',
  seller_type TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '',
  UNIQUE(source, source_listing_id)
);
CREATE INDEX IF NOT EXISTS idx_offers_last_seen ON offers(last_seen_at);
"""

RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS radar_incremental_runs (
  run_id TEXT PRIMARY KEY NOT NULL,
  request_sha256 TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  source_key TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  sort_contract_sha256 TEXT NOT NULL,
  committed_at_utc TEXT NOT NULL,
  processed_pages INTEGER NOT NULL,
  raw_item_count INTEGER NOT NULL,
  new_native_id_count INTEGER NOT NULL,
  observed_offer_count INTEGER NOT NULL,
  inserted_offer_count INTEGER NOT NULL,
  changed_offer_count INTEGER NOT NULL,
  refreshed_offer_count INTEGER NOT NULL,
  frontier_revision INTEGER NOT NULL,
  stop_reason TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  CHECK (observed_offer_count >= 0),
  CHECK (inserted_offer_count >= 0),
  CHECK (changed_offer_count >= 0),
  CHECK (refreshed_offer_count >= 0),
  CHECK (
    observed_offer_count =
      inserted_offer_count + changed_offer_count + refreshed_offer_count
  )
) STRICT;
CREATE TABLE IF NOT EXISTS radar_incremental_changes (
  run_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source <> ''),
  source_listing_id TEXT NOT NULL CHECK (source_listing_id <> ''),
  change_kind TEXT NOT NULL
    CHECK (change_kind IN ('inserted', 'material_update')),
  prior_material_sha256 TEXT,
  material_sha256 TEXT NOT NULL
    CHECK (
      length(material_sha256) = 64
      AND material_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
  observed_at_utc TEXT NOT NULL CHECK (observed_at_utc <> ''),
  offer_json TEXT NOT NULL CHECK (json_valid(offer_json)),
  PRIMARY KEY (run_id, source, source_listing_id),
  CHECK (
    (change_kind = 'inserted' AND prior_material_sha256 IS NULL)
    OR
    (change_kind = 'material_update'
      AND length(prior_material_sha256) = 64
      AND prior_material_sha256 NOT GLOB '*[^0-9a-f]*'
      AND prior_material_sha256 <> material_sha256)
  ),
  FOREIGN KEY (run_id) REFERENCES radar_incremental_runs(run_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
) STRICT;
"""


OFFER_COLUMNS = (
    "source",
    "source_listing_id",
    "source_url",
    "title",
    "make_model",
    "variant",
    "country",
    "price_eur",
    "raw_price",
    "currency",
    "year",
    "mileage_km",
    "fuel",
    "seller_type",
    "location",
    "fetched_at",
    "first_seen_at",
    "last_seen_at",
    "raw_json",
)

MATERIAL_OFFER_FIELDS = (
    "source_url",
    "title",
    "make_model",
    "variant",
    "country",
    "price_eur",
    "raw_price",
    "currency",
    "year",
    "mileage_km",
    "fuel",
    "seller_type",
    "location",
)
MATERIAL_INTEGER_FIELDS = frozenset({"price_eur", "year", "mileage_km"})


INSERT_OFFER = """
INSERT INTO offers (
  source, source_listing_id, source_url, title, make_model, variant, country,
  price_eur, raw_price, currency, year, mileage_km, fuel, seller_type, location,
  fetched_at, first_seen_at, last_seen_at, raw_json
) VALUES (
  :source, :source_listing_id, :source_url, :title, :make_model, :variant, :country,
  :price_eur, :raw_price, :currency, :year, :mileage_km, :fuel, :seller_type, :location,
  :fetched_at, :first_seen_at, :last_seen_at, :raw_json
)
"""

UPDATE_MATERIAL_OFFER = """
UPDATE offers SET
  source_url=:source_url,
  title=:title,
  make_model=:make_model,
  variant=:variant,
  country=:country,
  price_eur=:price_eur,
  raw_price=:raw_price,
  currency=:currency,
  year=:year,
  mileage_km=:mileage_km,
  fuel=:fuel,
  seller_type=:seller_type,
  location=:location,
  fetched_at=:fetched_at,
  last_seen_at=:last_seen_at,
  raw_json=:raw_json
WHERE source=:source AND source_listing_id=:source_listing_id
"""

REFRESH_OFFER = """
UPDATE offers SET fetched_at=:fetched_at, last_seen_at=:last_seen_at
WHERE source=:source AND source_listing_id=:source_listing_id
"""

RUN_TABLE_COLUMNS = (
    "run_id", "request_sha256", "input_sha256", "source_key",
    "partition_key", "sort_contract_sha256", "committed_at_utc",
    "processed_pages", "raw_item_count", "new_native_id_count",
    "observed_offer_count", "inserted_offer_count", "changed_offer_count",
    "refreshed_offer_count", "frontier_revision", "stop_reason",
    "receipt_json",
)
CHANGE_TABLE_COLUMNS = (
    "run_id", "source", "source_listing_id", "change_kind",
    "prior_material_sha256", "material_sha256", "observed_at_utc",
    "offer_json",
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _require_sealed_table(
    connection: sqlite3.Connection, table: str, expected_columns: tuple[str, ...]
) -> None:
    columns = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
    strict = next(
        (row[5] for row in connection.execute("PRAGMA table_list") if row[1] == table),
        0,
    )
    stored_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    match = re.search(
        rf"(CREATE TABLE IF NOT EXISTS {re.escape(table)} \(.*?\n\) STRICT);",
        RUN_SCHEMA,
        flags=re.DOTALL,
    )
    if match is None:  # pragma: no cover - sealed source constant
        raise AssertionError(f"missing sealed schema source for {table}")
    expected_sql = match.group(1).replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)
    normalize_sql = lambda value: " ".join(value.split())  # noqa: E731
    if (
        columns != expected_columns
        or strict != 1
        or stored_row is None
        or normalize_sql(stored_row[0]) != normalize_sql(expected_sql)
    ):
        raise SchemaError(f"{table} does not match the sealed incremental schema")


def canonical_utc(raw: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise IngestError("run timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise IngestError("run timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IngestError("run timestamp must include an offset")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat()


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    for table, columns in (
        ("radar_incremental_runs", RUN_TABLE_COLUMNS),
        ("radar_incremental_changes", CHANGE_TABLE_COLUMNS),
    ):
        if _table_exists(connection, table):
            _require_sealed_table(connection, table, columns)
    connection.executescript(OFFERS_SCHEMA)
    ensure_frontier_schema(connection)
    connection.executescript(RUN_SCHEMA)
    _require_sealed_table(connection, "radar_incremental_runs", RUN_TABLE_COLUMNS)
    _require_sealed_table(connection, "radar_incremental_changes", CHANGE_TABLE_COLUMNS)


def connect(path: Path, *, timeout_seconds: float = 5.0) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=timeout_seconds)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout_seconds * 1000))}")
        connection.execute("PRAGMA foreign_keys=ON")
        ensure_schema(connection)
        return connection
    except BaseException:
        connection.close()
        raise


def _bounded_int(value: Any, field: str) -> int:
    if type(value) is not int or not -9_223_372_036_854_775_807 <= value <= 9_223_372_036_854_775_807:
        raise IngestError(f"offer {field} is not a bounded integer")
    return value


def _text(value: Any, field: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise IngestError(f"offer {field} is not text")
    value = " ".join(value.split())
    if required and not value:
        raise IngestError(f"offer {field} is required")
    if len(value) > 100_000:
        raise IngestError(f"offer {field} is too large")
    return value


def normalize_offer(
    raw: Mapping[str, Any],
    *,
    source_key: str,
    observed_at_utc: str,
) -> dict[str, Any]:
    if set(raw) - {
        "source", "source_listing_id", "source_url", "title", "make_model",
        "variant", "country", "price_eur", "raw_price", "currency", "year",
        "mileage_km", "fuel", "seller_type", "location", "fetched_at", "raw_json",
    }:
        raise IngestError("offer contains unsupported fields")
    source = _text(raw.get("source"), "source", required=True)
    if source != source_key:
        raise IngestError("offer source does not match the incremental contract")
    source_listing_id = _text(
        raw.get("source_listing_id"), "source_listing_id", required=True
    )
    source_url = _text(raw.get("source_url"), "source_url", required=True)
    if not source_url.startswith("https://"):
        raise IngestError("offer source_url must be HTTPS")
    raw_json = raw.get("raw_json", "")
    if isinstance(raw_json, Mapping) or isinstance(raw_json, list):
        raw_json = json.dumps(
            raw_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    raw_json = _text(raw_json, "raw_json")
    fetched_at = canonical_utc(raw.get("fetched_at") or observed_at_utc)
    return {
        "source": source,
        "source_listing_id": source_listing_id,
        "source_url": source_url,
        "title": _text(raw.get("title"), "title"),
        "make_model": _text(raw.get("make_model"), "make_model"),
        "variant": _text(raw.get("variant"), "variant"),
        "country": _text(raw.get("country"), "country"),
        "price_eur": _bounded_int(raw.get("price_eur", 0), "price_eur"),
        "raw_price": _text(raw.get("raw_price"), "raw_price"),
        "currency": _text(raw.get("currency"), "currency"),
        "year": _bounded_int(raw.get("year", 0), "year"),
        "mileage_km": _bounded_int(raw.get("mileage_km", 0), "mileage_km"),
        "fuel": _text(raw.get("fuel"), "fuel"),
        "seller_type": _text(raw.get("seller_type"), "seller_type"),
        "location": _text(raw.get("location"), "location"),
        "fetched_at": fetched_at,
        "first_seen_at": observed_at_utc,
        "last_seen_at": observed_at_utc,
        "raw_json": raw_json,
    }


def _observed_offers(
    plan: FrontierPlan,
    *,
    source_key: str,
    observed_at_utc: str,
) -> list[dict[str, Any]]:
    offers: dict[tuple[str, str], dict[str, Any]] = {}
    for item in plan.observed_items:
        if item.offer is None:
            continue
        normalized = normalize_offer(
            item.offer,
            source_key=source_key,
            observed_at_utc=observed_at_utc,
        )
        identity = (normalized["source"], normalized["source_listing_id"])
        previous = offers.get(identity)
        if previous is not None and previous != normalized:
            raise IngestError("one run observed conflicting fields for an offer")
        offers[identity] = normalized
    return list(offers.values())


def _material_payload(offer: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in MATERIAL_OFFER_FIELDS:
        value = offer[field]
        if field in MATERIAL_INTEGER_FIELDS:
            payload[field] = _bounded_int(value, field)
        elif not isinstance(value, str):
            raise IngestError(f"stored offer {field} is not text")
        else:
            payload[field] = value
    return payload


def _canonical_material_json(offer: Mapping[str, Any]) -> str:
    return json.dumps(
        _material_payload(offer),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _material_sha256(offer_json: str) -> str:
    return hashlib.sha256(offer_json.encode("utf-8")).hexdigest()


def _write_change(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    offer: Mapping[str, Any],
    change_kind: str,
    prior_material_sha256: str | None,
    material_sha256: str,
    offer_json: str,
) -> None:
    connection.execute(
        """
        INSERT INTO radar_incremental_changes (
          run_id, source, source_listing_id, change_kind,
          prior_material_sha256, material_sha256, observed_at_utc, offer_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            offer["source"],
            offer["source_listing_id"],
            change_kind,
            prior_material_sha256,
            material_sha256,
            offer["last_seen_at"],
            offer_json,
        ),
    )


def _upsert_observed_offers(
    connection: sqlite3.Connection,
    offers: list[dict[str, Any]],
    *,
    run_id: str,
) -> tuple[int, int, int]:
    inserted = 0
    changed = 0
    refreshed = 0
    for offer in offers:
        existed = connection.execute(
            "SELECT * FROM offers WHERE source=? AND source_listing_id=?",
            (offer["source"], offer["source_listing_id"]),
        ).fetchone()
        if existed is not None:
            try:
                stored_last_seen = canonical_utc(existed["last_seen_at"])
            except IngestError as error:
                raise StaleObservationError(
                    "stored offer last_seen_at is invalid"
                ) from error
            if stored_last_seen > offer["last_seen_at"]:
                raise StaleObservationError(
                    "an older observation cannot overwrite a newer offer"
                )
        offer_json = _canonical_material_json(offer)
        material_sha256 = _material_sha256(offer_json)
        if existed is None:
            connection.execute(INSERT_OFFER, offer)
            _write_change(
                connection,
                run_id=run_id,
                offer=offer,
                change_kind="inserted",
                prior_material_sha256=None,
                material_sha256=material_sha256,
                offer_json=offer_json,
            )
            inserted += 1
        else:
            prior_json = _canonical_material_json(existed)
            prior_sha256 = _material_sha256(prior_json)
            if prior_sha256 == material_sha256:
                cursor = connection.execute(REFRESH_OFFER, offer)
                refreshed += 1
            else:
                cursor = connection.execute(UPDATE_MATERIAL_OFFER, offer)
                _write_change(
                    connection,
                    run_id=run_id,
                    offer=offer,
                    change_kind="material_update",
                    prior_material_sha256=prior_sha256,
                    material_sha256=material_sha256,
                    offer_json=offer_json,
                )
                changed += 1
            if cursor.rowcount != 1:
                raise ConcurrentFrontierUpdate("observed offer changed concurrently")
    return inserted, changed, refreshed


RECEIPT_ROW_FIELDS = (
    "run_id",
    "request_sha256",
    "input_sha256",
    "source_key",
    "partition_key",
    "sort_contract_sha256",
    "committed_at_utc",
    "processed_pages",
    "raw_item_count",
    "new_native_id_count",
    "observed_offer_count",
    "inserted_offer_count",
    "changed_offer_count",
    "refreshed_offer_count",
    "frontier_revision",
    "stop_reason",
)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RunConflictError("stored incremental run receipt has duplicate keys")
        value[key] = item
    return value


def _verify_change_ledger(
    connection: sqlite3.Connection,
    *,
    receipt: Mapping[str, Any],
) -> None:
    rows = connection.execute(
        """
        SELECT run_id, source, source_listing_id, change_kind,
               prior_material_sha256, material_sha256, observed_at_utc, offer_json
          FROM radar_incremental_changes
         WHERE run_id=?
         ORDER BY source, source_listing_id
        """,
        (receipt["run_id"],),
    ).fetchall()
    expected_count = receipt["inserted_offer_count"] + receipt["changed_offer_count"]
    if len(rows) != expected_count:
        raise RunConflictError("stored incremental change ledger count is corrupt")
    counts = {"inserted": 0, "material_update": 0}
    for row in rows:
        try:
            payload = json.loads(
                row["offer_json"], object_pairs_hook=_unique_json_object
            )
            canonical = _canonical_material_json(payload)
            observed_at = canonical_utc(row["observed_at_utc"])
        except (json.JSONDecodeError, TypeError, KeyError, IngestError) as error:
            raise RunConflictError("stored incremental change ledger is corrupt") from error
        if (
            not isinstance(payload, dict)
            or set(payload) != set(MATERIAL_OFFER_FIELDS)
            or canonical != row["offer_json"]
            or _material_sha256(canonical) != row["material_sha256"]
            or row["run_id"] != receipt["run_id"]
            or row["source"] != receipt["source_key"]
            or not isinstance(row["source_listing_id"], str)
            or not row["source_listing_id"]
            or row["source_listing_id"]
                != " ".join(row["source_listing_id"].split())
            or observed_at != receipt["committed_at_utc"]
            or row["change_kind"] not in counts
        ):
            raise RunConflictError("stored incremental change ledger is corrupt")
        prior = row["prior_material_sha256"]
        if row["change_kind"] == "inserted":
            if prior is not None:
                raise RunConflictError("stored inserted change has a prior hash")
        elif (
            not isinstance(prior, str)
            or not HEX_64.fullmatch(prior)
            or prior == row["material_sha256"]
        ):
            raise RunConflictError("stored material update prior hash is corrupt")
        counts[row["change_kind"]] += 1
    if (
        counts["inserted"] != receipt["inserted_offer_count"]
        or counts["material_update"] != receipt["changed_offer_count"]
    ):
        raise RunConflictError("stored incremental change ledger kinds are corrupt")


def _receipt_from_row(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any]:
    try:
        receipt = json.loads(
            row["receipt_json"], object_pairs_hook=_unique_json_object
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise RunConflictError("stored incremental run receipt is corrupt") from error
    if not isinstance(receipt, dict):
        raise RunConflictError("stored incremental run receipt is corrupt")
    expected = {
        "schema_version": RUN_SCHEMA_VERSION,
        "result": "RADAR_INCREMENTAL_INGEST_PASS",
    }
    expected.update({field: row[field] for field in RECEIPT_ROW_FIELDS})
    if set(receipt) != set(expected) or any(
        type(receipt[field]) is not type(expected[field])
        or receipt[field] != expected[field]
        for field in expected
    ):
        raise RunConflictError(
            "stored incremental run receipt does not match its database row"
        )
    if receipt["observed_offer_count"] != (
        receipt["inserted_offer_count"]
        + receipt["changed_offer_count"]
        + receipt["refreshed_offer_count"]
    ):
        raise RunConflictError("stored incremental run counts do not reconcile")
    _verify_change_ledger(connection, receipt=receipt)
    return receipt


def _existing_receipt(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    request_sha256: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM radar_incremental_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    if row["request_sha256"] != request_sha256:
        raise RunConflictError("run_id was reused with a different request digest")
    return _receipt_from_row(connection, row)


def _write_run_receipt(
    connection: sqlite3.Connection,
    *,
    receipt: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO radar_incremental_runs (
          run_id, request_sha256, input_sha256, source_key, partition_key,
          sort_contract_sha256, committed_at_utc, processed_pages,
          raw_item_count, new_native_id_count, observed_offer_count,
          inserted_offer_count, changed_offer_count, refreshed_offer_count,
          frontier_revision, stop_reason, receipt_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt["run_id"],
            receipt["request_sha256"],
            receipt["input_sha256"],
            receipt["source_key"],
            receipt["partition_key"],
            receipt["sort_contract_sha256"],
            receipt["committed_at_utc"],
            receipt["processed_pages"],
            receipt["raw_item_count"],
            receipt["new_native_id_count"],
            receipt["observed_offer_count"],
            receipt["inserted_offer_count"],
            receipt["changed_offer_count"],
            receipt["refreshed_offer_count"],
            receipt["frontier_revision"],
            receipt["stop_reason"],
            json.dumps(
                receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ),
    )


def ingest_incremental_run(
    connection: sqlite3.Connection,
    *,
    contract: SourceContract,
    allowlist: frozenset[ContractKey],
    run_id: str,
    request_sha256: str,
    observed_at_utc: str,
    pages: Iterable[SourcePage | Mapping[str, Any]],
) -> dict[str, Any]:
    """Plan outside a writer lock, then atomically commit if state is unchanged."""

    if connection.in_transaction:
        raise IngestError("incremental ingest requires a clean connection")
    if not RUN_ID.fullmatch(run_id):
        raise IngestError("run_id is invalid")
    if not HEX_64.fullmatch(request_sha256):
        raise IngestError("request_sha256 is invalid")
    observed_at_utc = canonical_utc(observed_at_utc)
    contract.validate(allowlist)

    existing = _existing_receipt(
        connection, run_id=run_id, request_sha256=request_sha256
    )
    if existing is not None:
        return existing

    snapshot = load_frontier(connection, contract, allowlist)
    plan = plan_frontier(snapshot, pages)
    offers = _observed_offers(
        plan,
        source_key=contract.source_key,
        observed_at_utc=observed_at_utc,
    )

    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _existing_receipt(
            connection, run_id=run_id, request_sha256=request_sha256
        )
        if existing is not None:
            connection.rollback()
            return existing
        assert_snapshot_current(connection, snapshot, allowlist)
        inserted, changed, refreshed = _upsert_observed_offers(
            connection, offers, run_id=run_id
        )
        revision = persist_frontier(
            connection,
            plan,
            run_id=run_id,
            updated_at_utc=observed_at_utc,
        )
        receipt = {
            "schema_version": RUN_SCHEMA_VERSION,
            "result": "RADAR_INCREMENTAL_INGEST_PASS",
            "run_id": run_id,
            "request_sha256": request_sha256,
            "input_sha256": plan.input_sha256,
            "source_key": contract.source_key,
            "partition_key": contract.partition_key,
            "sort_contract_sha256": contract.sort_contract_sha256,
            "committed_at_utc": observed_at_utc,
            "processed_pages": plan.processed_pages,
            "raw_item_count": plan.raw_item_count,
            "new_native_id_count": len(plan.new_native_ids),
            "observed_offer_count": len(offers),
            "inserted_offer_count": inserted,
            "changed_offer_count": changed,
            "refreshed_offer_count": refreshed,
            "frontier_revision": revision,
            "stop_reason": plan.stop_reason,
        }
        _write_run_receipt(connection, receipt=receipt)
        connection.commit()
        return receipt
    except sqlite3.OperationalError as error:
        connection.rollback()
        if "locked" in str(error).casefold() or "busy" in str(error).casefold():
            raise ConcurrentFrontierUpdate("incremental database writer is busy") from error
        raise
    except BaseException:
        connection.rollback()
        raise
