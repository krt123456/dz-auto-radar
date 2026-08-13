#!/usr/bin/env python3
"""Deterministic, fail-closed lifecycle policy for public radar listings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Iterator


POLICY_VERSION = "listing-availability-v1"
PUBLIC_VALIDITY_HOURS = 8

UNAVAILABLE_STATES = frozenset(
    {
        "discontinued",
        "expired",
        "outofstock",
        "removed",
        "sold",
        "soldout",
        "unavailable",
    }
)
ACTIVE_STATES = frozenset({"instock", "limitedavailability"})
EXPIRATION_FIELDS = (
    "expires_at",
    "expires",
    "validThrough",
    "priceValidUntil",
    "endDate",
)


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    status: str
    reason: str
    availability: str | None = None
    expires_at: str | None = None


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def canonical_timestamp(value: Any) -> str | None:
    parsed = parse_timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed is not None else None


def normalized_state(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        return None
    token = value.strip().rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    normalized = "".join(character for character in token.casefold() if character.isalnum())
    return normalized or None


def decide_lifecycle(
    *,
    availability: Any = None,
    expires_at: Any = None,
    as_of: datetime,
    valid_until: datetime,
    identity_proven: bool,
) -> LifecycleDecision:
    """Classify one listing for the complete public-validity window.

    Exact boundaries fail closed: an expiration equal to ``as_of`` is expired,
    and an expiration equal to ``valid_until`` cannot be published because it
    does not extend beyond the whole validity window.
    """

    if as_of.tzinfo is None or valid_until.tzinfo is None:
        raise ValueError("availability boundaries must be timezone-aware")
    checked = as_of.astimezone(UTC)
    boundary = valid_until.astimezone(UTC)
    if boundary <= checked:
        raise ValueError("public validity window must end after observation")

    raw_expiration_present = expires_at not in (None, "")
    expiration = parse_timestamp(expires_at) if raw_expiration_present else None
    canonical_expiration = (
        expiration.isoformat().replace("+00:00", "Z")
        if expiration is not None
        else None
    )
    state = normalized_state(availability)

    if raw_expiration_present and expiration is None:
        return LifecycleDecision("unknown", "malformed_expiration", state, None)
    if expiration is not None and expiration <= checked:
        return LifecycleDecision("dead", "expired_at_or_before_observation", state, canonical_expiration)
    if state in UNAVAILABLE_STATES:
        return LifecycleDecision("dead", f"authoritative_{state}", state, canonical_expiration)
    if state is not None and state not in ACTIVE_STATES:
        return LifecycleDecision("unknown", f"unsupported_availability_{state}", state, canonical_expiration)
    if expiration is not None and expiration <= boundary:
        return LifecycleDecision("unknown", "expires_within_public_validity", state, canonical_expiration)
    if not identity_proven:
        return LifecycleDecision("unknown", "listing_identity_unproven", state, canonical_expiration)
    reason = "structured_active_identity" if state in ACTIVE_STATES else "detail_identity"
    return LifecycleDecision("verified", reason, state, canonical_expiration)


class _StructuredDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ld_json_depth = 0
        self._buffer: list[str] = []
        self.json_documents: list[Any] = []
        self.meta_availability: list[str] = []
        self.meta_expirations: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        if tag.casefold() == "script" and "ld+json" in attributes.get("type", "").casefold():
            self._ld_json_depth = 1
            self._buffer = []
        elif self._ld_json_depth:
            self._ld_json_depth += 1
        if tag.casefold() == "meta":
            name = (attributes.get("itemprop") or attributes.get("property") or attributes.get("name") or "").casefold()
            content = attributes.get("content", "").strip()
            if content and name.endswith("availability"):
                self.meta_availability.append(content)
            if content and any(name.endswith(field.casefold()) for field in EXPIRATION_FIELDS):
                self.meta_expirations.append(content)

    def handle_endtag(self, tag: str) -> None:
        if not self._ld_json_depth:
            return
        self._ld_json_depth -= 1
        if self._ld_json_depth == 0:
            raw = "".join(self._buffer).strip()
            if raw:
                try:
                    self.json_documents.append(json.loads(raw))
                except (json.JSONDecodeError, ValueError):
                    pass
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._ld_json_depth:
            self._buffer.append(data)


def _objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _objects(nested)


def structured_lifecycle(html: str) -> tuple[str | None, str | None]:
    parser = _StructuredDataParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:
        return None, None

    availability_values = list(parser.meta_availability)
    expiration_values = list(parser.meta_expirations)
    for document in parser.json_documents:
        for item in _objects(document):
            availability = item.get("availability")
            if isinstance(availability, str) and availability.strip():
                availability_values.append(availability.strip())
            for field in EXPIRATION_FIELDS:
                expiration = item.get(field)
                if isinstance(expiration, str) and expiration.strip():
                    expiration_values.append(expiration.strip())

    normalized = [normalized_state(value) for value in availability_values]
    unavailable = next((value for value in normalized if value in UNAVAILABLE_STATES), None)
    active = next((value for value in normalized if value in ACTIVE_STATES), None)
    unknown = next((value for value in normalized if value), None)
    availability = unavailable or active or unknown

    parsed_expirations = [
        parsed for parsed in (parse_timestamp(value) for value in expiration_values)
        if parsed is not None
    ]
    if parsed_expirations:
        expiration = min(parsed_expirations).isoformat().replace("+00:00", "Z")
    elif expiration_values:
        expiration = expiration_values[0]
    else:
        expiration = None
    return availability, expiration
