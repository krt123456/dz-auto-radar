#!/usr/bin/env python3
"""Canonical source identities shared by Radar import and ranking paths."""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit


OLX_PL_CANONICAL_SOURCE = "olx.pl"
MAX_OLX_API_ID = (1 << 32) - 1


class IdentityError(ValueError):
    """A source identity cannot be represented without ambiguity."""


def source_key(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


OLX_PL_SOURCE_KEYS = frozenset(
    {
        source_key(OLX_PL_CANONICAL_SOURCE),
        source_key("www.olx.pl"),
        source_key("OLX Poland Cars"),
    }
)
PL_MIRROR_SOURCE_KEYS = frozenset(
    {
        "otomoto",
        "motogratka",
        "sprzedajemy cars",
        "aaa auto poland",
        "autotrader.pl",
        "truck1 poland",
        *OLX_PL_SOURCE_KEYS,
    }
)
IT_MIRROR_SOURCE_KEYS = frozenset({"automobile.it", "subito motori", "subito.it"})
BE_MIRROR_SOURCE_KEYS = frozenset(
    {
        "2dehands auto's",
        "2dehands autos",
        "2dehands.be",
        "2ememain autos",
        "2ememain.be",
    }
)

_CANONICAL_OLX_ID = re.compile(r"^olxpl_([1-9][0-9]*)$", re.IGNORECASE)
_OLD_INCREMENTAL_OLX_ID = re.compile(r"^olx[.]pl_([1-9][0-9]*)$", re.IGNORECASE)
_LEGACY_FULL_OLX_ID = re.compile(
    r"^olxpl_[a-z0-9][a-z0-9_.-]*(?:_[a-z0-9][a-z0-9_.-]*)*_([1-9][0-9]*)$",
    re.IGNORECASE,
)
_AUTOSCOUT24_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_AUTOSCOUT24_NUMERIC_ID = re.compile(r"(?:^|/)[1-9][0-9]{5,}(?:/|$)")


def autoscout24_non_detail_url(value: Any) -> bool:
    """Identify AutoScout collection/search URLs that cannot prove one listing."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host.startswith("autoscout24."):
        return False
    path = parsed.path or "/"
    if "lst" in {segment.casefold() for segment in path.split("/") if segment}:
        return True
    return not (
        _AUTOSCOUT24_UUID.search(path) or _AUTOSCOUT24_NUMERIC_ID.search(path)
    )


def canonical_source(value: Any) -> str:
    cleaned = " ".join(str(value or "").split())
    if source_key(cleaned) in OLX_PL_SOURCE_KEYS:
        return OLX_PL_CANONICAL_SOURCE
    return cleaned


def olx_pl_listing_id(api_id: Any) -> str:
    """Return the one production identity for an OLX Poland API integer."""
    if isinstance(api_id, bool):
        raise IdentityError("OLX API ID is not a canonical positive decimal integer")
    text = str(api_id)
    if re.fullmatch(r"[1-9][0-9]*", text) is None:
        raise IdentityError("OLX API ID is not a canonical positive decimal integer")
    if int(text) > MAX_OLX_API_ID:
        raise IdentityError("OLX API ID is outside the supported identity range")
    return f"olxpl_{text}"


def canonical_olx_pl_listing_id(value: Any) -> str:
    """Collapse canonical, old-incremental and legacy-full OLX IDs."""
    if not isinstance(value, str) or value != value.strip():
        raise IdentityError("OLX source_listing_id is not canonical text")
    for pattern in (
        _CANONICAL_OLX_ID,
        _OLD_INCREMENTAL_OLX_ID,
        _LEGACY_FULL_OLX_ID,
    ):
        match = pattern.fullmatch(value)
        if match is not None:
            return olx_pl_listing_id(match.group(1))
    raise IdentityError("OLX source_listing_id has no unambiguous API ID")


def canonical_source_identity(source: Any, source_listing_id: Any) -> tuple[str, str]:
    normalized_source = canonical_source(source)
    normalized_listing_id = str(source_listing_id or "").strip()
    if source_key(normalized_source) == source_key(OLX_PL_CANONICAL_SOURCE):
        normalized_listing_id = canonical_olx_pl_listing_id(source_listing_id)
    return normalized_source, normalized_listing_id


def source_identity_keys(value: Any) -> frozenset[str]:
    raw_key = source_key(value)
    keys = {raw_key} if raw_key else set()
    canonical_key = source_key(canonical_source(value))
    if canonical_key:
        keys.add(canonical_key)
    if canonical_key == source_key(OLX_PL_CANONICAL_SOURCE):
        keys.update(OLX_PL_SOURCE_KEYS)
    if "autoscout24" in raw_key and raw_key not in {
        "autoscout24.ch",
        "autoscout24.ch liechtenstein",
    }:
        keys.add("autoscout24")
    return frozenset(keys)


def source_family(value: Any) -> str:
    key = source_key(canonical_source(value))
    if "autoscout24" in key:
        return (
            "autoscout24.ch"
            if key in {"autoscout24.ch", "autoscout24.ch liechtenstein"}
            else "autoscout24"
        )
    if key in PL_MIRROR_SOURCE_KEYS:
        return "pl_listing_mirrors"
    if key in IT_MIRROR_SOURCE_KEYS:
        return "it_listing_mirrors"
    if key in BE_MIRROR_SOURCE_KEYS:
        return "be_listing_mirrors"
    return key
