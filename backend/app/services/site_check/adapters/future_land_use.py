"""future_land_use adapter — city Future Land Use designations.

Ported verbatim from legacy_services/core/public_intel_adapters/
future_land_use.py (May 2026). Identity / contract import
paths adjusted; SOURCE_ID matches registry; coverage_status
field added.

Per-jurisdiction dispatch — Austin, Buda, Kyle, Round Rock,
Pflugerville, Hutto, Georgetown, Cedar Park have programmatic
ArcGIS layers. Manor, Dripping Springs, Leander, Taylor are
HTML/PDF-only — return manual upload steps.

Confidence is CAPPED at SCREENED_INDICATOR even on a clean
Austin FLU answer per spec — comp plans are planning intent,
NOT zoning entitlement.

Requires `identity.jurisdiction_candidates` populated by the
orchestrator (after austin_zoning resolves). Falls back to
"unincorporated" / "JURISDICTION_NOT_COVERED" when empty.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    ManualUploadRequest,
    SourceAccessStatus,
    map_legacy_status,
)
from app.services.site_check.county_source_registry import (
    SourceMode,
    get_jurisdiction_sources,
)

log = logging.getLogger("propdesk.site_check.future_land_use")

SOURCE_ID = "future_land_use"

_AUSTIN_FLU_QUERY = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/arcgis/rest/"
    "services/PLANNINGCADASTRE_future_land_use_map/"
    "FeatureServer/0/query"
)
_BUDA_FLU_QUERY = (
    "https://services6.arcgis.com/vXZW4vAaPRr14z2s/arcgis/rest/"
    "services/Future_Land_Use/FeatureServer/2/query"
)
_KYLE_LANDUSE_QUERY = (
    "https://utility.arcgis.com/usrsvcs/servers/"
    "d0537f925cef4dd6b0273be61701d562/rest/services/"
    "KCH-ESRI/LanduseDistricts/FeatureServer/0/query"
)
_ROUND_ROCK_FLU_QUERY = (
    "https://maps.roundrocktexas.gov/arcgis/rest/services/"
    "Planning/Planning_Multi/MapServer/11/query"
)
_PFLUGERVILLE_FLU_QUERY = (
    "https://maps.pflugervilletx.gov/arcgis/rest/services/"
    "Planning/Future_Land_Use/FeatureServer/0/query"
)
_HUTTO_FLU_QUERY = (
    "https://services.arcgis.com/YZhxlqU7ABWQBGTG/arcgis/rest/"
    "services/Future_Land_Use/FeatureServer/0/query"
)
_GEORGETOWN_FLU_QUERY = (
    "https://gis.georgetowntexas.gov/arcgis/rest/services/"
    "PublicWebMaps/FutureLandUse_WebMap/MapServer/655/query"
)
_CEDAR_PARK_FLU_QUERY = (
    "https://gisrest.cedarparktexas.gov/cpgis/rest/services/"
    "Planning/FutureLandUse/MapServer/0/query"
)

_LAYERS_BY_JURISDICTION: dict[str, str] = {
    "austin": _AUSTIN_FLU_QUERY,
    "buda": _BUDA_FLU_QUERY,
    "kyle": _KYLE_LANDUSE_QUERY,
    "round_rock": _ROUND_ROCK_FLU_QUERY,
    "pflugerville": _PFLUGERVILLE_FLU_QUERY,
    "hutto": _HUTTO_FLU_QUERY,
    "georgetown": _GEORGETOWN_FLU_QUERY,
    "cedar_park": _CEDAR_PARK_FLU_QUERY,
}

_TIMEOUT = 25

_FLU_UPLOAD_TYPES = [
    "future land use map screenshot",
    "comp-plan excerpt",
    "planner email",
]

_DESIGNATION_FIELD_CANDIDATES: tuple[str, ...] = (
    "FUTURE_LAND_USE", "FLU_NAME", "Type", "District",
    "FLU", "LU_code", "LANDUSE", "LU_TYPE", "LU_DESC",
    "GENERAL_LU", "flu_Abr", "DESIGNATION",
    "LAND_USE", "LU_NAME", "PLU_NAME", "NAME",
)

_AUSTIN_FLU_CODES: dict[int, str] = {
    10: "Agriculture", 50: "Rural Residential",
    100: "Single Family", 111: "Higher-Density Single Family",
    113: "Mobile Homes", 130: "Mixed Residential",
    170: "Residential Core", 200: "Multi-family",
    270: "Neighborhood Transition", 300: "Commercial",
    305: "Commerce", 315: "Neighborhood Commercial",
    325: "Neighborhood Mixed Use", 330: "Mixed Use",
    335: "High Density Mixed Use",
    340: "Specific Regulating District",
    350: "Warehouse/Limited Office",
    370: "Neighborhood Node",
    380: "Mixed-use Activity Hub/Corridor",
    390: "Activity Center", 400: "Office",
    430: "Mixed Use/Office", 490: "Major Planned Development",
    500: "Industry", 560: "Major Impact Facilities",
    600: "Civic", 680: "Special District",
    700: "Recreation & Open Space",
    750: "Environmental Conservation",
    800: "Transportation", 870: "Utilities",
    900: "Undeveloped", 940: "Water",
    999: "Excluded from FLUM",
}

_FLU_HTML_PDF_REGISTRY: dict[str, dict[str, Any]] = {
    "manor": {
        "plan_name": "Manor Comprehensive Plan",
        "plan_url": "https://www.cityofmanor.org/page/planning.compplan",
        "instructions": (
            "1) Open the Manor planning page. "
            "2) Open the Comprehensive Plan PDF. "
            "3) Locate the Future Land Use Map; screenshot "
            "the designation."
        ),
    },
    "dripping_springs": {
        "plan_name": "Dripping Springs Comprehensive Plan",
        "plan_url": "https://www.cityofdrippingsprings.com/page/planning.compplan",
        "instructions": (
            "1) Open the Dripping Springs comp plan page. "
            "2) Download the Future Land Use Map PDF. "
            "3) Screenshot the parcel category + legend."
        ),
    },
    "leander": {
        "plan_name": "Leander Comprehensive Plan",
        "plan_url": "https://www.leandertx.gov/planning/page/comprehensive-plan",
        "instructions": (
            "1) Open the Leander planning page. "
            "2) Download the Future Land Use Map PDF. "
            "3) Screenshot the designation."
        ),
    },
    "taylor": {
        "plan_name": "Taylor Comprehensive Plan",
        "plan_url": "https://www.taylortx.gov/197/Comprehensive-Plan",
        "instructions": (
            "1) Open the Taylor comp plan page. "
            "2) Download the Future Land Use Map PDF. "
            "3) Screenshot the designation."
        ),
    },
}

_COUNTY_PATTERN_HINTS: tuple[str, ...] = (" county", "county")


def _normalize_candidates(raw: list[str] | None) -> list[str]:
    """Verbatim legacy_services normalization: lowercase + strip,
    then add underscore variants for multi-word city names
    ("Round Rock" → 'round rock' AND 'round_rock'). NOTE: we
    do NOT strip "ETJ" or "(unincorporated)" suffixes — those
    parcels are correctly OUT of city FLU coverage and must
    fall through to the unincorporated / not-covered branches.
    Adding suffix-stripping made Austin ETJ parcels mistakenly
    route to Austin's FLU layer."""
    out: list[str] = []
    for c in raw or []:
        cl = (c or "").lower().strip()
        if not cl:
            continue
        out.append(cl)
        if " " in cl:
            out.append(cl.replace(" ", "_"))
    return out


def _resolve_jurisdiction(identity: PropertyIdentity) -> str | None:
    candidates = _normalize_candidates(
        identity.jurisdiction_candidates
    )
    for key in _LAYERS_BY_JURISDICTION:
        if key in candidates:
            return key
    return None


def _resolve_html_pdf_city(identity: PropertyIdentity) -> str | None:
    candidates = _normalize_candidates(
        identity.jurisdiction_candidates
    )
    for c in candidates:
        if c in _FLU_HTML_PDF_REGISTRY:
            return c
    return None


def _looks_unincorporated(identity: PropertyIdentity) -> bool:
    if not identity.county:
        return False
    cands = _normalize_candidates(identity.jurisdiction_candidates)
    if not cands:
        return True
    all_keys = (
        set(_LAYERS_BY_JURISDICTION) | set(_FLU_HTML_PDF_REGISTRY)
    )
    for c in cands:
        if c in all_keys:
            return False
    return any(
        any(hint in c for hint in _COUNTY_PATTERN_HINTS)
        for c in cands
    )


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    jurisdiction = _resolve_jurisdiction(identity)
    if jurisdiction is None:
        # No wired REST FLU layer for this city. Before falling back to a manual
        # "unknown" result, consult the county source registry: a city may have
        # an OFFICIAL Future Land Use Map PDF / comp plan / staff-report source
        # that produces a real (screened) answer. Per the Travis-coverage
        # directive, an official-but-not-REST source is COVERED, not UNKNOWN.
        registry_result = _registry_official_flu_result(
            identity,
            config=config if isinstance(config, dict) else None,
            session=http_session(session),
        )
        if registry_result is not None:
            return registry_result
        html_pdf_city = _resolve_html_pdf_city(identity)
        if html_pdf_city is not None:
            return _html_or_pdf_only_result(html_pdf_city)
        if _looks_unincorporated(identity):
            return _no_city_flu_authority_result(identity)
        return _jurisdiction_not_covered_result(identity)

    layer_url = _LAYERS_BY_JURISDICTION[jurisdiction]
    sess = http_session(session)

    # Centroid-first: query the parcel interior point (when the boundary is
    # known) so the FLU lookup lands inside the lot, not on the road-edge
    # geocode. Falls back to the address point. docs/specs/travis_county_coverage.md.
    q_lat, q_lng = identity.query_point()
    features, error = _query_layer(
        sess, layer_url, q_lat, q_lng,
    )
    if error:
        return _fetch_failed_result(error_msg=error)

    designations = _summarize_features(features)
    legacy_status = (
        "mixed" if len(designations) > 1
        else "clear" if designations
        else "unknown"
    )
    confidence = (
        DataConfidenceStatus.UNKNOWN
        if legacy_status == "unknown"
        else map_legacy_status(legacy_status, screening_only=True)
    )
    advisory = _build_advisory(designations, jurisdiction)

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=confidence,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": legacy_status,
            "jurisdiction": jurisdiction,
            "designations": designations,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_FLU_UPLOAD_TYPES),
            reason=(
                "Future land use is planning intent, not a "
                "zoning entitlement. Upload a comp-plan excerpt "
                "or a planner email to confirm the current "
                "designation."
            ),
        ),
    )


def _query_layer(
    session: requests.Session, layer_url: str,
    lat: float, lng: float,
) -> tuple[list[dict[str, Any]], str | None]:
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = session.get(layer_url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("features", []) or [], None
    except Exception as exc:
        log.warning("FLU layer query failed (%s): %s", layer_url, exc)
        return [], str(exc)[:200]


def _resolve_designation(attrs: dict[str, Any]) -> str | None:
    for candidate in _DESIGNATION_FIELD_CANDIDATES:
        value = attrs.get(candidate)
        if value is None or value == "":
            continue
        if isinstance(value, int):
            mapped = _AUSTIN_FLU_CODES.get(value)
            return mapped or f"FLU code {value}"
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _summarize_features(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features:
        attrs = feat.get("attributes") or {}
        designation = _resolve_designation(attrs)
        overlays: list[str] = []
        if attrs.get("MIXED_USE_BUILDING"):
            overlays.append("Mixed Use Building")
        if attrs.get("NEIGHBORHOOD_URBAN_CENTER"):
            overlays.append("Neighborhood Urban Center")
        if attrs.get("RESIDENTIAL_INFILL"):
            overlays.append("Residential Infill")
        npa = attrs.get("NEIGHBORHOOD_PLANNING_AREA")
        out.append({
            "designation": designation or "Unspecified",
            "neighborhood_planning_area": (
                npa.strip() if isinstance(npa, str) and npa.strip() else None
            ),
            "overlays": overlays,
            "raw_attributes": attrs,
        })
    return out


def _build_advisory(
    designations: list[dict[str, Any]], jurisdiction: str,
) -> str:
    if not designations:
        return (
            f"No future land use designation found for this "
            f"point in {jurisdiction.title()}'s comp plan. "
            "Verify with the planning department or upload "
            "a comp-plan excerpt."
        )
    names = ", ".join(d["designation"] for d in designations[:3])
    qual = (
        "intersects multiple designations"
        if len(designations) > 1 else "designated"
    )
    return (
        f"{jurisdiction.title()} comprehensive plan {qual}: "
        f"{names}. This is planning intent, NOT a zoning "
        "entitlement — confirms what the city plans, not what "
        "the parcel is permitted to do today."
    )


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown", "jurisdiction": None,
            "designations": [],
            "advisory": "Coordinates required for FLU lookup.",
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for FLU point query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_FLU_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable — upload a comp-plan "
                "excerpt or future-land-use map screenshot."
            ),
        ),
    )


def _no_city_flu_authority_result(
    identity: PropertyIdentity,
) -> AdapterResult:
    label = identity.county or "this county"
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": "no_city_flu_authority",
            "jurisdiction": None,
            "designations": [],
            "advisory": (
                f"Parcel sits in unincorporated {label.title()} "
                "County — no city comprehensive plan applies. "
                "Future Land Use is a CITY planning instrument; "
                "Texas counties do not adopt comp plans. Use "
                "restrictions, if any, come from deed-recorded "
                "restrictive covenants or county subdivision "
                "regulations."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=[
                "recorded restrictive covenants / CC&Rs",
                "county subdivision regulations",
                "approved subdivision plat",
            ],
            reason=(
                f"Unincorporated {label.title()} County has no "
                "city FLU. Use deed-recorded covenants and the "
                "county's subdivision regulations to confirm "
                "any use limitations."
            ),
        ),
    )


_REGISTRY_OFFICIAL_FLU_MODES = frozenset({
    SourceMode.OFFICIAL_PDF_OR_MAP,
    SourceMode.OFFICIAL_STAFF_REPORT,
    SourceMode.OFFICIAL_WEB_PORTAL,
    SourceMode.HTML_TABLE,
})


def _registry_official_flu_result(
    identity: PropertyIdentity,
    *,
    config: dict[str, Any] | None = None,
    session: requests.Session | None = None,
) -> AdapterResult | None:
    """Return a COVERED + SCREENED_INDICATOR FLU result when the county source
    registry knows an OFFICIAL (non-REST) FLU source for this jurisdiction.

    This is the fix for the Manor miss: Manor publishes its Future Land Use Map
    as an official PDF and states parcel designations (e.g. "Commercial
    Corridor") in rezoning staff reports, but has no public REST FLU layer. That
    is a covered official source — surface it with the source link and an honest
    "screened, verify against the document" confidence, never UNKNOWN.

    Returns None when no registry entry applies (caller keeps legacy fallback).
    The actual designation text is filled by the document-read path (Phase 3);
    until then the result names the official source + the broker next step."""
    county = identity.county
    for candidate in identity.jurisdiction_candidates or []:
        sources = get_jurisdiction_sources(county, candidate)
        if sources is None:
            continue
        card = sources.future_land_use
        if card.mode not in _REGISTRY_OFFICIAL_FLU_MODES:
            continue
        city_label = sources.display_name
        # Official-source baseline only. The exact designation is filled later by
        # the orchestrator's metered AI read (flu_reader.enrich_flu_with_reading),
        # which holds the DB session + license + model. The adapter stays
        # HTTP-pure and never makes the metered call itself.
        read_result: dict[str, Any] | None = None
        designation: str | None = None
        zoning: str | None = None
        designations: list[dict[str, Any]] = []
        mode_label = (
            "official Future Land Use Map / comp-plan PDF"
            if card.mode == SourceMode.OFFICIAL_PDF_OR_MAP
            else "official city staff report / agenda packet"
            if card.mode == SourceMode.OFFICIAL_STAFF_REPORT
            else "official city planning portal"
            if card.mode == SourceMode.OFFICIAL_WEB_PORTAL
            else "official city data table"
        )
        if designation:
            advisory = (
                f"{city_label}'s future land use is published as an "
                f"{mode_label}, not a queryable GIS layer. PropDesk read an "
                f"official candidate document and found {designation}. Future "
                "land use is comprehensive-plan intent, not a zoning entitlement."
            )
        else:
            advisory = (
                f"{city_label}'s future land use is published as an {mode_label}, "
                "not a queryable GIS layer. PropDesk identified the official "
                "source below — open it to read this parcel's designation. "
                "Future land use is comprehensive-plan intent, not a zoning "
                "entitlement."
            )
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=(
                SourceAccessStatus.HTML_OR_PDF_ONLY
                if card.mode in {
                    SourceMode.OFFICIAL_PDF_OR_MAP,
                    SourceMode.OFFICIAL_WEB_PORTAL,
                }
                else SourceAccessStatus.PROGRAMMATIC_PUBLIC
            ),
            data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
            coverage_status=CoverageStatus.COVERED,
            normalized_facts={
                "status": (
                    "document_read_designation_found"
                    if designation else "official_source_available"
                ),
                "jurisdiction": city_label.lower(),
                "designations": designations,
                "designation": designation,
                "zoning": zoning,
                "source_mode": card.mode.value,
                "official_source_url": card.url,
                "source_note": card.note,
                "flu_document_read": read_result,
                "advisory": advisory,
                "contradiction_flags": [],
            },
            identifier_used="lat_lng",
            fetched_at=datetime.now(UTC),
            advisories=[advisory],
            manual_upload_request=ManualUploadRequest(
                document_types=list(_FLU_UPLOAD_TYPES),
                reason=(
                    f"{city_label} future land use comes from an official "
                    f"{mode_label}. Open {card.url or 'the city source'} to read "
                    "the parcel designation, or upload the comp-plan excerpt / "
                    "staff report to anchor it."
                ),
            ),
        )
    return None




def _html_or_pdf_only_result(city_key: str) -> AdapterResult:
    entry = _FLU_HTML_PDF_REGISTRY[city_key]
    plan_name = entry["plan_name"]
    plan_url = entry["plan_url"]
    instructions = entry["instructions"]
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.HTML_OR_PDF_ONLY,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "manual_verification_required",
            "jurisdiction": city_key,
            "designations": [],
            "plan_name": plan_name,
            "plan_url": plan_url,
            "manual_instructions": instructions,
            "advisory": (
                f"{city_key.replace('_', ' ').title()}'s comp "
                f"plan ({plan_name}) is published only as HTML/"
                f"PDF — no GIS layer. Follow the upload "
                "instructions to capture the parcel's future "
                "land use designation."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=[
                "future land use map screenshot",
                "comp-plan excerpt PDF",
                "planner email confirming designation",
            ],
            reason=(
                f"{plan_name} only published as HTML/PDF. "
                f"Steps: {instructions} Source: {plan_url}"
            ),
        ),
    )


def _jurisdiction_not_covered_result(
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
            "status": "unknown", "jurisdiction": None,
            "designations": [],
            "advisory": (
                f"No FLU layer wired for {label} yet. Upload a "
                "comp-plan excerpt or planner email."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_FLU_UPLOAD_TYPES),
            reason=(
                f"FLU coverage for {label} is not wired. "
                "Upload a comp-plan excerpt or planner email."
            ),
        ),
    )


def _fetch_failed_result(*, error_msg: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "unknown", "jurisdiction": None,
            "designations": [],
            "advisory": (
                "FLU service did not respond. Upload a "
                "comp-plan excerpt or planner email."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=[f"FLU fetch failed: {error_msg}"[:500]],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_FLU_UPLOAD_TYPES),
            reason=(
                "FLU service did not respond. Retry, or upload "
                "a comp-plan excerpt / planner email."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
