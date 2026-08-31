#!/usr/bin/env python3
"""Auksjonen.no v2: JSON API based full harvest."""
import requests, re, json, time, os
import concurrent.futures as cf
import datetime as dt

UTC = dt.timezone.utc
BASE = "https://auksjonen.no"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120", "Accept": "application/json"}
H_HTML = {"User-Agent": H["User-Agent"]}
s = requests.Session()
CATEGORIES = ["bruktbil", "lastebil_og_henger", "campingvogn", "torget", "landbruk", "bygg_og_anlegg"]

def get(url, headers=None):
    for attempt in range(3):
        try:
            r = s.get(url, headers=headers or H, timeout=20)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 403, 503):
                time.sleep(1.5 * (attempt + 1))
            else:
                return None
        except Exception:
            time.sleep(1)
    return None

# 1) collect IDs from list pages (light HTML)
all_ids = {}
for cat in CATEGORIES:
    page = 1
    while page <= 60:
        u = f"{BASE}/auksjoner/{cat}" if page == 1 else f"{BASE}/auksjoner/{cat}?p={page}"
        r = get(u, headers=H_HTML)
        if not r:
            break
        found = re.findall(r'href="(/auksjon/[^"]+/(\d+))"', r.text)
        new = 0
        for path, oid in found:
            if oid not in all_ids:
                all_ids[oid] = f"https://www.auksjonen.no{path}"
                new += 1
        if new == 0:
            break
        page += 1
print(f"IDs: {len(all_ids)}", flush=True)

BRANDS = re.compile(r"\b(renault|subaru|smart|mercedes|nissan|volkswagen|vw|audi|bmw|opel|ford|toyota|skoda|škoda|peugeot|citroen|fiat|volvo|hyundai|kia|honda|mazda|mitsubishi|suzuki|dacia|seat|jeep|porsche|tesla|man\b|scania|iveco|daf\b|alfa|lancia|lexus|jaguar|mini\b|land\s*rover)\b", re.I)
now = dt.datetime.now(UTC)

FX_RATE, FX_DATE = None, ""


def detail(oid_url):
    oid, url = oid_url
    r = get(f"{BASE}/api/auction/{oid}")
    if not r:
        return None
    try:
        d = r.json()
    except Exception:
        return None
    title = str(d.get("title") or "")
    if not title:
        return None
    det = {str(a): str(b) for a, b in (d.get("details") or []) if isinstance((a, b), tuple)}
    merke = det.get("Merke") or det.get("Make") or ""
    # vehicle gate: known brand OR car-ish categories/title words
    cat_ok = str(d.get("category2") or "") in ("2006", "2007", "2008", "2010")
    if not (BRANDS.search(title) or BRANDS.search(merke) or cat_ok or re.search(r"(?i)(bil\b|truck|lastebil|campingvogn)", title)):
        return None
    year = None
    try:
        year = int(det.get("Årsmodell") or det.get("Arsmodell") or det.get("Year") or 0) or None
    except Exception:
        year = None
    if year and not (1950 <= year <= now.year + 1):
        year = None
    km = None
    try:
        km = int(re.sub(r"[^\d]", "", det.get("Kilometerstand") or "")) or None
    except Exception:
        km = None
    fuel_map = {"bensin": "petrol", "diesel": "diesel", "elektrisk": "electric", "hybrid": "hybrid", "gpl": "lpg", "metan": "cng"}
    fuel = "unknown"
    drivstoff = det.get("Drivstoff") or ""
    for k, v in fuel_map.items():
        if k in drivstoff.lower():
            fuel = v
            break
    if fuel == "unknown":
        fm = re.search(r"(?i)\b(diesel|bensin|elektrisk|hybrid)\b", title)
        if fm:
            fuel = {"bensin": "petrol", "elektrisk": "electric"}.get(fm.group(1).lower(), fm.group(1).lower())
    amount = d.get("currentBidAmount")
    try:
        amount = float(amount) if amount else None
    except Exception:
        amount = None
    currency = str(d.get("currency") or "NOK").strip().upper() or "NOK"
    fx_rate = FX_RATE
    end_ms = d.get("endTime")
    end_utc = None
    try:
        end_utc = dt.datetime.fromtimestamp(end_ms / 1000.0, tz=UTC).isoformat()
    except Exception:
        pass
    status = str(d.get("status") or "")
    if end_utc and end_utc < now.isoformat() and status == "FINISHED":
        return None
    return {
        "id": f"auksjonen:{oid}", "source": "auksjonen", "source_key": "auksjonen",
        "source_name": "Auksjonen.no", "country": "no", "url": url,
        "title": title[:200], "model": (det.get("Modell") or title)[:200], "year": year,
        "registration_date": None, "mileage_km": km, "fuel": fuel,
        "price_amount": to_eur(amount, fx_rate) if (amount is not None and fx_rate) else amount,
        "price_currency": "EUR" if (amount is not None and fx_rate) else currency,
        "price_eur": to_eur(amount, fx_rate) if (amount is not None and fx_rate) else None,
        "price_kind": "current_bid" if amount else "unknown",
        "price_label": (f"current bid {amount} {currency} (EUR, ECB daily rate)" if amount else ""),
        "canonical_end_utc": end_utc, "sale_end_utc": end_utc, "sale_event_utc": None,
        "last_seen_at": now.isoformat(), "eligibility_status": "review_required",
        "eligibility_reason": "Norwegian public auction; verify terms before bidding.",
        "bid_visibility": "public" if amount else "unknown",
        "access_sale_note": "", "auction_status": "active" if status != "FINISHED" else "closed",
        "damage": "", "documents": "",
        "description": str(d.get("description") or "")[:2500],
        "registration_no": det.get("Reg.nr.") or None,
    }

FX_RATE, FX_DATE = fetch_ecb_units_per_eur("NOK")
print(f"fx NOK/EUR {FX_RATE} ({FX_DATE})", flush=True)
rows = []
items = sorted(all_ids.items())
with cf.ThreadPoolExecutor(max_workers=3) as pool:
    futs = [pool.submit(detail, it) for it in items]
    for i, fut in enumerate(cf.as_completed(futs), 1):
        res = fut.result()
        if res:
            rows.append(res)
        if i % 300 == 0:
            print(f"progress {i}/{len(items)} rows={len(rows)}", flush=True)

rows.sort(key=lambda x: (x.get("canonical_end_utc") or "9999", x["id"]))
out = {"schema_version": 1, "lane": "official_auction_watch",
       "generated_at_utc": now.isoformat(), "row_count": len(rows), "rows": rows,
       "source_reports": {"auksjonen": {"status": "ok", "catalogue_total": len(items),
                                        "accepted_vehicle_rows": len(rows)}}}
tmp = "/var/lib/sonardeals-radar/runtime/auksjonen_watch.json.tmp"
with open(tmp, "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
os.replace(tmp, "/var/lib/sonardeals-radar/runtime/auksjonen_watch.json")
print(f"AUKSJONEN V2 DONE: {len(rows)} rows", flush=True)
