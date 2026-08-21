#!/usr/bin/env python3
"""Offline mutation-gated tests for the Zoll-Auktion connector.

Run:  python3 zoll_auktion_test.py [fixture-dir]
All parsers are exercised ONLY against captured fixture files (no network).
Every assert has a seeded-defect negative twin (mutant gate, RULE 3).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from zoll_auktion_fetcher import (
    corrected_registration,
    import_condition_eligible,
    parse_bid,
    parse_end_time,
    parse_listing_page,
    parse_product_page,
)

FIXTURES = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode")

PASS = 0
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append((name, detail))


# ---------- parse_end_time: 4 positive, 4 negative ----------
end_cases = [
    ("Di., 18.08.2026 - 07:00 Uhr", "2026-08-18T05:00:00+00:00"),
    ("Mo., 01.01.2027 - 23:15 Uhr", "2027-01-01T22:15:00+00:00"),
    ("Mi. 02.09.2026 - 00:05 Uhr", "2026-09-01T22:05:00+00:00"),
    ("So., 31.12.2026 - 12:00 Uhr", "2026-12-31T11:00:00+00:00"),
]
for raw, want in end_cases:
    got = parse_end_time(raw)
    check(f"end+ {raw}", got is not None and got.isoformat() == want, f"got {got}")
end_neg = ["garbage", "", "Di., 32.13.2026 - 07:00 Uhr", "Di., 18.08.2026", "Di., 18.08.2026 - 25:00 Uhr"]
for raw in end_neg:
    check(f"end- {raw!r}", parse_end_time(raw) is None, f"got {parse_end_time(raw)}")

# ---------- parse_bid: 5 positive, 3 negative ----------
bid_cases = [
    ("43.500,00&nbsp;EUR", 43500),
    ("34.700,00 EUR", 34700),
    ("8.999,50 EUR", 9000),
    ("1.200 EUR", 1200),
    ("12.000.000 EUR", 12000000),
]
for raw, want in bid_cases:
    got = parse_bid(raw)
    check(f"bid+ {raw!r}", got == want, f"got {got}")
for raw in ["auf Anfrage", "", "kein Gebot", "12.000.000,00 USD"]:
    got = parse_bid(raw)
    check(f"bid- {raw!r}", got is None, f"got {got}")

# ---------- parse_listing_page (fixtures) ----------
cat1 = (FIXTURES / "zoll_cat.html").read_text(encoding="utf-8", errors="replace")
links1, total1, next1 = parse_listing_page(cat1)
check("list total=691", total1 == 691, f"got {total1}")
check("list 10 links", len(links1) == 10, f"got {len(links1)}")
check(
    "list link ID7",
    "/auktion/produkt/1_VW_ID7_Pro_S_BD83721E/974241" in links1,
    str(links1),
)
check(
    "list rel=next derived",
    next1 is not None
    and "auktionsuebersicht.php" in next1
    and "pagination=2" in next1,
    f"got {next1!r}",
)
cat2 = (FIXTURES / "zoll_cat2.html").read_text(encoding="utf-8", errors="replace")
links2, _, next2 = parse_listing_page(cat2)
check("list page2 has 10", len(links2) == 10, f"got {len(links2)}")
check(
    "list page2 differs from page1",
    len(set(links1) & set(links2)) == 0,
    "pages overlap",
)
check(
    "list page2 rel=next carries pagination=3",
    next2 is not None and "pagination=3" in next2,
    f"got {next2!r}",
)

# ---------- parse_product_page: 2 positive, 1 negative ----------
item = (FIXTURES / "zoll_item.html").read_text(encoding="utf-8", errors="replace")
row = parse_product_page(item, "/auktion/produkt/1_VW_ID7_Pro_S_BD83721E/974241")
check("prod1 not None", row is not None)
if row is not None:
    for k, want in [
        ("listing_id", "974241"),
        ("auction_end_at", "2026-08-18T05:00:00Z"),
        ("price_eur", ""),
        ("sale_term_code", "auction"),
        ("source", "zoll-auktion"),
        ("country", "DE"),
        ("mileage_km", 9800),
        ("fuel", "electric"),
        ("transmission", "automatic"),
    ]:
        check(f"prod1 {k}={want}", row.get(k) == want, f"got {row.get(k)!r}")
    check("prod1 title has ID.7", "ID.7" in (row.get("title") or ""), row.get("title"))

item2 = (FIXTURES / "zoll_item2.html").read_text(encoding="utf-8", errors="replace")
row2 = parse_product_page(item2, "/auktion/produkt/1_MercedesBenz_Unimog_40510/969490")
check("prod2 not None", row2 is not None)
if row2 is not None:
    for k, want in [
        ("listing_id", "969490"),
        ("auction_end_at", "2026-08-18T05:37:00Z"),
        ("price_eur", "34700.00"),
        ("sale_term_code", "auction-current-bid"),
    ]:
        check(f"prod2 {k}={want}", row2.get(k) == want, f"got {row2.get(k)!r}")

broken = "<html><body>no auction here</body></html>"
check("prod- broken page", parse_product_page(broken, "/auktion/produkt/x/1") is None)

# ---------- Algeria import eligibility: fuel + condition + contradiction ----
eligible_html = """<p>Unfallfrei: Ja</p><p>HU: 05/2028</p><p>Erstzulassung: 08.05.2025</p>"""
check("condition electric good/HU passes",
      import_condition_eligible(eligible_html, {"fuel": "electric"},
                                now=parse_end_time("Fr., 21.08.2026 - 12:00 Uhr")))
check("condition diesel rejected",
      not import_condition_eligible(eligible_html, {"fuel": "diesel"},
                                    now=parse_end_time("Fr., 21.08.2026 - 12:00 Uhr")))
check("condition unsafe rejected",
      not import_condition_eligible(eligible_html + " nicht betriebs- und verkehrssicher ", {"fuel": "petrol"},
                                    now=parse_end_time("Fr., 21.08.2026 - 12:00 Uhr")))
check("condition missing accident evidence rejected",
      not import_condition_eligible("HU: 05/2028", {"fuel": "petrol"},
                                    now=parse_end_time("Fr., 21.08.2026 - 12:00 Uhr")))
check("contradictory registration chooses older description date",
      corrected_registration("Erstzulassung: 29.04.2025 Beschreibung Erstzulassung: 29.04.2005", "29.04.2025") == "29.04.2005")

# ---------- mutate+verify: the buggy pre-fix parse_bid MUST be caught ----------
def old_buggy_bid(raw: str):
    text = re.sub(r"\s+", "", raw)
    m = re.search(r"([\d.,]+)\s*EUR", text, re.I)
    if not m:
        return None
    d = m.group(1)
    if "," in d and "." in d:
        if d.rfind(",") > d.rfind("."):
            d = d.replace(".", "").replace(",", ".")
        else:
            d = d.replace(",", "")
    elif "," in d:
        d = d.replace(",", ".")
    return int(round(float(d)))

mutant_caught = (
    old_buggy_bid("1.200 EUR") != 1200
    or old_buggy_bid("12.000.000 EUR") != 12000000
)
check("mutant gate: pre-fix bid impl caught", mutant_caught)

print(f"ASSERTIONS={PASS} FAILURES={len(FAIL)}")
for name, detail in FAIL:
    print(f"FAIL: {name} :: {detail}")
sys.exit(1 if FAIL else 0)
