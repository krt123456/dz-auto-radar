#!/usr/bin/env python3
"""Shared fail-closed condition gate for ordinary vehicle listings.

Only phrases that positively describe a damaged, parts-only, salvage, or
repair vehicle belong here.  Generic roots such as ``grele``, ``wypad`` or
``unfall`` are intentionally avoided because they also occur in phrases such
as ``anti-grele``, ``bezwypadkowy`` and ``unfallfrei``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


CONDITION_EXCLUSION_REASON = "damage_parts_repair"


def fold_condition_text(*values: Any) -> str:
    """Accent-fold and normalize listing text without losing word boundaries."""
    value = " ".join(str(item or "") for item in values)
    folded = unicodedata.normalize(
        "NFKD",
        value.casefold().translate(str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss"})),
    )
    return " ".join(
        "".join(character for character in folded if not unicodedata.combining(character)).split()
    )


DAMAGE_PARTS_REPAIR_PATTERN = re.compile(
    r"(?:"
    # English.
    r"\bsalvage\b|\baccident(?:ed)?\b|\bdamaged\b|"
    r"\b(?:spares?|parts?)\s+(?:or\s+)?repair\b|\bparts?\s+only\b|"
    r"\b(?:for|needs?)\s+repairs?\b|\brepairable\s+vehicle\b|\bnon[ -]?runner\b|"
    # French. Text is accent-folded before matching.
    r"\bepave\b|\baccidente(?:e|es|ees)?\b|\bendommag(?:e|ee|es|ees)\b|"
    r"\bsinistre(?:e|es|ees)?\b|\bimpact(?:s)?(?:\s+de)?\s+grele\b|"
    r"\b(?:pour\s+pieces|a\s+reparer|non\s+roulant)\b|"
    # Polish. Exact phrases avoid bezkolizyjny / bezwypadkowy / bezszkodowy.
    r"\bpo\s+kolizji\b|\bpowypadk(?:owy|owa|owe|owym|owa)?\b|"
    r"\buszkodz(?:ony|ona|one|eni|ona)?\b|\bdo\s+naprawy\b|\bna\s+czesci\b|"
    # German.
    r"\b(?:unfallwagen|unfallschaden|frontschaden|heckschaden|hagelschaden|motorschaden|"
    r"bastlerfahrzeug|ersatzteilspender|reparaturbedurftig)\b|"
    r"\bzum\s+ausschlachten\b|"
    # Italian, Spanish, Portuguese, Dutch and Romanian.
    r"\b(?:incidentat[aoe]|sinistrat[aoe]|danneggiat[aoe]|da\s+riparare|per\s+ricambi)\b|"
    r"\b(?:accidentad[ao]|siniestrad[ao]|averiad[ao]|para\s+piezas|para\s+reparar)\b|"
    r"\b(?:schadeauto|ongevalwagen|voor\s+onderdelen|te\s+repareren)\b|"
    r"\b(?:avariat[ae]?|accidentat[ae]?|pentru\s+piese|de\s+reparat)\b|"
    # Czech, Slovak, Hungarian and South Slavic languages.
    r"\b(?:havarovan[ey]|bouran[ey]|poskozen[ey]|na\s+dily)\b|"
    r"\b(?:torott|serult|alkatresznek|javitando)\b|"
    r"\b(?:ostecen[ao]?|karamboliran[ao]?|za\s+dijelove|poskodovan[ao]?|za\s+dele)\b|"
    # Nordic and Baltic languages.
    r"\b(?:krockskadad|reparationsobjekt|kolariauto|vaurioitunut|varaosiksi)\b|"
    r"\b(?:skadet\s+bil|delebil|reservedelsbil)\b|"
    r"\b(?:dauztas|avariiline|varuosadeks)\b|"
    # Greek and Bulgarian.
    r"\b(?:τρακαρισμεν[οη]|για\s+ανταλλακτικα|катастрофирал[ао]?|ударен[ао]?|за\s+части)\b"
    r")",
    re.IGNORECASE,
)

# Backward-compatible name used by existing pipeline tests and callers.
RISK_PATTERN = DAMAGE_PARTS_REPAIR_PATTERN


def condition_exclusion_reason(*values: Any) -> str | None:
    text = fold_condition_text(*values)
    if DAMAGE_PARTS_REPAIR_PATTERN.search(text):
        return CONDITION_EXCLUSION_REASON
    return None
