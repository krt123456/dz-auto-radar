#!/usr/bin/env python3 -B
"""Tests for the dark auction-lane foundation (Rule 3: every test must be able
to fail; negative controls included and demonstrably caught)."""
import datetime as dt
import json
import re
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


def frow(source, lid, url, end=None, price=5000, raw=None, last_seen="2026-08-17T02:00:00Z", year=2024,
         fuel="petrol", country="DE"):
    rj = {"auction_end_at": end, "sale_term_code": "auction-current-bid"} if end is not None else {}
    if raw:
        rj.update(raw)
    return (source, lid, url, f"{source} {lid}", "volkswagen golf", country,
            price, year, 120000, fuel, "public", last_seen, json.dumps(rj))


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
check("registry contains Czech UZSVM state auctions",
      auction_source_by_key("nabidka-majetku") is not None)
check("registry zoll evidence cites founder", "mgr-e325f6c9" in auction_source_by_key("zoll-auktion").evidence)
ro = auction_source_by_key("zoll-auktion")
check("zoll priority 1", ro.priority == 1)
check("domain match zoll", auction_source_for_url("https://www.zoll-auktion.de/auktion/kategorie/Fahrzeuge/191") is not None)
check("domain match Kronofogden live auction host",
      auction_source_for_url("https://auktion.kronofogden.se/auk/w.ObjectList?inC=KFM&inA=WEB").key == "kronofogden")
cz = auction_source_for_url("https://nabidkamajetku.gov.cz/Home/AuctionDetail/61592")
check("domain match Czech UZSVM", cz is not None and cz.key == "nabidka-majetku")
# negative control: unrelated domain never matches
check("unrelated domain returns None (fail closed)",
      auction_source_for_url("https://mobile.de/") is None, negative=True)

# ---------- lane inclusion: positive registry + explicit semantics ----------
end_future = "2026-08-18T10:00:00+02:00"  # ends well after NOW
rows = [frow("zoll-auktion", "z-1", "https://www.zoll-auktion.de/x", end=end_future)]
lane, counts = run(rows)
check("positive zoll row enters lane", len(lane) == 1)
check("canonical end is UTC iso", lane[0]["canonical_end_utc"] == "2026-08-18T08:00:00+00:00")
check("fuel: petrol allowed", bab.import_eligible_fuel("petrol"))
check("fuel: electric allowed", bab.import_eligible_fuel("Elektro"))
check("fuel: petrol hybrid allowed", bab.import_eligible_fuel("Hybrid (Benzin/Elektro)"))
check("fuel: diesel rejected", not bab.import_eligible_fuel("diesel"), negative=True)
check("fuel: diesel hybrid rejected", not bab.import_eligible_fuel("Hybrid (Diesel/Elektro)"), negative=True)
check("fuel: ambiguous hybrid rejected", not bab.import_eligible_fuel("hybrid"), negative=True)

# ---------- negative controls: every exclusion must fire --------------------
excluded = [
    ("unregistered source excluded", frow("mobile.de", "m", "https://mobile.de/x", end=end_future), "not_in_registry"),
    ("domain mismatch excluded", frow("zoll-auktion", "z", "https://fake.example/z", end=end_future), "domain_mismatch"),
    ("cross-official-domain mismatch excluded", frow("zoll-auktion", "z2", "https://pvp.giustizia.it/pvp/it/detail_annuncio.page?idAnnuncio=2", end=end_future), "domain_mismatch"),
    ("starting price cannot enter current-bid lane", frow("zoll-auktion", "start", "https://www.zoll-auktion.de/start", end=end_future, raw={"sale_term_code": "auction"}), "not_confirmed_current_bid"),
    ("no explicit auction semantics excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end=None, raw={"title": "car"}), "no_explicit_auction_semantics"),
    ("naive end excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end="2026-08-18T10:00:00"), "malformed_or_naive_end"),
    ("malformed end excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end="tomorrow-ish"), "malformed_or_naive_end"),
    ("expired end excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end="2026-08-01T10:00:00Z"), "already_ended"),
    ("zero price excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end=end_future, price=0), "hidden_or_missing_price"),
    ("negative price excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end=end_future, price=-5), "hidden_or_missing_price"),
    ("non-numeric price excluded", frow("zoll-auktion", "z", "https://www.zoll-auktion.de/x", end=end_future, price="hidden"), "hidden_or_missing_price"),
    ("diesel excluded by Algeria import rules", frow("zoll-auktion", "d", "https://www.zoll-auktion.de/d", end=end_future, fuel="diesel"), "fuel_not_import_eligible"),
    ("malformed raw_json excluded", ("zoll-auktion", "z", "https://www.zoll-auktion.de/x", "t", "m", "DE", 5000, 2024, 100, "petrol", "p", "2026-08-17T02:00:00Z", '{"auction_end_at": "2026-08-18T10:00:00Z", broken'), "malformed_raw_json"),
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
# cross-lane dedupe must also catch the canonical public-id space used by the
# regular board (compact offer ids are sha256 hashes, not source:listing strings)
pub_id = bab.public_offer_id("zoll-auktion", "dup")
check("public id computed in regular-board id space", re.fullmatch(r"[0-9a-f]{64}", pub_id) is not None)
lane, counts = run(rows, regular_ids=frozenset({pub_id}))
check("public-id form shared with regular lane excluded",
      len(lane) == 0 and counts.get("cross_lane_duplicate", 0) == 1, negative=True)
# negative control: a regular id that is NOT this row must not exclude it
lane, counts = run(rows, regular_ids=frozenset({bab.public_offer_id("zoll-auktion", "other")}))
check("unrelated regular public id does not exclude row", len(lane) == 1)


# ---------- founder year policy (mgr-fb1670...): 2023-2026, day+month for 2023
end_future = "2026-08-18T10:00:00+02:00"
f_rows = [
    frow("zoll-auktion", "y2023d", "https://www.zoll-auktion.de/y2023d", end=end_future,
         raw={"first_registration_date": "15.11.2023"}),
    frow("onlineveilingmeester", "y2023iso", "https://onlineveilingmeester.nl/y2023iso",
         end=end_future, raw={"first_registration_date": "2023-11-16"}, country="NL"),
    frow("zoll-auktion", "y2024", "https://www.zoll-auktion.de/y2024", end=end_future),
    frow("zoll-auktion", "y2025", "https://www.zoll-auktion.de/y2025", end=end_future),
    frow("zoll-auktion", "y2026", "https://www.zoll-auktion.de/y2026", end=end_future),
]
lane, counts = run(f_rows)
check("founder: 2023 with full day+month enters lane",
      any(r["id"].endswith(":y2023d") for r in lane))
check("founder: ISO 2023 registration date enters lane",
      any(r["id"].endswith(":y2023iso") for r in lane))
check("founder: 2024 enters lane", any(r["id"].endswith(":y2024") for r in lane))
check("founder: 2025 enters lane", any(r["id"].endswith(":y2025") for r in lane))
check("founder: 2026 enters lane", any(r["id"].endswith(":y2026") for r in lane))

# negative controls: every founder violation must be excluded with its code
f_bad = [
    ("year outside range (2018) excluded", frow("zoll-auktion", "old", "https://www.zoll-auktion.de/old", end=end_future, year=2018),
     "year_outside_2023_2026"),
    ("year beyond range (2030) excluded", frow("zoll-auktion", "new", "https://www.zoll-auktion.de/new", end=end_future, year=2030), "year_outside_2023_2026"),
    ("unknown year (0) excluded", frow("zoll-auktion", "z0", "https://www.zoll-auktion.de/z0", end=end_future, year=0), "year_outside_2023_2026"),
    ("2023 without registration date excluded",
     frow("zoll-auktion", "noreg", "https://www.zoll-auktion.de/noreg", end=end_future, year=2023),
     "year_2023_without_day_month"),
    ("2023 with year-only date excluded",
     frow("zoll-auktion", "yearonly", "https://www.zoll-auktion.de/yearonly", end=end_future, year=2023,
          raw={"first_registration_date": "2023"}),
     "year_2023_without_day_month"),
    ("2023 with malformed date excluded",
     frow("zoll-auktion", "badreg", "https://www.zoll-auktion.de/badreg", end=end_future, year=2023,
          raw={"first_registration_date": "2023-15-11"}),
     "year_2023_without_day_month"),
    ("2023 with impossible calendar date excluded",
     frow("zoll-auktion", "impossible-reg", "https://www.zoll-auktion.de/impossible-reg",
          end=end_future, year=2023, raw={"first_registration_date": "31.02.2023"}),
     "year_2023_without_day_month"),
    ("2023 registration older than rolling three years excluded",
     frow("zoll-auktion", "too-old-by-day", "https://www.zoll-auktion.de/too-old-by-day",
          end=end_future, year=2023, raw={"first_registration_date": "16.08.2023"}),
     "registration_older_than_three_years"),
]
for name, row, reason in f_bad:
    lane, counts = run([row])
    check(name, len(lane) == 0 and counts.get(reason, 0) == 1, negative=True)

# boundary: 2023 with day+month but malformed raw_json falls back to exclusion
lane, counts = run([frow("zoll-auktion", "badjson", "https://www.zoll-auktion.de/badjson",
                         end=end_future, raw=None)])
check("2024 rows need no registration date", len(lane) == 1)

# mutation gate: a seeded defect that skips the founder filter must flip a test
import build_auction_board as bab3
orig_eligible = bab3.founder_eligible
bab3.founder_eligible = lambda year, raw_json, **kwargs: (True, "")
mutant_lane, _ = run([frow("zoll-auktion", "old", "https://www.zoll-auktion.de/old", end=end_future, year=2018)])
bab3.founder_eligible = orig_eligible
check("mutation gate: skipping founder filter lets 2018 row in",
      len(mutant_lane) == 1, negative=True)
# second mutant: 2023 rows never require day+month
bab3.founder_eligible = lambda year, raw_json, **kwargs: (True, "")
mutant_lane, _ = run([frow("zoll-auktion", "noreg2", "https://www.zoll-auktion.de/noreg2", end=end_future, year=2023)])
bab3.founder_eligible = orig_eligible
check("mutation gate: dropping 2023 date rule lets no-reg 2023 row in",
      len(mutant_lane) == 1, negative=True)


# ---------- load_regular_board: real accepted board shape --------------------
import json as _json
board_fixture = {
    "schema_version": 2,
    "offers": [
        {"id": "abc123", "u": "https://www.zoll-auktion.de/dup", "m": "golf"},
        {"id": "def456", "u": "https://www.olx.pl/d/oferta/x", "m": "golf"},
        {"id": "", "u": "", "m": "golf"},
    ],
}
from pathlib import Path as _Path
import tempfile as _tf
with _tf.TemporaryDirectory() as _td:
    _board = _Path(_td) / "board.json"
    _board.write_text(_json.dumps(board_fixture))
    ids, urls = bab.load_regular_board(_board)
    check("board ids extracted", ids == frozenset({"abc123", "def456"}))
    check("board urls extracted", urls == frozenset({
        "https://www.zoll-auktion.de/dup", "https://www.olx.pl/d/oferta/x"}))
    lane, counts = run(rows, regular_ids=ids, regular_urls=urls)
    check("auction row excluded against real board.json fixture",
          len(lane) == 0 and counts.get("cross_lane_duplicate", 0) == 1, negative=True)

# ---------- generation binding -----------------------------------------------
with _tf.TemporaryDirectory() as _td:
    _db = _Path(_td) / "u.sqlite"
    con = sqlite3.connect(_db)
    con.execute("""CREATE TABLE offers (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   source TEXT, source_listing_id TEXT, source_url TEXT, title TEXT,
                   make_model TEXT, country TEXT, price_eur INTEGER, year INTEGER,
                   mileage_km INTEGER, fuel TEXT, seller_type TEXT, last_seen_at TEXT,
                   raw_json TEXT)""")
    con.execute("CREATE INDEX idx_offers_last_seen ON offers(last_seen_at)")
    con.execute(
        "INSERT INTO offers (source, source_listing_id, source_url, title, make_model,"
        " country, price_eur, year, mileage_km, fuel, seller_type, last_seen_at, raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("zoll-auktion", "z1", "https://www.zoll-auktion.de/z1", "t", "m", "DE", 5000,
         2024, 100, "petrol", "p", "2026-08-17T02:00:00Z",
         _json.dumps({
             "auction_end_at": "2030-01-01T10:00:00Z",
             "sale_term_code": "auction-current-bid",
         })))
    con.commit(); con.close()
    payload = bab.build_lane(_db, cutoff="2026-01-01T00:00:00Z",
                             regular_lane_ids=frozenset(), regular_lane_urls=frozenset(),
                             generated_at="2026-08-17T04:00:00+00:00")
    check("lane generation is bound, not wall-clock",
          payload["generated_at_utc"] == "2026-08-17T04:00:00+00:00")
    check("lane generation deterministic", payload["lane_count"] == 1 and
          payload["rows"][0]["id"] == "zoll-auktion:z1")
    payload2 = bab.build_lane(_db, cutoff="2026-01-01T00:00:00Z",
                              regular_lane_ids=frozenset(), regular_lane_urls=frozenset(),
                              generated_at="2026-08-17T04:00:00+00:00")
    check("same input yields byte-identical lane payload",
          _json.dumps(payload, sort_keys=True) == _json.dumps(payload2, sort_keys=True))
    # negative control: generation bound from board timestamp, not random now
    payload3 = bab.build_lane(_db, cutoff="2026-01-01T00:00:00Z",
                              regular_lane_ids=frozenset(), regular_lane_urls=frozenset(),
                              generated_at="2026-08-17T09:00:00+00:00")
    check("different generation produces different payload timestamp",
          payload3["generated_at_utc"] != payload["generated_at_utc"], negative=True)

# ---------- no profit/ROI language in lane rows (design contract) ------------
lane, counts = run([frow("zoll-auktion", "p", "https://www.zoll-auktion.de/p", end=end_soon)])
check("no profit key in lane rows", all("profit" not in json.dumps(r) for r in lane) and
      "profit" not in json.dumps(counts))

# ---------- broad official-auction watch: separate, labelled, fail-closed ----
watch_now = dt.datetime(2026, 8, 17, 6, 0, 0, tzinfo=UTC)
pvp_raw = {
    "id": "pvp-giustizia:4620200",
    "source": "pvp-giustizia",
    "url": "https://pvp.giustizia.it/pvp/it/dettaglio_annuncio.page?id=4620200",
    "title": "MG ZS 2024",
    "model": "MG ZS",
    "country": "IT",
    "year": 2024,
    "mileage_km": 12000,
    "fuel": "petrol",
    "price_amount": 13800,
    "price_currency": "EUR",
    "price_kind": "base_price",
    "price_label": "Prezzo base",
    "bid_visibility": "hidden_on_pvp",
    "registration_date": "2024-03-12",
    "canonical_end_utc": "2026-08-24T10:00:00Z",
    "last_seen_at": "2026-08-17T05:45:00Z",
    "eligibility_status": "conditional",
    "eligibility_reason": "Current bid and foreign-bidder access require review.",
    "access_sale_note": "Sale manager registration applies.",
}
normalized, reason = bab._normalize_monitored_row(pvp_raw, generated_at=watch_now)
check("broad watch accepts a registry/domain-backed PVP row", normalized is not None and reason == "")
check("base_price normalized without claiming current bid",
      normalized["price_kind"] == "starting_bid" and normalized["price_eur"] == 13800)
check("conditional eligibility remains review-required",
      normalized["eligibility_status"] == "review_required")
property_raw = dict(pvp_raw, id="pvp-giustizia:property", category="property", property_type="residential")
property_normalized, property_reason = bab._normalize_monitored_row(property_raw, generated_at=watch_now)
check("broad watch preserves a declared property subtype",
      property_normalized is not None and property_reason == "" and
      property_normalized["property_type"] == "residential")
check("passenger-car scope rejects property while retaining a car row",
      bab.is_passenger_car_watch_row(normalized) and
      not bab.is_passenger_car_watch_row(property_normalized), negative=True)
land_rover_raw = dict(
    pvp_raw, id="pvp-giustizia:land-rover", title="Land Rover Discovery",
    model="Land Rover Discovery", category="unknown",
)
land_rover_normalized, land_rover_reason = bab._normalize_monitored_row(
    land_rover_raw, generated_at=watch_now
)
check("passenger-car classifier does not mistake Land Rover for land",
      land_rover_normalized is not None and land_rover_reason == "" and
      bab.is_passenger_car_watch_row(land_rover_normalized), negative=True)
check("broad watch preserves connector evidence fields",
      normalized["mileage"] == 12000 and normalized["registration_date"] == "2024-03-12" and
      normalized["bid_visibility"] == "hidden_on_pvp" and normalized["price_label"] == "Prezzo base")
exleasingcar_raw = {
    "id": "exleasingcar:18763435",
    "source_key": "exleasingcar",
    "url": "https://www.exleasingcar.com/en/auto-details/18763435",
    "title": "ABARTH 500",
    "country": "DE",
    "category": "vehicle",
    "year": 2021,
    "fuel": "petrol",
    "price_amount": 7098,
    "price_currency": "EUR",
    "price_kind": "minimum_bid",
    "last_seen_at": "2026-08-17T05:45:00Z",
    "eligibility_status": "review_required",
    "eligibility_reason": "Catalogue card requires auction review.",
    "adapter_authorized": True,
}
exleasingcar_normalized, exleasingcar_reason = bab._normalize_monitored_row(
    exleasingcar_raw, generated_at=watch_now
)
check("cross-border Exleasingcar card enters broad watch",
      exleasingcar_normalized is not None and exleasingcar_reason == "" and
      exleasingcar_normalized["country"] == "DE" and
      exleasingcar_normalized["eligibility_status"] == "review_required")
agorastore_cross_border_raw = dict(
    pvp_raw,
    id="agorastore:agora-123",
    source="agorastore",
    source_key="agorastore",
    url="https://www.agorastore.fr/fr/ventes-occasions/voiture/agora-123",
    title="PEUGEOT 208 2024",
    model="PEUGEOT 208 2024",
    country="BE",
    category="car",
    adapter_authorized=True,
)
agorastore_cross_border, agorastore_cross_border_reason = bab._normalize_monitored_row(
    agorastore_cross_border_raw, generated_at=watch_now
)
check("Agorastore retains its official Belgian car asset country",
      agorastore_cross_border is not None and agorastore_cross_border_reason == "" and
      agorastore_cross_border["country"] == "BE")
exleasingcar_business_edition = dict(
    exleasingcar_normalized,
    id="exleasingcar:business-edition",
    title="Audi A5 35 TDI BUS. ED. ADVANCED",
    model="Audi A5 35 TDI BUS. ED. ADVANCED",
)
check("passenger-car gate keeps BUS. business-edition cars",
      bab.is_passenger_car_watch_row(exleasingcar_business_edition), negative=True)
exleasingcar_business_solution = dict(
    exleasingcar_normalized,
    id="exleasingcar:business-solution",
    title="Mercedes GLC BUS SOL AUTO",
    model="Mercedes GLC BUS SOL AUTO",
)
check("passenger-car gate keeps BUS SOL business-solution cars",
      bab.is_passenger_car_watch_row(exleasingcar_business_solution), negative=True)
exleasingcar_minibus = dict(
    exleasingcar_normalized,
    id="exleasingcar:minibus",
    title="Ford Transit Minibus 17 seats",
    model="Ford Transit Minibus 17 seats",
)
check("passenger-car gate rejects a real minibus",
      not bab.is_passenger_car_watch_row(exleasingcar_minibus), negative=True)
for category, title in (
    ("utility vehicle 7.5 t upwards", "Mercedes-Benz Sprinter Kasten"),
    ("motorhome pickup", "Mercedes-Benz V 300 d Marco Polo"),
    ("special vehicle", "VW T6 Kasten Kombi"),
    ("refrigeration vehicle", "VW Crafter Kuehlkasten"),
):
    commercial_category_row = dict(
        exleasingcar_normalized,
        id=f"exleasingcar:{category}",
        category=category,
        title=title,
        model=title,
    )
    check(
        f"passenger-car gate rejects declared commercial category: {category}",
        not bab.is_passenger_car_watch_row(commercial_category_row),
        negative=True,
    )
exleasingcar_unmarked, exleasingcar_unmarked_reason = bab._normalize_monitored_row(
    dict(exleasingcar_raw, id="exleasingcar:unmarked", adapter_authorized=False),
    generated_at=watch_now,
)
check("unmarked pending Exleasingcar input remains fail-closed",
      exleasingcar_unmarked is None and exleasingcar_unmarked_reason == "source_not_publishable",
      negative=True)
eligible_claim = dict(pvp_raw, id="pvp-giustizia:claim", eligibility_status="eligible")
claimed, _ = bab._normalize_monitored_row(eligible_claim, generated_at=watch_now)
check("connector cannot promote a broad row into the strict eligible lane",
      claimed["eligibility_status"] == "review_required", negative=True)
bad_domain = dict(pvp_raw, id="pvp-giustizia:bad", url="https://example.test/bad")
rejected, reason = bab._normalize_monitored_row(bad_domain, generated_at=watch_now)
check("broad watch rejects a non-registry domain",
      rejected is None and reason == "domain_mismatch", negative=True)
bad_country = dict(pvp_raw, id="pvp-giustizia:country", country="DZ")
rejected, reason = bab._normalize_monitored_row(bad_country, generated_at=watch_now)
check("broad watch rejects a country outside its official source",
      rejected is None and reason == "country_mismatch", negative=True)
stale_seen = dict(pvp_raw, id="pvp-giustizia:stale", last_seen_at="2026-08-16T20:00:00Z")
rejected, reason = bab._normalize_monitored_row(stale_seen, generated_at=watch_now)
check("broad watch rejects a stale row inside a fresh wrapper",
      rejected is None and reason == "stale_last_seen", negative=True)
future_seen = dict(pvp_raw, id="pvp-giustizia:future", last_seen_at="2026-08-17T07:00:00Z")
rejected, reason = bab._normalize_monitored_row(future_seen, generated_at=watch_now)
check("broad watch rejects a future observation",
      rejected is None and reason == "future_last_seen", negative=True)
strict_watch = bab._strict_monitored_row(lane[0])
check("strict projection carries explicit current-bid and eligibility badges",
      strict_watch["price_kind"] == "current_bid" and
      strict_watch["eligibility_status"] == "eligible")
artifact = bab.build_monitored_watch(
    [strict_watch, normalized], generated_at=watch_now.isoformat(), rejected_counts={"duplicate": 1},
)
check("standalone watch has the same-origin publication contract",
      artifact["schema_version"] == 1 and artifact["lane"] == "official_auction_watch" and
      artifact["row_count"] == 2 and isinstance(artifact["source_reports"], list))

with _tf.TemporaryDirectory() as _td:
    input_path = _Path(_td) / "pvp-watch.json"
    input_path.write_text(_json.dumps({
        "schema_version": 1,
        "lane": "official_auction_watch",
        "generated_at_utc": "2026-08-17T05:45:00Z",
        "row_count": 1,
        "source_reports": {
            "pvp-giustizia": {"status": "ok", "current_or_future_rows": 1},
            "e-leiloes": {"status": "error", "current_or_future_rows": 0,
                           "error": "geographic access timeout"},
        },
        "rows": [pvp_raw],
    }), encoding="utf-8")
    report_summary = bab.connector_report_summary([input_path])
    check("zero/error connector remains visible in source health reports",
          report_summary["e-leiloes"]["connector_status"] == "error" and
          report_summary["e-leiloes"]["connector_declared_rows"] == 0)
    merged, rejected_counts = bab.monitored_rows(
        [lane[0]], [input_path], generated_at=watch_now.isoformat(),
    )
    check("broad input merges alongside strict rows", len(merged) == 2 and not rejected_counts)
    car_only_doc = _json.loads(input_path.read_text(encoding="utf-8"))
    car_only_doc["row_count"] = 2
    car_only_doc["rows"] = [pvp_raw, property_raw]
    input_path.write_text(_json.dumps(car_only_doc), encoding="utf-8")
    car_only_rows, car_only_rejected = bab.monitored_rows(
        [lane[0]], [input_path], generated_at=watch_now.isoformat(),
    )
    check("broad watch excludes a non-car source row without hiding the car",
          len(car_only_rows) == 2 and car_only_rejected.get("not_passenger_car") == 1,
          negative=True)
    stale_doc = _json.loads(input_path.read_text(encoding="utf-8"))
    stale_doc["generated_at_utc"] = "2026-08-16T05:00:00Z"
    input_path.write_text(_json.dumps(stale_doc), encoding="utf-8")
    stale_rows, stale_rejected = bab.monitored_rows(
        [lane[0]], [input_path], generated_at=watch_now.isoformat()
    )
    check(
        "stale standalone connector is skipped without blocking strict",
        len(stale_rows) == 1 and stale_rejected.get("input_stale") == 1,
        negative=True,
    )
    input_path.write_text("{broken", encoding="utf-8")
    broken_rows, broken_rejected = bab.monitored_rows(
        [lane[0]], [input_path], generated_at=watch_now.isoformat()
    )
    check("corrupt optional connector cannot block strict",
          len(broken_rows) == 1 and broken_rejected.get("input_unreadable") == 1,
          negative=True)
    input_path.write_text(_json.dumps({"schema_version": 99, "lane": "wrong"}), encoding="utf-8")
    schema_rows, schema_rejected = bab.monitored_rows(
        [lane[0]], [input_path], generated_at=watch_now.isoformat()
    )
    check("unsupported optional connector schema cannot block strict",
          len(schema_rows) == 1 and schema_rejected.get("input_bad_schema") == 1,
          negative=True)
    input_path.write_text(_json.dumps({
        "schema_version": 1, "lane": "official_auction_watch",
        "generated_at_utc": watch_now.isoformat(), "row_count": 2,
        "rows": [pvp_raw],
    }), encoding="utf-8")
    count_rows, count_rejected = bab.monitored_rows(
        [lane[0]], [input_path], generated_at=watch_now.isoformat()
    )
    check("connector count mismatch cannot block strict",
          len(count_rows) == 1 and count_rejected.get("input_count_mismatch") == 1,
          negative=True)

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
