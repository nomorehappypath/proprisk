"""twdb_ccn adapter — TWDB Certificate of Convenience and Necessity.

Ported verbatim from legacy_services/core/public_intel_adapters/
ccn.py (May 2026). Identity / contract import paths adjusted
for reference_backend; SOURCE_ID matches registry; coverage_status
field added on every AdapterResult.

Combined water + wastewater provider lookup. Queries TWO TWDB
layers per kind:
  - CCN layer       = LEGAL authority of record (slow to update)
  - PWS Service Areas = ACTUAL OPERATING provider (TCEQ)
Cross-source disagreement downgrades to SCREENED_INDICATOR.

CCN territory tells you who has authority — capacity is a
separate question that always requires a will-serve letter.
The manual_upload_request is always populated for that reason.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

import requests
from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union

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
)

log = logging.getLogger("propdesk.site_check.twdb_ccn")

SOURCE_ID = "twdb_ccn"
SOURCE_ACCESS_STATUS = SourceAccessStatus.PROGRAMMATIC_PUBLIC

# URLs + radii come from settings (docs/specs/sewer_availability_screening.md;
# no-hardcoded-config). Live incident 2026-07-22: TWDB REMOVED its CCN Sewer
# service — the old constant 404'd and per-layer degradation swallowed it, so
# every property silently screened with no wastewater provider. Sewer now uses
# PUCT's own layer (the org behind the official CCN viewer), water keeps TWDB
# first with the PUCT layer as mirror.
from app.config.settings import get_settings

_MAP_CLIP_RADIUS_MILES = 6

_TWDB_PUC_SEARCH_URL = (
    "https://www14.tceq.texas.gov/iwud/utility/index.cfm"
)

_AMBIGUOUS_PROVIDER_NAMES: dict[str, str] = {
    "AQUA": (
        "There are TWO unrelated 'Aqua' utilities in Central TX. "
        "AQUA WATER SUPPLY CORPORATION (Aqua WSC) is a non-profit "
        "member-owned utility based in Bastrop serving rural "
        "Travis / Bastrop / Lee / Caldwell counties. AQUA TEXAS "
        "INC is a private subsidiary of Essential Utilities "
        "serving cities like Kyle, Wimberley, and parts of "
        "Austin. Confirm which one is the named provider before "
        "requesting a will-serve letter."
    ),
}

_UTILITY_UPLOAD_TYPES = [
    "will-serve letter",
    "SER response",
    "utility concept review response",
    "provider confirmation email",
    "utility map PDF",
]

_DATA_LAG_NOTE = (
    "If the named provider doesn't match what you know about "
    "the parcel — utilities buy each other regularly and "
    "TWDB's published CCN layer can lag transfers by months — "
    "treat the will-serve letter from the actual provider as "
    "the authoritative answer."
)

_PROVIDER_FIELD_CANDIDATES: tuple[str, ...] = (
    "UTILITY_NAME", "UTILITY", "UTIL_NAME", "PROVIDER",
    "PWSName", "WS_NAME", "SYSTEM_NAME", "WW_NAME",
    "NAME", "CCN_HOLDER",
)


def _format_provider_list(label: str, providers: list[str]) -> str:
    if not providers:
        return ""
    if len(providers) == 1:
        return f"{label} provider: {providers[0]}"
    return (
        f"{label} — {len(providers)} candidate providers within "
        f"{get_settings().ccn_point_buffer_m}m: {', '.join(providers)}. "
        f"Boundary parcel or recent transfer; verify with each."
    )


def _disambiguation_notes(providers: list[str]) -> list[str]:
    notes = []
    seen_keys = set()
    for p in providers:
        for key, note in _AMBIGUOUS_PROVIDER_NAMES.items():
            if key in p.upper() and key not in seen_keys:
                notes.append(note)
                seen_keys.add(key)
    return notes


def _build_advisory(
    water_providers: list[str],
    wastewater_providers: list[str],
    water_disagreement: bool,
    wastewater_disagreement: bool,
    wastewater_nearest: dict[str, Any] | None = None,
    sewer_district_candidates: list[dict[str, Any]] | None = None,
    municipal_wastewater_candidate: str | None = None,
) -> str:
    parts: list[str] = []
    if water_providers:
        parts.append(_format_provider_list("Water", water_providers))
        parts.append("Capacity unverified.")
    else:
        parts.append(
            "No water provider mapped at this location — parcel "
            "may be on private well or outside any provider's "
            "service area. Verify with neighbors or the county "
            "appraisal district utilities line."
        )
    if wastewater_providers:
        parts.append(
            _format_provider_list("Wastewater", wastewater_providers)
        )
        parts.append("Capacity unverified.")
    elif municipal_wastewater_candidate:
        parts.append(
            "No state-certificated sewer area covers this parcel, but it is "
            f"inside {municipal_wastewater_candidate} city limits. Contact "
            f"{municipal_wastewater_candidate} first: cities can provide sewer "
            "without a CCN covering the parcel. A nearby CCN is context only, "
            "not evidence that its provider serves this site."
        )
    elif sewer_district_candidates:
        names = ", ".join(
            f"{c['name']} ({c['kind']})" for c in sewer_district_candidates[:3]
        )
        parts.append(
            "No state-certificated sewer area covers this parcel, but it lies "
            f"inside {names} — utility districts of this kind commonly provide "
            "sewer inside their boundary. Ask the district for a written "
            "service-availability answer."
        )
    elif wastewater_nearest:
        parts.append(
            "No sewer service area covers this parcel. The nearest "
            f"certificated sewer provider is {wastewater_nearest['provider']}"
            + (f" (CCN {wastewater_nearest['ccn_no']})"
               if wastewater_nearest.get("ccn_no") else "")
            + f", within about {wastewater_nearest['within_m']:,} m. "
            "An extension that far is a provider decision — request a "
            "written service-availability answer. Otherwise on-site "
            "septic (OSSF) through the county is the likely path. "
            "Note: some cities serve sewer without a certificated area, "
            "so if the parcel is inside city limits, ask the city "
            "utility first."
        )
    else:
        parts.append(
            "No sewer service area covers this parcel, and none was "
            "found nearby. On-site septic (OSSF) through the county is "
            "the likely path. Note: some cities serve sewer without a "
            "certificated area, so if the parcel is inside city limits, "
            "ask the city utility first."
        )
    if water_disagreement or wastewater_disagreement:
        parts.append(
            "TWDB's legal CCN layer and operating-provider "
            "layer disagree for this parcel — likely a recent "
            "service-area transfer not yet reflected in the "
            "CCN of record. Confirm the actual current "
            "provider before requesting a will-serve letter; "
            "TWDB Public Utility Search: "
            f"{_TWDB_PUC_SEARCH_URL}"
        )
    parts.extend(_disambiguation_notes(
        list(water_providers) + list(wastewater_providers),
    ))
    if water_providers or wastewater_providers:
        parts.append(_DATA_LAG_NOTE)
    return " ".join(parts)


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    sess = http_session(session)
    settings = get_settings()
    jurisdiction = config.get("jurisdiction") if isinstance(config, dict) else None
    jurisdiction_kind = str(
        jurisdiction.get("jurisdiction_kind") if isinstance(jurisdiction, dict) else ""
    ).casefold()
    jurisdiction_city = str(
        jurisdiction.get("city") if isinstance(jurisdiction, dict) else ""
    ).strip()
    municipal_wastewater_candidate = (
        f"City of {jurisdiction_city}"
        if jurisdiction_city and jurisdiction_kind in {"city", "full_purpose_city"}
        else None
    )
    buffer_m = settings.ccn_point_buffer_m
    # Membership is asked of the verified parcel polygon when the run has one
    # (spec §4); the point+buffer remains both the fallback and the nearest-
    # search geometry.
    parcel_geom = _membership_geometry(identity)

    ccn_water_features, water_ccn_error = _query_layer_multi(
        sess, settings.ccn_water_query_urls, identity.lat, identity.lng,
        buffer_m=0 if parcel_geom else buffer_m, geometry_override=parcel_geom,
    )
    pws_features, pws_error = _query_layer(
        sess, settings.ccn_pws_operator_query_url, identity.lat, identity.lng,
        buffer_m=0 if parcel_geom else buffer_m,
        where="Active = 1", geometry_override=parcel_geom,
    )
    ccn_sewer_features, sewer_error = _query_layer_multi(
        sess, settings.ccn_wastewater_query_urls,
        identity.lat, identity.lng,
        buffer_m=0 if parcel_geom else buffer_m, geometry_override=parcel_geom,
    )

    ccn_water_providers = _extract_providers(ccn_water_features)
    pws_water_providers = _extract_providers(pws_features)
    sewer_providers = _extract_providers(ccn_sewer_features)

    # No sewer CCN → check TCEQ water districts (MUD/SUD/WCID/FWSD) covering
    # the parcel: they commonly provide sewer inside their boundary (spec §2
    # branch 3). Point-in-polygon per layer; failures degrade per-layer.
    sewer_district_candidates: list[dict[str, Any]] = []
    district_features: list[dict[str, Any]] = []
    if not sewer_providers and not sewer_error:
        for layer_url in settings.tceq_water_district_query_urls:
            d_features, d_error = _query_layer(
                sess, layer_url, identity.lat, identity.lng,
                geometry_override=parcel_geom,
            )
            if d_error:
                continue
            for feat in d_features[:5]:
                attrs = feat.get("attributes") or {}
                name = str(attrs.get("NAME") or "").strip()
                if not name:
                    continue
                if str(attrs.get("STATUS") or "A").strip() not in ("A", ""):
                    continue
                sewer_district_candidates.append({
                    "name": name,
                    "kind": str(attrs.get("TYPE") or "district").strip(),
                })
                district_features.append(feat)


    # No sewer CCN at the parcel and the layer itself answered → find the
    # NEAREST certificated sewer area at escalating radii, so the broker gets
    # "who is closest and roughly how far" instead of a bare none
    # (spec §2 branch 5). A layer failure skips this: unknown stays unknown.
    wastewater_nearest: dict[str, Any] | None = None
    nearest_sewer_features: list[dict[str, Any]] = []
    if not sewer_providers and not sewer_error:
        for radius in settings.sewer_nearest_search_radii_m:
            near_features, near_error = _query_layer_multi(
                sess, settings.ccn_wastewater_query_urls,
                identity.lat, identity.lng, buffer_m=radius,
            )
            if near_error:
                break
            near_providers = _extract_providers(near_features)
            if near_providers:
                attrs = (near_features[0].get("attributes") or {})
                wastewater_nearest = {
                    "provider": near_providers[0],
                    "ccn_no": attrs.get("CCN_NO"),
                    "within_m": radius,
                }
                # Their POLYGONS ship too — "where are the closest sewers"
                # must be visible on the map, not only named in text
                # (Admin, 2026-07-22). The display clip bounds them.
                nearest_sewer_features = near_features
                break

    water_disagreement = bool(
        ccn_water_providers
        and pws_water_providers
        and not _name_overlap(
            ccn_water_providers, pws_water_providers,
        )
    )

    water_providers = _dedupe_preserve_order(
        ccn_water_providers + pws_water_providers,
    )
    service_territories = [
        *_service_territories(
            ccn_water_features,
            service="water",
            authority="legal_ccn",
            source="TWDB / Public Utility Commission CCN Water",
            center_lat=identity.lat,
            center_lng=identity.lng,
        ),
        *_service_territories(
            pws_features,
            service="water",
            authority="operating_provider",
            source="TWDB / TCEQ Public Water Service Areas",
            center_lat=identity.lat,
            center_lng=identity.lng,
        ),
        *_service_territories(
            ccn_sewer_features,
            service="wastewater",
            authority="legal_ccn",
            source="PUCT CCN Sewer Service Areas",
            center_lat=identity.lat,
            center_lng=identity.lng,
        ),
        *_service_territories(
            district_features,
            service="district",
            authority="tceq_water_district",
            source="TCEQ Water Districts",
            center_lat=identity.lat,
            center_lng=identity.lng,
        ),
        *_service_territories(
            nearest_sewer_features,
            service="wastewater",
            authority="nearest_ccn",
            source="PUCT CCN Sewer Service Areas (nearest — does NOT cover this parcel)",
            center_lat=identity.lat,
            center_lng=identity.lng,
        ),
    ]

    errors: list[str] = []
    if water_ccn_error:
        errors.append(f"water CCN query failed: {water_ccn_error}")
    if pws_error:
        errors.append(f"PWS service-area query failed: {pws_error}")
    if sewer_error:
        errors.append(f"wastewater CCN query failed: {sewer_error}")

    all_failed = water_ccn_error and pws_error and sewer_error
    if all_failed:
        confidence = DataConfidenceStatus.UNKNOWN
        status = "unknown"
    elif (
        water_disagreement
        or len(water_providers) > 1
        or water_ccn_error or pws_error or sewer_error
    ):
        confidence = DataConfidenceStatus.SCREENED_INDICATOR
        status = "mixed"
    else:
        confidence = DataConfidenceStatus.VERIFIED_PUBLIC_FACT
        status = (
            "clear"
            if (water_providers or sewer_providers)
            else "no_ccn_coverage"
        )

    advisory = _build_advisory(
        water_providers, sewer_providers,
        water_disagreement=water_disagreement,
        wastewater_disagreement=False,
        wastewater_nearest=wastewater_nearest,
        sewer_district_candidates=sewer_district_candidates,
        municipal_wastewater_candidate=municipal_wastewater_candidate,
    )

    # Ready-to-send availability request (spec §2): the app does the drafting;
    # only the provider's written answer is authoritative.
    letter_provider = (
        sewer_providers[0] if sewer_providers
        else municipal_wastewater_candidate if municipal_wastewater_candidate
        else sewer_district_candidates[0]["name"] if sewer_district_candidates
        else wastewater_nearest["provider"] if wastewater_nearest
        else None
    )
    availability_request_text = None
    if letter_provider:
        where = identity.address or f"{identity.lat:.5f}, {identity.lng:.5f}"
        parcel_ref = (
            f" (parcel ID {identity.parcel_id})"
            if getattr(identity, "parcel_id", None) else ""
        )
        availability_request_text = (
            f"To {letter_provider}:\n\n"
            f"Please confirm in writing whether wastewater (sewer) service is "
            f"currently available to the property at {where}{parcel_ref}, "
            "including: the location and size of the nearest sewer main, "
            "available capacity, the anticipated connection point, any "
            "extension requirements, and all tap and impact fees. Please also "
            "confirm the equivalent for water service if you provide it.\n\n"
            "A written service-availability (will-serve) answer is requested."
        )

    if confidence != DataConfidenceStatus.UNKNOWN:
        upload_request = ManualUploadRequest(
            document_types=list(_UTILITY_UPLOAD_TYPES),
            reason=(
                "CCN territory tells you which provider has "
                "authority — it does NOT confirm capacity, "
                "extension distance, or upgrade burden. Upload "
                "a will-serve letter or SER response to "
                "confirm capacity. To verify the named "
                "provider when in doubt, use the TWDB Public "
                f"Utility Search: {_TWDB_PUC_SEARCH_URL}"
            ),
        )
    else:
        upload_request = ManualUploadRequest(
            document_types=list(_UTILITY_UPLOAD_TYPES),
            reason=(
                "TWDB CCN service did not respond. Retry, or "
                "upload a will-serve letter / utility map / "
                "provider confirmation email."
            ),
        )

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SOURCE_ACCESS_STATUS,
        data_confidence_status=confidence,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": status,
            "water_provider": (
                water_providers[0] if water_providers else None
            ),
            "wastewater_provider": (
                sewer_providers[0] if sewer_providers else None
            ),
            "water_providers": water_providers,
            "wastewater_providers": sewer_providers,
            "water_provider_legal_ccn": ccn_water_providers,
            "water_provider_operating": pws_water_providers,
            "wastewater_nearest": wastewater_nearest,
            "municipal_wastewater_candidate": municipal_wastewater_candidate,
            "sewer_district_candidates": sewer_district_candidates,
            "availability_request_text": availability_request_text,
            "service_territories": service_territories,
            "providers_disagree": water_disagreement,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors if errors else None,
        manual_upload_request=upload_request,
    )


def _query_layer_multi(
    session: requests.Session,
    layer_urls: list[str],
    lat: float,
    lng: float,
    *,
    buffer_m: int = 0,
    where: str | None = None,
    geometry_override: tuple[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Try each mirror in order; first URL that ANSWERS wins (an empty feature
    list is an answer — 'no CCN here' is a real result, not a reason to ask a
    second mirror). Only transport/HTTP failures rotate. All mirrors failing
    returns the last error, so the caller degrades exactly as before."""
    last_error: str | None = "no CCN layer URLs configured"
    for url in layer_urls:
        features, error = _query_layer(
            session, url, lat, lng, buffer_m=buffer_m, where=where,
            geometry_override=geometry_override,
        )
        if error is None:
            return features, None
        last_error = error
    return [], last_error


def _membership_geometry(identity: PropertyIdentity) -> tuple[str, str] | None:
    """The parcel polygon as an ArcGIS geometry, when the run has verified one.

    Spec §4: membership is asked of the PARCEL, not just its anchor point — a
    corner anchor can sit in a different CCN than most of the land. Falls back
    to the point+buffer when no polygon is known (returns None)."""
    import json as _json
    polygon = identity.parcel_geometry or {}
    if polygon.get("type") == "Polygon" and polygon.get("coordinates"):
        return (
            _json.dumps({
                "rings": polygon["coordinates"],
                "spatialReference": {"wkid": 4326},
            }),
            "esriGeometryPolygon",
        )
    return None


def _query_layer(
    session: requests.Session,
    layer_url: str,
    lat: float,
    lng: float,
    *,
    buffer_m: int = 0,
    where: str | None = None,
    geometry_override: tuple[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": "5",
        "maxAllowableOffset": "0.0002",
        # NO resultRecordCount: TWDB MapServer layers reject it outright
        # ("Pagination is not supported", HTTP 200 + in-JSON 400) and TCEQ's
        # older server does the same. The old code sent it AND swallowed
        # in-JSON errors, so the legal-CCN layer silently returned nothing.
        # Feature volume is bounded client-side ([:10] in the consumers).
        "f": "json",
    }
    if geometry_override is not None:
        params["geometry"], params["geometryType"] = geometry_override
    if buffer_m and buffer_m > 0:
        params["distance"] = buffer_m
        params["units"] = "esriSRUnit_Meter"
    if where:
        params["where"] = where
    try:
        resp = session.get(
            layer_url, params=params,
            timeout=get_settings().ccn_query_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        # ArcGIS reports many failures as HTTP 200 + an in-JSON error object.
        # Treating that as "no features" would make a broken mirror look like
        # an authoritative empty answer — surface it as the error it is.
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = f"arcgis error {err.get('code')}: {err.get('message')}"
            log.warning("CCN layer in-JSON error (%s): %s", layer_url, msg)
            return [], msg[:200]
        return data.get("features", []) or [], None
    except Exception as exc:
        log.warning(
            "CCN layer query failed (%s): %s", layer_url, exc,
        )
        return [], str(exc)[:200]


def _extract_providers(
    features: list[dict[str, Any]],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        for candidate in _PROVIDER_FIELD_CANDIDATES:
            value = attrs.get(candidate)
            if (
                value and isinstance(value, str)
                and value.strip()
            ):
                stripped = value.strip()
                upper = stripped.upper()
                if upper not in seen:
                    seen.add(upper)
                    out.append(stripped)
                break
    return out


def _service_territories(
    features: list[dict[str, Any]],
    *,
    service: str,
    authority: str,
    source: str,
    center_lat: float,
    center_lng: float,
) -> list[dict[str, Any]]:
    territories: list[dict[str, Any]] = []
    for feature in features[:10]:
        geometry = feature.get("geometry") or {}
        rings = geometry.get("rings")
        if not isinstance(rings, list) or not rings:
            continue
        clipped_geometry = _clipped_arcgis_polygon(
            rings,
            center_lat=center_lat,
            center_lng=center_lng,
        )
        if clipped_geometry is None:
            continue
        provider = _extract_providers([feature])
        territories.append({
            "service": service,
            "authority": authority,
            "provider": provider[0] if provider else "Provider not named",
            "source": source,
            "geometry_scope": f"display_clip_{_MAP_CLIP_RADIUS_MILES}mi",
            "geometry": clipped_geometry,
        })
    return territories


def _clipped_arcgis_polygon(
    rings: list[Any],
    *,
    center_lat: float,
    center_lng: float,
) -> dict[str, Any] | None:
    polygons = []
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 4:
            continue
        try:
            polygon = Polygon([
                (float(point[0]), float(point[1]))
                for point in ring
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ])
        except (TypeError, ValueError):
            continue
        if polygon.is_empty:
            continue
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty:
            polygons.append(polygon)
    if not polygons:
        return None

    # ArcGIS encodes exterior and interior rings in one list. Applying an
    # even/odd fill preserves holes and disjoint islands without trusting ring
    # orientation, which varies across older public services.
    geometry = polygons[0]
    for polygon in polygons[1:]:
        geometry = geometry.symmetric_difference(polygon)
    lat_delta = _MAP_CLIP_RADIUS_MILES / 69
    longitude_miles_per_degree = max(
        1.0,
        69 * math.cos(math.radians(center_lat)),
    )
    lng_delta = _MAP_CLIP_RADIUS_MILES / longitude_miles_per_degree
    clipped = geometry.intersection(box(
        center_lng - lng_delta,
        center_lat - lat_delta,
        center_lng + lng_delta,
        center_lat + lat_delta,
    ))
    if clipped.is_empty:
        return None
    if clipped.geom_type == "GeometryCollection":
        clipped = unary_union([
            item for item in clipped.geoms
            if item.geom_type in {"Polygon", "MultiPolygon"}
        ])
    if clipped.is_empty or clipped.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    return mapping(clipped)


def _name_overlap(
    list_a: list[str], list_b: list[str],
) -> bool:
    upper_a = [a.upper() for a in list_a]
    upper_b = [b.upper() for b in list_b]
    for a in upper_a:
        for b in upper_b:
            if a == b or a in b or b in a:
                return True
    return False


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        upper = x.upper()
        if upper not in seen:
            seen.add(upper)
            out.append(x)
    return out


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SOURCE_ACCESS_STATUS,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown",
            "water_provider": None,
            "wastewater_provider": None,
            "water_providers": [],
            "wastewater_providers": [],
            "water_provider_legal_ccn": [],
            "water_provider_operating": [],
            "wastewater_nearest": None,
            "sewer_district_candidates": [],
            "availability_request_text": None,
            "service_territories": [],
            "providers_disagree": False,
            "advisory": (
                "Coordinates required for CCN territory lookup. "
                "Upload a utility service map or provider "
                "confirmation email."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=[
            ("lat/lng required for CCN point query; "
            "PropertyIdentity has none"),
        ],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_UTILITY_UPLOAD_TYPES),
            reason=(
                "Coordinates not available for TWDB CCN lookup. "
                "Upload a will-serve letter or provider map."
            ),
        ),
    )


__all__ = ["SOURCE_ACCESS_STATUS", "SOURCE_ID", "fetch"]
