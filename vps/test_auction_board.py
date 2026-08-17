#!/usr/bin/env python3 -B
"""Tests for the dark auction-lane foundation (Rule 3: every test must be able
to fail; negative controls included and demonstrably caught)."""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_auction_board as bab
from auction_registry import auction_source_for_url, auction_source_by_key

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 17, 4, 30, 0, tzinfo=UTC)

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


def make_db(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, source_listing_id TEXT, source_url TEXT, title TEXT,
            make_model TEXT, country TEXT, price_eur INTEGER, year INTEGER,
            mileage_km INTEGER, fuel TEXT, seller_type TEXT, last_seen_at TEXT,
            raw_json TEXT
        );
        CREATE INDEX idx_offers_last_seen ON offers(last_seen_at);
    """)
    for r in rows:
        con.execute(
            """INSERT INTO offers (source, source_listing_id, source_url, title,
               make_model, country, price_eur, year, mileage_km, fuel, seller_type,
               last_seen_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", r)
    con.commit()
    return con


def frow(source, lid, url, end=None, price=5000, raw=None, last_seen="2026-08-17T02:00:00Z"):
    rj = {"auction_end_at": end} if end is not None else {}
    if raw:
        rj.update(raw)
    return (source, lid, url, f"{source} {lid}", "volkswagen golf", "DE",
            price, 2018, 120000, "petrol", "public", last_seen, json.dumps(rj))


def run(rows, regular_ids=frozenset(), regular_urls=frozenset(), now=NOW):
    con = make_db(rows)
    try:
        return bab.auction_rows(con, cutoff="2026-01-01T00:00:00Z",
                                regular_lane_ids=regular_ids,
                                regular_lane_urls=regular_urls, now=now)
    finally:
        con.close()


# ---------- registry (positive, derived from founder message + design doc) ---
check("registry contains zoll (founder prio 1)", auction_source_by_key("zoll-auktion") is not None)
check("registry contains kronofogden (founder list)",
      auction_source_by_key("kronofogden") is not None)
check("registry contains licytacje-komornik (founder list)",
      auction_source_by_key("licytacje-komornik") is not None)
check("registry zoll evidence cites founder", "mgr-e325f6c9" in auction_source_by_key("zoll-auktion").evidence)
ro = auction_source_by_key("zoll-auktion")
check("zoll priority 1", ro.priority == 1)
check("domain match zoll", auction_source_for_url("https://www.zoll-auktion.de/auktion/kategorie/Fahrzeuge/191") is not None)
# negative control: unrelated domain never matches
check("unrelated domain returns None (fail closed)",
      auction_source_for_url("https://mobile.de/") is None, negative=True)

# ---------- lane inclusion: positive registry + explicit semantics ----------
end_future = "2026-08-18T10:00:00+02:00"  # ends well after NOW
rows = [frow("zoll-auktion", "z-1", "https://www.zoll-auktion.de/x", end=end_future)]
lane, counts = run(rows)
check("positive zoll row enters lane", len(lane) == 1)
check("canonical end is UTC iso", lane[0]["canonical_end_utc"] == "2026-08-18T08:00:00+00:00")

# ---------- negative controls: every exclusion must fire --------------------
excluded = [
    ("unregistered source excluded", frow("mobile.de", "m", "https://mobile.de/x", end=end_future), "not_in_registry"),
    ("domain mismatch excluded", frow("zoll-auktion", "z", "https://fake.example/z", end=end_future), "domain_mismatch"),
    ("no explicit auction semantics excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end=None, raw={"title": "car"}), "no_explicit_auction_semantics"),
    ("naive end excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end="2026-08-18T10:00:00"), "malformed_or_naive_end"),
    ("malformed end excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end="tomorrow-ish"), "malformed_or_naive_end"),
    ("expired end excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end="2026-08-01T10:00:00Z"), "already_ended"),
    ("zero price excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end=end_future, price=0), "hidden_or_missing_price"),
    ("negative price excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end=end_future, price=-5), "hidden_or_missing_price"),
    ("non-numeric price excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end=end_future, price="hidden"), "hidden_or_missing_price"),
    ("malformed raw_json excluded", ("zoll-auktion", "z", "https://www.zoll-auktion.de/x", "t", "m", "DE", 5000, 2018, 100, "petrol", "p", "2026-08-17T02:00:00Z", '{"auction_end_at": "2026-08-18T10:00:00Z", broken'), "malformed_raw_json"),
]
for name, row, reason in excluded:
    lane, counts = run([row])
    check(name, len(lane) == 0 and counts.get(reason, 0) == 1, negative=True)

# ---------- ends_soon (<24h) boundary ---------------------------------------
end_soon = (NOW + dt.timedelta(hours=10)).isoformat()
end_later = (NOW + dt.timedelta(hours=40)).isoformat()
rows = [frow("zoll-auktion", "s", "https://www.zoll-auktion.de/s", end=end_soon),
        frow("zoll-auktion", "l", "https://www.zoll-auktion.de/l", end=end_later)]
lane, counts = run(rows)
check("ends_soon True inside 24h", any(r["ends_soon"] for r in lane if r["id"].endswith(":s")))
check("ends_soon False beyond 24h", not any(r["ends_soon"] for r in lane if r["id"].endswith(":l")))

# ---------- sort: ending soon first, then end time, then bid, then priority --
end_soon2 = (NOW + dt.timedelta(hours=2)).isoformat()
rows = [
    frow("zoll-auktion", "late", "https://www.zoll-auktion.de/l", end=end_later, price=3000),
    frow("zoll-auktion", "soon2", "https://www.zoll-auktion.de/s2", end=end_soon2, price=9000),
    frow("zoll-auktion", "soon1", "https://www.zoll-auktion.de/s1", end=end_soon, price=8000),
]
lane, counts = run(rows)
ids = [r["id"] for r in lane]
check("ending-soon rows sort first", ids.index("zoll-auktion:soon1") < ids.index("zoll-auktion:late")
      and ids.index("zoll-auktion:soon2") < ids.index("zoll-auktion:late"))
check("soonest end first among soon", ids.index("zoll-auktion:soon2") < ids.index("zoll-auktion:soon1"))

# ---------- cross-lane dedupe ------------------------------------------------
rows = [frow("zoll-auktion", "dup", "https://www.zoll-auktion.de/dup", end=end_soon)]
lane, counts = run(rows, regular_ids=frozenset({"zoll-auktion:dup"}))
check("id shared with regular lane excluded", len(lane) == 0 and counts.get("cross_lane_duplicate", 0) == 1, negative=True)
lane, counts = run(rows, regular_urls=frozenset({"https://www.zoll-auktion.de/dup"}))
check("url shared with regular lane excluded", len(lane) == 0 and counts.get("cross_lane_duplicate", 0) == 1, negative=True)

# ---------- no profit/ROI language in lane rows (design contract) ------------
lane, counts = run([frow("zoll-auktion", "p", "https://www.zoll-auktion.de/p", end=end_soon)])
check("no profit key in lane rows", all("profit" not in json.dumps(r) for r in lane) and
      "profit" not in json.dumps(counts))

# ---------- mutation gate: a seeded defect must flip a test -----------------
import build_auction_board as bab2
orig = bab2.parse_canonical_end


def mutate_naive_accept(v):
    if v is None:
        return None
    t = str(v).strip()
    if not t:
        return None
    if "+" not in t and not t.endswith("Z"):
        return dt.datetime.fromisoformat(t).replace(tzinfo=UTC)  # mutant accepts naive
    return orig(v)


bab2.parse_canonical_end = mutate_naive_accept
mutant_lane, _ = run([frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end="2026-08-18T10:00:00")])
bab2.parse_canonical_end = orig
# The test above ("naive end excluded") must DETECT this defect: with the
# mutant, the naive-end row enters the lane, so the lane is non-empty.
check("mutation gate: naive-accept defect flips the naive-end test",
      len(mutant_lane) == 1, negative=True)

# ---------- summary -----------------------------------------------------------
total = PASSED + FAILED
print(f"ASSERTIONS_PASSED={PASSED}")
print(f"ASSERTIONS_FAILED={FAILED}")
print(f"NEGATIVE_CONTROLS={NEGATIVE_CONTROLS} (each demonstrably caught)")
if FAILURES:
    print("FAILURES:")
    for f in FAILURES:
        print(" - " + f)
    sys.exit(1)
print("ALL_TESTS_GREEN")
sys.exit(0)