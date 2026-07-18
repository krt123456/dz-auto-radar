#!/usr/bin/env python3
"""Atomically extract the complete Schengen observation lake from the world feed."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


SCHENGEN = frozenset(
    "AT BE BG CH CZ DE DK EE ES FI FR GR HR HU IS IT LI LT LU LV MT NL NO PL PT RO SE SI SK".split()
)
REQUIRED = frozenset(
    {"listing_id", "source", "source_url", "country", "price_eur", "first_registration_date"}
)
MAX_SOURCE_URL_BYTES = 8 * 1024


def positive_price(value: object) -> bool:
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def normalize_source_url(value: object) -> str:
    text = html.unescape(str(value or "").strip())
    clean = re.split(r'''[\s"'<>]''', text, maxsplit=1)[0]
    if len(clean.encode("utf-8", "replace")) > MAX_SOURCE_URL_BYTES:
        return ""
    return clean


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=Path("/home/krt/eu_harvest/store/eu_full_offers.csv"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("/var/lib/sonardeals-radar/schengen_observation_lake.csv"),
    )
    parser.add_argument(
        "--report", type=Path,
        default=Path("/var/lib/sonardeals-radar/schengen_observation_lake.json"),
    )
    args = parser.parse_args()
    if not args.input.is_file():
        raise RuntimeError(f"world observation feed is unavailable: {args.input}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter[str] = Counter()
    sources: set[str] = set()
    countries: set[str] = set()
    temporary: Path | None = None
    try:
        with args.input.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            missing = REQUIRED - set(fields)
            if missing:
                raise RuntimeError(f"world feed lacks required columns: {sorted(missing)}")
            with tempfile.NamedTemporaryFile(
                mode="w", newline="", encoding="utf-8", dir=args.output.parent,
                prefix=f".{args.output.name}.", delete=False,
            ) as destination:
                temporary = Path(destination.name)
                writer = csv.DictWriter(destination, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for row in reader:
                    stats["world_rows"] += 1
                    country = str(row.get("country") or "").strip().upper()
                    if country not in SCHENGEN:
                        continue
                    stats["schengen_rows"] += 1
                    source_name = str(row.get("source") or "").strip()
                    listing_id = str(row.get("listing_id") or "").strip()
                    raw_url = str(row.get("source_url") or "").strip()
                    url = normalize_source_url(raw_url)
                    parsed = urlparse(url)
                    if (
                        not source_name or not listing_id or parsed.scheme != "https"
                        or not parsed.netloc or not positive_price(row.get("price_eur"))
                    ):
                        stats["rejected_identity_price_url"] += 1
                        continue
                    row["country"] = country
                    row["source_url"] = url
                    if url != raw_url:
                        stats["repaired_source_urls"] += 1
                    writer.writerow(row)
                    stats["accepted_rows"] += 1
                    sources.add(source_name)
                    countries.add(country)
                destination.flush()
                os.fsync(destination.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, args.output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    report = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input": str(args.input),
        "input_size_bytes": args.input.stat().st_size,
        "output": str(args.output),
        "output_size_bytes": args.output.stat().st_size,
        **stats,
        "source_count": len(sources),
        "country_count": len(countries),
        "countries": sorted(countries),
    }
    temporary_report = args.report.with_suffix(args.report.suffix + ".tmp")
    temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary_report, 0o640)
    os.replace(temporary_report, args.report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
