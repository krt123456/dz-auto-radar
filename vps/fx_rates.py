#!/usr/bin/env python3
"""Shared ECB daily reference-rate helpers for EUR display conversion.

The founder requires every public offer to display in EUR.  Sources that bid
in PLN/NOK/SEK/ISK/CZK/DKK convert at the European Central Bank daily
reference rate; the rate used is recorded in each source report for audit.
"""
from __future__ import annotations

import math
import re
import urllib.request


ECB_FX_URL_TEMPLATE = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.{currency}.EUR.SP00.A"
    "?format=csvdata&lastNObservations=1"
)


class FxRateError(RuntimeError):
    """The ECB reference rate could not be read."""


def fetch_ecb_units_per_eur(currency: str, *, timeout: int = 30) -> tuple[float, str]:
    """Return (currency units per 1 EUR, observation date)."""
    url = ECB_FX_URL_TEMPLATE.format(currency=currency)
    try:
        response = urllib.request.urlopen(url, timeout=timeout)
        text = response.read().decode("utf-8", "replace")
    except Exception as error:
        raise FxRateError(f"ECB {currency}/EUR reference rate unavailable: {error}") from error
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise FxRateError(f"ECB {currency}/EUR reference rate response is empty")
    header = lines[0].split(",")
    try:
        value_index = header.index("OBS_VALUE") if "OBS_VALUE" in header else header.index("value")
        date_index = header.index("TIME_PERIOD")
    except ValueError as error:
        raise FxRateError(f"ECB {currency}/EUR reference rate CSV is malformed") from error
    fields = lines[1].split(",")
    try:
        rate = float(fields[value_index])
    except (ValueError, IndexError) as error:
        raise FxRateError(f"ECB {currency}/EUR reference rate value is invalid") from error
    if not math.isfinite(rate) or rate <= 0:
        raise FxRateError(f"ECB {currency}/EUR reference rate value is out of range")
    observation_date = fields[date_index] if date_index < len(fields) else ""
    return rate, observation_date


def to_eur(amount: int | float | None, units_per_eur: float) -> int | float | None:
    """Convert an amount expressed in `units_per_eur` currency units into EUR."""
    if amount is None:
        return None
    converted = float(amount) / units_per_eur
    converted = round(converted, 2)
    return int(converted) if converted.is_integer() else converted


def convert_or_none(amount: int | float | None, currency: str, rates: dict[str, tuple[float, str]]) -> int | float | None:
    """Convert when a rate for `currency` is available, else pass through None."""
    if amount is None:
        return None
    entry = rates.get(currency)
    if entry is None:
        return None
    return to_eur(amount, entry[0])
