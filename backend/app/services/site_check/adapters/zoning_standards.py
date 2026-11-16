"""zoning_standards adapter — curated dimensional standards + permitted uses.

Ported verbatim from legacy_services/core/public_intel_adapters/
zoning_standards.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field added.
Curated YAMLs + engine + schema + freshness modules copied
verbatim into app.services.site_check.zoning_standards/.

Reads `district` (and optional `intended_use`) from the
orchestrator's `config` arg, populated from austin_zoning's
normalized_facts after that adapter runs (the zoning adapter's
`base_zone` field carries the district code string).

Confidence model:
  - District curated AND use permitted → VERIFIED_PUBLIC_FACT
  - District curated, use not listed → SCREENED_INDICATOR
  - City curated, district missing → SCREENED_INDICATOR
  - City not curated → JURISDICTION_NOT_COVERED
  - No district passed → UNKNOWN

Manual upload (zoning verification letter) is always populated
as the path to upgrade or contradict the curated answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    ManualUploadRequest,
    SourceAccessStatus,
)
from app.services.site_check.zoning_standards.engine import (
    get_district_rules,
    is_use_permitted,
    list_curated_cities,
)

SOURCE_ID = "zoning_standards"

_ZONING_UPLOAD_TYPES = [
    "zoning verification letter",
    "city parking table screenshot",
    "planner email",
    "zoning code excerpt",
]

_AUSTIN_COMBINING_SUFFIXES = (
    "-V", "-CO", "-NP", "-H", "-NCCD", "-DB90", "-DBETOD",
)

_NO_ZONING_AUTHORITY_TYPES = frozenset({
    "NO_CITY_ZONING",
})


def _resolve_city(identity: PropertyIdentity) -> str | None:
    curated = set(list_curated_cities())
    for candidate in identity.jurisdiction_candidates:
        c = candidate.lower().strip()
        c_normalized = c.replace(" ", "_")
        if c in curated:
            return c
        if c_normalized in curated:
            return c_normalized
    return None


def _is_etj_zoning_type(zoning_type: str) -> bool:
    return zoning_type.upper().endswith("_ETJ")


def _strip_combining_suffixes(district: str) -> str:
    out = district
    while True:
        stripped = False
        for suffix in _AUSTIN_COMBINING_SUFFIXES:
            if out.endswith(suffix):
                out = out[: -len(suffix)]
                stripped = True
                break
        if not stripped:
            return out


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config: dict | None = None,
) -> AdapterResult:
    """`config` shape (passed by the orchestrator after
    austin_zoning runs):
      {"district": "MF-3", "intended_use": "multifamily_residential"}
    Either may be missing — adapter handles each case."""
    cfg = config or {}
    district = cfg.get("district")
    intended_use = cfg.get("intended_use")

    # Authoritative jurisdiction verdict (jurisdiction_resolver, Wave 0.5): when
    # the parcel is ETJ or unincorporated, no city zoning code applies — collapse
    # cleanly (the Zoning/Jurisdiction card already states the verdict) instead
    # of rendering a noisy standalone "no district" card.
    verdict = cfg.get("jurisdiction") if isinstance(cfg.get("jurisdiction"), dict) else None
    if verdict:
        vkind = verdict.get("jurisdiction_kind")
        if vkind == "etj":
            return _jurisdiction_collapse_result("etj", verdict.get("city"))
        if vkind == "unincorporated_county":
            return _jurisdiction_collapse_result("unincorporated", verdict.get("city"))

    if district and district.upper() in _NO_ZONING_AUTHORITY_TYPES:
        if _has_city_zoning_conflict(identity, cfg):
            return _jurisdiction_conflict_result(identity, district, cfg)
        return _no_zoning_authority_result(identity, district)
    if district and _is_etj_zoning_type(district):
        return _etj_result(identity, district)

    city = _resolve_city(identity)

    if city is None:
        return _city_not_curated_result(identity)

    if not district:
        return _no_district_result(city)

    lookup_key = _strip_combining_suffixes(district)
    rules = get_district_rules(city, lookup_key)
    if rules is None:
        return _district_not_curated_result(city, district)

    use_answer: dict[str, Any] | None = None
    use_status: str | None = None
    use_entry = None
    if intended_use:
        use_status, use_entry = is_use_permitted(
            city, district, intended_use,
        )
        use_answer = {
            "use_id": intended_use,
            "status": use_status or "not_listed",
            "code_ref": use_entry.code_ref if use_entry else None,
            "notes": use_entry.notes if use_entry else None,
        }

    dimensional = {
        key: {
            "value": cv.value,
            "unit": cv.unit,
            "code_ref": cv.code_ref,
            "notes": cv.notes,
        }
        for key, cv in rules.dimensional_standards.items()
    }

    parking = [
        {
            "use_id": p.use_id,
            "rate": p.rate,
            "unit": p.unit,
            "code_ref": p.code_ref,
            "notes": p.notes,
        }
        for p in rules.parking
    ]

    overlays = [
        {
            "overlay_id": o.overlay_id,
            "effect": o.effect,
            "code_ref": o.code_ref,
        }
        for o in rules.overlay_interactions
    ]

    if intended_use and use_status is None:
        confidence = DataConfidenceStatus.SCREENED_INDICATOR
        status = "use_not_explicitly_listed"
    else:
        confidence = DataConfidenceStatus.VERIFIED_PUBLIC_FACT
        status = "clear"

    advisory = _build_advisory(city, district, rules, use_answer)

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=confidence,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": status,
            "city": city,
            "district": district,
            "district_full_name": rules.full_name,
            "district_code_section": rules.code_section,
            "use_query": use_answer,
            "dimensional_standards": dimensional,
            "parking": parking,
            "overlay_interactions": overlays,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ZONING_UPLOAD_TYPES),
            reason=(
                "Curated rules carry code citations and a "
                "last-verified date, but the city is the "
                "binding authority. Upload a zoning "
                "verification letter for any deal that turns "
                "on the rule."
            ),
        ),
    )


def _build_advisory(
    city: str, district: str,
    rules,
    use_answer: dict[str, Any] | None,
) -> str:
    parts: list[str] = [
        (f"{city.title()} {district} ({rules.full_name}) per "
        f"§{rules.code_section}."),
    ]
    if use_answer:
        if use_answer["status"] == "permitted":
            parts.append(
                f"{use_answer['use_id']} is permitted by right "
                f"per §{use_answer['code_ref']}."
            )
        elif use_answer["status"] == "conditional":
            parts.append(
                f"{use_answer['use_id']} requires a conditional "
                f"use permit per §{use_answer['code_ref']}."
            )
        elif use_answer["status"] == "prohibited":
            parts.append(
                f"{use_answer['use_id']} is prohibited per "
                f"§{use_answer['code_ref']}."
            )
        else:
            parts.append(
                f"{use_answer['use_id']} is NOT explicitly "
                "listed for this district — verify with planning."
            )
    if rules.dimensional_standards:
        h = rules.dimensional_standards.get("max_height")
        d = rules.dimensional_standards.get("max_density")
        i = rules.dimensional_standards.get("max_impervious_cover")
        snippets: list[str] = []
        if h:
            snippets.append(f"max height {h.value} {h.unit}")
        if d:
            snippets.append(f"max density {d.value} {d.unit}")
        if i:
            snippets.append(f"max impervious cover {i.value}{i.unit or ''}")
        if snippets:
            parts.append(", ".join(snippets) + ".")
    if rules.overlay_interactions:
        parts.append(
            f"{len(rules.overlay_interactions)} overlay "
            "district(s) may modify these rules — review "
            "advisory text per overlay."
        )
    parts.append(
        "Zoning verification letter from the city is the "
        "binding authority."
    )
    return " ".join(parts)


def _no_zoning_authority_result(
    identity: PropertyIdentity, zoning_type: str,
) -> AdapterResult:
    label = identity.county or "this property's county"
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": "no_city_zoning_authority",
            "city": None,
            "district": zoning_type,
            "advisory": (
                f"Parcel sits in unincorporated {label.title()} "
                "— no city zoning code applies. Subdivision "
                "review and use restrictions, if any, come "
                "from the county and from deed-recorded "
                "restrictive covenants. Pull the recorded "
                "subdivision plat + restrictive covenants for "
                "a complete answer."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=[
                "recorded subdivision plat",
                "recorded restrictive covenants / CC&Rs",
                "county subdivision regulations",
            ],
            reason=(
                "Unincorporated parcels are governed by county "
                "subdivision rules + deed-recorded restrictive "
                "covenants, not a city zoning code."
            ),
        ),
    )


def _adapter_facts(config: dict, source_id: str) -> dict[str, Any]:
    upstream = config.get("upstream_results")
    if not isinstance(upstream, dict):
        return {}
    raw = upstream.get(source_id)
    if not isinstance(raw, dict):
        return {}
    facts = raw.get("normalized_facts")
    if isinstance(facts, dict):
        return facts
    return raw


def _text_from_values(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text_from_values(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text_from_values(v) for v in value)
    return str(value or "")


def _has_city_zoning_conflict(
    identity: PropertyIdentity,
    config: dict,
) -> bool:
    haystack = " ".join(
        [
            identity.address or "",
            _text_from_values(_adapter_facts(config, "twdb_ccn")),
            _text_from_values(_adapter_facts(config, "plats_intel")),
            _text_from_values(_adapter_facts(config, "austin_zoning").get(
                "contradiction_flags",
            )),
        ],
    ).lower()
    cedar_park_signals = (
        "cedar park",
        "local business",
        " lb ",
        "planning department",
        "gas station services",
        "gasoline service station",
    )
    return any(signal in f" {haystack} " for signal in cedar_park_signals)


def _jurisdiction_conflict_result(
    identity: PropertyIdentity,
    district: str,
    config: dict,
) -> AdapterResult:
    advisory = (
        "Public facts conflict with the no-city-zoning result. Verify the "
        "parcel against the Cedar Park official zoning map / city-limits "
        "sources before relying on county-only rules."
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts={
            "status": "jurisdiction_conflict",
            "city": None,
            "district": district,
            "advisory": advisory,
            "contradiction_flags": [
                "city_signal_conflicts_with_no_city_zoning",
            ],
            "verification_sources": [
                {
                    "label": "Cedar Park Find & View / My Zoning",
                    "url": "https://www.cedarparktexas.gov/680/Find-View",
                },
                {
                    "label": "Cedar Park official zoning GIS layer",
                    "url": "https://gisrest.cedarparktexas.gov/cpgis/rest/services/Planning/Zoning/MapServer/3",
                },
                {
                    "label": "Williamson County Development Services & Drainage",
                    "url": "https://www.wilcotx.gov/639/Development-Services-Drainage",
                },
            ],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ZONING_UPLOAD_TYPES),
            reason=(
                "No-city-zoning result conflicts with city signals. Upload "
                "a zoning verification letter, city map screenshot, or "
                "planner email."
            ),
        ),
    )


def _etj_result(
    identity: PropertyIdentity, zoning_type: str,
) -> AdapterResult:
    city_label = zoning_type.upper().removesuffix("_ETJ").title()
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": "etj_no_zoning_code",
            "city": None,
            "district": zoning_type,
            "advisory": (
                f"Parcel sits in {city_label}'s ETJ "
                "(extraterritorial jurisdiction). The city "
                "reviews subdivision plats and infrastructure "
                "but does NOT enforce a zoning code here. Use "
                "restrictions, if any, come from deed-recorded "
                "restrictive covenants. Confirm subdivision "
                "review requirements with the city's planning "
                "department."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=[
                "recorded subdivision plat",
                "recorded restrictive covenants / CC&Rs",
                f"{city_label} ETJ subdivision agreement",
            ],
            reason=(
                f"ETJ parcels in {city_label} are subject to "
                "subdivision review but not a zoning code."
            ),
        ),
    )


def _city_not_curated_result(
    identity: PropertyIdentity,
) -> AdapterResult:
    candidates = identity.jurisdiction_candidates or []
    label = candidates[0] if candidates else (
        identity.county or "this property's jurisdiction"
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.JURISDICTION_NOT_COVERED,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": "city_not_curated",
            "city": None,
            "district": None,
            "advisory": (
                f"Curated zoning standards for {label} are not "
                "yet in the rules engine. Upload a zoning "
                "verification letter or planner email."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ZONING_UPLOAD_TYPES),
            reason=(
                f"Zoning standards for {label} not yet curated. "
                "Upload a zoning verification letter / planner "
                "email."
            ),
        ),
    )


def _jurisdiction_collapse_result(kind: str, city: str | None) -> AdapterResult:
    """Clean collapse when the jurisdiction verdict says no city zoning code
    applies (ETJ / unincorporated). The Zoning/Jurisdiction card already states
    the verdict; the frontend hides this dependent state so there's no noisy
    standalone Zoning Standards card."""
    city_label = (city or "").title() or "this city"
    if kind == "etj":
        status = "etj_no_zoning_code"
        advisory = (
            f"In {city_label} ETJ, not city limits — no city zoning code "
            "applies. Subdivision / site-development review may still apply."
        )
    else:
        status = "no_city_zoning_authority"
        advisory = "Unincorporated county — no city zoning code applies."
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": status,
            "city": None,
            "district": None,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
    )


def _no_district_result(city: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts={
            "status": "no_district_provided",
            "city": city,
            "district": None,
            "advisory": (
                f"{city.title()} zoning standards will populate once the "
                "parcel's specific zoning district is identified. Look up the "
                "district on the city's zoning map or upload a zoning "
                "verification letter."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ZONING_UPLOAD_TYPES),
            reason=(
                "District not yet resolved. Upload a zoning "
                "verification letter to anchor the answer."
            ),
        ),
    )


def _district_not_curated_result(
    city: str, district: str,
) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts={
            "status": "district_not_curated",
            "city": city,
            "district": district,
            "advisory": (
                f"{city.title()} {district} not yet in the "
                "curated rules engine. The district name is "
                "verified (from the city's zoning layer); the "
                "specific dimensional standards + permitted "
                "uses for this district are pending curation. "
                "Upload a zoning verification letter."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ZONING_UPLOAD_TYPES),
            reason=(
                f"{city.title()} {district} dimensional + use "
                "rules not yet curated. Upload a zoning "
                "verification letter or contact city planning."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
