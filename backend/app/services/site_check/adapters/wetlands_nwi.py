"""wetlands_nwi adapter — USFWS National Wetlands Inventory.

Ported verbatim from legacy_services/core/public_intel_adapters/
wetlands.py (May 2026). Identity / contract import paths
adjusted for reference_backend; SOURCE_ID set to the registry
key "wetlands_nwi" (legacy_services used "usfws_nwi"); a
`coverage_status` field is added on every AdapterResult to
satisfy the reference_backend contract (legacy_services contract
doesn't have that axis).

Public NWI layer is screening-only. Confidence is CAPPED at
SCREENED_INDICATOR even on a clean "no features" answer.
A field delineation by a qualified consultant or a USACE
jurisdictional determination is the only definitive answer.

USGS-hosted endpoint (anonymous-accessible) is the canonical
public path; the legacy www.fws.gov host returns 403 to
non-browser User-Agents.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests
from pyproj import Geod
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import unary_union

from app.config.settings import get_settings
from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

log = logging.getLogger("propdesk.site_check.wetlands_nwi")

SOURCE_ID = "wetlands_nwi"
SOURCE_ACCESS_STATUS = SourceAccessStatus.PROGRAMMATIC_PUBLIC

_NWI_WETLANDS_QUERY = (
    "https://fwspublicservices.wim.usgs.gov/"
    "wetlandsmapservice/rest/services/"
    "Wetlands/MapServer/0/query"
)
_NWI_FWS_SPLIT_QUERIES = (
    ("https://fwsprimary.wim.usgs.gov/server/rest/services/Test/"
    "Wetlands_gdb_split/MapServer/0/query"),
    ("https://fwsprimary.wim.usgs.gov/server/rest/services/Test/"
    "Wetlands_gdb_split/MapServer/1/query"),
    ("https://fwsprimary.wim.usgs.gov/server/rest/services/Test/"
    "Wetlands_gdb_split/MapServer/2/query"),
    ("https://fwsprimary.wim.usgs.gov/server/rest/services/Test/"
    "Wetlands_gdb_split/MapServer/3/query"),
    ("https://fwsprimary.wim.usgs.gov/server/rest/services/Test/"
    "Wetlands_gdb_split/MapServer/4/query"),
    ("https://fwsprimary.wim.usgs.gov/server/rest/services/Test/"
    "Wetlands_gdb_split/MapServer/5/query"),
)
_NWI_ESRI_USA_WETLANDS_QUERY = (
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
    "USA_Wetlands/FeatureServer/0/query"
)
_TIMEOUT = 45  # NWI ArcGIS commonly responds in 22-30s; legacy_services uses 25.
_RETRY_BACKOFF_SECONDS = 1.5
_RADIUS_MIN_M = 100
_RADIUS_MAX_M = 500
_RADIUS_DEFAULT_M = 300
_SQFT_PER_ACRE = 4047
_GEOD = Geod(ellps="WGS84")
_SQM_PER_ACRE = 4046.8564224

_WETLAND_UPLOAD_TYPES = [
    "wetlands delineation",
    "USACE JD",
    "environmental consultant memo",
]

_ADVISORY_BY_STATUS: dict[str, str] = {
    "clear": (
        "No mapped wetland indicator found in screened NWI "
        "layers; delineation not performed."
    ),
    "mixed": (
        "Mapped wetland indicators are present in the screened "
        "NWI layer. Confirm parcel impact with a survey, site "
        "plan, wetlands delineation, or USACE jurisdictional "
        "determination before treating this as a development "
        "constraint."
    ),
    "unknown": (
        "Wetlands screening was not completed (NWI fetch "
        "failed or coordinates unavailable). Confirm with a "
        "wetlands delineation or USACE jurisdictional "
        "determination."
    ),
}


@dataclass(frozen=True)
class _NwiQueryResult:
    features: list[dict[str, Any]]
    source_warning: str | None = None
    # True when a zero-feature FALLBACK answer was proven trustworthy: the
    # same fallback layer returned mapped wetlands within the wide coverage
    # probe radius, so "no features at the parcel" means "no wetlands", not
    # "no data for this area" (2026-07-15 — see coverage-proof note below).
    coverage_proven: bool = False


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    radius_m: int | None = None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    sess = http_session(session)

    effective_radius = _radius_from_acreage(
        identity.acreage,
        default=radius_m or _RADIUS_DEFAULT_M,
    )
    geometry_param, geometry_type = _build_query_geometry(
        identity=identity,
        radius_m=effective_radius,
    )

    try:
        query_result = _query_nwi(
            sess,
            geometry_param,
            geometry_type,
            lat=identity.lat,
            lng=identity.lng,
        )
        if not isinstance(query_result, _NwiQueryResult):
            query_result = _NwiQueryResult(features=query_result)
    except Exception as exc:
        log.warning(
            "USFWS NWI fetch failed for (%s, %s): %s",
            identity.lat, identity.lng, exc,
        )
        return _fetch_failed_result(
            radius_m=effective_radius,
            error_msg=str(exc),
        )

    return _build_success_result(
        features=query_result.features,
        radius_m=effective_radius,
        geometry_type=geometry_type,
        parcel_geometry=identity.parcel_geometry,
        source_warning=query_result.source_warning,
        coverage_proven=query_result.coverage_proven,
    )


def _radius_from_acreage(
    acreage: float | None, *, default: int = _RADIUS_DEFAULT_M,
) -> int:
    if not acreage or acreage <= 0:
        return max(_RADIUS_MIN_M, min(_RADIUS_MAX_M, default))
    raw = math.ceil(math.sqrt(float(acreage) * _SQFT_PER_ACRE))
    return max(_RADIUS_MIN_M, min(_RADIUS_MAX_M, raw))


def _build_envelope_param(
    lat: float, lng: float, radius_m: int,
) -> str:
    deg_lat = radius_m / 111_320.0
    deg_lng = radius_m / (
        111_320.0 * max(math.cos(math.radians(lat)), 0.01)
    )
    xmin = lng - deg_lng
    xmax = lng + deg_lng
    ymin = lat - deg_lat
    ymax = lat + deg_lat
    return f"{xmin},{ymin},{xmax},{ymax}"


def _build_query_geometry(
    *,
    identity: PropertyIdentity,
    radius_m: int,
) -> tuple[str, str]:
    polygon = identity.parcel_geometry or {}
    if polygon.get("type") == "Polygon" and polygon.get("coordinates"):
        return (
            json.dumps({
                "rings": polygon["coordinates"],
                "spatialReference": {"wkid": 4326},
            }),
            "esriGeometryPolygon",
        )
    if identity.lat is None or identity.lng is None:
        raise ValueError("lat/lng required for NWI envelope query")
    return (
        _build_envelope_param(identity.lat, identity.lng, radius_m),
        "esriGeometryEnvelope",
    )


def _query_nwi(
    session: requests.Session,
    geometry_param: str | dict[str, Any],
    geometry_type: str,
    *,
    lat: float | None = None,
    lng: float | None = None,
) -> _NwiQueryResult:
    """Single retry on 500 / timeout. The USGS-hosted NWI
    service returns transient 500 Internal Server Errors a
    few times an hour; legacy_services ships without retries
    and gets unlucky often. One retry with a short backoff
    closes the parity gap on flaky days."""
    params = {
        "geometry": geometry_param,
        "geometryType": geometry_type,
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "Wetlands.WETLAND_TYPE,Wetlands.ATTRIBUTE,Wetlands.ACRES"
        ),
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    last_exc: Exception | None = None
    primary_zero_unproven = False
    for attempt in (1, 2):
        try:
            primary_features = _query_nwi_endpoint(
                session, _NWI_WETLANDS_QUERY, params,
            )
            if primary_features:
                return _NwiQueryResult(features=primary_features)
            # The primary answered with ZERO features. A radius probe
            # cannot prove that zero (2026-07-16 live finding: the
            # restored post-migration primary is PATCHY at quad level —
            # dead-center Lady Bird Lake returns zero while a quad
            # within 5 km has data). The only honest check is a direct
            # cross-check of the SAME screening envelope against the
            # fallback layers: fall through and let them confirm or
            # contradict the zero.
            primary_zero_unproven = True
            break
        except (
            requests.exceptions.HTTPError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectTimeout,
        ) as exc:
            last_exc = exc
            if attempt == 1:
                import time as _time
                _time.sleep(_RETRY_BACKOFF_SECONDS)
                continue
    fallback_features: list[dict[str, Any]] = []
    fallback_had_success = False
    fallback_errors: list[str] = []
    fallback_params = {**params, "outFields": "*"}
    for fallback_url in _NWI_FWS_SPLIT_QUERIES:
        try:
            features = _query_nwi_endpoint(
                session, fallback_url, fallback_params,
            )
        except Exception as exc:
            fallback_errors.append(str(exc)[:200])
            continue
        fallback_had_success = True
        fallback_features.extend(features)
    source_warning = (
        (
            "The primary NWI public service is back online but returned no "
            "data for this area and could not prove local coverage (the "
            "provider's dataset reload appears incomplete). PropDesk "
            "screened via an alternate FWS wetlands layer instead."
        )
        if primary_zero_unproven
        else (
            "The primary NWI public service is temporarily unavailable "
            "while the provider migration/outage is in progress. PropDesk "
            "kept that source first and used an alternate FWS wetlands "
            "layer as a fallback."
        )
    )
    if fallback_features:
        return _NwiQueryResult(
            features=fallback_features,
            source_warning=source_warning,
        )
    if fallback_had_success and not fallback_errors:
        # Zero features from a fallback layer is only trustworthy when the
        # layer demonstrably covers this area. Prove it: the same layers must
        # show mapped wetlands somewhere within the wide probe radius
        # (2026-07-15 — verified live: Wetlands_CONUS_West and Esri
        # USA_Wetlands both return real Texas features, so a proven-empty
        # screen means "no wetlands", not "no data").
        proven = _fallback_coverage_proven(session, lat, lng)
        if primary_zero_unproven and proven:
            # Primary zero + fallback zero + proven coverage: two
            # independent sources agree — a clean clear, no warning.
            return _NwiQueryResult(features=[], coverage_proven=True)
        return _NwiQueryResult(
            features=[],
            source_warning=source_warning,
            coverage_proven=proven,
        )
    try:
        esri_features = _query_nwi_endpoint(
            session, _NWI_ESRI_USA_WETLANDS_QUERY, fallback_params,
        )
        return _NwiQueryResult(
            features=esri_features,
            source_warning=source_warning,
            coverage_proven=(
                False if esri_features
                else _fallback_coverage_proven(session, lat, lng)
            ),
        )
    except Exception as exc:
        fallback_errors.append(str(exc)[:200])
    raise RuntimeError(
        "NWI primary query failed; fallbacks failed: "
        + "; ".join(dict.fromkeys(fallback_errors))[:400]
    ) from last_exc


def _fallback_coverage_proven(
    session: requests.Session,
    lat: float | None,
    lng: float | None,
    probe_urls: tuple[str, ...] | None = None,
) -> bool:
    """Return True when any fallback wetlands layer has mapped features
    within the wide coverage-probe envelope around the subject point.

    Cheapest possible check: returnCountOnly against each fallback layer,
    stopping at the first positive count. False (no proof) keeps the
    honest unknown/failure behavior — never fabricate a clear screen."""
    if lat is None or lng is None:
        return False
    settings = get_settings()
    probe_params = {
        "geometry": _build_envelope_param(
            lat, lng, settings.nwi_fallback_coverage_probe_radius_m,
        ),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true",
        "f": "json",
    }
    if probe_urls is None:
        probe_urls = (*_NWI_FWS_SPLIT_QUERIES, _NWI_ESRI_USA_WETLANDS_QUERY)
    for probe_url in probe_urls:
        try:
            resp = session.get(probe_url, params=probe_params, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                continue
            if int(data.get("count") or 0) > 0:
                return True
        except Exception as exc:
            log.debug("NWI probe endpoint %s failed: %s", probe_url, exc)
            continue
    return False


def _query_nwi_endpoint(
    session: requests.Session,
    query_url: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    resp = session.get(query_url, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    error = data.get("error")
    if error:
        raise requests.exceptions.HTTPError(str(error)[:200])
    return data.get("features", []) or []


def _summarize_features(
    features: list[dict[str, Any]],
) -> dict[str, Any]:
    summarized: list[dict[str, Any]] = []
    types_seen: list[str] = []

    def _pick(attrs: dict, *candidates: str):
        for c in candidates:
            v = attrs.get(c)
            if v is not None and v != "":
                return v
        for c in candidates:
            suffix = f".{c}"
            for key, v in attrs.items():
                if key.endswith(suffix) and v is not None and v != "":
                    return v
        return None

    for feat in features:
        attrs = feat.get("attributes") or {}
        wtype = _pick(
            attrs, "Wetlands.WETLAND_TYPE", "WETLAND_TYPE",
        ) or ""
        attribute = _pick(
            attrs, "Wetlands.ATTRIBUTE", "ATTRIBUTE",
        ) or ""
        acres = _pick(attrs, "Wetlands.ACRES", "ACRES")
        summarized.append({
            "wetland_type": wtype,
            "attribute": attribute,
            "acres": acres,
        })
        if wtype and wtype not in types_seen:
            types_seen.append(wtype)

    return {
        "status": "mixed" if features else "clear",
        "wetland_features": summarized,
        "wetland_types_seen": types_seen,
    }


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SOURCE_ACCESS_STATUS,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown",
            "wetland_features": [],
            "wetland_types_seen": [],
            "envelope_radius_m": None,
            "advisory": _ADVISORY_BY_STATUS["unknown"],
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=[
            ("lat/lng required for NWI envelope query; "
            "PropertyIdentity has none"),
        ],
    )


def _fetch_failed_result(
    *, radius_m: int, error_msg: str,
) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SOURCE_ACCESS_STATUS,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "unknown",
            "wetland_features": [],
            "wetland_types_seen": [],
            "envelope_radius_m": radius_m,
            "advisory": _ADVISORY_BY_STATUS["unknown"],
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=[f"NWI fetch failed: {error_msg}"[:500]],
    )


def _build_success_result(
    *,
    features: list[dict[str, Any]],
    radius_m: int,
    geometry_type: str,
    parcel_geometry: dict[str, Any] | None = None,
    source_warning: str | None = None,
    coverage_proven: bool = False,
) -> AdapterResult:
    summary = _summarize_features(features)
    status = summary["status"]
    # A zero-feature fallback answer stays honest-unknown UNLESS the same
    # fallback layer proved local coverage (mapped wetlands within the wide
    # probe radius) — then "clear" is a real screen, not missing data.
    fallback_clear_is_incomplete = (
        bool(source_warning) and status == "clear" and not coverage_proven
    )
    if fallback_clear_is_incomplete:
        status = "unknown"
    parcel_specific = geometry_type == "esriGeometryPolygon"
    wetlands_geometry, wetlands_acres = _parcel_wetland_geometry(
        features,
        parcel_geometry if parcel_specific else None,
    )
    if status == "mixed" and not parcel_specific:
        advisory = (
            "Mapped wetland indicators are present within the "
            f"{radius_m} m address search radius, not necessarily "
            "on the subject parcel. Add/verify the parcel ID or "
            "parcel boundary before treating this as parcel-specific; "
            "order a wetlands delineation or USACE JD if the mapped "
            "feature may affect the development area."
        )
    elif status == "clear" and source_warning and coverage_proven:
        advisory = (
            "No mapped wetland indicator found on the screened area. The "
            "fallback NWI layer demonstrably covers this vicinity (mapped "
            "wetlands exist within the wider probe radius), so this is a "
            "meaningful screen; delineation not performed and a wetlands "
            "delineation or USACE JD still controls."
        )
    else:
        advisory = _ADVISORY_BY_STATUS[status]
    if source_warning:
        advisory = f"{source_warning} {advisory}"
    errors = None
    if fallback_clear_is_incomplete:
        errors = [
            (
                "NWI primary service is temporarily unavailable; the fallback "
                "returned no mapped indicators, so PropDesk cannot confirm a "
                "clear wetlands screen. Refresh this source later."
            ),
        ]

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SOURCE_ACCESS_STATUS,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=(
            CoverageStatus.PARTIAL if fallback_clear_is_incomplete
            else CoverageStatus.COVERED
        ),
        normalized_facts={
            "result_schema_version": 2,
            "status": status,
            "wetland_features": summary["wetland_features"],
            "wetland_types_seen": summary["wetland_types_seen"],
            "envelope_radius_m": radius_m,
            "geometry_type": (
                "parcel_polygon" if parcel_specific else "radius_envelope"
            ),
            "parcel_specific": parcel_specific,
            "wetlands_map_geometry": wetlands_geometry,
            "wetlands_intersection_acres": wetlands_acres,
            "source_warning": source_warning,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors,
    )


def _feature_shape(feature: dict[str, Any]):
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return None
    try:
        if geometry.get("type"):
            return shape(geometry)
        rings = geometry.get("rings")
        if isinstance(rings, list) and rings:
            return Polygon(rings[0], rings[1:])
    except Exception:
        return None
    return None


def _parcel_wetland_geometry(
    features: list[dict[str, Any]],
    parcel_geometry: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, float | None]:
    if not isinstance(parcel_geometry, dict):
        return None, None
    try:
        parcel = shape(parcel_geometry)
        if not parcel.is_valid:
            parcel = parcel.buffer(0)
    except Exception:
        return None, None
    clipped = []
    for feature in features:
        wetland = _feature_shape(feature)
        if wetland is None:
            continue
        intersection = wetland.intersection(parcel)
        if not intersection.is_empty:
            clipped.append(intersection)
    if not clipped:
        return None, 0.0
    combined = unary_union(clipped)
    area_sqm, _ = _GEOD.geometry_area_perimeter(combined)
    return mapping(combined), round(abs(area_sqm) / _SQM_PER_ACRE, 3)


__all__ = ["SOURCE_ACCESS_STATUS", "SOURCE_ID", "fetch"]
