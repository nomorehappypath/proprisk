"""wcad_parcel adapter — Williamson CAD parcel polygons.

Williamson County publishes a public ArcGIS parcel layer with
PIN, acreage, alternate PIN, and polygon geometry. Exact parcel
resolution is safe only when the geocoded point intersects one
parcel or the user supplied a known parcel ID. Intersection-style
addresses often land in road ROW and return several nearby
candidates; in that case this adapter surfaces candidates but
does NOT pick one.
"""

from __future__ import annotations

import math
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
)

SOURCE_ID = "wcad_parcel"

_WCAD_PARCELS_QUERY = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/"
    "services/EXTERNAL_wcad_parcel/FeatureServer/0/query"
)
_WCAD_SEARCH_URL = "https://search.wcad.org/"
_TIMEOUT = 25
_NEARBY_RADIUS_M = 200
_NEIGHBOR_CONTEXT_RADIUS_M = 500
_MAX_NEIGHBOR_PARCELS = 30


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    county = (identity.county or "").lower().strip()
    if county != "williamson":
        return _not_williamson_result(county)

    sess = http_session(session)

    try:
        if identity.parcel_id:
            parcels = _query_by_pin(sess, identity.parcel_id)
            if parcels:
                parcel = parcels[0]
                return _exact_result(
                    parcel,
                    status="parcel_found_from_supplied_id",
                    identifier_used="parcel_id",
                    neighboring_features=_neighboring_features(sess, parcel),
                )
            return _not_found_result(
                note=(
                    f"WCAD did not return parcel ID {identity.parcel_id}. "
                    "Verify the PIN in Williamson CAD."
                ),
                identifier_used="parcel_id",
            )

        if identity.lat is None or identity.lng is None:
            return _no_coordinates_result()

        point_matches = _query_point(sess, identity.lat, identity.lng)
        if len(point_matches) == 1:
            return _exact_result(
                point_matches[0],
                status="parcel_found_at_geocoded_point",
                identifier_used="lat_lng",
                neighboring_features=(
                    _neighboring_features(sess, point_matches[0])
                ),
            )
        if len(point_matches) > 1:
            return _candidate_result(
                point_matches,
                identity.lat,
                identity.lng,
                status="multiple_parcels_at_geocoded_point",
                note=(
                    "WCAD returned multiple parcels at the geocoded "
                    "point. Select the correct parcel ID before using "
                    "parcel-specific conclusions."
                ),
            )

        nearby = _query_nearby(
            sess, identity.lat, identity.lng, radius_m=_NEARBY_RADIUS_M,
        )
        if nearby:
            return _candidate_result(
                nearby,
                identity.lat,
                identity.lng,
                status="nearby_candidates_only",
                note=(
                    "No WCAD parcel contains the geocoded point. This "
                    "often happens for intersection addresses or road "
                    "frontage points. Nearby parcel candidates are shown, "
                    "but PropDesk did not choose one automatically."
                ),
            )

        return _not_found_result(
            note=(
                "No WCAD parcel matched the geocoded point or the nearby "
                "candidate search. Search Williamson CAD by address or "
                "legal description and add the parcel ID to the project."
            ),
            identifier_used="lat_lng",
        )
    except Exception as exc:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=(
                SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
            ),
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.PARTIAL,
            normalized_facts={
                "county": "williamson",
                "status": "fetch_failed",
                "manual_lookup_url": _WCAD_SEARCH_URL,
                "note": (
                    "Williamson CAD parcel lookup failed. Retry, or "
                    "search WCAD directly by address / legal description."
                ),
            },
            errors=[f"WCAD parcel lookup failed: {exc}"[:500]],
            fetched_at=datetime.now(UTC),
            identifier_used="lat_lng",
        )


def _query_by_pin(
    session: requests.Session,
    pin: str,
) -> list[dict[str, Any]]:
    safe_pin = pin.replace("'", "''").strip().upper()
    return _query(session, {
        "where": f"PIN = '{safe_pin}'",
        "outFields": "PIN,ACREAGE,ALTPIN",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    })


def _query_point(
    session: requests.Session,
    lat: float,
    lng: float,
) -> list[dict[str, Any]]:
    return _query(session, {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "PIN,ACREAGE,ALTPIN",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    })


def _query_nearby(
    session: requests.Session,
    lat: float,
    lng: float,
    *,
    radius_m: int,
    result_record_count: int = 20,
) -> list[dict[str, Any]]:
    deg_lat = radius_m / 111_320.0
    deg_lng = radius_m / (
        111_320.0 * max(math.cos(math.radians(lat)), 0.01)
    )
    envelope = (
        f"{lng - deg_lng},{lat - deg_lat},"
        f"{lng + deg_lng},{lat + deg_lat}"
    )
    return _query(session, {
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "PIN,ACREAGE,ALTPIN",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": str(result_record_count),
        "f": "json",
    })


def _query(
    session: requests.Session,
    params: dict[str, str],
) -> list[dict[str, Any]]:
    resp = session.get(_WCAD_PARCELS_QUERY, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", data["error"]))
    return data.get("features", []) or []


def _exact_result(
    feature: dict[str, Any],
    *,
    status: str,
    identifier_used: str,
    neighboring_features: list[dict[str, Any]] | None = None,
) -> AdapterResult:
    parcel = _feature_to_parcel(feature)
    parcel_id = parcel.get("parcel_id")
    acreage = parcel.get("acreage")
    note = (
        f"WCAD parcel {parcel_id} resolved from the public parcel "
        "polygon layer. Parcel-specific screens can use this boundary."
    )
    subject_centroid = _centroid(parcel.get("parcel_polygon"))
    neighboring_parcels = []
    for neighbor_feature in neighboring_features or []:
        neighbor = _feature_to_parcel(neighbor_feature)
        if not neighbor.get("parcel_id") or neighbor.get("parcel_id") == parcel_id:
            continue
        centroid = _centroid(neighbor.get("parcel_polygon"))
        distance_ft = None
        if subject_centroid and centroid:
            distance_ft = _haversine_ft(
                subject_centroid[1], subject_centroid[0], centroid[1], centroid[0],
            )
        neighboring_parcels.append({
            **neighbor,
            "distance_from_subject_centroid_ft": (
                round(distance_ft) if distance_ft is not None else None
            ),
        })
    neighboring_parcels.sort(key=lambda row: (
        row.get("distance_from_subject_centroid_ft") or 999_999,
        row.get("parcel_id") or "",
    ))
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "county": "williamson",
            "status": status,
            "parcel_id": parcel_id,
            "cad_account": parcel.get("cad_account"),
            "acreage": acreage,
            "acres": acreage,
            "parcel_polygon": parcel.get("parcel_polygon"),
            "neighboring_parcels": neighboring_parcels[:_MAX_NEIGHBOR_PARCELS],
            "neighboring_parcel_count": min(
                len(neighboring_parcels), _MAX_NEIGHBOR_PARCELS,
            ),
            "manual_lookup_url": _WCAD_SEARCH_URL,
            "note": note,
        },
        fetched_at=datetime.now(UTC),
        identifier_used=identifier_used,
    )


def _neighboring_features(
    session: requests.Session,
    subject_feature: dict[str, Any],
) -> list[dict[str, Any]]:
    """Best-effort neighbor context must never downgrade the subject parcel."""
    parcel = _feature_to_parcel(subject_feature)
    centroid = _centroid(parcel.get("parcel_polygon"))
    if centroid is None:
        return []
    try:
        return _query_nearby(
            session,
            centroid[1],
            centroid[0],
            radius_m=_NEIGHBOR_CONTEXT_RADIUS_M,
            result_record_count=_MAX_NEIGHBOR_PARCELS + 1,
        )
    except Exception:
        return []


def _candidate_result(
    features: list[dict[str, Any]],
    lat: float,
    lng: float,
    *,
    status: str,
    note: str,
) -> AdapterResult:
    candidates = [
        _feature_to_candidate(f, lat, lng)
        for f in features
    ]
    candidates = [
        c for c in candidates
        if c.get("parcel_id")
    ]
    candidates.sort(key=lambda c: c.get("distance_ft") or 999_999)

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "county": "williamson",
            "status": status,
            "parcel_id": None,
            "nearby_candidates": candidates[:10],
            "candidate_count": len(candidates),
            "candidate_search_radius_m": _NEARBY_RADIUS_M,
            "manual_lookup_url": _WCAD_SEARCH_URL,
            "note": note,
        },
        manual_upload_request=ManualUploadRequest(
            document_types=["tax_assessment", "deed", "survey"],
            reason=(
                "Select the correct Williamson CAD parcel ID from "
                "the nearby candidates or from WCAD search before "
                "treating flood, wetlands, or entitlement checks as "
                "parcel-specific."
            ),
        ),
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
    )


def _not_found_result(
    *,
    note: str,
    identifier_used: str,
) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "county": "williamson",
            "status": "no_parcel_match",
            "parcel_id": None,
            "manual_lookup_url": _WCAD_SEARCH_URL,
            "note": note,
        },
        manual_upload_request=ManualUploadRequest(
            document_types=["tax_assessment", "deed", "survey"],
            reason=(
                "Search Williamson CAD and add the parcel ID to "
                "anchor parcel-specific checks."
            ),
        ),
        fetched_at=datetime.now(UTC),
        identifier_used=identifier_used,
    )


def _no_coordinates_result() -> AdapterResult:
    return _not_found_result(
        note=(
            "Latitude/longitude are required for WCAD point lookup. "
            "Retry geocoding or search WCAD manually by address."
        ),
        identifier_used="address",
    )


def _not_williamson_result(county: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.JURISDICTION_NOT_COVERED,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "county": county or None,
            "status": "jurisdiction_not_covered",
            "note": "WCAD parcel lookup applies only in Williamson County.",
        },
        fetched_at=datetime.now(UTC),
        identifier_used="county",
    )


def _feature_to_parcel(feature: dict[str, Any]) -> dict[str, Any]:
    attrs = feature.get("attributes") or {}
    geometry = feature.get("geometry") or {}
    pin = attrs.get("PIN")
    acreage = attrs.get("ACREAGE")
    altpin = attrs.get("ALTPIN")
    return {
        "parcel_id": str(pin).strip().upper() if pin else None,
        "acreage": float(acreage) if isinstance(acreage, (int, float)) else None,
        "cad_account": str(int(altpin)) if isinstance(altpin, (int, float)) else None,
        "parcel_polygon": _arcgis_polygon_to_geojson(geometry),
    }


def _feature_to_candidate(
    feature: dict[str, Any],
    lat: float,
    lng: float,
) -> dict[str, Any]:
    parcel = _feature_to_parcel(feature)
    centroid = _centroid(parcel.get("parcel_polygon"))
    dist_ft = (
        _haversine_ft(lat, lng, centroid[1], centroid[0])
        if centroid
        else None
    )
    return {
        "parcel_id": parcel.get("parcel_id"),
        "acreage": parcel.get("acreage"),
        "cad_account": parcel.get("cad_account"),
        "distance_ft": round(dist_ft) if dist_ft is not None else None,
    }


def _arcgis_polygon_to_geojson(
    geometry: dict[str, Any],
) -> dict[str, Any] | None:
    rings = geometry.get("rings")
    if not rings:
        return None
    return {
        "type": "Polygon",
        "coordinates": rings,
    }


def _centroid(
    polygon: dict[str, Any] | None,
) -> tuple[float, float] | None:
    if not polygon:
        return None
    rings = polygon.get("coordinates") or []
    if not rings:
        return None
    pts = [
        p for p in rings[0]
        if isinstance(p, list) and len(p) >= 2
    ]
    if not pts:
        return None
    lng = sum(float(p[0]) for p in pts) / len(pts)
    lat = sum(float(p[1]) for p in pts) / len(pts)
    return lng, lat


def _haversine_ft(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
) -> float:
    radius_ft = 20_902_231
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return 2 * radius_ft * math.asin(min(1.0, math.sqrt(a)))


__all__ = ["SOURCE_ID", "fetch"]
