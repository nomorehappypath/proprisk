"""schools_intel adapter — TEA Schools layer.

Ported verbatim from legacy_services/core/public_intel_adapters/
schools_intel.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field
added.

Single-call query against TEA's annual statewide schools
ArcGIS layer — full TX coverage, no per-county plumbing.
Returns schools within ~5mi sorted by haversine distance,
grouped by elementary / middle / high band.

Confidence is always SCREENED_INDICATOR — proximity is signal,
not assignment (assignment is by ISD attendance zone, not
distance). A-F rating per school requires manual lookup at
txschools.gov.

URL note: TEA bumps the layer year annually (Schools_2024_to_2025
this year, Schools_2025_to_2026 next August). Variance harness
catches the URL bump.
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

log = logging.getLogger("propdesk.site_check.schools_intel")

SOURCE_ID = "schools_intel"

_TEA_SCHOOLS_QUERY = (
    "https://services2.arcgis.com/5MVN2jsqIrNZD4tP/arcgis/rest/"
    "services/Schools_2024_to_2025/FeatureServer/0/query"
)
_TEA_REPORT_CARD_URL = "https://txschools.gov/"

_TIMEOUT = 25
_DEFAULT_BUFFER_MI = 5.0
_MAX_SCHOOLS_RETURNED = 10

_SCHOOLS_UPLOAD_TYPES = [
    "TEA Texas Schools Report Card excerpt",
    "ISD address-lookup screenshot (assigned schools)",
    "ISD attendance zone map",
]


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    buffer_mi: float = _DEFAULT_BUFFER_MI,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    sess = http_session(session)

    features, error = _query_layer(
        sess, _TEA_SCHOOLS_QUERY,
        identity.lat, identity.lng, buffer_mi,
    )
    if error:
        return _fetch_failed_result(error)

    schools = _summarize_schools(
        features, identity.lat, identity.lng,
    )
    by_band = _group_by_grade_band(schools)
    advisory = _build_advisory(schools, by_band, buffer_mi)

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "clear" if schools else "no_schools_in_buffer",
            "search_radius_mi": buffer_mi,
            "schools_count": len(schools),
            "schools": schools[:_MAX_SCHOOLS_RETURNED],
            "nearest_by_band": by_band,
            "report_card_lookup_url": _TEA_REPORT_CARD_URL,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_SCHOOLS_UPLOAD_TYPES),
            reason=(
                "School proximity is screening only — "
                "assignment is by ISD attendance zone, not "
                "by distance. Look up assigned schools at the "
                "ISD's address-lookup tool, and A-F ratings "
                f"at {_TEA_REPORT_CARD_URL}."
            ),
        ),
    )


def _query_layer(
    session: requests.Session, url: str,
    lat: float, lng: float, buffer_mi: float,
) -> tuple[list[dict[str, Any]], str | None]:
    buffer_m = buffer_mi * 1609.34
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "distance": buffer_m,
        "units": "esriSRUnit_Meter",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "USER_School_Name,USER_District_Name,"
            "USER_Grade_Range,USER_School_Street_Address,"
            "USER_School_City,USER_School_Phone,"
            "USER_School_Enrollment_as_of_Oc,"
            "USER_District_Number,USER_School_Number,"
            "USER_NCES_School_ID,USER_School_Web_Page_Address"
        ),
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    try:
        resp = session.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return [], (
                f"non-JSON content-type ({ct[:40]}) — "
                "TEA endpoint may have moved (annual URL "
                "bump at start of fall)"
            )
        data = resp.json()
        return data.get("features", []) or [], None
    except Exception as exc:
        log.warning("TEA schools query failed: %s", exc)
        return [], str(exc)[:200]


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


def _classify_band(grade_range: str) -> str:
    if not grade_range:
        return "unknown"
    cleaned = (
        grade_range.replace("'", "").replace(" ", "").upper()
    )
    if "-" not in cleaned:
        return "unknown"
    low_str, high_str = cleaned.split("-", 1)
    low = _grade_to_int(low_str)
    high = _grade_to_int(high_str)
    if low is None or high is None:
        return "unknown"
    if high <= 5:
        return "elementary"
    if low >= 6 and high <= 8:
        return "middle"
    if low >= 9:
        return "high"
    if low <= 5 and high >= 9:
        return "mixed"
    return "mixed"


def _grade_to_int(s: str) -> int | None:
    if s == "PK":
        return -1
    if s in ("KG", "K"):
        return 0
    try:
        return int(s)
    except ValueError:
        return None


def _summarize_schools(
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
        grade_range = attrs.get("USER_Grade_Range") or ""
        band = _classify_band(grade_range)
        if isinstance(grade_range, str):
            grade_range = grade_range.lstrip("'").strip()
        enrollment = attrs.get("USER_School_Enrollment_as_of_Oc")
        if isinstance(enrollment, (int, float)) and enrollment < 0:
            enrollment = None
        out.append({
            "name": attrs.get("USER_School_Name") or "Unnamed",
            "district": attrs.get("USER_District_Name") or "",
            "grade_range": grade_range,
            "band": band,
            "address": attrs.get("USER_School_Street_Address") or "",
            "city": attrs.get("USER_School_City") or "",
            "phone": attrs.get("USER_School_Phone") or "",
            "enrollment": enrollment,
            "school_number": attrs.get("USER_School_Number") or "",
            "website": attrs.get("USER_School_Web_Page_Address") or "",
            "distance_mi": round(dist, 2),
            "lat": slat, "lng": slng,
        })
    out.sort(key=lambda s: s["distance_mi"])
    return out


def _group_by_grade_band(
    schools: list[dict[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    out: dict[str, dict[str, Any] | None] = {
        "elementary": None,
        "middle": None,
        "high": None,
    }
    for s in schools:
        b = s.get("band")
        if b in out and out[b] is None:
            out[b] = s
        if b == "mixed":
            for k, v in out.items():
                if v is None:
                    out[k] = s
        if all(out.values()):
            break
    return out


def _build_advisory(
    schools: list[dict[str, Any]],
    by_band: dict[str, dict[str, Any] | None],
    buffer_mi: float,
) -> str:
    if not schools:
        return (
            f"No schools within {buffer_mi:.0f} mi of the "
            "parcel in TEA's published layer. Verify with "
            "the serving ISD's attendance-zone tool."
        )
    parts: list[str] = [
        f"{len(schools)} schools within {buffer_mi:.0f} mi."
    ]
    for label, key in (
        ("Elementary", "elementary"),
        ("Middle", "middle"),
        ("High", "high"),
    ):
        s = by_band.get(key)
        if s:
            parts.append(
                f"Nearest {label.lower()}: {s['name']} "
                f"({s['district']}) — {s['distance_mi']} mi"
            )
    parts.append(
        "Proximity is screening — actual school assignment "
        "is by ISD attendance zone, NOT distance. Confirm "
        "with the ISD's address-lookup tool. A-F rating "
        "per school: txschools.gov."
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
            "schools": [], "schools_count": 0,
            "nearest_by_band": {
                "elementary": None, "middle": None, "high": None,
            },
            "report_card_lookup_url": _TEA_REPORT_CARD_URL,
            "advisory": (
                "Coordinates required for school-proximity "
                "lookup. Use the ISD address-lookup tool "
                "directly."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for schools query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_SCHOOLS_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable. Use the ISD's "
                "address-lookup tool to find assigned "
                "schools, then upload the result."
            ),
        ),
    )


def _fetch_failed_result(error_msg: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "unknown",
            "schools": [], "schools_count": 0,
            "nearest_by_band": {
                "elementary": None, "middle": None, "high": None,
            },
            "report_card_lookup_url": _TEA_REPORT_CARD_URL,
            "advisory": (
                "TEA schools layer did not respond. Look up "
                f"schools manually at {_TEA_REPORT_CARD_URL}."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=[f"TEA schools query failed: {error_msg}"[:500]],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_SCHOOLS_UPLOAD_TYPES),
            reason=(
                "TEA schools layer did not respond. Look up "
                f"schools at {_TEA_REPORT_CARD_URL} and "
                "upload report-card excerpts for assigned "
                "schools."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
