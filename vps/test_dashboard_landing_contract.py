#!/usr/bin/env python3
"""Dependency-free contract checks for the crawlable Arabic landing surface."""

from __future__ import annotations

import html as html_module
import json
import os
from pathlib import Path
import re
import sys


CANONICAL = "https://krt123456.github.io/dz-auto-radar/"
DEFAULT_INDEX = Path(__file__).with_name("dashboard_index.html")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def attributes(tag: str) -> dict[str, str]:
    return {
        key.lower(): html_module.unescape(value)
        for key, _, value in re.findall(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", tag, re.DOTALL)
    }


def unique_meta(source: str, attr: str, key: str) -> str:
    values = []
    for tag in re.findall(r"<meta\b[^>]*>", source, re.IGNORECASE):
        attrs = attributes(tag)
        if attrs.get(attr) == key:
            values.append(attrs.get("content", ""))
    require(len(values) == 1, f"expected one meta {attr}={key!r}, found {len(values)}")
    return values[0]


def visible_text(fragment: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(html_module.unescape(without_tags).split())


def main() -> None:
    path = Path(os.environ.get("INDEX_HTML", DEFAULT_INDEX))
    source = path.read_text(encoding="utf-8")
    lower = source.lower()

    require(re.search(r'<html\s+lang="ar"\s+dir="rtl">', source) is not None, "document must be Arabic RTL")
    title_match = re.search(r"<title>(.*?)</title>", source, re.DOTALL | re.IGNORECASE)
    require(title_match is not None, "missing title")
    title = visible_text(title_match.group(1))
    require(len(title) >= 35 and re.search(r"[\u0600-\u06ff]", title), "title must be substantive Arabic")

    description = unique_meta(source, "name", "description")
    require(len(description) >= 90 and re.search(r"[\u0600-\u06ff]", description), "description must be substantive Arabic")
    require(unique_meta(source, "name", "robots").replace(" ", "").lower() == "index,follow", "robots must be index, follow")

    canonicals = []
    for tag in re.findall(r"<link\b[^>]*>", source, re.IGNORECASE):
        attrs = attributes(tag)
        if attrs.get("rel", "").lower() == "canonical":
            canonicals.append(attrs.get("href"))
    require(canonicals == [CANONICAL], f"canonical must be exactly {CANONICAL}")

    icons = []
    for tag in re.findall(r"<link\b[^>]*>", source, re.IGNORECASE):
        attrs = attributes(tag)
        if "icon" in attrs.get("rel", "").lower().split():
            icons.append(attrs.get("href", ""))
    require(len(icons) == 1, f"expected one favicon link, found {len(icons)}")
    require(
        icons[0].startswith("data:image/svg+xml,%3Csvg") and "favicon.ico" not in icons[0].lower(),
        "favicon must be an inline SVG data URI",
    )

    expected_properties = {
        "og:type": "website",
        "og:locale": "ar_DZ",
        "og:site_name": "رادار الصفقات",
        "og:title": "رادار صفقات السيارات الأوروبية",
        "og:url": CANONICAL,
    }
    for key, expected in expected_properties.items():
        require(unique_meta(source, "property", key) == expected, f"unexpected {key}")
    require(len(unique_meta(source, "property", "og:description")) >= 50, "OG description is too thin")
    require(unique_meta(source, "name", "twitter:card") == "summary", "Twitter card must be summary")
    require(len(unique_meta(source, "name", "twitter:title")) >= 20, "Twitter title is too thin")
    require(len(unique_meta(source, "name", "twitter:description")) >= 40, "Twitter description is too thin")
    require("og:image" not in lower and "twitter:image" not in lower, "image metadata must be omitted")
    require("hreflang" not in lower, "hreflang must be omitted until variants exist")

    json_ld_blocks = re.findall(
        r'<script\s+type="application/ld\+json"\s*>(.*?)</script>',
        source,
        re.DOTALL | re.IGNORECASE,
    )
    require(len(json_ld_blocks) == 1, "expected exactly one JSON-LD block")
    structured = json.loads(json_ld_blocks[0])
    require(structured.get("@context") == "https://schema.org", "JSON-LD context must be schema.org")
    graph = structured.get("@graph")
    require(isinstance(graph, list) and len(graph) == 2, "JSON-LD graph must contain WebSite and WebPage")
    by_type = {node.get("@type"): node for node in graph}
    require(set(by_type) == {"WebSite", "WebPage"}, "only truthful WebSite and WebPage types are allowed")
    for kind, node in by_type.items():
        require(node.get("url") == CANONICAL, f"{kind} URL must equal canonical")
        require(node.get("inLanguage") == "ar", f"{kind} language must be Arabic")
        require(re.search(r"[\u0600-\u06ff]", node.get("name", "")) is not None, f"{kind} name must be Arabic")
        require(re.search(r"[\u0600-\u06ff]", node.get("description", "")) is not None, f"{kind} description must be Arabic")
    forbidden_types = {"SearchAction", "Dataset", "Product", "Offer", "ItemList"}
    require(not (forbidden_types & set(by_type)), "unsupported structured-data type present")

    landing_match = re.search(
        r'<div\s+id="lock">(.*?)<div\s+class="wrap hidden"\s+id="app">',
        source,
        re.DOTALL,
    )
    require(landing_match is not None, "public landing must precede hidden app")
    landing = landing_match.group(1)
    landing_text = visible_text(landing)
    for phrase in (
        "منهجية المقارنة",
        "الحداثة والمصدر",
        "حدود الاستخدام",
        "فتح لوحة المقارنة بالرقم السري",
        "يحمي الرقم السري تفاصيل مقارنات السيارات المرتبة",
        "بيانات رصد المزادات الرسمية وروابط مصادرها فهي عامة",
    ):
        require(phrase in landing_text, f"landing is missing required message: {phrase}")
    require(re.search(r"[0-9٠-٩]", landing_text) is None, "public landing must not publish volatile counts")
    require('<a href="official_auction_watch.json">' in landing, "public auction-watch data must have a direct link")
    require("<noscript>" in landing and "JavaScript" in landing, "landing needs a substantive no-JS message")

    for element_id in ("lock", "lockcard", "pin", "enter", "remember", "lockmsg", "app"):
        require(len(re.findall(rf'\bid="{element_id}"', source)) == 1, f"PIN flow ID {element_id!r} must remain unique")
    require(re.search(r'<div\s+class="wrap hidden"\s+id="app">', source) is not None, "app must be hidden in initial HTML")

    refresh_group_match = re.search(
        r'<div\b(?=[^>]*\bid="manualRefreshControls")[^>]*>(.*?)</div>',
        source,
        re.DOTALL | re.IGNORECASE,
    )
    require(refresh_group_match is not None, "manual refresh controls need one bounded group")
    refresh_group_tag = refresh_group_match.group(0).split(">", 1)[0] + ">"
    refresh_group_attrs = attributes(refresh_group_tag)
    require(
        re.search(r"\shidden(?:\s|=|>)", refresh_group_tag, re.IGNORECASE) is not None,
        "manual refresh controls must remain hidden while their queue consumer is disabled",
    )
    require(refresh_group_attrs.get("aria-hidden") == "true", "hidden refresh controls must leave the accessibility tree")
    refresh_group = refresh_group_match.group(1)
    for element_id in ("refreshBtn", "fullRefreshBtn", "refreshStatus"):
        require(f'id="{element_id}"' in refresh_group, f"manual refresh wiring lost {element_id}")
    compact_source = re.sub(r"\s+", "", source)
    require(
        ".refreshctl[hidden]{display:none!important}" in compact_source,
        "author styles must not override the hidden refresh group",
    )

    runtime_match = re.search(r'<script>\s*"use strict";(.*?)</script>', source, re.DOTALL)
    require(runtime_match is not None, "dashboard runtime script is missing")
    runtime = runtime_match.group(1)
    decrypt_at = runtime.find('crypto.subtle.decrypt({name:"AES-GCM"')
    await_unlock_at = runtime.find("const payload=await unlock(pin)")
    boot_at = runtime.find("boot(payload,monitored)")
    reveal = '$("lock").classList.add("hidden");$("app").classList.remove("hidden")'
    reveal_at = runtime.find(reveal)
    require(-1 not in (decrypt_at, await_unlock_at, boot_at, reveal_at), "AES-GCM unlock/reveal contract is incomplete")
    require(decrypt_at < await_unlock_at < boot_at < reveal_at, "app reveal must follow successful AES-GCM unlock and boot")
    require(runtime.count(reveal) == 1, "app must have one successful reveal path")

    print(f"DASHBOARD_LANDING_CONTRACT_PASS path={path}")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, json.JSONDecodeError, OSError) as exc:
        print(f"DASHBOARD_LANDING_CONTRACT_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
