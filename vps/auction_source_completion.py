#!/usr/bin/env python3
"""Build and validate the 118-source auction completion ledger.

This is the reusable control plane for source research.  It does not scrape a
site and it never turns a generic homepage match into a completed connector.
Instead, every source-specific probe/connector writes one small batch fragment;
this program validates those fragments against the authoritative DOCX-derived
inventory, derives honest completion states, and fails on omissions or false
completion claims.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


UTC = dt.timezone.utc
EXPECTED_DOCUMENT_ENTRIES = 129
EXPECTED_CANONICAL_IDENTITIES = 118

CATALOGUE_STATES = {"verified", "partial", "blocked", "not_found", "unknown"}
ACCESS_STATES = {"public", "restricted", "authorized", "blocked", "unknown"}
CONNECTOR_STATES = {"implemented", "prototype", "not_implemented", "not_applicable"}
CONNECTOR_KINDS = {"source_specific", "official_feed", "generic_research", "none"}
WORK_STATES = {"complete", "partial", "blocked", "not_started"}
INTEGRATION_STATES = {"tested", "path_defined", "blocked", "not_started"}
PUBLICATION_STATES = {"accepted", "pending", "blocked"}
OVERALL_STATES = {
    "verified_complete",
    "technical_complete_research_only",
    "blocked",
    "incomplete",
}
URL_RE = re.compile(r"^https://", re.I)


class ContractError(ValueError):
    """The inventory or a completion fragment violates the audit contract."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _evidence_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _clean(value)


def _string_list(value: Any, field: str, *, urls: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        text = _clean(item)
        if not text:
            raise ContractError(f"{field} contains an empty value")
        if urls:
            parsed = urlparse(text)
            if not URL_RE.match(text) or not parsed.hostname:
                raise ContractError(f"{field} contains a non-HTTPS evidence URL: {text}")
        result.append(text)
    if len(result) != len(set(result)):
        raise ContractError(f"{field} contains duplicate values")
    return result


def _optional_count(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{field} must be null or a non-negative integer")
    return value


def inventory_groups(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 1:
        raise ContractError("inventory schema_version must be 1")
    claims = inventory.get("document_claims")
    entries = inventory.get("entries")
    if (
        not isinstance(claims, dict)
        or claims.get("entry_count") != EXPECTED_DOCUMENT_ENTRIES
        or claims.get("canonical_identity_count") != EXPECTED_CANONICAL_IDENTITIES
        or not isinstance(entries, list)
        or len(entries) != EXPECTED_DOCUMENT_ENTRIES
    ):
        raise ContractError("inventory is not the authoritative 129-entry/118-identity document")

    expected_entries = set(range(1, EXPECTED_DOCUMENT_ENTRIES + 1))
    grouped: dict[str, dict[str, Any]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise ContractError("inventory entry must be an object")
        document_entry = raw.get("document_entry")
        identity = _clean(raw.get("canonical_identity"))
        batch = raw.get("batch")
        keys = raw.get("registry_keys")
        if (
            document_entry not in expected_entries
            or not identity
            or isinstance(batch, bool)
            or not isinstance(batch, int)
            or not 1 <= batch <= 12
            or not isinstance(keys, list)
            or not keys
        ):
            raise ContractError(f"invalid inventory entry: {document_entry!r}")
        expected_entries.remove(document_entry)
        group = grouped.setdefault(
            identity,
            {
                "canonical_identity": identity,
                "batch": batch,
                "document_entries": [],
                "platforms": [],
                "registry_keys": [],
                "display_urls": [],
            },
        )
        if group["batch"] != batch:
            raise ContractError(f"identity occurs in multiple batches: {identity}")
        group["document_entries"].append(document_entry)
        group["platforms"].append(_clean(raw.get("platform")))
        group["registry_keys"].extend(_clean(value) for value in keys)
        group["display_urls"].append(_clean(raw.get("display_url")))
    if expected_entries or len(grouped) != EXPECTED_CANONICAL_IDENTITIES:
        raise ContractError("inventory has a gap or a duplicate document entry")
    for group in grouped.values():
        for key in ("platforms", "registry_keys", "display_urls"):
            group[key] = sorted({value for value in group[key] if value})
        group["document_entries"].sort()
    return grouped


def _placeholder(group: dict[str, Any]) -> dict[str, Any]:
    return {
        **group,
        "catalogue": {"status": "unknown", "url": None, "evidence_urls": []},
        "access": {"status": "unknown", "blocker": "", "evidence_urls": []},
        "connector": {"status": "not_implemented", "kind": "none", "path": ""},
        "enumeration": {
            "status": "not_started",
            "method": "",
            "discovered_count": None,
            "visited_count": None,
            "proof": "",
        },
        "classification": {
            "status": "not_started",
            "classifiable_count": None,
            "classified_count": None,
            "counts": {},
        },
        "integration": {"status": "not_started", "path": "", "test": ""},
        "publication": {"status": "pending", "basis": "", "evidence_urls": []},
        "overall_status": "incomplete",
    }


def _object(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ContractError(f"{source.get('canonical_identity')}.{key} must be an object")
    return value


def _state(
    value: Any,
    allowed: set[str],
    field: str,
    alias: Any = None,
) -> str:
    text = _clean(value)
    if text in allowed:
        return text
    canonical = alias(text) if alias else None
    if canonical not in allowed:
        raise ContractError(f"{field} has invalid state {text!r}")
    return canonical


def _catalogue_alias(value: str) -> str | None:
    if "verified" in value:
        return "verified"
    if value.startswith(("authenticated", "registered_business", "business_account")):
        return "blocked"
    if value.startswith(("public_", "official_")):
        return "partial"
    return None


def _access_alias(value: str) -> str | None:
    if value == "robots_disallow_all":
        return "blocked"
    if value.startswith(("blocked_", "blocked_by_")):
        return "blocked"
    if value.startswith("public_"):
        return "public"
    return None


def _connector_alias(value: str) -> str | None:
    if value == "research_only_robots_preflight_blocked":
        return "prototype"
    if value.startswith("not_implemented"):
        return "not_implemented"
    if value.startswith("existing_connector_partial"):
        return "prototype"
    if value == "blocked":
        return "prototype"
    if value.startswith("shared_target_connector_exists_alias"):
        return "not_applicable"
    if value.startswith("source_specific_research"):
        return "implemented"
    if value.startswith(("prototype_", "research_probe_", "probe_only_", "quarantined")):
        return "prototype"
    return None


def _work_alias(value: str) -> str | None:
    if value.startswith("balanced_zero_rows_blocked_"):
        return "blocked"
    if value in {
        "complete_snapshot",
        "preliminary_complete",
        "technical_complete_snapshot",
        "stable_complete_root_snapshot_not_production_invariant",
        "triage_complete_eligibility_pending_details",
    }:
        return "complete"
    if value in {
        "declared_total_only",
        "not_proven",
        "registry_snapshot_enumerated_not_vehicle_atomic",
        "not_complete_non_atomic_lots",
    }:
        return "partial"
    if value.startswith("blocked_"):
        return "blocked"
    if value.startswith("not_attempted_"):
        return "blocked"
    if value.startswith(("not_independently_", "delegated_to_target_")):
        return "not_started"
    if value.startswith("not_run"):
        return "blocked" if "block" in value else "not_started"
    return None


def _integration_alias(value: str) -> str | None:
    if value.startswith("blocked_"):
        return "blocked"
    if value.startswith("quarantined_"):
        return "blocked"
    if value.startswith("not_integrated"):
        return "blocked" if "hold" in value or "block" in value else "not_started"
    if value.startswith("pending_alias_"):
        return "not_started"
    return None


def _publication_alias(value: str) -> str | None:
    if value.startswith("blocked_"):
        return "blocked"
    if value.startswith("pending_"):
        return "pending"
    if value.startswith(("attribution_basis_", "not_assessed_")):
        return "pending"
    return None


def _reported_overall_alias(value: str) -> str | None:
    if value.startswith("blocked_"):
        return "blocked"
    if value.startswith(("pending_", "incomplete_")) or value in {
        "pending",
        "not_started",
    }:
        return "incomplete"
    if value.startswith("redirect_alias_pending_"):
        return "incomplete"
    if value in {
        "technical_snapshot_complete_publication_blocked",
        "technical_complete_publication_blocked",
    }:
        return "blocked"
    return None


def _save_detail(container: dict[str, Any], key: str, raw: Any, canonical: str) -> None:
    detail = _clean(raw)
    if detail and detail != canonical:
        container[f"{key}_detail"] = detail


def _has_blocker_evidence(source: dict[str, Any]) -> bool:
    access = _object(source, "access")
    publication = _object(source, "publication")
    evidence = list(access.get("evidence_urls") or []) + list(
        publication.get("evidence_urls") or []
    )
    return bool(_clean(access.get("blocker")) and evidence)


def _technical_complete(source: dict[str, Any]) -> bool:
    catalogue = _object(source, "catalogue")
    connector = _object(source, "connector")
    enumeration = _object(source, "enumeration")
    classification = _object(source, "classification")
    integration = _object(source, "integration")
    discovered = enumeration.get("discovered_count")
    visited = enumeration.get("visited_count")
    classifiable = classification.get("classifiable_count")
    classified = classification.get("classified_count")
    return bool(
        catalogue.get("status") == "verified"
        and connector.get("status") == "implemented"
        and connector.get("kind") in {"source_specific", "official_feed"}
        and _clean(connector.get("path"))
        and enumeration.get("status") == "complete"
        and isinstance(discovered, int)
        and not isinstance(discovered, bool)
        and discovered >= 0
        and visited == discovered
        and _clean(enumeration.get("method"))
        and _clean(enumeration.get("proof"))
        and classification.get("status") == "complete"
        and classifiable == visited
        and classified == classifiable
        and integration.get("status") in {"tested", "path_defined"}
        and _clean(integration.get("path"))
        and _clean(integration.get("test"))
    )


def derive_overall_status(source: dict[str, Any]) -> str:
    publication = _object(source, "publication")
    access = _object(source, "access")
    if _technical_complete(source):
        if publication.get("status") == "accepted" and access.get("status") in {
            "public",
            "authorized",
        }:
            return "verified_complete"
        if publication.get("status") == "blocked" and _has_blocker_evidence(source):
            return "blocked"
        return "technical_complete_research_only"
    if (
        access.get("status") == "blocked"
        or publication.get("status") == "blocked"
        or _object(source, "catalogue").get("status") == "blocked"
        or _object(source, "enumeration").get("status") == "blocked"
    ) and _has_blocker_evidence(source):
        return "blocked"
    return "incomplete"


def validate_source(raw: Any, group: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ContractError("source result must be an object")
    identity = _clean(raw.get("canonical_identity"))
    if identity != group["canonical_identity"] or raw.get("batch") != group["batch"]:
        raise ContractError(f"source identity/batch mismatch for {group['canonical_identity']}")
    keys = _string_list(raw.get("registry_keys"), f"{identity}.registry_keys")
    if set(keys) != set(group["registry_keys"]):
        raise ContractError(f"registry key mismatch for {identity}")

    source = {**group, **raw, "registry_keys": sorted(keys)}
    catalogue = _object(source, "catalogue")
    raw_catalogue_status = catalogue.get("status")
    catalogue["status"] = _state(
        raw_catalogue_status,
        CATALOGUE_STATES,
        f"{identity}.catalogue.status",
        _catalogue_alias,
    )
    _save_detail(catalogue, "status", raw_catalogue_status, catalogue["status"])
    catalogue["url"] = _clean(catalogue.get("url")) or None
    if catalogue["url"] is not None:
        _string_list([catalogue["url"]], f"{identity}.catalogue.url", urls=True)
    catalogue["evidence_urls"] = _string_list(
        catalogue.get("evidence_urls"), f"{identity}.catalogue.evidence_urls", urls=True
    )
    if catalogue["status"] == "verified" and not catalogue["url"]:
        raise ContractError(f"verified catalogue has no URL for {identity}")

    access = _object(source, "access")
    raw_access_status = access.get("status")
    access["status"] = _state(
        raw_access_status,
        ACCESS_STATES,
        f"{identity}.access.status",
        _access_alias,
    )
    _save_detail(access, "status", raw_access_status, access["status"])
    access["blocker"] = _clean(access.get("blocker"))
    access["evidence_urls"] = _string_list(
        access.get("evidence_urls"), f"{identity}.access.evidence_urls", urls=True
    )

    connector = _object(source, "connector")
    raw_connector_status = connector.get("status")
    connector["status"] = _state(
        raw_connector_status,
        CONNECTOR_STATES,
        f"{identity}.connector.status",
        _connector_alias,
    )
    _save_detail(connector, "status", raw_connector_status, connector["status"])
    raw_connector_kind = _clean(connector.get("kind"))
    canonical_kind = raw_connector_kind
    if canonical_kind not in CONNECTOR_KINDS:
        if connector["status"] in {"not_implemented", "not_applicable"}:
            canonical_kind = "none"
        elif "source_specific" in raw_connector_kind or "official_csv" in raw_connector_kind:
            canonical_kind = "source_specific"
        elif any(token in raw_connector_kind for token in ("generic", "heuristic", "browser")):
            canonical_kind = "generic_research"
        elif "probe" in raw_connector_kind:
            canonical_kind = "source_specific"
    connector["kind"] = _state(
        canonical_kind, CONNECTOR_KINDS, f"{identity}.connector.kind"
    )
    _save_detail(connector, "kind", raw_connector_kind, connector["kind"])
    connector["path"] = _clean(connector.get("path"))
    if connector["path"].lower() == "none":
        connector["path"] = ""
    if connector["status"] == "implemented" and (
        connector["kind"] not in {"source_specific", "official_feed"}
        or not connector["path"]
    ):
        raise ContractError(f"implemented connector lacks a source-specific path for {identity}")

    enumeration = _object(source, "enumeration")
    raw_enumeration_status = enumeration.get("status")
    enumeration["status"] = _state(
        raw_enumeration_status,
        WORK_STATES,
        f"{identity}.enumeration.status",
        _work_alias,
    )
    _save_detail(
        enumeration, "status", raw_enumeration_status, enumeration["status"]
    )
    enumeration["method"] = _clean(enumeration.get("method"))
    enumeration["proof"] = _evidence_text(enumeration.get("proof"))
    enumeration["discovered_count"] = _optional_count(
        enumeration.get("discovered_count"), f"{identity}.enumeration.discovered_count"
    )
    enumeration["visited_count"] = _optional_count(
        enumeration.get("visited_count"), f"{identity}.enumeration.visited_count"
    )

    classification = _object(source, "classification")
    raw_classification_status = classification.get("status")
    classification["status"] = _state(
        raw_classification_status,
        WORK_STATES,
        f"{identity}.classification.status",
        _work_alias,
    )
    _save_detail(
        classification,
        "status",
        raw_classification_status,
        classification["status"],
    )
    classification["classifiable_count"] = _optional_count(
        classification.get("classifiable_count"),
        f"{identity}.classification.classifiable_count",
    )
    classification["classified_count"] = _optional_count(
        classification.get("classified_count"), f"{identity}.classification.classified_count"
    )
    counts = classification.get("counts")
    if not isinstance(counts, dict):
        raise ContractError(f"{identity}.classification.counts must be an object")
    clean_counts: dict[str, int] = {}
    for label, count in counts.items():
        label = _clean(label)
        if not label or label in clean_counts:
            raise ContractError(f"{identity}.classification.counts has an empty/duplicate label")
        normalized_count = _optional_count(
            count, f"{identity}.classification.counts.{label}"
        )
        if normalized_count is None:
            raise ContractError(
                f"{identity}.classification.counts.{label} cannot be null"
            )
        clean_counts[label] = normalized_count
    classification["counts"] = dict(sorted(clean_counts.items()))
    if classification["classified_count"] is not None and sum(clean_counts.values()) != classification["classified_count"]:
        raise ContractError(f"classification counts do not balance for {identity}")

    integration = _object(source, "integration")
    raw_integration_status = integration.get("status")
    integration["status"] = _state(
        raw_integration_status,
        INTEGRATION_STATES,
        f"{identity}.integration.status",
        _integration_alias,
    )
    _save_detail(integration, "status", raw_integration_status, integration["status"])
    integration["path"] = _clean(integration.get("path"))
    integration["test"] = _clean(integration.get("test"))

    publication = _object(source, "publication")
    raw_publication_status = publication.get("status")
    publication["status"] = _state(
        raw_publication_status,
        PUBLICATION_STATES,
        f"{identity}.publication.status",
        _publication_alias,
    )
    _save_detail(publication, "status", raw_publication_status, publication["status"])
    publication["basis"] = _clean(publication.get("basis"))
    publication["evidence_urls"] = _string_list(
        publication.get("evidence_urls"),
        f"{identity}.publication.evidence_urls",
        urls=True,
    )

    derived = derive_overall_status(source)
    reported_detail = _clean(raw.get("overall_status"))
    reported = _state(
        reported_detail,
        OVERALL_STATES,
        f"{identity}.overall_status",
        _reported_overall_alias,
    ) if reported_detail else ""
    if reported and reported != derived:
        raise ContractError(
            f"false completion state for {identity}: reported {reported_detail}, derived {derived}"
        )
    if reported_detail and reported_detail != derived:
        source["overall_status_detail"] = reported_detail
    source["overall_status"] = derived
    return source


def validate_fragment(
    fragment: Any, groups: dict[str, dict[str, Any]], *, path: Path | None = None
) -> list[dict[str, Any]]:
    label = str(path) if path else "fragment"
    if not isinstance(fragment, dict) or fragment.get("schema_version") != 1:
        raise ContractError(f"{label}: schema_version must be 1")
    batch = fragment.get("batch")
    rows = fragment.get("sources")
    if isinstance(batch, bool) or not isinstance(batch, int) or not 1 <= batch <= 12:
        raise ContractError(f"{label}: invalid batch")
    if not isinstance(rows, list):
        raise ContractError(f"{label}: sources must be a list")
    expected = {identity for identity, group in groups.items() if group["batch"] == batch}
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for row in rows:
        identity = _clean(row.get("canonical_identity") if isinstance(row, dict) else "")
        if identity not in expected or identity in seen:
            raise ContractError(f"{label}: unexpected/duplicate identity {identity!r}")
        seen.add(identity)
        validated.append(validate_source(row, groups[identity]))
    missing = expected - seen
    if missing:
        raise ContractError(f"{label}: silently omitted identities: {sorted(missing)}")
    return sorted(validated, key=lambda row: row["canonical_identity"])


def build_ledger(
    inventory: dict[str, Any], fragments: Iterable[tuple[Path, Any]]
) -> dict[str, Any]:
    groups = inventory_groups(inventory)
    by_identity = {identity: _placeholder(group) for identity, group in groups.items()}
    supplied_batches: set[int] = set()
    for path, fragment in fragments:
        batch = fragment.get("batch") if isinstance(fragment, dict) else None
        if batch in supplied_batches:
            raise ContractError(f"duplicate fragment for batch {batch}: {path}")
        for source in validate_fragment(fragment, groups, path=path):
            by_identity[source["canonical_identity"]] = source
        supplied_batches.add(batch)

    sources = sorted(
        by_identity.values(), key=lambda row: (row["batch"], row["canonical_identity"])
    )
    states = Counter(source["overall_status"] for source in sources)
    batches: dict[int, Counter[str]] = defaultdict(Counter)
    for source in sources:
        batches[source["batch"]][source["overall_status"]] += 1
    summary = {
        "document_entries": sum(len(source["document_entries"]) for source in sources),
        "canonical_identities": len(sources),
        "fragments_loaded": len(supplied_batches),
        "batches_loaded": sorted(supplied_batches),
        "verified_complete": states["verified_complete"],
        "technical_complete_research_only": states["technical_complete_research_only"],
        "blocked": states["blocked"],
        "incomplete": states["incomplete"],
        "resolved_without_silent_omission": len(sources) - states["incomplete"],
        "enumeration_complete": sum(
            source["enumeration"]["status"] == "complete" for source in sources
        ),
        "classification_complete": sum(
            source["classification"]["status"] == "complete" for source in sources
        ),
        "production_publishable": states["verified_complete"],
    }
    if (
        summary["document_entries"] != EXPECTED_DOCUMENT_ENTRIES
        or summary["canonical_identities"] != EXPECTED_CANONICAL_IDENTITIES
        or sum(states.values()) != EXPECTED_CANONICAL_IDENTITIES
    ):
        raise ContractError("ledger does not account for the authoritative source inventory")
    return {
        "schema_version": 1,
        "generated_at_utc": dt.datetime.now(UTC).isoformat(),
        "contract": {
            "document_entries": EXPECTED_DOCUMENT_ENTRIES,
            "canonical_identities": EXPECTED_CANONICAL_IDENTITIES,
            "verified_complete_requires": [
                "verified catalogue URL",
                "source-specific connector or official feed",
                "finite enumeration proof with visited_count == discovered_count",
                "classified_count == classifiable_count == visited_count",
                "tested or explicitly defined SonarDeals integration path",
                "accepted publication basis and public/authorized access",
            ],
            "blocked_is_not_complete": True,
            "generic_homepage_discovery_is_not_complete": True,
            "audited_batch_count": 12,
            "all_batches_required_for_publication": True,
        },
        "summary": summary,
        "batch_summary": {
            str(batch): {state: counter[state] for state in sorted(OVERALL_STATES)}
            for batch, counter in sorted(batches.items())
        },
        "sources": sources,
    }


def require_all_batches(ledger: dict[str, Any]) -> None:
    summary = ledger.get("summary") if isinstance(ledger, dict) else None
    if (
        not isinstance(summary, dict)
        or summary.get("fragments_loaded") != 12
        or summary.get("batches_loaded") != list(range(1, 13))
    ):
        raise ContractError("completion ledger does not contain all 12 audited batches")


def markdown_report(ledger: dict[str, Any]) -> str:
    summary = ledger["summary"]
    lines = [
        "# سجل اكتمال مصادر المزادات",
        "",
        f"- هويات المصادر: **{summary['canonical_identities']}** (من {summary['document_entries']} إدخالًا في الوثيقة).",
        f"- مكتملة وقابلة للنشر: **{summary['verified_complete']}**.",
        f"- مكتملة تقنيًا للبحث فقط: **{summary['technical_complete_research_only']}**.",
        f"- محجوبة بدليل وليست مكتملة: **{summary['blocked']}**.",
        f"- ما زالت غير مكتملة: **{summary['incomplete']}**.",
        f"- اكتمل تعداد كل العروض: **{summary['enumeration_complete']} / {summary['canonical_identities']}**.",
        f"- اكتمل تصنيف كل العروض المعدودة: **{summary['classification_complete']} / {summary['canonical_identities']}**.",
        "",
        "| الدفعة | المصدر | الكتالوج | التعداد | التصنيف | الدمج | النشر | النتيجة |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for source in ledger["sources"]:
        lines.append(
            "| {batch} | {identity} | {catalogue} | {enumeration} | {classification} | "
            "{integration} | {publication} | {overall} |".format(
                batch=source["batch"],
                identity=source["canonical_identity"].replace("|", "\\|"),
                catalogue=source["catalogue"]["status"],
                enumeration=source["enumeration"]["status"],
                classification=source["classification"]["status"],
                integration=source["integration"]["status"],
                publication=source["publication"]["status"],
                overall=source["overall_status"],
            )
        )
    lines.extend(
        [
            "",
            "> المصدر المحجوب موثّق، لكنه لا يُحسب ضمن المصادر المكتملة ولا تُنشر منه عروض.",
            "",
        ]
    )
    return "\n".join(lines)


def write_templates(root: Path, groups: dict[str, dict[str, Any]]) -> None:
    for batch in range(1, 13):
        sources = [
            _placeholder(group)
            for group in sorted(
                (value for value in groups.values() if value["batch"] == batch),
                key=lambda value: value["canonical_identity"],
            )
        ]
        atomic_write_json(
            root / f"batch{batch}_completion_template.json",
            {"schema_version": 1, "batch": batch, "sources": sources},
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge source-specific audits into an exact 118-source completion ledger"
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--fragment", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--templates-dir", type=Path)
    parser.add_argument(
        "--require-all-batches",
        action="store_true",
        help="fail unless independently supplied fragments cover batches 1 through 12",
    )
    parser.add_argument(
        "--require-resolved",
        action="store_true",
        help="fail until every identity is either verified, technically complete, or blocked with evidence",
    )
    args = parser.parse_args()
    inventory = load_json(args.inventory)
    groups = inventory_groups(inventory)
    if args.templates_dir:
        write_templates(args.templates_dir, groups)
    fragments = [(path, load_json(path)) for path in args.fragment]
    ledger = build_ledger(inventory, fragments)
    if args.require_all_batches:
        require_all_batches(ledger)
    atomic_write_json(args.output, ledger)
    if args.report:
        atomic_write_text(args.report, markdown_report(ledger))
    print(json.dumps(ledger["summary"], ensure_ascii=False, sort_keys=True))
    if args.require_resolved and ledger["summary"]["incomplete"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
