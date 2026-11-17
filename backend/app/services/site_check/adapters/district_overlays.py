"""district_overlays adapter — county subdivision + special districts.

Ported verbatim from legacy_services/core/public_intel_adapters/
district_overlays.py (May 2026). Identity / contract import
paths adjusted; SOURCE_ID matches registry; coverage_status
field added.

County dispatch — Travis / Williamson / Hays / Bastrop have
verified local ArcGIS endpoints. Every Texas coordinate also checks
TCEQ's statewide MUD and WCID layers. Local publishers add subdivision,
ESD, PID, TIRZ, and school-district coverage where available.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

import requests
from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union

from app.regions import loader as regions
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

log = logging.getLogger("propdesk.site_check.district_overlays")

SOURCE_ID = "district_overlays"


_TCEQ_WATER_DISTRICTS_BASE = (
    "https://gisweb.tceq.texas.gov/arcgis/rest/services/iwud/"
    "WaterDistricts_PRD/MapServer"
)
_TCEQ_MUD_QUERY = f"{_TCEQ_WATER_DISTRICTS_BASE}/6/query"
_TCEQ_WCID_QUERY = f"{_TCEQ_WATER_DISTRICTS_BASE}/13/query"

LayerSpec = tuple[str, tuple[str, ...], str]

# From each county file's `districts:` list (app/regions/tx/counties/*.yaml),
# in file order. Within one entry the URLs are fallbacks, tried in order.
_LAYERS_BY_COUNTY: dict[str, list[LayerSpec]] = {
    county: [(d["label"], tuple(d["urls"]), d["kind"]) for d in districts]
    for county, districts in regions.county_sections("TX", "districts").items()
}

_TEXAS_STATEWIDE_LAYERS: list[LayerSpec] = [
    ("TCEQ statewide municipal utility districts", (_TCEQ_MUD_QUERY,), "mud"),
    (
        "TCEQ statewide water control and improvement districts",
        (_TCEQ_WCID_QUERY,),
        "wcid",
    ),
]

_TIMEOUT = 25
_DISTRICT_UPLOAD_TYPES = ["survey", "recorded plat", "title commitment"]

_NAME_FIELD_CANDIDATES: tuple[str, ...] = (
    "SUBDIV_NAME", "SUB_NAME", "SUBDIVISION", "SUBDIV",
    "PLAT_NAME", "PROJECT_NAME",
    "ESD_NAME", "ESD_Name", "ESD_Name_Full",
    "EntityName", "full_mud_name", "wcid_name",
    "school_district_name", "esd_name",
    "DISTRICT_NAME", "DIST_NAME",
    "NAME", "Name",
    "desc_", "DESC", "Description",
)
_ID_FIELD_CANDIDATES: tuple[str, ...] = (
    "SUBDIV_ID", "DISTRICT_ID", "DIST_ID", "PLAT_ID",
    "CoA_ESD_Code", "County_ESD_Code", "FIRCODE", "FIR2CODE",
    "EntityCode", "esd", "code", "OBJECTID", "FID",
)


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    county = identity.county
    layers = [
        *_LAYERS_BY_COUNTY.get(county or "", []),
        *_TEXAS_STATEWIDE_LAYERS,
    ]

    sess = http_session(session)

    overlays_by_kind: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    source_warnings: list[str] = []
    successful_layer_count = 0

    # Centroid-first: query the parcel interior point (when known) so overlay
    # lookups land inside the lot, not on the road-edge geocode. Falls back to
    # the address point. docs/specs/travis_county_coverage.md.
    q_lat, q_lng = identity.query_point()
    for display_name, query_urls, kind in layers:
        features, error, fallback_warning = _query_layer_with_fallbacks(
            sess, query_urls, q_lat, q_lng,
        )
        if fallback_warning:
            source_warnings.append(
                f"{display_name}: primary public layer is temporarily "
                "unavailable; PropDesk used an official fallback layer."
            )
        if error:
            errors.append(
                f"{display_name}: public layer is temporarily unavailable."
            )
            continue
        successful_layer_count += 1
        overlays_by_kind.setdefault(kind, []).extend(
            _summarize_features(features, lat=q_lat, lng=q_lng),
        )

    overlays_by_kind = {
        kind: _dedupe_overlays(rows)
        for kind, rows in overlays_by_kind.items()
    }

    if successful_layer_count == 0:
        confidence = DataConfidenceStatus.UNKNOWN
        status = "unknown"
    elif errors:
        confidence = DataConfidenceStatus.SCREENED_INDICATOR
        status = "mixed"
    else:
        confidence = DataConfidenceStatus.VERIFIED_PUBLIC_FACT
        any_matches = any(overlays_by_kind.values())
        status = "clear" if any_matches else "no_overlays"

    advisory = _build_advisory(overlays_by_kind, county or "")
    if errors or source_warnings:
        advisory = (
            advisory
            + " Some county GIS layers are temporarily unavailable; "
            "PropDesk kept the original source checks and used official "
            "fallback layers only where they answered."
        )

    if confidence != DataConfidenceStatus.UNKNOWN:
        upload_request = ManualUploadRequest(
            document_types=list(_DISTRICT_UPLOAD_TYPES),
            reason=(
                "Public overlay layers are screening — a "
                "recorded plat or survey is authoritative for "
                "subdivision boundaries and parcel geometry."
            ),
        )
    else:
        upload_request = ManualUploadRequest(
            document_types=list(_DISTRICT_UPLOAD_TYPES),
            reason=(
                "County overlay service did not respond. "
                "Upload a recorded plat or survey."
            ),
        )

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=confidence,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": status,
            "county": county,
            "subdivisions": overlays_by_kind.get("subdivision", []),
            "mud_districts": overlays_by_kind.get("mud", []),
            "wcid_districts": overlays_by_kind.get("wcid", []),
            "esd_districts": overlays_by_kind.get("esd", []),
            "pid_districts": overlays_by_kind.get("pid", []),
            "tirz_districts": overlays_by_kind.get("tirz", []),
            "school_districts": overlays_by_kind.get("school", []),
            "unavailable_layers": errors,
            "source_warnings": source_warnings,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors if successful_layer_count == 0 and errors else None,
        manual_upload_request=upload_request,
    )


def _query_layer_with_fallbacks(
    session: requests.Session,
    layer_urls: tuple[str, ...],
    lat: float,
    lng: float,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    errors: list[str] = []
    for layer_url in layer_urls:
        features, error = _query_layer(session, layer_url, lat, lng)
        if error is None:
            warning = "; ".join(dict.fromkeys(errors))[:500] if errors else None
            return features, None, warning
        errors.append(error)
    return [], "; ".join(dict.fromkeys(errors))[:500], None


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
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": 6,
        "maxAllowableOffset": 0.00005,
        "f": "json",
    }
    try:
        resp = session.get(layer_url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("features", []) or [], None
    except Exception as exc:
        log.warning(
            "district overlay query failed (%s): %s", layer_url, exc,
        )
        return [], str(exc)[:200]


def _summarize_features(
    features: list[dict[str, Any]],
    *,
    lat: float,
    lng: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features:
        attrs = feat.get("attributes") or {}
        name = _first_nonempty(attrs, _NAME_FIELD_CANDIDATES)
        identifier = _first_nonempty(attrs, _ID_FIELD_CANDIDATES)
        geometry = _bounded_polygon_geometry(feat.get("geometry"), lat=lat, lng=lng)
        out.append({
            "name": name or "Unnamed",
            "id": identifier,
            **({"map_geometry": geometry} if geometry else {}),
        })
    return out


def _bounded_polygon_geometry(
    geometry: Any,
    *,
    lat: float,
    lng: float,
) -> dict[str, Any] | None:
    if not isinstance(geometry, dict):
        return None
    polygons = []
    for ring in geometry.get("rings") or []:
        if not isinstance(ring, list):
            continue
        coordinates = []
        for point in ring:
            if not isinstance(point, list) or len(point) < 2:
                continue
            try:
                coordinates.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                continue
        if len(coordinates) < 4:
            continue
        try:
            polygon = Polygon(coordinates)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if not polygon.is_empty:
                polygons.append(polygon)
        except (TypeError, ValueError):
            continue
    if not polygons:
        return None
    # The point query can return a county-scale district. Keep only the local
    # five-mile context so one district never dominates the map payload.
    dlat = 5 / 69
    dlng = 5 / (69 * max(0.2, math.cos(math.radians(lat))))
    clipped = unary_union(polygons).intersection(
        box(lng - dlng, lat - dlat, lng + dlng, lat + dlat),
    )
    if clipped.is_empty:
        return None
    if not clipped.is_valid:
        clipped = clipped.buffer(0)
    if clipped.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    return mapping(clipped.simplify(0.00004, preserve_topology=True))


def _first_nonempty(
    attrs: dict[str, Any], candidates: tuple[str, ...],
) -> str | None:
    for candidate in candidates:
        value = attrs.get(candidate)
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        elif value:
            return str(value)
    return None


def _dedupe_overlays(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("name") or row.get("id") or "").strip().casefold()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        output.append(row)
    return output


def _build_advisory(
    overlays_by_kind: dict[str, list[dict[str, Any]]],
    county: str,
) -> str:
    parts: list[str] = []
    subdivisions = overlays_by_kind.get("subdivision") or []
    if subdivisions:
        names = ", ".join(s["name"] for s in subdivisions[:3])
        parts.append(f"Subdivision: {names}.")
    for kind, label in (
        ("mud", "MUD"), ("wcid", "WCID"),
        ("esd", "ESD"), ("pid", "PID"),
        ("tirz", "TIRZ"), ("school", "School district"),
    ):
        items = overlays_by_kind.get(kind) or []
        if items:
            names = ", ".join(s["name"] for s in items)
            parts.append(f"{label}: {names}.")
    if not parts:
        parts.append(
            "No subdivision or special-district overlay matched "
            "the property at this point — verify with a recorded "
            "plat."
        )
    parts.append(
        "Public overlay layers are screening; recorded plat is "
        "authoritative."
    )
    return " ".join(parts)


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown",
            "county": None,
            "subdivisions": [],
            "mud_districts": [], "wcid_districts": [],
            "esd_districts": [], "pid_districts": [],
            "tirz_districts": [], "school_districts": [],
            "advisory": (
                "Coordinates required for overlay lookup. Upload "
                "a recorded plat or survey instead."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=[
            ("lat/lng required for district overlay lookup; "
            "PropertyIdentity has none"),
        ],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_DISTRICT_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable for county overlay "
                "lookup. Upload a recorded plat / survey."
            ),
        ),
    )


def _jurisdiction_not_covered_result(
    county: str | None,
) -> AdapterResult:
    county_label = county or "this property's county"
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.JURISDICTION_NOT_COVERED,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": "unknown",
            "county": county,
            "subdivisions": [],
            "mud_districts": [], "wcid_districts": [],
            "esd_districts": [], "pid_districts": [],
            "tirz_districts": [], "school_districts": [],
            "advisory": (
                f"No overlay layers wired for {county_label} yet. "
                "Upload a recorded plat or survey for "
                "definitive subdivision / district context."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_DISTRICT_UPLOAD_TYPES),
            reason=(
                f"County overlay coverage for {county_label} is "
                "not yet wired in this build. Upload a recorded "
                "plat or survey."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
