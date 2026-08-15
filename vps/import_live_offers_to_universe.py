#!/usr/bin/env python3
"""Import the current live-offer market into the broad universe store."""

from __future__ import annotations

import argparse
import csv
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

try:
    from .source_identity import IdentityError, canonical_source_identity
except ImportError:
    from source_identity import IdentityError, canonical_source_identity


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import live_offers.csv rows into universe_offers.sqlite."
    )
    parser.add_argument("--input-csv", default="live_offers.csv")
    parser.add_argument("--db", default="universe_offers.sqlite")
    parser.add_argument("--batch-size", type=int, default=1000)
    return parser.parse_args()


def clean_text(raw: object) -> str:
    return " ".join(str(raw or "").split())


def parse_year(raw: object) -> int:
    match = re.search(r"(19[5-9]\d|20[0-3]\d)", str(raw or ""))
    return int(match.group(1)) if match else 0


def parse_int(raw: object) -> int:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return int(digits or "0")


def parse_price_eur(raw: object) -> int:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        return 0
    try:
        return int(Decimal(text.replace(",", ".")))
    except InvalidOperation:
        return parse_int(raw)


def listing_identity(row: dict[str, str]) -> str:
    source = clean_text(row.get("source"))
    url = clean_text(row.get("source_url"))
    listing_id = clean_text(row.get("listing_id"))
    if source == "Vroom.be":
        match = re.search(r"-(\d{6,})/?$", url)
        if match:
            return match.group(1)
    if source == "Biltorvet":
        match = re.search(r"/(\d{5,})/?$", url)
        if match:
            return match.group(1)
    return listing_id


def raw_payload(row: dict[str, str], source_listing_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "listing_id": source_listing_id,
        "model_key": row.get("model_key", ""),
        "auction_end_at": row.get("auction_end_at", ""),
        "sale_term_code": row.get("sale_term_code", ""),
        "sale_certainty": row.get("sale_certainty", ""),
        "sale_certainty_note": row.get("sale_certainty_note", ""),
    }
    params = clean_text(row.get("source_params_json"))
    if params:
        try:
            payload["source_params"] = json.loads(params)
        except json.JSONDecodeError:
            payload["source_params_raw"] = params
    return payload


def convert_row(row: dict[str, str], fetched_at: str) -> dict[str, object] | None:
    source = clean_text(row.get("source"))
    source_listing_id = listing_identity(row)
    if not source or not source_listing_id:
        return None
    source, source_listing_id = canonical_source_identity(source, source_listing_id)
    return {
        "source": source,
        "source_listing_id": source_listing_id,
        "source_url": clean_text(row.get("source_url")),
        "title": clean_text(row.get("title")),
        "make_model": clean_text(row.get("model_key")),
        "variant": clean_text(row.get("transmission")),
        "country": clean_text(row.get("country")),
        "price_eur": parse_price_eur(row.get("price_eur")),
        "raw_price": clean_text(row.get("price_eur")),
        "currency": "EUR",
        "year": parse_year(row.get("first_registration_date")),
        "mileage_km": parse_int(row.get("mileage_km")),
        "fuel": clean_text(row.get("fuel")),
        "seller_type": clean_text(row.get("seller_type")),
        "location": "",
        "fetched_at": fetched_at,
        "raw_json": raw_payload(row, source_listing_id),
    }


def batched(
    rows: Iterable[dict[str, object]], size: int
) -> Iterable[list[dict[str, object]]]:
    batch: list[dict[str, object]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def main() -> None:
    from universe_store import connect, stats, upsert_rows, utc_now

    args = parse_args()
    conn = connect(Path(args.db))
    fetched_at = utc_now()
    raw_rows = 0
    converted_rows = 0
    identity_rejected_rows = 0
    inserted_total = 0
    updated_total = 0

    def row_iter() -> Iterable[dict[str, object]]:
        nonlocal raw_rows, converted_rows, identity_rejected_rows
        with Path(args.input_csv).open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                raw_rows += 1
                try:
                    converted = convert_row(row, fetched_at)
                except IdentityError:
                    identity_rejected_rows += 1
                    continue
                if converted is None:
                    continue
                converted_rows += 1
                yield converted

    for batch in batched(row_iter(), max(1, args.batch_size)):
        inserted, updated = upsert_rows(conn, batch)
        inserted_total += inserted
        updated_total += updated

    print(
        json.dumps(
            {
                "input_csv": str(args.input_csv),
                "raw_rows": raw_rows,
                "converted_rows": converted_rows,
                "identity_rejected_rows": identity_rejected_rows,
                "inserted": inserted_total,
                "updated": updated_total,
                "store": stats(conn),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
