#!/usr/bin/env python3
"""Pure, fail-closed OLX Europe adapter for the incremental Radar primitive.

The module deliberately performs no network, database, scheduling, publication,
or notification work.  A transport owner supplies explicitly tagged page
results.  This adapter validates the PL contract and deterministically orders a
fully observed equal-second group by numeric ID before yielding SourcePage
objects for the dark incremental ingest primitive.  OLX itself is requested only
with created_at:desc; this module does not claim that OLX guarantees numeric ID
as a native secondary order.

An equal-second group is not released until a lower timestamp or an
authoritative empty data list is observed.  This lets a tie span raw API page
boundaries without weakening the order to timestamp-only.  Missing, malformed,
failed, or prematurely exhausted results are never treated as source exhaustion.
Offset overlap detects common insertion/deletion shifts, but OLX exposes no
proven snapshot token.  Live canary remains explicitly disabled.  Poland also
exceeds the 1,000-page safety cap, so an independently sealed frontier seed under
this exact v2 contract is mandatory before any future live activation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Literal, Mapping, TypeAlias
from urllib.parse import unquote, urlsplit

try:
    from .incremental_frontier import (
        ContractError,
        ContractKey,
        PageContractError,
        PageItem,
        SourceContract,
        SourcePage,
    )
    from .radar_incremental_ingest import canonical_utc
    from .source_identity import OLX_PL_CANONICAL_SOURCE, olx_pl_listing_id
except ImportError:
    from incremental_frontier import (
        ContractError,
        ContractKey,
        PageContractError,
        PageItem,
        SourceContract,
        SourcePage,
    )
    from radar_incremental_ingest import canonical_utc
    from source_identity import OLX_PL_CANONICAL_SOURCE, olx_pl_listing_id


SIGNED_64_MAX = 9_223_372_036_854_775_807
NUMERIC_ID_MAX = (1 << 32) - 1
CREATED_EPOCH_MAX = SIGNED_64_MAX >> 32
MAX_TRANSPORT_PAGES = 1_000
MAX_EQUAL_SECOND_ITEMS = 10_000
PARAM_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DECIMAL_ID = re.compile(r"^[1-9][0-9]*$")
TRANSPORT_FAILURE_KINDS = frozenset({"timeout", "network", "http", "json"})
MIN_OFFER_PRICE_EUR = 200
MAX_OFFER_PRICE_EUR = 1_000_000
MIN_PRODUCTION_YEAR = 1950
MAX_PRODUCTION_YEAR = 2039
PRODUCTION_CANONICALIZER_SOURCE_SHA256 = (
    "782dd54e5f52d7dbbb77f319e9541fc6c4e396aaa576b209ce817601e2094e1f"
)

PRODUCTION_MAKES = (
    "mercedes-benz", "land rover", "range rover", "alfa romeo", "great wall",
    "toyota", "volkswagen", "mercedes", "bmw", "audi", "ford", "renault",
    "peugeot", "citroen", "opel", "vauxhall", "nissan", "hyundai", "kia",
    "honda", "mazda", "fiat", "skoda", "seat", "volvo", "mitsubishi",
    "suzuki", "dacia", "chevrolet", "jeep", "landrover", "porsche", "mini",
    "lexus", "subaru", "jaguar", "chrysler", "dodge", "ram", "gmc",
    "cadillac", "buick", "lincoln", "tesla", "ssangyong", "isuzu",
    "daihatsu", "infiniti", "acura", "genesis", "cupra", "ds", "smart",
    "lada", "chery", "geely", "byd", "mg", "haval", "proton", "perodua",
    "vw", "scania", "iveco", "setra", "neoplan", "solaris", "temsa",
    "bova", "fuso", "daf", "man",
)
PRODUCTION_MAKE_ALIASES = MappingProxyType(
    {
        "vw": "volkswagen",
        "mercedes": "mercedes-benz",
        "landrover": "land rover",
        "vauxhall": "opel",
    }
)
PRODUCTION_MAKE_RE = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(value) for value in PRODUCTION_MAKES)
    + r")(?![a-z])",
    re.IGNORECASE,
)


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


PRODUCTION_CANONICALIZER_SPEC = MappingProxyType(
    {
        "algorithm": "spine-reference-title-make-model-v1",
        "source_sha256": PRODUCTION_CANONICALIZER_SOURCE_SHA256,
        "makes": PRODUCTION_MAKES,
        "aliases": dict(PRODUCTION_MAKE_ALIASES),
        "output": "canonical-make colon canonical-model",
    }
)
PRODUCTION_CANONICALIZER_SHA256 = _canonical_json_sha256(
    dict(PRODUCTION_CANONICALIZER_SPEC)
)

FRONTIER_SEED_REQUIRED = True
PL_RETAINED_MARKET_ESTIMATE = 301_945
LIVE_CANARY_ALLOWED = False
LIVE_CANARY_BLOCKER = "OLX offset pagination has no proven immutable snapshot token"
# These constants seal review evidence only.  They are not an activation gate;
# any future launcher must independently enforce a reviewed live policy.
LIVE_POLICY_CONSTANTS_ARE_DOCUMENTATION_ONLY = True
FRONTIER_SEED_POLICY = MappingProxyType(
    {
        "country": "PL",
        "reason": "retained market exceeds 1000 bounded transport pages",
        "minimum_evidence": (
            "complete source snapshot, exact v2 request contract, canonical raw IDs, "
            "frontier digest, independent count and boundary receipt"
        ),
        "live_canary_allowed": LIVE_CANARY_ALLOWED,
    }
)
FRONTIER_SEED_POLICY_SHA256 = _canonical_json_sha256(dict(FRONTIER_SEED_POLICY))

PRODUCTION_OFFER_FIELDS = frozenset(
    {
        "source",
        "source_listing_id",
        "source_url",
        "title",
        "make_model",
        "variant",
        "country",
        "price_eur",
        "raw_price",
        "currency",
        "year",
        "mileage_km",
        "fuel",
        "seller_type",
        "location",
        "fetched_at",
        "raw_json",
    }
)


class OlxAdapterError(PageContractError):
    """An OLX result cannot be represented by the strict incremental contract."""


class OlxTransportError(OlxAdapterError):
    """A tagged transport result is failed, malformed, or out of sequence."""


class OlxPayloadError(OlxAdapterError):
    """A nominally successful OLX payload violates the expected shape."""


class OlxStreamIncomplete(OlxAdapterError):
    """The bounded transport stream ended without authoritative data exhaustion."""


@dataclass(frozen=True)
class OlxRequestDescriptor:
    host: str
    category_id: int
    sort_by: str
    offset: int
    limit: int
    sort_contract_sha256: str
    canonicalizer_sha256: str

    @property
    def sha256(self) -> str:
        return _canonical_json_sha256(
            {
                "host": self.host,
                "category_id": self.category_id,
                "sort_by": self.sort_by,
                "offset": self.offset,
                "limit": self.limit,
                "sort_contract_sha256": self.sort_contract_sha256,
                "canonicalizer_sha256": self.canonicalizer_sha256,
            }
        )


@dataclass(frozen=True)
class TransportSuccess:
    """One decoded HTTP result bound to an immutable request descriptor."""

    request: OlxRequestDescriptor
    request_sha256: str
    status_code: int
    payload: Mapping[str, Any]
    tag: Literal["success"] = field(default="success", init=False)


@dataclass(frozen=True)
class TransportFailure:
    """One explicit transport/HTTP/JSON failure that can never mean empty data."""

    request: OlxRequestDescriptor
    request_sha256: str
    kind: str
    detail: str = ""
    tag: Literal["failure"] = field(default="failure", init=False)


TransportResult: TypeAlias = TransportSuccess | TransportFailure


@dataclass(frozen=True)
class OlxCountryContract:
    country_code: str
    source_key: str
    partition_key: str
    api_host: str
    category_id: int
    sort_by: str
    hostnames: frozenset[str]
    source_currency: str
    eur_rate_micros: int
    transport_page_size: int
    transport_overlap: int
    output_page_size: int
    sort_contract_sha256: str

    @property
    def key(self) -> ContractKey:
        return ContractKey(
            self.source_key,
            self.partition_key,
            self.sort_contract_sha256,
        )


def _sort_contract_hash(
    country_code: str,
    *,
    api_host: str = "www.olx.pl",
    category_id: int = 84,
    sort_by: str = "created_at:desc",
    overlap_fingerprint: str = "canonical-json-sha256+native-id+created-epoch:v1",
    page_step: int = 45,
) -> str:
    specification = {
        "adapter": "olx-europe-incremental-v3",
        "country": country_code,
        "api_host": api_host,
        "category_id": category_id,
        "sort_by": sort_by,
        "native_identity": "olxpl underscore canonical-decimal-api-id",
        "source_order_claim": "created_at:desc only; no native ID tie-break claim",
        "derived_order": "complete equal-second group then numeric-id-desc",
        "sort_value": "created-time epoch seconds shift-left-32 or numeric-id",
        "transport_terminal": "http-200 object with exact data empty-list",
        "transport_page_size": 50,
        "transport_overlap": 5,
        "overlap_fingerprint": overlap_fingerprint,
        "page_step": page_step,
        "output_page_size": 50,
        "canonicalizer_sha256": PRODUCTION_CANONICALIZER_SHA256,
        "live_snapshot_proven": False,
    }
    return _canonical_json_sha256(specification)


PL_COUNTRY_CONTRACT = OlxCountryContract(
    country_code="PL",
    source_key=OLX_PL_CANONICAL_SOURCE,
    partition_key="cars.pl.created-time-id.v2",
    api_host="www.olx.pl",
    category_id=84,
    sort_by="created_at:desc",
    hostnames=frozenset({"olx.pl", "www.olx.pl"}),
    source_currency="PLN",
    eur_rate_micros=235_000,
    transport_page_size=50,
    transport_overlap=5,
    output_page_size=50,
    sort_contract_sha256=_sort_contract_hash("PL"),
)

SUPPORTED_COUNTRIES = MappingProxyType({"PL": PL_COUNTRY_CONTRACT})


def country_contract(country_code: str) -> OlxCountryContract:
    if not isinstance(country_code, str) or country_code != country_code.strip().upper():
        raise ContractError("OLX country code is not canonical")
    try:
        return SUPPORTED_COUNTRIES[country_code]
    except KeyError as error:
        raise ContractError("OLX incremental country is not explicitly enabled") from error


def request_descriptor_for(
    country_code: str,
    *,
    offset: int,
) -> OlxRequestDescriptor:
    country = country_contract(country_code)
    if type(offset) is not int or offset < 0:
        raise ContractError("OLX request offset is invalid")
    return OlxRequestDescriptor(
        host=country.api_host,
        category_id=country.category_id,
        sort_by=country.sort_by,
        offset=offset,
        limit=country.transport_page_size,
        sort_contract_sha256=country.sort_contract_sha256,
        canonicalizer_sha256=PRODUCTION_CANONICALIZER_SHA256,
    )


def source_contract_for(
    country_code: str,
    *,
    max_pages: int,
    frontier_cap: int,
) -> SourceContract:
    country = country_contract(country_code)
    contract = SourceContract(
        source_key=country.source_key,
        partition_key=country.partition_key,
        sort_contract_sha256=country.sort_contract_sha256,
        max_pages=max_pages,
        frontier_cap=frontier_cap,
        strict_newest=True,
        stop_after_known_pages=2,
    )
    contract.validate(frozenset({country.key}))
    if frontier_cap < country.output_page_size * 2:
        raise ContractError("OLX frontier must retain at least two complete output pages")
    return contract


@dataclass(frozen=True)
class ProductionModelCanonicalizer:
    """Sealed snapshot of the production spine title canonicalizer."""

    source_sha256: str = PRODUCTION_CANONICALIZER_SOURCE_SHA256
    catalog_sha256: str = PRODUCTION_CANONICALIZER_SHA256

    def __post_init__(self) -> None:
        if (
            self.source_sha256 != PRODUCTION_CANONICALIZER_SOURCE_SHA256
            or self.catalog_sha256 != PRODUCTION_CANONICALIZER_SHA256
        ):
            raise ContractError("production model canonicalizer digest drifted")

    def canonicalize(self, raw_title: object) -> str | None:
        title = _clean_text(raw_title, "title", required=True).lower()
        make_match = PRODUCTION_MAKE_RE.search(title)
        if make_match is None:
            return None
        make = make_match.group(1).casefold()
        make = PRODUCTION_MAKE_ALIASES.get(make, make)
        model = ""
        for word in title[make_match.end():].strip().split()[:3]:
            if re.fullmatch(r"(19|20)[0-9]{2}", word):
                continue
            if len(word) >= 2 and word[0].isalpha():
                model = re.sub(r"[^a-z0-9-]", "", word)
                break
        if not model:
            return None
        return f"{make}:{model}"


def production_canonicalizer() -> ProductionModelCanonicalizer:
    return ProductionModelCanonicalizer()


@dataclass(frozen=True)
class _PreparedItem:
    numeric_id: int
    created_epoch: int
    item: PageItem


@dataclass(frozen=True)
class _RawFingerprint:
    native_id: str
    created_epoch: int
    item_sha256: str


def _clean_text(raw: object, field_name: str, *, required: bool = False) -> str:
    if raw is None:
        value = ""
    elif isinstance(raw, str):
        value = " ".join(raw.split())
    else:
        raise OlxPayloadError(f"OLX {field_name} is not text")
    if required and not value:
        raise OlxPayloadError(f"OLX {field_name} is required")
    if len(value) > 100_000:
        raise OlxPayloadError(f"OLX {field_name} is too large")
    return value


def _numeric_id(raw: object) -> tuple[str, int]:
    if type(raw) is int:
        value = raw
        canonical = str(raw)
    elif isinstance(raw, str) and len(raw) <= 10 and DECIMAL_ID.fullmatch(raw):
        canonical = raw
        try:
            value = int(raw)
        except ValueError as error:
            raise OlxPayloadError("OLX ID cannot be represented as an integer") from error
    else:
        raise OlxPayloadError("OLX ID is not a canonical positive decimal integer")
    if not 1 <= value <= NUMERIC_ID_MAX:
        raise OlxPayloadError("OLX ID is outside the 32-bit sort component")
    return canonical, value


def _created_epoch(raw: object) -> tuple[str, int]:
    value = _clean_text(raw, "created_time", required=True)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OlxPayloadError("OLX created_time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.microsecond != 0:
        raise OlxPayloadError("OLX created_time must be whole-second and offset-aware")
    utc = parsed.astimezone(UTC)
    delta = utc - datetime(1970, 1, 1, tzinfo=UTC)
    epoch = delta.days * 86_400 + delta.seconds
    if not 0 <= epoch <= CREATED_EPOCH_MAX:
        raise OlxPayloadError("OLX created_time is outside the signed-64-bit contract")
    return utc.isoformat(), epoch


def _params_map(raw: object) -> dict[str, Any]:
    if not isinstance(raw, list):
        raise OlxPayloadError("OLX params must be a list")
    mapped: dict[str, Any] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise OlxPayloadError("OLX param entry is not an object")
        key = _clean_text(entry.get("key"), "param key", required=True).casefold()
        if not PARAM_KEY.fullmatch(key) or key in mapped:
            raise OlxPayloadError("OLX param key is invalid or duplicated")
        if "value" not in entry:
            raise OlxPayloadError("OLX param has no value")
        mapped[key] = entry["value"]
    return mapped


def _display_value(value: object, field_name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("label", "value", "key"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                if isinstance(candidate, float) and not math.isfinite(candidate):
                    raise OlxPayloadError(f"OLX {field_name} is nonfinite")
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                    return str(candidate)
                return _clean_text(candidate, field_name)
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        raise OlxPayloadError(f"OLX {field_name} is nonfinite")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return _clean_text(value, field_name)


def _bounded_digits(value: object, field_name: str, *, maximum: int) -> int:
    text = _display_value(value, field_name)
    digits = "".join(character for character in text if character.isascii() and character.isdigit())
    if not digits:
        return 0
    parsed = int(digits)
    if parsed > maximum:
        raise OlxPayloadError(f"OLX {field_name} is outside the supported range")
    return parsed


def _price(
    value: object,
    country: OlxCountryContract,
) -> tuple[int, str]:
    if not isinstance(value, Mapping):
        raise OlxPayloadError("OLX price is not an object")
    currency = _clean_text(value.get("currency"), "price currency", required=True).upper()
    if currency != country.source_currency:
        raise OlxPayloadError("OLX price currency does not match the country contract")
    raw_amount = value.get("value")
    if isinstance(raw_amount, bool) or not isinstance(raw_amount, (str, int, float, Decimal)):
        raise OlxPayloadError("OLX price amount is invalid")
    try:
        amount = Decimal(str(raw_amount))
    except InvalidOperation as error:
        raise OlxPayloadError("OLX price amount is invalid") from error
    if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000"):
        raise OlxPayloadError("OLX price amount is outside the supported range")
    eur = (
        amount * Decimal(country.eur_rate_micros) / Decimal(1_000_000)
    ).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    eur_int = int(eur)
    if not 1 <= eur_int <= SIGNED_64_MAX:
        raise OlxPayloadError("OLX converted price is outside the supported range")
    amount_text = format(amount, "f")
    if "." in amount_text:
        amount_text = amount_text.rstrip("0").rstrip(".")
    return eur_int, f"{amount_text} {currency}"


def _source_url(raw: object, country: OlxCountryContract) -> str:
    if not isinstance(raw, str):
        raise OlxPayloadError("OLX source_url is not text")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise OlxPayloadError("OLX source_url contains control characters")
    value = _clean_text(raw, "source_url", required=True)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise OlxPayloadError("OLX source_url is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in country.hostnames
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or not parsed.path.startswith("/d/oferta/")
        or len(parsed.path) <= len("/d/oferta/")
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in unquote(parsed.path)
        )
    ):
        raise OlxPayloadError("OLX source_url is outside the country contract")
    return value


def _location(raw: object) -> str:
    if raw in (None, ""):
        return ""
    if not isinstance(raw, Mapping):
        raise OlxPayloadError("OLX location is not an object")
    city = raw.get("city")
    if isinstance(city, Mapping):
        return _clean_text(city.get("name"), "location city")
    if city in (None, ""):
        return ""
    return _clean_text(city, "location city")


def _project_offer(
    raw: Mapping[str, Any],
    *,
    canonical_id: str,
    numeric_id: int,
    canonical_created_time: str,
    params: Mapping[str, Any],
    canonicalizer: ProductionModelCanonicalizer,
    country: OlxCountryContract,
    observed_at_utc: str,
) -> Mapping[str, Any] | None:
    title = _clean_text(raw.get("title"), "title", required=True)
    canonical_model = canonicalizer.canonicalize(title)
    if type(raw.get("business")) is not bool:
        raise OlxPayloadError("OLX business flag is not boolean")

    if "price" not in params:
        raise OlxPayloadError("OLX accepted offer has no price")
    price_eur, raw_price = _price(params["price"], country)
    year_value = params.get("year", params.get("motor_year"))
    mileage_value = params.get("milage", params.get("mileage"))
    year = _bounded_digits(year_value, "year", maximum=9_999)
    source_url = _source_url(raw.get("url") or raw.get("external_url"), country)
    source_listing_id = olx_pl_listing_id(canonical_id)
    raw_params = raw.get("params")
    compact_raw = {
        "api_id": numeric_id,
        "created_time": canonical_created_time,
        "country": country.country_code,
        "params": raw_params,
    }
    try:
        json.dumps(
            compact_raw,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise OlxPayloadError("OLX raw payload is not canonical JSON data") from error

    if (
        canonical_model is None
        or not MIN_OFFER_PRICE_EUR <= price_eur <= MAX_OFFER_PRICE_EUR
        or (
            year != 0
            and not MIN_PRODUCTION_YEAR <= year <= MAX_PRODUCTION_YEAR
        )
    ):
        return None
    offer = {
        "source": country.source_key,
        "source_listing_id": source_listing_id,
        "source_url": source_url,
        "title": title,
        "make_model": canonical_model,
        "variant": _display_value(params.get("transmission"), "transmission"),
        "country": country.country_code,
        "price_eur": price_eur,
        "raw_price": raw_price,
        "currency": "EUR",
        "year": year,
        "mileage_km": _bounded_digits(mileage_value, "mileage", maximum=10_000_000),
        "fuel": _display_value(params.get("petrol", params.get("fuel")), "fuel"),
        "seller_type": "dealer" if raw["business"] else "private",
        "location": _location(raw.get("location")),
        "fetched_at": observed_at_utc,
        "raw_json": compact_raw,
    }
    if set(offer) != PRODUCTION_OFFER_FIELDS or not offer["make_model"]:
        raise OlxPayloadError("OLX production offer projection is incomplete")
    return offer


def _prepare_item(
    raw: object,
    *,
    country: OlxCountryContract,
    canonicalizer: ProductionModelCanonicalizer,
    observed_at_utc: str,
) -> _PreparedItem:
    if not isinstance(raw, Mapping):
        raise OlxPayloadError("OLX data entry is not an object")
    canonical_id, numeric_id = _numeric_id(raw.get("id"))
    canonical_time, epoch = _created_epoch(raw.get("created_time"))
    sort_value = (epoch << 32) | numeric_id
    if not 0 <= sort_value <= SIGNED_64_MAX:
        raise OlxPayloadError("OLX composite sort value is outside signed SQLite integer")
    params = _params_map(raw.get("params"))
    offer = _project_offer(
        raw,
        canonical_id=canonical_id,
        numeric_id=numeric_id,
        canonical_created_time=canonical_time,
        params=params,
        canonicalizer=canonicalizer,
        country=country,
        observed_at_utc=observed_at_utc,
    )
    native_id = olx_pl_listing_id(canonical_id)
    return _PreparedItem(
        numeric_id=numeric_id,
        created_epoch=epoch,
        item=PageItem(native_id=native_id, sort_value=sort_value, offer=offer),
    )


def _raw_fingerprint(raw: object, country: OlxCountryContract) -> _RawFingerprint:
    if not isinstance(raw, Mapping):
        raise OlxPayloadError("OLX data entry is not an object")
    canonical_id, _numeric = _numeric_id(raw.get("id"))
    _canonical_time, epoch = _created_epoch(raw.get("created_time"))
    try:
        item_sha256 = _canonical_json_sha256(raw)
    except (TypeError, ValueError, OverflowError) as error:
        raise OlxPayloadError("OLX data entry is not finite canonical JSON") from error
    return _RawFingerprint(
        native_id=f"{country.source_key}_{canonical_id}",
        created_epoch=epoch,
        item_sha256=item_sha256,
    )


def _validate_result_request(
    result: object,
    *,
    expected_request: OlxRequestDescriptor,
) -> TransportSuccess:
    if not isinstance(result, (TransportSuccess, TransportFailure)):
        raise OlxTransportError("OLX transport result is not explicitly tagged")
    if (
        not isinstance(result.request, OlxRequestDescriptor)
        or result.request != expected_request
        or not isinstance(result.request_sha256, str)
        or result.request_sha256 != expected_request.sha256
        or result.request_sha256 != result.request.sha256
    ):
        raise OlxTransportError("OLX transport request descriptor or digest mismatched")
    if isinstance(result, TransportFailure):
        if result.kind not in TRANSPORT_FAILURE_KINDS or not isinstance(result.detail, str):
            raise OlxTransportError("OLX transport failure tag is invalid")
        raise OlxTransportError(f"OLX transport failed closed: {result.kind}")
    if type(result.status_code) is not int or result.status_code != 200:
        raise OlxTransportError("OLX successful transport tag does not carry HTTP 200")
    if not isinstance(result.payload, Mapping):
        raise OlxPayloadError("OLX success payload is not an object")
    try:
        json.dumps(
            result.payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise OlxPayloadError("OLX success payload is not finite canonical JSON") from error
    return result


def iter_source_pages(
    results: Iterable[TransportResult],
    *,
    country_code: str,
    contract: SourceContract,
    canonicalizer: ProductionModelCanonicalizer,
    observed_at_utc: str,
    max_transport_pages: int = MAX_TRANSPORT_PAGES,
) -> Iterator[SourcePage]:
    """Yield deterministic incremental pages from tagged, fixture-owned results."""

    country = country_contract(country_code)
    contract.validate(frozenset({country.key}))
    if contract.key != country.key:
        raise ContractError("OLX source contract does not match the country partition")
    if contract.frontier_cap < country.output_page_size * 2:
        raise ContractError("OLX frontier cannot retain two complete output pages")
    if (
        type(max_transport_pages) is not int
        or not 1 <= max_transport_pages <= MAX_TRANSPORT_PAGES
    ):
        raise ContractError("OLX transport page bound is invalid")
    if not isinstance(canonicalizer, ProductionModelCanonicalizer):
        raise ContractError("sealed production model canonicalizer is required")
    canonicalizer.__post_init__()
    observed_at_utc = canonical_utc(observed_at_utc)

    iterator = iter(results)
    expected_offset = 0
    seen_ids: set[str] = set()
    pending_epoch: int | None = None
    pending_group: list[_PreparedItem] = []
    ready: list[PageItem] = []
    output_number = 1
    last_output_sort: int | None = None
    previous_offset: int | None = None
    previous_fingerprints: tuple[_RawFingerprint, ...] = ()

    def release_pending() -> None:
        nonlocal pending_group
        ordered = sorted(pending_group, key=lambda value: value.numeric_id, reverse=True)
        ready.extend(value.item for value in ordered)
        pending_group = []

    def take_output_page() -> SourcePage:
        nonlocal output_number, last_output_sort
        items = tuple(ready[: country.output_page_size])
        del ready[: country.output_page_size]
        for item in items:
            if last_output_sort is not None and item.sort_value >= last_output_sort:
                raise OlxPayloadError("OLX composite order is not strictly decreasing")
            last_output_sort = item.sort_value
        page = SourcePage(output_number, items)
        output_number += 1
        return page

    for _transport_number in range(1, max_transport_pages + 1):
        try:
            raw_result = next(iterator)
        except StopIteration as error:
            raise OlxStreamIncomplete(
                "OLX result iterator ended without an exact empty data list"
            ) from error
        expected_request = request_descriptor_for(
            country.country_code,
            offset=expected_offset,
        )
        result = _validate_result_request(
            raw_result,
            expected_request=expected_request,
        )
        current_offset = expected_offset
        expected_offset += country.transport_page_size - country.transport_overlap
        if "data" not in result.payload:
            raise OlxPayloadError("OLX success payload has no data field")
        data = result.payload["data"]
        if not isinstance(data, list):
            raise OlxPayloadError("OLX data field is not an exact list")
        if len(data) > country.transport_page_size:
            raise OlxPayloadError("OLX data page exceeds its requested limit")
        if data == []:
            if (
                previous_offset is not None
                and previous_offset + len(previous_fingerprints) > current_offset
            ):
                raise OlxPayloadError(
                    "OLX empty page contradicts the required overlap evidence"
                )
            if pending_group:
                release_pending()
            while len(ready) >= country.output_page_size:
                yield take_output_page()
            if ready:
                yield take_output_page()
            yield SourcePage(output_number, ())
            return

        fingerprints = tuple(_raw_fingerprint(item, country) for item in data)
        fingerprint_ids = [value.native_id for value in fingerprints]
        if len(set(fingerprint_ids)) != len(fingerprint_ids):
            raise OlxPayloadError("OLX raw page repeats a native ID")
        overlap_count = 0
        if previous_offset is not None:
            if (
                len(previous_fingerprints)
                < country.transport_page_size - country.transport_overlap
            ):
                raise OlxPayloadError(
                    "OLX returned data beyond a prior short page, implying an omission"
                )
            overlap_count = max(
                0,
                previous_offset + len(previous_fingerprints) - current_offset,
            )
            if (
                overlap_count > country.transport_overlap
                or len(fingerprints) < overlap_count
                or fingerprints[:overlap_count]
                != previous_fingerprints[len(previous_fingerprints) - overlap_count:]
            ):
                raise OlxPayloadError(
                    "OLX page overlap shifted, reordered, changed, or omitted data"
                )
        previous_offset = current_offset
        previous_fingerprints = fingerprints

        for raw_item in data[overlap_count:]:
            prepared = _prepare_item(
                raw_item,
                country=country,
                canonicalizer=canonicalizer,
                observed_at_utc=observed_at_utc,
            )
            if prepared.item.native_id in seen_ids:
                raise OlxPayloadError("OLX page stream repeats a native ID")
            seen_ids.add(prepared.item.native_id)
            if pending_epoch is None:
                pending_epoch = prepared.created_epoch
            elif prepared.created_epoch > pending_epoch:
                raise OlxPayloadError("OLX created_time order regressed toward newer data")
            elif prepared.created_epoch < pending_epoch:
                release_pending()
                pending_epoch = prepared.created_epoch
                while len(ready) >= country.output_page_size:
                    yield take_output_page()
            pending_group.append(prepared)
            if len(pending_group) > MAX_EQUAL_SECOND_ITEMS:
                raise OlxPayloadError("OLX equal-second group exceeds its safety bound")

    raise OlxStreamIncomplete(
        "OLX transport page bound ended without an exact empty data list"
    )
