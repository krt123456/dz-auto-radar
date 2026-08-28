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
from urllib.parse import urlsplit

REGISTRY_SHA256_SOURCE = "auction_registry.py:v4"

AUTHORITATIVE_SOURCE_INVENTORY = {
    "document_entries": 129,
    "canonical_identities": 118,
    "mapped_document_entries": 129,
    "unmapped_document_entries": 0,
}

_END_FLAG_PATTERNS = (
    re.compile(r"auction_end_at", re.I),
    re.compile(r"closing\s*-?\s*(at|time|date)", re.I),
    re.compile(r"sale[_ ]term", re.I),
)

ACCEPTED_SOURCE_KEYS = frozenset({
    "elicytacje-kas",
})
LEGACY_OPERATIONAL_SOURCE_KEYS = frozenset({
    "pvp-giustizia",
    "klaravik-se", "klaravik-dk", "auksjonen", "troostwijk",
    "veacom", "vebeg", "astegiudiziarie", "portaldrazeb", "campen",
    "nav-hu", "campenauktioner",
    "boe-subastas", "kronofogden", "encheres-du-domaine", "nabidka-majetku",
    "justiz-auktion", "onlineveilingmeester", "zoll-auktion", "finshop",
    "licytacje-komornik", "copart-de", "copart-es", "copart-fi",
    "oksjonikeskus", "anabi", "eaukcionai", "sodnedrazbe", "e-arveres-mnv",
    "ropk", "nva-latvia", "caraukce", "aurena", "auctionmaster", "bilweb", "kvdcars", "kiertonet", "auktionshuset-dab",
})
BLOCKED_SOURCE_KEYS = frozenset({
    "retrade", "autoa-bid", "avariilised", "avcars",
    "autorola-eu", "autorola-at", "autorola-lu",
    "autorola-pt", "autorola-sk", "utrupe", "evg-auction",
    "e-leiloes", "caronsale", "ecarstrade", "ayvens-carmarket",
})

SOURCE_CONNECTOR_STAGES = {
    "autobid": "research_source_specific",
    "elicytacje-kas": "production_source_specific",
}

SOURCE_COVERAGE_NOTES_AR = {
    "bca-eu": "يتطلب حساب تاجر موثق؛ الوصول الآلي العام محجوب، ولا يوجد كتالوج مجهول كامل قابل للتحقق.",
    "openlane-eu": "منصة لتجار السيارات المحترفين فقط؛ الكتالوج الكامل والحساب يتطلبان تحققًا مهنيًا.",
    "autorola-eu": "محظور حتى موافقة مكتوبة؛ التفاصيل الكاملة تتطلب الدخول والشروط تمنع النسخ الآلي وإعادة النشر.",
    "auto1": "الشراء والكتالوج التشغيلي يتطلبان حساب B2B موثقًا؛ الصفحة العامة تسويقية فقط.",
    "autobid": "موصل خاص كامل بحثيًا؛ يعالج كل نتائج الكتالوج العامة، لكن النشر ينتظر حسم حق إعادة الاستخدام وحساب التاجر مطلوب للمزايدة.",
    "caronsale": "محظور حتى إذن مكتوب؛ الشروط تمنع الاستعلامات الآلية والاستخراج المنهجي وإعادة استخدام البيانات.",
    "ecarstrade": "محظور حتى إذن مكتوب؛ الشروط تمنع حفظ بيانات المنصة أو نسخها أو توزيعها أو إتاحتها للعامة.",
    "ayvens-carmarket": "محظور حتى إذن مكتوب؛ الإشعار القانوني يمنع النسخ والنشر وإعادة النشر والتخزين المؤقت دون موافقة مسبقة.",
    "autoproff": "البحث التشغيلي يعيد إلى تسجيل الدخول؛ لا يظهر كتالوج مجهول كامل صالح للحصاد.",
    "manheim-eu": "تظهر مواعيد وواجهات كتالوج عامة، لكن المشاركة والتفاصيل التشغيلية تتطلب تسجيل الدخول.",
}


def auction_source_publication_status(key: str) -> str:
    normalized = str(key or "").strip().lower()
    if normalized in ACCEPTED_SOURCE_KEYS:
        return "accepted"
    if normalized in LEGACY_OPERATIONAL_SOURCE_KEYS:
        return "migration"
    if normalized in BLOCKED_SOURCE_KEYS:
        return "blocked"
    return "pending"


@dataclasses.dataclass(frozen=True)
class AuctionSource:
    key: str  # canonical source key used in universe offers.source
    name: str
    country: str
    domains: tuple[str, ...]  # official domain suffixes to match against source_url
    priority: int  # 1 = highest priority for daily follow-up (founder ordering)
    evidence: str  # exact citation of the authoritative source for this entry
    kind: str = "official"  # official | broker (design: dealer-only lanes stay out)


def _host(url_or_domain: str) -> Optional[str]:
    """Return a normalized URL hostname without reading query/fragment text."""
    text = str(url_or_domain or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text if "://" in text else f"//{text}")
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        parsed.port  # Validate malformed ports before accepting the hostname.
        host = parsed.hostname
    except ValueError:
        return None
    if not host:
        return None
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None


# Founders priority for cars/vans (message): Zoll -> Domaine -> Justiz -> NL DRZ -> Poland.
# Design doc shortlist order (1=DRZ,2=Domaine,3=Zoll,4=Justiz,5=FinShop,6=PVP,7=BOE,8=e-Leiloes,
# 9=VEBEG,10=Alcopa,11=Huutokaupat,12=Copart).  Copart's current first-party
# Schengen country selector exposes Germany, Spain and Finland; each country is
# a separate source because bidder access rules differ.
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
                  ("auktionstorget.kronofogden.se", "auktion.kronofogden.se"), 9,
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
                  "design doc RADAR_AUCTION_DISCOVERY_20260815.md prio 12 + official Copart Europe country selector",
                  kind="broker"),
    AuctionSource("copart-es", "Copart Spain", "es",
                  ("copart.es",), 14,
                  "founder request 2026-08-22 + official Copart Europe country selector",
                  kind="broker"),
    AuctionSource("copart-fi", "Copart Finland", "fi",
                  ("copart.fi",), 14,
                  "founder request 2026-08-22 to include all Copart Schengen countries + official Copart Europe country selector",
                  kind="broker"),
    AuctionSource("oksjonikeskus", "Oksjonikeskus", "ee",
                  ("oksjonikeskus.ee",), 15,
                  "official Estonian Chamber of Bailiffs and Trustees public auction environment"),
    AuctionSource("anabi", "ANABI Licitatii Online", "ro",
                  ("anabi.just.ro",), 16,
                  "official Romanian Ministry of Justice ANABI online-auction platform"),
    AuctionSource("eaukcionai", "eAukcionai", "lt",
                  ("eaukcionai.lt",), 17,
                  "official Lithuanian Registers Centre electronic auction platform"),
    AuctionSource("sodnedrazbe", "SodneDrazbe.si", "si",
                  ("sodnedrazbe.si",), 18,
                  "official Slovenian Supreme Court electronic judicial-auction platform"),
    AuctionSource("e-arveres-mnv", "MNV Elektronikus Aukcios Rendszer", "hu",
                  ("e-arveres.mnv.hu",), 19,
                  "official Hungarian National Asset Management electronic auction system"),
    AuctionSource("ropk", "Register ponukaneho majetku statu", "sk",
                  ("ropk.sk",), 20,
                  "official Slovak state offered-property register"),
    AuctionSource("nva-latvia", "Nodrosinajuma valsts agentura", "lv",
                  ("nva.iem.gov.lv", "izsoles.ta.gov.lv"), 21,
                  "official Latvian Ministry of Interior State Provision Agency vehicle-auction notices"),

    # === Cross-border European platforms (Schengen_Trusted_Online_Car_Auctions_2026) ===
    AuctionSource("bca-eu", "BCA Europe", "eu",
                  ("bca.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Europe cross-border gateway",
                  kind="broker"),
    AuctionSource("openlane-eu", "OPENLANE Europe", "eu",
                  ("openlane.eu",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: OPENLANE Europe cross-border gateway",
                  kind="broker"),
    AuctionSource("autorola-eu", "Autorola Europe", "eu",
                  ("autorola.eu",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Autorola Europe cross-border gateway",
                  kind="broker"),
    AuctionSource("auto1", "AUTO1.com", "de",
                  ("auto1.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: European B2B wholesale platform"),
    AuctionSource("autobid", "Autobid.de", "de",
                  ("autobid.de",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Auktion & Markt AG B2B platform"),
    AuctionSource("caronsale", "CarOnSale", "de",
                  ("caronsale.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: B2B platform for Mercedes-Benz returns"),
    AuctionSource("ecarstrade", "eCarsTrade", "be",
                  ("ecarstrade.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: international B2B auction platform"),
    AuctionSource("ayvens-carmarket", "Ayvens Carmarket", "nl",
                  ("carmarket.ayvens.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Ayvens fleet lease-return platform"),
    AuctionSource("autoproff", "AutoProff", "dk",
                  ("autoproff.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: AutoScout24-backed B2B platform"),
    AuctionSource("manheim-eu", "Manheim Europe", "es",
                  ("manheim.eu",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Cox Automotive European wholesale"),
    AuctionSource("manheim-express", "Manheim Express", "de",
                  ("manheim-express.eu",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Cox Automotive digital dealer platform"),
    AuctionSource("atc-nl", "Automotive Trade Center", "nl",
                  ("automotivetradecenter.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Dutch international trade platform"),
    AuctionSource("exleasingcar", "Exleasingcar", "lt",
                  ("exleasingcar.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: international lease-return auctions"),
    AuctionSource("troostwijk", "Troostwijk Auctions", "nl",
                  ("troostwijkauctions.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: European auction house since 1930s"),
    AuctionSource("rbauction-eu", "Ritchie Bros. Europe", "nl",
                  ("rbauction.eu",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: global auction house European arm"),
    AuctionSource("ironplanet-eu", "IronPlanet Europe", "nl",
                  ("eu.ironplanet.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Ritchie Bros. online platform"),
    AuctionSource("2ndmove", "2ndMove by Europcar", "fr",
                  ("b2b.2ndmove.eu",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Europcar fleet direct B2B"),
    AuctionSource("carcollect", "CarCollect", "nl",
                  ("carcollect.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: European B2B auction exchange"),
    AuctionSource("2trde", "2trde Auctions", "de",
                  ("2trde.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: German digital B2B auction platform"),
    AuctionSource("wom", "WOM Auktion", "de",
                  ("womauktion.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Copart-linked damaged-vehicle platform"),
    AuctionSource("cars2click", "Cars2Click", "nl",
                  ("cars2click.com",), 22,
                  "Schengen_Trusted_Online_Car_Auctions_2026: European B2B remarketing platform"),

    # === Austria ===
    AuctionSource("aurena", "AURENA", "at",
                  ("aurena.at",), 31,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Austrian auction platform"),
    AuctionSource("dorotheum", "Dorotheum Vehicles", "at",
                  ("fahrzeuge.dorotheum.com",), 31,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Austrian auction house vehicles"),
    AuctionSource("autorola-at", "Autorola Austria", "at",
                  ("autorola.at",), 31,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Autorola Austrian fleet auctions"),

    # === Belgium ===
    AuctionSource("auctim", "Auctim", "be",
                  ("auctim.com",), 32,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Belgian electronic auction platform"),
    AuctionSource("vavato", "Vavato", "be",
                  ("vavato.com",), 32,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Belgian/Dutch electronic auction house"),
    AuctionSource("bca-be", "BCA Belgium", "be",
                  ("bca.com",), 32,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Belgian fleet auctions"),

    # === Bulgaria ===
    AuctionSource("zapori-mjs", "Portal drazheb MJS", "bg",
                  ("zapori.mjs.bg",), 33,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Bulgarian Ministry of Justice e-auction portal"),
    AuctionSource("nra-sales", "NRA Sales", "bg",
                  ("sales.nra.bg",), 33,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Bulgarian National Revenue Agency seized-asset sales"),

    # === Croatia ===
    AuctionSource("fina-edrazba", "FINA e-Auction", "hr",
                  ("edrazba.fina.hr",), 34,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Croatian FINA official e-auction"),
    AuctionSource("fina-ponip", "FINA Ponip", "hr",
                  ("ponip.fina.hr",), 34,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Croatian FINA asset register"),
    AuctionSource("autopoint", "Autopoint", "hr",
                  ("autopoint.eu",), 34,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Croatian/Slovenian regional auction platform"),

    # === Czech Republic ===
    AuctionSource("portaldrazeb", "Portal drazeb", "cz",
                  ("portaldrazeb.cz",), 35,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Czech Chamber of Bailiffs judicial auction portal"),
    AuctionSource("caraukce", "CarAukce", "cz",
                  ("caraukce.cz",), 35,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Czech car auction platform"),
    AuctionSource("auction24-cz", "Auction24", "cz",
                  ("auction24.cz",), 35,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Czech multi-category auction platform"),
    AuctionSource("veacom", "Veacom", "cz",
                  ("veacom.cz",), 35,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Czech professional vehicle auction platform"),

    # === Denmark ===
    AuctionSource("bca-dk", "BCA Denmark", "dk",
                  ("bca.com",), 36,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Danish fleet auctions"),
    AuctionSource("campen", "Campen Auktioner", "dk",
                  ("campenauktioner.dk",), 36,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Danish auction house"),
    AuctionSource("auktionshuset-dab", "Auktionshuset dab", "dk",
                  ("auktionshuset.dk",), 36,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Danish public vehicle auction house"),
    AuctionSource("klaravik-dk", "Klaravik Denmark", "dk",
                  ("klaravik.dk",), 36,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Scandinavian auction platform Danish arm"),
    AuctionSource("retrade", "Retrade", "dk",
                  ("retrade.eu",), 36,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Nordic surplus-asset platform"),

    # === Estonia ===
    AuctionSource("romu", "Romu", "ee",
                  ("romu.ee",), 37,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Estonian damaged/salvage car platform"),
    AuctionSource("avariilised", "Avariilised-autod", "ee",
                  ("avariilised-autod.ee",), 37,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Estonian accident-damaged car auctions"),
    AuctionSource("weby", "WEBY", "ee",
                  ("weby.ee",), 37,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Baltic insurance-asset auction platform"),

    # === Finland ===
    AuctionSource("kiertonet", "Kiertonet", "fi",
                  ("kiertonet.fi",), 38,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Finnish public-sector surplus platform"),
    AuctionSource("psauction-fi", "PS Auction Finland", "fi",
                  ("psauction.fi",), 38,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Scandinavian auction group Finnish arm"),

    # === France ===
    AuctionSource("vpauto", "VPauto", "fr",
                  ("vpauto.eu",), 39,
                  "Schengen_Trusted_Online_Car_Auctions_2026: French used-car inspection-guaranteed auctions"),
    AuctionSource("interencheres", "Interencheres", "fr",
                  ("interencheres.com",), 39,
                  "Schengen_Trusted_Online_Car_Auctions_2026: French network of certified auction houses"),
    AuctionSource("agorastore", "Agorastore", "fr",
                  ("agorastore.fr",), 39,
                  "Schengen_Trusted_Online_Car_Auctions_2026: French public-sector fleet auction platform"),
    AuctionSource("encheres-vo", "Encheres VO", "fr",
                  ("encheres-vo.com",), 39,
                  "Schengen_Trusted_Online_Car_Auctions_2026: French used-car auction network"),

    # === Germany ===
    AuctionSource("wom-de", "WOM Germany", "de",
                  ("womauktion.com",), 40,
                  "Schengen_Trusted_Online_Car_Auctions_2026: German damaged-vehicle insurance auctions"),

    # === Greece ===
    AuctionSource("auto-auctions-gr", "Auto-Auctions.gr", "gr",
                  ("auto-auctions.gr",), 41,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Greek B2B car auction platform"),
    AuctionSource("ayvens-gr", "Ayvens Carmarket Greece", "gr",
                  ("carmarket.ayvens.com",), 41,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Ayvens Greek fleet auctions",
                  kind="broker"),

    # === Hungary ===
    AuctionSource("arveres-nav", "NAV Electronic Auction", "hu",
                  ("arveres.nav.gov.hu",), 42,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Hungarian tax/customs e-auction"),
    AuctionSource("arveres-mbvk", "MBVK EAr", "hu",
                  ("arveres.mbvk.hu",), 42,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Hungarian Chamber of Bailiffs e-auction"),

    # === Iceland ===
    AuctionSource("bilauppbod", "Bilauppboð", "is",
                  ("bilauppbod.is",), 43,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Icelandic car auction platform"),

    # === Italy ===
    AuctionSource("astegiudiziarie", "Aste Giudiziarie", "it",
                  ("astegiudiziarie.it",), 44,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Italian judicial auction platform"),
    AuctionSource("gobid", "Gobid", "it",
                  ("gobid.it",), 44,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Italian judicial auction manager"),
    AuctionSource("industrial-discount", "Industrial Discount", "it",
                  ("industrialdiscount.com",), 44,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Italian liquidation/surplus platform"),
    AuctionSource("bca-it", "BCA Italy", "it",
                  ("bca.com",), 44,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Italian fleet auctions"),

    # === Latvia ===
    AuctionSource("utrupe", "Utrupe", "lv",
                  ("utrupe.lv",), 45,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Latvian damaged-car auction platform"),

    # === Lithuania ===
    AuctionSource("evarzytynes", "Evaržytynės", "lt",
                  ("evarzytynes.lt",), 46,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Lithuanian national e-auction system"),
    AuctionSource("autoa-bid", "Autoa.bid", "lt",
                  ("autoa.bid",), 46,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Lithuanian car auction platform"),
    AuctionSource("evg-auction", "EVG Auction", "lt",
                  ("evg.auction",), 46,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Lithuanian insurance-damage auction platform"),
    AuctionSource("avcars", "AVCARS", "lt",
                  ("avcars.eu",), 46,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Baltic damaged-car delivery platform"),

    # === Luxembourg ===
    AuctionSource("autorola-lu", "Autorola Luxembourg", "lu",
                  ("autorola.lu",), 47,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Autorola Luxembourg fleet auctions"),
    AuctionSource("clickar-lu", "Clickar Luxembourg", "lu",
                  ("clickar.com",), 47,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Luxembourg fleet-vehicle B2B platform"),
    AuctionSource("ayvens-lu", "Ayvens Carmarket Luxembourg", "lu",
                  ("carmarket.ayvens.com",), 47,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Ayvens Luxembourg fleet auctions",
                  kind="broker"),

    # === Malta ===
    AuctionSource("us-embassy-mt", "US Embassy Valletta Auction", "mt",
                  ("online-auction.state.gov",), 48,
                  "Schengen_Trusted_Online_Car_Auctions_2026: US Embassy surplus property auctions"),

    # === Netherlands ===
    AuctionSource("auctionmaster", "Auctionmaster", "nl",
                  ("auctionmaster.com",), 49,
                  "Schengen_Trusted_Online_Car_Auctions_2026: DRZ government vehicle auction platform"),
    AuctionSource("bca-nl", "BCA Netherlands", "nl",
                  ("bca.com",), 49,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Dutch fleet auctions"),
    AuctionSource("automotive-auctions-nl", "Automotive Auctions", "nl",
                  ("automotive-auctions.nl",), 49,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Dutch classic/specialty car auctions"),

    # === Norway ===
    AuctionSource("auksjonen", "Auksjonen.no", "no",
                  ("auksjonen.no",), 50,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Norwegian major auction platform"),
    AuctionSource("bca-no", "BCA Norway", "no",
                  ("bca.com",), 50,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Norwegian fleet auctions"),

    # === Poland ===
    AuctionSource("elicytacje-kas", "eLicytacje KAS", "pl",
                  ("elicytacje.mf.gov.pl",), 51,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Polish tax-administration e-auctions (launched July 2026)"),
    AuctionSource("pkoleasing", "PKO Leasing Auctions", "pl",
                  ("aukcje.pkoleasing.pl",), 51,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Polish PKO Leasing vehicle auctions"),
    AuctionSource("bca-pl", "BCA Poland", "pl",
                  ("bca.com",), 51,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Polish fleet auctions"),
    AuctionSource("exleasingcar-pl", "Exleasingcar Poland", "pl",
                  ("exleasingcar.pl",), 51,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Polish lease-return auction platform"),

    # === Portugal ===
    AuctionSource("manheim-pt", "Manheim Portugal", "pt",
                  ("manheim.pt",), 52,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Cox Automotive Portuguese wholesale"),
    AuctionSource("bca-pt", "BCA Portugal", "pt",
                  ("bca.com",), 52,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Portuguese fleet auctions"),
    AuctionSource("autorola-pt", "Autorola Portugal", "pt",
                  ("autorola.pt",), 52,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Autorola Portuguese fleet auctions"),

    # === Romania ===
    AuctionSource("ayvens-ro", "Ayvens Carmarket Romania", "ro",
                  ("carmarket.ayvens.com",), 53,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Ayvens Romanian fleet auctions"),
    AuctionSource("exleasingcar-ro", "Exleasingcar Romania", "ro",
                  ("exleasingcar.ro",), 53,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Romanian lease-return auctions"),

    # === Slovakia ===
    AuctionSource("autorola-sk", "Autorola Slovakia", "sk",
                  ("autorola.sk",), 54,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Autorola Slovak fleet auctions"),
    AuctionSource("ayvens-sk", "Ayvens Carmarket Slovakia", "sk",
                  ("carmarket.ayvens.com",), 54,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Ayvens Slovak fleet auctions"),

    # === Slovenia ===
    AuctionSource("edrazbe-si", "eDražbe.si", "si",
                  ("edrazbe.si",), 55,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Slovenian electronic auction platform"),

    # === Spain ===
    AuctionSource("alcopa-es", "Alcopa Auction Spain", "es",
                  ("alcopa-auction.es",), 56,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Alcopa Spanish auction arm"),
    AuctionSource("escrapalia", "Escrapalia", "es",
                  ("escrapalia.com",), 56,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Spanish online auction platform since 2012"),
    AuctionSource("northgatetrade", "Northgate Trade", "es",
                  ("northgatetrade.es",), 56,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Spanish Northgate fleet B2B"),
    AuctionSource("bca-es", "BCA Spain", "es",
                  ("bca.com",), 56,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Spanish fleet auctions"),

    # === Sweden ===
    AuctionSource("kvdcars", "KVD Cars", "se",
                  ("kvd.se", "kvdcars.com"), 57,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Swedish public vehicle auction platform"),
    AuctionSource("klaravik-se", "Klaravik Sweden", "se",
                  ("klaravik.se",), 57,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Scandinavian auction platform Swedish arm"),
    AuctionSource("psauction-se", "PS Auction Sweden", "se",
                  ("psauction.com",), 57,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Swedish auction house since 1958"),
    AuctionSource("blinto", "Blinto", "se",
                  ("blinto.se",), 57,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Swedish vehicle/machinery auction platform"),
    AuctionSource("bilweb", "Bilweb Auctions", "se",
                  ("bilwebauctions.se",), 57,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Swedish classic/collector car auctions"),
    AuctionSource("bca-se", "BCA Sweden", "se",
                  ("bca.com",), 57,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Swedish fleet auctions"),

    # === Switzerland ===
    AuctionSource("carauktion-ch", "CARAUKTION AG", "ch",
                  ("carauktion.ch",), 58,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Swiss daily car auction platform"),
    AuctionSource("restwertboerse", "Restwertbörse.ch", "ch",
                  ("restwertboerse.ch",), 58,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Swiss damaged-vehicle exchange"),
    AuctionSource("bca-ch", "BCA Switzerland", "ch",
                  ("bca.com",), 58,
                  "Schengen_Trusted_Online_Car_Auctions_2026: BCA Swiss fleet auctions"),
    AuctionSource("ayvens-ch", "Ayvens Carmarket Switzerland", "ch",
                  ("ayvens.com",), 58,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Ayvens Swiss fleet auctions"),
    AuctionSource("autoauction24-ch", "AutoAuction24.ch", "ch",
                  ("autoauction24.ch",), 58,
                  "Schengen_Trusted_Online_Car_Auctions_2026: Swiss public car auction platform"),
    AuctionSource("nav-hu", "NAV Elektronikus Arveres harvest", "hu",
                  ("arveres.nav.gov.hu",), 60,
                  "harvested 2026-08-25 via official NAV e-auction pages"),
    AuctionSource("campenauktioner", "Campen Auktioner harvest", "dk",
                  ("campenauktioner.dk",), 61,
                  "harvested 2026-08-25 from public Campen listing pages"),
]

_AUCTION_BY_KEY: Dict[str, AuctionSource] = {s.key: s for s in _AUCTION_SOURCES}
if len(_AUCTION_BY_KEY) != len(_AUCTION_SOURCES):
    raise RuntimeError("duplicate auction source key")

_AUCTION_BY_DOMAIN_LISTS: Dict[str, List[AuctionSource]] = {}
for _s in _AUCTION_SOURCES:
    for _d in _s.domains:
        _domain = _host(_d)
        if not _domain:
            raise RuntimeError(f"invalid auction source domain: {_d!r}")
        _AUCTION_BY_DOMAIN_LISTS.setdefault(_domain, []).append(_s)
_AUCTION_BY_DOMAIN = {
    domain: tuple(sources)
    for domain, sources in _AUCTION_BY_DOMAIN_LISTS.items()
}


def auction_sources() -> List[AuctionSource]:
    return sorted(_AUCTION_SOURCES, key=lambda s: s.priority)


def auction_source_by_key(key: str) -> Optional[AuctionSource]:
    return _AUCTION_BY_KEY.get(key)


def auction_sources_for_url(url: str) -> tuple[AuctionSource, ...]:
    """Return every source on the longest official domain matching ``url``."""
    host = _host(url) if url else None
    if not host:
        return ()
    best_length = -1
    matches: list[AuctionSource] = []
    for domain, sources in _AUCTION_BY_DOMAIN.items():
        if host == domain or host.endswith("." + domain):
            domain_length = len(domain)
            if domain_length > best_length:
                best_length = domain_length
                matches = list(sources)
            elif domain_length == best_length:
                matches.extend(sources)
    unique: dict[str, AuctionSource] = {}
    for source in matches:
        unique[source.key] = source
    return tuple(unique.values())


def auction_url_matches_source(url: str, expected_key: str) -> bool:
    """Match a URL to an explicit source key, including shared official hosts."""
    expected = auction_source_by_key(expected_key)
    if expected is None:
        return False
    return any(source.key == expected.key for source in auction_sources_for_url(url))


def auction_source_for_url(url: str) -> Optional[AuctionSource]:
    """Return one unambiguous source; shared-domain URLs deliberately return None."""
    matches = auction_sources_for_url(url)
    return matches[0] if len(matches) == 1 else None


def source_has_explicit_auction_semantics(source: str, raw_json: str) -> bool:
    """Explicit end/term evidence in the listing itself; never name-substring."""
    if not raw_json:
        return False
    return any(p.search(raw_json) for p in _END_FLAG_PATTERNS)


def registry_digest_json() -> str:
    return json.dumps(
        {"registry_sha256_source": REGISTRY_SHA256_SOURCE,
          "authoritative_inventory": AUTHORITATIVE_SOURCE_INVENTORY,
          "sources": [{"key": s.key, "name": s.name, "domains": list(s.domains), "priority": s.priority,
                       "publication_status": auction_source_publication_status(s.key),
                       "connector_stage": SOURCE_CONNECTOR_STAGES.get(s.key, ""),
                       "coverage_note_ar": SOURCE_COVERAGE_NOTES_AR.get(s.key, ""),
                       "evidence": s.evidence} for s in _AUCTION_SOURCES]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
