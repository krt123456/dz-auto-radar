# -*- coding: utf-8 -*-
"""Positive source registry for the radar auction lane.

Authoritative sources for every entry (RULE 1: no invented allow-lists):
  - Founder message mgr-e325f6c9e1fb46caa29d008a75a1e20d (telegram:update:27023685)
    listing the ten official European auction venues to add to the GitHub app.
  - RADAR_AUCTION_DISCOVERY_20260815.md (accepted executive design) primary-source
    shortlist and its decision: never infer an auction from a source-name substring;
    build the lane only from a positive registry with a canonical UTC end, EUR bid,
    link validation, access/sale semantics, and a source evidence key.

A source NOT in this registry never enters the auction lane (fail closed to
unknown / exclude-from-lane, never to invalid / reject the row globally).
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Dict, List, Optional

REGISTRY_SHA256_SOURCE = "auction_registry.py:v1"

_END_FLAG_PATTERNS = (
    re.compile(r"auction_end_at", re.I),
    re.compile(r"closing\s*-?\s*(at|time|date)", re.I),
    re.compile(r"sale[_ ]term", re.I),
)


@dataclasses.dataclass(frozen=True)
class AuctionSource:
    key: str  # canonical source key used in universe offers.source
    name: str
    country: str
    domains: tuple[str, ...]  # official domain suffixes to match against source_url
    priority: int  # 1 = highest priority for daily follow-up (founder ordering)
    evidence: str  # exact citation of the authoritative source for this entry
    kind: str = "official"  # official | broker (design: dealer-only lanes stay out)


def _e(url_or_domain: str) -> Optional[str]:
    m = re.match(r"https?://([^/]+)", url_or_domain)
    return (m.group(1).lower() if m else url_or_domain.lower()).lstrip(".")


# Founders priority for cars/vans (message): Zoll -> Domaine -> Justiz -> NL DRZ -> Poland.
# Design doc shortlist order (1=DRZ,2=Domaine,3=Zoll,4=Justiz,5=FinShop,6=PVP,7=BOE,8=e-Leiloes,
# 9=VEBEG,10=Alcopa,11=Huutokaupat,12=Copart).
# We keep the founder's ranking where it is explicit and map design-only entries after them.
_AUCTION_SOURCES: List[AuctionSource] = [
    AuctionSource("zoll-auktion", "Zoll-Auktion", "de",
                  ("zoll-auktion.de",), 1,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d + design doc prio 3"),
    AuctionSource("encheres-du-domaine", "Les Enchères du Domaine", "fr",
                  ("encheres-domaine.gouv.fr",), 2,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d + design doc prio 2"),
    AuctionSource("justiz-auktion", "Justiz-Auktion", "de",
                  ("justiz-auktion.de",), 3,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d + design doc prio 4"),
    AuctionSource("domeinenrz", "Domeinen Roerende Zaken", "nl",
                  ("domeinenrz.nl",), 4,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d + design doc prio 1"),
    AuctionSource("onlineveilingmeester", "Onlineveilingmeester", "nl",
                  ("onlineveilingmeester.nl",), 4,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d (NL auctions hosted here) + design doc prio 1"),
    AuctionSource("licytacje-komornik", "Licytacje Komornicze", "pl",
                  ("licytacje.komornik.pl",), 5,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d (Poland, cars/vans recommendation)"),
    AuctionSource("finshop", "Fin Shop", "be",
                  ("finshop.belgium.be", "fin.belgium.be"), 6,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d + design doc prio 5"),
    AuctionSource("pvp-giustizia", "Portale Vendite Pubbliche", "it",
                  ("pvp.giustizia.it",), 7,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d + design doc prio 6"),
    AuctionSource("boe-subastas", "BOE Subastas", "es",
                  ("subastas.boe.es",), 8,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d + design doc prio 7"),
    AuctionSource("kronofogden", "Kronofogden Auktionstorget", "se",
                  ("auktionstorget.kronofogden.se",), 9,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d"),
    AuctionSource("e-leiloes", "e-Leilões", "pt",
                  ("e-leiloes.pt",), 10,
                  "founder mgr-e325f6c9e1fb46caa29d008a75a1e20d + design doc prio 8"),
    AuctionSource("nabidka-majetku", "Nabidka majetku UZSVM", "cz",
                  ("nabidkamajetku.gov.cz",), 11,
                  "official Czech UZSVM state-property auction system and public AuctionList API"),
    AuctionSource("vebeg", "VEBEG", "de",
                  ("vebeg.de",), 11,
                  "design doc RADAR_AUCTION_DISCOVERY_20260815.md prio 9"),
    AuctionSource("alcopa", "Alcopa Auction", "fr",
                  ("alcopa-auction.fr", "alcopa.fr"), 12,
                  "design doc RADAR_AUCTION_DISCOVERY_20260815.md prio 10"),
    AuctionSource("huutokaupat", "Huutokaupat", "fi",
                  ("huutokaupat.com",), 13,
                  "design doc RADAR_AUCTION_DISCOVERY_20260815.md prio 11"),
    AuctionSource("copart-de", "Copart Germany", "de",
                  ("copart.de",), 14,
                  "design doc RADAR_AUCTION_DISCOVERY_20260815.md prio 12"),
]

_AUCTION_BY_KEY: Dict[str, AuctionSource] = {s.key: s for s in _AUCTION_SOURCES}
_AUCTION_BY_DOMAIN: Dict[str, AuctionSource] = {}
for _s in _AUCTION_SOURCES:
    for _d in _s.domains:
        _AUCTION_BY_DOMAIN[_d] = _s


def auction_sources() -> List[AuctionSource]:
    return sorted(_AUCTION_SOURCES, key=lambda s: s.priority)


def auction_source_by_key(key: str) -> Optional[AuctionSource]:
    return _AUCTION_BY_KEY.get(key)


def auction_source_for_url(url: str) -> Optional[AuctionSource]:
    """Positive-domain match only. Returns None (lane-exclude, fail closed)
    for any source not in the registry regardless of name similarity."""
    host = _e(url) if url else ""
    if not host:
        return None
    for domain, source in _AUCTION_BY_DOMAIN.items():
        if host == domain or host.endswith("." + domain):
            return source
    return None


def source_has_explicit_auction_semantics(source: str, raw_json: str) -> bool:
    """Explicit end/term evidence in the listing itself; never name-substring."""
    if not raw_json:
        return False
    return any(p.search(raw_json) for p in _END_FLAG_PATTERNS)


def registry_digest_json() -> str:
    return json.dumps(
        {"registry_sha256_source": REGISTRY_SHA256_SOURCE,
         "sources": [{"key": s.key, "domains": list(s.domains), "priority": s.priority,
                      "evidence": s.evidence} for s in _AUCTION_SOURCES]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
