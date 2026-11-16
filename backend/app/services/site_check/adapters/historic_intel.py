"""historic_intel adapter — Austin LHD + landmarks; THC Atlas pointer elsewhere.

Ported verbatim from legacy_services/core/public_intel_adapters/
historic_intel.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field
added.

Travis only — Austin's Local Historic District + landmark
layers. Other counties get HTML_OR_PDF_ONLY pointer to THC
Atlas + city planning portal.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger("propdesk.site_check.historic_intel")

SOURCE_ID = "historic_intel"

_AUSTIN_LHD_QUERY = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/arcgis/rest/"
    "services/LHD_Boundaries/FeatureServer/0/query"
)
_AUSTIN_LANDMARKS_QUERY = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/arcgis/rest/"
    "services/STRUCTURE_historical_landmarks/FeatureServer/0/query"
)
_THC_ATLAS_URL = "https://atlas.thc.texas.gov/"

_TIMEOUT = 25
_LHD_BUFFER_M = 200
_LANDMARK_BUFFER_MI = 0.25

_AUSTIN_LHD_COUNTIES = {"travis"}

_HISTORIC_UPLOAD_TYPES = [
    "historic landmark verification letter",
    "Historic Landmark Commission case file",
    "NRHP / RTHL designation document",
    "design-standards PDF",
]


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    county = (identity.county or "").lower().strip()
    sess = http_session(session)

    if county not in _AUSTIN_LHD_COUNTIES:
        return _portal_only_result(county)

    lhd_features, lhd_error = _query_layer(
        sess, _AUSTIN_LHD_QUERY,
        identity.lat, identity.lng, _LHD_BUFFER_M,
    )
    landmark_features, lm_error = _query_layer(
        sess, _AUSTIN_LANDMARKS_QUERY,
        identity.lat, identity.lng,
        _LANDMARK_BUFFER_MI * 1609.34,
    )

    in_district = _summarize_districts(lhd_features)
    nearby_landmarks = _summarize_landmarks(
        landmark_features, identity.lat, identity.lng,
    )
    is_in_lhd = bool(in_district)
    advisory = _build_advisory(
        in_district, nearby_landmarks, is_in_lhd,
    )

    errors: list[str] = []
    if lhd_error:
        errors.append(f"Austin LHD query: {lhd_error}")
    if lm_error:
        errors.append(f"Austin landmarks query: {lm_error}")

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": (
                "in_historic_district"
                if is_in_lhd
                else (
                    "near_landmark"
                    if nearby_landmarks else "clear"
                )
            ),
            "is_in_local_historic_district": is_in_lhd,
            "districts": in_district,
            "nearby_landmarks_count": len(nearby_landmarks),
            "nearby_landmarks": nearby_landmarks[:5],
            "thc_atlas_url": _THC_ATLAS_URL,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors if errors else None,
        manual_upload_request=ManualUploadRequest(
            document_types=list(_HISTORIC_UPLOAD_TYPES),
            reason=(
                "Local Historic District / landmark "
                "membership flags the constraint. The "
                "binding answers — exact design standards, "
                "Historic Landmark Commission case "
                "history, NRHP / RTHL listing details — "
                f"live in city case files + THC Atlas at "
                f"{_THC_ATLAS_URL}."
            ),
        ),
    )


def _query_layer(
    session: requests.Session, url: str,
    lat: float, lng: float, buffer_m: float,
) -> tuple[list[dict[str, Any]], str | None]:
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "distance": buffer_m,
        "units": "esriSRUnit_Meter",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    try:
        resp = session.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return [], f"non-JSON content-type ({ct[:40]})"
        return resp.json().get("features", []) or [], None
    except Exception as exc:
        log.warning("Historic layer query failed: %s", exc)
        return [], str(exc)[:200]


def _summarize_districts(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        nm = attrs.get("LHD_Name") or attrs.get("DISTRICT_NAME")
        if not nm:
            continue
        out.append({
            "name": nm,
            "case_number": attrs.get("Case_Num") or "",
            "ordinance_number": attrs.get("Ord_Num") or "",
            "ordinance_url": attrs.get("Ord_Link") or "",
            "design_standards_url": attrs.get("Des_Stds") or "",
        })
    return out


def _haversine_mi(
    lat1: float, lng1: float, lat2: float, lng2: float,
) -> float:
    R_MI = 3958.7613
    rad1, rad2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rad1) * math.cos(rad2)
        * math.sin(dlng / 2) ** 2
    )
    return 2 * R_MI * math.asin(min(1.0, math.sqrt(a)))


def _summarize_landmarks(
    features: list[dict[str, Any]],
    parcel_lat: float, parcel_lng: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}
        slat = geom.get("y")
        slng = geom.get("x")
        if slat is None or slng is None:
            continue
        try:
            dist = _haversine_mi(
                parcel_lat, parcel_lng, slat, slng,
            )
        except (TypeError, ValueError):
            continue
        out.append({
            "name": attrs.get("BUILDING_NAME") or "Unnamed Landmark",
            "address": attrs.get("ADDRESS") or "",
            "case_number": attrs.get("CASE_NUMBER") or "",
            "landmark_id": (
                attrs.get("HISTORIC_LANDMARK_ID")
                or attrs.get("LANDMARK_ID")
            ),
            "distance_mi": round(dist, 2),
        })
    out.sort(key=lambda lm: lm["distance_mi"])
    return out


def _build_advisory(
    districts: list[dict[str, Any]],
    landmarks: list[dict[str, Any]],
    is_in_district: bool,
) -> str:
    if is_in_district:
        names = ", ".join(d["name"] for d in districts[:3])
        return (
            f"Parcel falls inside Local Historic District "
            f"boundary: {names}. Every exterior modification "
            "(facade, roofline, windows, paint, fence) "
            "requires Historic Landmark Commission review — "
            "adds 6-12 months to permit timelines + material "
            "restrictions per the district's design standards."
        )
    if landmarks:
        nearest = landmarks[0]
        return (
            f"Parcel is not inside a local historic district, "
            f"but {len(landmarks)} historic landmark(s) sit "
            f"within 0.25 mi (nearest: {nearest['name']} at "
            f"{nearest['address']}, {nearest['distance_mi']} "
            "mi). Adjacent landmarks can trigger compatibility "
            "review for new construction."
        )
    return (
        "No Local Historic District or individual landmark "
        "within Austin's published boundaries near the parcel. "
        f"Confirm with THC Atlas ({_THC_ATLAS_URL}) for any "
        "NRHP / RTHL / archaeological-site designation not "
        "in the city's local layer."
    )


def _portal_only_result(county: str) -> AdapterResult:
    label = county or "this property's county"
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.HTML_OR_PDF_ONLY,
        data_confidence_status=DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "manual_verification_required",
            "is_in_local_historic_district": False,
            "districts": [],
            "nearby_landmarks_count": 0,
            "nearby_landmarks": [],
            "thc_atlas_url": _THC_ATLAS_URL,
            "advisory": (
                f"Local-historic-district mapping for "
                f"{label.title()} County isn't wired as a "
                f"queryable layer. Search the Texas "
                f"Historical Commission Atlas at "
                f"{_THC_ATLAS_URL} (search by address) and "
                "the city's planning department for any "
                "local landmark or district designation."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_HISTORIC_UPLOAD_TYPES),
            reason=(
                f"County not wired. Use THC Atlas at "
                f"{_THC_ATLAS_URL} + city planning portal."
            ),
        ),
    )


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown",
            "is_in_local_historic_district": False,
            "districts": [],
            "nearby_landmarks_count": 0,
            "nearby_landmarks": [],
            "thc_atlas_url": _THC_ATLAS_URL,
            "advisory": (
                "Coordinates required for historic overlay "
                "lookup."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for historic query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_HISTORIC_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable. Search THC Atlas "
                "manually."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
