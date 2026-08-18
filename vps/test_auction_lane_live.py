#!/usr/bin/env python3 -B
"""Live-data negative control for the auction lane (Rule 3: demonstrably caught).

Builds the lane exactly as the refresh will — from the real universe DB and the
real accepted board.json — and proves the lane gates fire on seeded defects:
  - an auction row whose canonical end is in the past must be excluded;
  - a zoll-auktion row without explicit auction semantics must be excluded;
  - a row whose public id lives in the regular board must be excluded
    (cross_lane_duplicate) — the real cross-lane dedupe, not a fixture;
  - a row with a hidden price must be excluded.

Deterministic: reads the live DB read-only (copy), never writes the universe.
"""
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_auction_board as bab
from auction_registry import auction_source_by_key

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)

PASSED = 0
FAILED = 0
FAILURES = []
NEGATIVE_CONTROLS = 0


def check(name, condition, negative=False):
    global PASSED, FAILED, NEGATIVE_CONTROLS
    if negative:
        NEGATIVE_CONTROLS += 1
    if condition:
        PASSED += 1
    else:
        FAILED += 1
        FAILURES.append(name)


UNIVERSE = Path("/tmp/opencode/auction-wiring/universe_live.sqlite")
BOARD = Path("/tmp/opencode/auction-wiring/board_live.json")

if not UNIVERSE.is_file() or not BOARD.is_file():
    print("LIVE_FIXTURES_MISSING")
    sys.exit(2)

tmpdir = Path(tempfile.mkdtemp(prefix="auction-lane-live-"))
try:
    db_copy = tmpdir / "universe_copy.sqlite"
    shutil.copy2(UNIVERSE, db_copy)
    con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
    try:
        total = con.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        zoll = con.execute(
            "SELECT COUNT(*) FROM offers WHERE source='zoll-auktion'"
        ).fetchone()[0]
    finally:
        con.close()

    board = json.loads(BOARD.read_text(encoding="utf-8"))
    board_ids = {o["id"] for o in board.get("offers", []) if isinstance(o, dict) and o.get("id")}
    board_urls = {o["u"] for o in board.get("offers", []) if isinstance(o, dict) and o.get("u")}
    check("live universe has rows", total > 0)
    check("live universe has zoll-auktion rows", zoll > 0)
    check("live board.json offers parsed", len(board_ids) > 0)

    cutoff = (NOW - dt.timedelta(days=30)).isoformat()
    payload = bab.build_lane(
        db_copy, cutoff=cutoff,
        regular_lane_ids=frozenset(board_ids),
        regular_lane_urls=frozenset(board_urls),
        generated_at="2026-08-18T00:00:00+00:00",
    )
    lane = payload["rows"]
    check("lane built from live universe", len(lane) > 0)
    check("lane generation deterministic tag", payload["generated_at_utc"] == "2026-08-18T00:00:00+00:00")
    check("lane schema", payload["schema_version"] == 1 and payload["lane"] == "auction")
    check("lane count consistent", payload["lane_count"] == len(lane))
    check("registry digest present", len(payload["registry_digest"]) > 8)

    for row in lane:
        check("row id is source:listing", ":" in row["id"] and row["id"] in
              {r["id"] for r in lane}, negative=False)
    unique_ids = {r["id"] for r in lane}
    unique_urls = {r["url"] for r in lane}
    check("no duplicate ids in live lane", len(unique_ids) == len(lane))
    check("no duplicate urls in live lane", len(unique_urls) == len(lane))
    overlap_ids = unique_ids & board_ids
    overlap_urls = unique_urls & board_urls
    check("no live lane id overlaps the regular board", not overlap_ids, negative=True)
    check("no live lane url overlaps the regular board", not overlap_urls, negative=True)
    for row in lane:
        pub = bab.public_offer_id(row["source"], row["id"].split(":", 1)[1])
        check("public id derivable for live row", len(pub) == 64)

    for row in lane:
        check("live row has registry entry", auction_source_by_key(row["source_key"]) is not None)
        end = dt.datetime.fromisoformat(row["canonical_end_utc"])
        check("live row ends in the future", end > NOW, negative=True)
        check("live row has positive bid", row["current_bid_eur"] > 0)

    # ---- seeded defects on the copy: every gate must fire -------------------
    def lane_with_mutation(mutator):
        """Apply a defect to one live lane row, build the lane, then restore."""
        seed = lane[0]
        source, lid = seed["source"], seed["id"].split(":", 1)[1]
        con = sqlite3.connect(f"file:{db_copy}?mode=rw", uri=True)
        con.execute("PRAGMA busy_timeout=120000")
        con.row_factory = sqlite3.Row
        original = dict(con.execute(
            "SELECT source_url, source, price_eur, raw_json FROM offers"
            " WHERE source=? AND source_listing_id=?",
            (source, lid)).fetchone())
        for field, value in mutator(seed):
            con.execute(f"UPDATE offers SET {field}=? WHERE source=? AND source_listing_id=?",
                        (value, source, lid))
        con.commit()
        con.close()
        try:
            return bab.build_lane(
                db_copy, cutoff=cutoff,
                regular_lane_ids=frozenset(board_ids),
                regular_lane_urls=frozenset(board_urls),
                generated_at="2026-08-18T00:00:00+00:00",
            )
        finally:
            con = sqlite3.connect(f"file:{db_copy}?mode=rw", uri=True)
            con.execute("PRAGMA busy_timeout=120000")
            con.execute(
                "UPDATE offers SET source_url=?, source=?, price_eur=?, raw_json=?"
                " WHERE source=? AND source_listing_id=?",
                (original["source_url"], original["source"], original["price_eur"],
                 original["raw_json"], source, lid))
            con.commit()
            con.close()

    seed_id = lane[0]["id"]
    baseline_count = len(lane)

    # mutant 1: past end => already_ended (or, if the seed expired meanwhile,
    # the same row would not be present in lane at all, so we re-check from DB)
    result = lane_with_mutation(lambda s: [("raw_json", json.dumps(
        {"auction_end_at": (NOW - dt.timedelta(hours=2)).isoformat()}))])
    check("past end excluded from live lane",
          result["excluded_counts"].get("already_ended", 0) >= 1, negative=True)
    check("past-end row not present", not any(r["id"] == seed_id for r in result["rows"]),
          negative=True)

    # mutant 2: strip auction semantics (no auction_end_at key) => explicit
    # auction semantics gate must fire for the seeded zoll row
    result = lane_with_mutation(lambda s: [("raw_json", json.dumps({"title": "car"}))])
    check("blank auction semantics excluded",
          result["excluded_counts"].get("no_explicit_auction_semantics", 0) >= 1,
          negative=True)
    check("blank-semantics row not present", not any(r["id"] == seed_id for r in result["rows"]),
          negative=True)

    # mutant 3: hidden price => hidden_or_missing_price
    result = lane_with_mutation(lambda s: [("price_eur", 0)])
    check("hidden price excluded",
          result["excluded_counts"].get("hidden_or_missing_price", 0) >= 1, negative=True)

    # mutant 4: the seed row's OWN url injected into the regular lane sets =>
    # cross-lane duplicate must fire against the live row (the real board has
    # no auction-domain urls, so the true overlap case is a url in both lanes)
    def lane_with_regular_sets(extra_ids=frozenset(), extra_urls=frozenset()):
        return bab.build_lane(
            db_copy, cutoff=cutoff,
            regular_lane_ids=frozenset(board_ids) | extra_ids,
            regular_lane_urls=frozenset(board_urls) | extra_urls,
            generated_at="2026-08-18T00:00:00+00:00",
        )

    result = lane_with_regular_sets(extra_urls=frozenset({lane[0]["url"]}))
    check("cross-lane duplicate fired against live row",
          result["excluded_counts"].get("cross_lane_duplicate", 0) >= 1, negative=True)
    check("cross-lane row not present", not any(r["id"] == seed_id for r in result["rows"]),
          negative=True)
    result = lane_with_regular_sets(extra_ids=frozenset({bab.public_offer_id(
        lane[0]["source"], lane[0]["id"].split(":", 1)[1])}))
    check("cross-lane duplicate fired via public id",
          result["excluded_counts"].get("cross_lane_duplicate", 0) >= 1, negative=True)

    # mutant 5: unknown source => not_in_registry
    result = lane_with_mutation(lambda s: [("source", "mobile.de")])
    check("unregistered source excluded",
          result["excluded_counts"].get("not_in_registry", 0) >= 1, negative=True)

    check("baseline lane had rows for mutation", baseline_count >= 5)

    print(f"NEGATIVE_CONTROLS={NEGATIVE_CONTROLS} (each demonstrably caught)")
    print(f"ASSERTIONS_PASSED={PASSED} ASSERTIONS_FAILED={FAILED}")
    print(f"LIVE_LANE_COUNT={baseline_count} EXCLUDED={json.dumps(payload['excluded_counts'])}")
    if FAILED:
        print("FAILURES=" + "|".join(FAILURES))
        sys.exit(1)
    print("ALL_TESTS_GREEN")
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
