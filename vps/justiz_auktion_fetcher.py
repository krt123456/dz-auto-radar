#!/usr/bin/env python3
"""Fetch live vehicle lots from the official Justiz-Auktion website."""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


ORIGIN = "https://www.justiz-auktion.de"
CATEGORY = ORIGIN + "/Fahrzeuge~1848"
BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/135 Safari/537.36",
           "Accept-Language": "de-DE,de;q=0.9,en;q=0.7"}
FIELDNAMES = [
    "listing_id", "model_key", "title", "source", "source_url",
    "first_registration_date", "fuel", "engine_cc", "mileage_km",
    "price_eur", "seller_type", "accident_free", "service_history",
    "transmission", "country", "auction_end_at", "sale_term_code",
    "sale_certainty", "sale_certainty_note",
]
DETAIL_LINK_RE = re.compile(r"-(\d{4,})$")
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
END_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")
MONEY_RE = re.compile(r"([\d.]+(?:,\d{1,2})?)\s*(?:€|EUR|Euro)", re.I)
MILEAGE_RE = re.compile(r"Kilometerstand\s*:\s*([\d.]+)", re.I)
YEAR_HINT_RE = re.compile(r"(?:Baujahr|Bj\.?)\s*[:.]?\s*(20\d{2}|19\d{2})", re.I)


def compact(value: str) -> str:
    return " ".join(html.unescape(value or "").split())


def money(value: str) -> int:
    match = MONEY_RE.search(compact(value))
    if not match:
        return 0
    return int(round(float(match.group(1).replace(".", "").replace(",", "."))))


def model_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:80]


def detail_links(markup: str) -> list[str]:
    soup = BeautifulSoup(markup, "html.parser")
    found: dict[str, str] = {}
    for anchor in soup.select("ul.auktionen a[href]"):
        href = str(anchor.get("href") or "").strip()
        match = DETAIL_LINK_RE.search(href)
        if match:
            found[match.group(1)] = urljoin(ORIGIN + "/", href)
    return list(found.values())


def labelled_values(soup: BeautifulSoup) -> dict[str, str]:
    values: dict[str, str] = {}
    for term in soup.find_all("dt"):
        label = compact(term.get_text(" ", strip=True)).rstrip(":")
        definition = term.find_next_sibling("dd")
        if label and definition:
            values[label] = compact(definition.get_text(" ", strip=True))
    return values


def parse_detail(markup: str, url: str, *, now: datetime | None = None) -> dict[str, str] | None:
    now = now or datetime.now(UTC)
    soup = BeautifulSoup(markup, "html.parser")
    id_match = DETAIL_LINK_RE.search(url)
    title_node = soup.select_one("h2.auktionstitel")
    if not id_match or not title_node:
        return None
    title = compact(title_node.get_text(" ", strip=True))
    values = labelled_values(soup)
    end_text = values.get("Versteigerungsende", "")
    if not end_text:
        candidates = [value for label, value in values.items() if "ende" in label.lower()]
        end_text = candidates[0] if candidates else ""
    end_match = END_RE.search(end_text)
    if not end_match:
        return None
    day, month, year, hour, minute, second = [int(value or 0) for value in end_match.groups()]
    try:
        end = datetime(year, month, day, hour, minute, second, tzinfo=BERLIN).astimezone(UTC)
    except ValueError:
        return None
    if end <= now:
        return None
    bid = money(values.get("Aktuelles Gebot", ""))
    if bid <= 0:
        return None
    description_node = soup.select_one("#beschreibung") or soup.select_one("div.beschreibung")
    description = compact((description_node or soup).get_text(" ", strip=True))
    if (
        not re.search(r"Fahrbereit\s*:\s*Ja\b", description, re.I)
        or not re.search(r"Papiere\s+vorhanden\s*:\s*Ja\b", description, re.I)
        or re.search(r"(?:nicht\s+fahrbereit|Motorschaden|Unfallfahrzeug|Bastlerfahrzeug)", description, re.I)
    ):
        return None
    registration_match = re.search(r"Erstzulassung\s*:\s*(\d{1,2}\.\d{1,2}\.\d{4})", description, re.I)
    registration = registration_match.group(1) if registration_match else ""
    registration_year = int(DATE_RE.search(registration).group(3)) if DATE_RE.search(registration) else 0
    hinted_years = [int(value) for value in YEAR_HINT_RE.findall(description)]
    vehicle_year = min(hinted_years) if hinted_years else registration_year
    # Contradictory descriptions fail toward the older, explicit Baujahr and
    # cannot accidentally turn an old car into an import-eligible 2026 car.
    if registration_year and hinted_years and min(hinted_years) < registration_year:
        vehicle_year = min(hinted_years)
    if not 2023 <= vehicle_year <= 2026:
        return None
    mileage_match = MILEAGE_RE.search(description)
    mileage = mileage_match.group(1).replace(".", "") if mileage_match else ""
    fuel_match = re.search(r"(?:Antriebsart/)?Kraftstoff\s*:\s*([^\n|;]+?)(?=\s+(?:Getriebeart|Fahrbereit|Schlüssel|Papiere)\s*:|$)", description, re.I)
    fuel = compact(fuel_match.group(1)) if fuel_match else ""
    transmission_match = re.search(r"Getriebeart\s*:\s*([^\n|;]+?)(?=\s+(?:Fahrbereit|Schlüssel|Papiere)\s*:|$)", description, re.I)
    transmission = compact(transmission_match.group(1)) if transmission_match else ""
    engine_match = re.search(r"Hubraum\s*\(cm.\)\s*:\s*([\d.]+)", description, re.I)
    country = "AT" if "Österreich Fahne" in markup or "Oesterreich Fahne" in markup else "DE"
    return {
        "listing_id": id_match.group(1), "model_key": model_key(title), "title": title,
        "source": "justiz-auktion", "source_url": url,
        "first_registration_date": registration, "fuel": fuel,
        "engine_cc": (engine_match.group(1).replace(".", "") if engine_match else ""),
        "mileage_km": mileage, "price_eur": f"{bid}.00", "seller_type": "auction",
        "accident_free": "unknown", "service_history": "unknown",
        "transmission": transmission, "country": country,
        "auction_end_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sale_term_code": "auction-current-bid", "sale_certainty": "auction",
        "sale_certainty_note": (
            "Official German/Austrian justice auction with an explicit current bid, "
            "roadworthy statement and vehicle papers; verify lot conditions before bidding."
        ),
    }


def fetch(session: requests.Session, url: str, timeout: int) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def harvest(*, max_pages: int, timeout: int, sleep_seconds: float) -> tuple[list[dict[str, str]], dict[str, int]]:
    session = requests.Session()
    links: dict[str, str] = {}
    page_url = CATEGORY
    for page in range(max_pages):
        markup = fetch(session, page_url, timeout)
        for url in detail_links(markup):
            links[url] = url
        soup = BeautifulSoup(markup, "html.parser")
        next_link = soup.select_one("div.pageNext a[href]")
        if not next_link:
            break
        page_url = urljoin(CATEGORY, str(next_link.get("href")))
        if sleep_seconds:
            time.sleep(sleep_seconds)
    rows: list[dict[str, str]] = []
    excluded = 0
    for url in links:
        try:
            row = parse_detail(fetch(session, url, timeout), url)
        except requests.RequestException:
            row = None
        if row:
            rows.append(row)
        else:
            excluded += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return rows, {"discovered": len(links), "accepted": len(rows), "excluded": excluded}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--sleep", type=float, default=0.25)
    args = parser.parse_args()
    rows, report = harvest(max_pages=args.max_pages, timeout=args.timeout, sleep_seconds=args.sleep)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader(); writer.writerows(rows)
    temporary.replace(args.out)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
