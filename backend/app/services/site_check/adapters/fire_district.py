"""fire_district adapter — fire-station proximity + manual flow uploads.

Combined port of legacy_services/core/public_intel_adapters/
fire_intel.py + fire_district.py (May 2026). Registry maps
this source_id to both files because the legacy_services side
splits station lookup (fire_intel) from the manual fire-flow
upload prompt (fire_district); reference_backend folds them
into one adapter under the registry's existing source_id.

Travis / Williamson (via Round Rock layer) / Bastrop have
queryable station layers. Other counties get the manual ISO
+ fire-flow upload prompt only.

Confidence always SCREENED_INDICATOR — distance is straight-
line not driving; ISO rating + flow capacity remain manual.
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

log = logging.getLogger("propdesk.site_check.fire_district")

SOURCE_ID = "fire_district"

_TRAVIS_FIRE_STATIONS_QUERY = (
    "https://gis.traviscountytx.gov/server1/rest/services/"
    "Services_and_Facilities/Emergency_Services_Fire_EMS_Stations/"
    "MapServer/0/query"
)
_TRAVIS_ESD_SERVICE_AREA_QUERY = (
    "https://services1.arcgis.com/HGcSYZ5bvjRswoCb/arcgis/rest/services/"
    "Emergency_Service_Districts_agol/FeatureServer/1/query"
)
_TRAVIS_ESD_TAXMAPS_QUERY = (
    "https://taxmaps.traviscountytx.gov/arcgis/rest/services/"
    "ESD/FeatureServer/0/query"
)
_ROUNDROCK_FIRE_STATIONS_QUERY = (
    "https://maps.roundrocktexas.gov/arcgis/rest/services/"
    "Fire/POLICE_and_FIRE/MapServer/0/query"
)
_BASTROP_FIRE_STATIONS_QUERY = (
    "https://services3.arcgis.com/wdTkTU0MdZbNBEZy/arcgis/rest/"
    "services/FireStation/FeatureServer/0/query"
)

_LAYERS_BY_COUNTY: dict[str, str] = {
    "travis":     _TRAVIS_FIRE_STATIONS_QUERY,
    "williamson": _ROUNDROCK_FIRE_STATIONS_QUERY,
    "bastrop":    _BASTROP_FIRE_STATIONS_QUERY,
}

_TIMEOUT = 25
_DEFAULT_BUFFER_MI = 5.0
_MAX_STATIONS_RETURNED = 5

_ISO_LOOKUP_URL = "https://www.isomitigation.com/ppc/lookup/"
_ISO_UPLOAD_TYPES = [
    "ISO Fire Suppression Rating letter",
    "fire-flow test report",
    "fire marshal letter",
    "fire department capacity confirmation",
]

_NAME_FIELDS = ("NAME", "STATION_NAME", "name", "full_name")
_ADDRESS_FIELDS = ("ADDRESS", "PHYS_ADDR", "full_addr", "addr")
_DEPT_FIELDS = (
    "DEPARTMENT", "DEPT", "JURISDICTI", "AGENCY",
    "msag_com", "municipal",
)
_STATION_NUM_FIELDS = (
    "STATION_NU", "STATION_NUMBER", "STATIONNO", "STATION_ID",
)


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    buffer_mi: float = _DEFAULT_BUFFER_MI,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    county = (identity.county or "").lower().strip()
    layer_url = _LAYERS_BY_COUNTY.get(county)
    if not layer_url:
        return _jurisdiction_not_covered_result(county)

    sess = http_session(session)

    features, error = _query_layer(
        sess, layer_url, identity.lat, identity.lng, buffer_mi,
    )
    if error:
        if county == "travis":
            fallback = _travis_service_area_fallback(
                sess, identity.lat, identity.lng, error,
            )
            if fallback is not None:
                return fallback
        return _fetch_failed_result(county, error)

    stations = _summarize_stations(
        features, identity.lat, identity.lng,
    )
    nearest = stations[0] if stations else None
    advisory = _build_advisory(stations, county, buffer_mi)

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "clear" if stations else "no_stations_in_buffer",
            "county": county,
            "search_radius_mi": buffer_mi,
            "stations_count": len(stations),
            "nearest_station": nearest,
            "stations": stations[:_MAX_STATIONS_RETURNED],
            "iso_rating_lookup_url": _ISO_LOOKUP_URL,
            "fire_flow_status": "manual_verification_required",
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ISO_UPLOAD_TYPES),
            reason=(
                "Distance to the nearest fire station screens "
                "for response-time risk; the binding answers "
                "(ISO Fire Suppression Rating, fire-flow "
                "capacity at the nearest hydrant, written "
                "response-time commitments) require manual "
                "verification. Look up the ISO rating at "
                f"{_ISO_LOOKUP_URL} (search by community / "
                "fire department), then upload the rating "
                "letter + a fire-flow test report from the "
                "serving department."
            ),
        ),
    )


def _travis_service_area_fallback(
    session: requests.Session,
    lat: float,
    lng: float,
    station_error: str,
) -> AdapterResult | None:
    errors: list[str] = []
    for layer_url in (
        _TRAVIS_ESD_SERVICE_AREA_QUERY,
        _TRAVIS_ESD_TAXMAPS_QUERY,
    ):
        features, error = _query_service_area_layer(session, layer_url, lat, lng)
        if error is not None:
            errors.append(error)
            continue
        service_areas = _summarize_service_areas(features)
        if not service_areas:
            continue
        primary = service_areas[0]
        advisory = _build_service_area_advisory(primary)
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
            coverage_status=CoverageStatus.PARTIAL,
            normalized_facts={
                "status": "service_area_only",
                "county": "travis",
                "stations": [],
                "stations_count": 0,
                "nearest_station": None,
                "service_area": primary,
                "service_areas": service_areas,
                "station_lookup_status": "temporarily_unavailable",
                "station_lookup_error": (
                    "Travis County's fire-station public layer is "
                    "temporarily unavailable."
                ),
                "source_warning": (
                    "PropDesk kept the original Travis County fire-station "
                    "lookup first. That public layer is temporarily down, so "
                    "the card uses the official Travis fire/ESD service-area "
                    "fallback and leaves station distance open."
                ),
                "search_radius_mi": _DEFAULT_BUFFER_MI,
                "iso_rating_lookup_url": _ISO_LOOKUP_URL,
                "fire_flow_status": "manual_verification_required",
                "advisory": advisory,
                "contradiction_flags": [],
            },
            identifier_used="lat_lng",
            fetched_at=datetime.now(UTC),
            advisories=[advisory],
            errors=[
                (
                    "Fire-station lookup is temporarily unavailable; PropDesk "
                    "used the official Travis fire/ESD service-area fallback "
                    "and left station distance open."
                ),
            ],
            manual_upload_request=ManualUploadRequest(
                document_types=list(_ISO_UPLOAD_TYPES),
                reason=(
                    "Serving fire/ESD area was identified from an official "
                    "Travis County fallback layer. Fire-station distance, ISO "
                    "rating, and hydrant fire-flow capacity still require "
                    "department confirmation."
                ),
            ),
        )
    log.warning(
        "travis fire service-area fallbacks failed after station error=%s "
        "fallback_errors=%s",
        station_error,
        errors,
    )
    return None


def _query_service_area_layer(
    session: requests.Session,
    layer_url: str,
    lat: float,
    lng: float,
) -> tuple[list[dict[str, Any]], str | None]:
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
        "where": "1=1",
    }
    try:
        resp = session.get(layer_url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return [], (
                f"non-JSON content-type ({ct[:40]}) — "
                "endpoint may have moved"
            )
        data = resp.json()
        error = data.get("error")
        if error:
            return [], str(error)[:200]
        return data.get("features", []) or [], None
    except Exception as exc:
        log.warning(
            "travis fire service-area fallback query failed (%s): %s",
            layer_url,
            exc,
        )
        return [], str(exc)[:200]


def _query_layer(
    session: requests.Session, layer_url: str,
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
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    try:
        resp = session.get(layer_url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return [], (
                f"non-JSON content-type ({ct[:40]}) — "
                "endpoint may have moved"
            )
        data = resp.json()
        return data.get("features", []) or [], None
    except Exception as exc:
        log.warning(
            "fire-station layer query failed (%s): %s", layer_url, exc,
        )
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


def _first_nonempty(
    attrs: dict[str, Any], candidates: tuple[str, ...],
) -> str | None:
    for c in candidates:
        v = attrs.get(c)
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
        if v and not isinstance(v, str):
            return str(v)
    return None


def _summarize_service_areas(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        name = _first_nonempty(
            attrs,
            (
                "ESD_Name", "ESD_Name_Full", "FIR", "FIR2",
                "NAME", "Name",
            ),
        ) or "Unnamed fire service area"
        code = _first_nonempty(
            attrs,
            (
                "CoA_ESD_Code", "County_ESD_Code", "FIRCODE",
                "FIR2CODE", "OBJECTID",
            ),
        )
        out.append({
            "name": name,
            "full_name": _first_nonempty(
                attrs,
                ("ESD_Name_Full", "FIR", "FIR2"),
            ) or name,
            "code": code,
            "phone": _first_nonempty(attrs, ("Phone_Number", "PHONE")),
            "address": _first_nonempty(attrs, ("Address", "ADDRESS")) or "",
            "source": "Travis County fire/ESD service-area layer",
        })
    return out


def _build_service_area_advisory(service_area: dict[str, Any]) -> str:
    name = service_area.get("name") or "the serving fire/ESD area"
    full_name = service_area.get("full_name")
    code = service_area.get("code")
    address = service_area.get("address")
    phone = service_area.get("phone")
    parts = [f"Serving fire/ESD area: {name}"]
    if full_name and full_name != name:
        parts.append(f"({full_name})")
    if code:
        parts.append(f"[{code}]")
    sentence = " ".join(parts) + "."
    detail_parts: list[str] = []
    if address:
        detail_parts.append(f"listed address: {address}")
    if phone:
        detail_parts.append(f"phone: {phone}")
    if detail_parts:
        sentence += " " + "; ".join(detail_parts) + "."
    return (
        sentence
        + " Fire-station distance is temporarily unavailable because the "
        "Travis County fire-station layer is down. Verify response time, ISO "
        "rating, and fire-flow capacity with the serving department."
    )


def _summarize_stations(
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
            dist_mi = _haversine_mi(
                parcel_lat, parcel_lng, slat, slng,
            )
        except (TypeError, ValueError):
            continue
        out.append({
            "name": _first_nonempty(attrs, _NAME_FIELDS) or "Unnamed",
            "address": _first_nonempty(attrs, _ADDRESS_FIELDS) or "",
            "department": _first_nonempty(attrs, _DEPT_FIELDS) or "",
            "station_number": _first_nonempty(
                attrs, _STATION_NUM_FIELDS,
            ) or "",
            "distance_mi": round(dist_mi, 2),
            "lat": slat, "lng": slng,
        })
    out.sort(key=lambda s: s["distance_mi"])
    return out


def _build_advisory(
    stations: list[dict[str, Any]],
    county: str, buffer_mi: float,
) -> str:
    if not stations:
        return (
            f"No fire stations within a {buffer_mi:.0f}-mile "
            f"radius of the parcel in {county.title()} County's "
            "published fire-station layer. Verify with the "
            "serving ESD / fire department directly."
        )
    nearest = stations[0]
    parts = [
        f"Nearest fire station: {nearest['name']}"
        + (
            f" (Station #{nearest['station_number']})"
            if nearest['station_number'] else ""
        )
        + f" — {nearest['distance_mi']} mi straight-line"
        + (
            f", {nearest['department']}"
            if nearest['department'] else ""
        )
        + (
            f", {nearest['address']}"
            if nearest['address'] else ""
        )
        + "."
    ]
    if len(stations) > 1:
        more = ", ".join(
            f"{s['name']} ({s['distance_mi']} mi)"
            for s in stations[1:3]
        )
        parts.append(
            f"Other stations within {buffer_mi:.0f} mi: {more}."
        )
    parts.append(
        "Distance is straight-line, not driving. ISO "
        "Suppression Rating + fire-flow capacity at the "
        "nearest hydrant remain manual lookups."
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
            "stations": [], "stations_count": 0,
            "nearest_station": None,
            "iso_rating_lookup_url": _ISO_LOOKUP_URL,
            "fire_flow_status": "manual_verification_required",
            "advisory": (
                "Coordinates required for fire-station "
                "proximity lookup. Upload an ISO rating "
                "letter or fire-flow test report."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for fire-station query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ISO_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable. Upload an ISO "
                "rating letter or fire marshal capacity "
                "letter from the serving department."
            ),
        ),
    )


def _jurisdiction_not_covered_result(county: str) -> AdapterResult:
    label = county or "this property's county"
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.JURISDICTION_NOT_COVERED,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": "unknown",
            "county": county,
            "stations": [], "stations_count": 0,
            "nearest_station": None,
            "iso_rating_lookup_url": _ISO_LOOKUP_URL,
            "fire_flow_status": "manual_verification_required",
            "advisory": (
                f"No fire-station layer wired for "
                f"{label.title()} County in this build. "
                "Look up the ISO rating at "
                f"{_ISO_LOOKUP_URL} and upload an ISO "
                "rating letter / fire marshal capacity letter."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ISO_UPLOAD_TYPES),
            reason=(
                f"County not wired. Look up ISO rating at "
                f"{_ISO_LOOKUP_URL} and upload the rating "
                "letter."
            ),
        ),
    )


def _fetch_failed_result(county: str, error_msg: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "unknown",
            "county": county,
            "stations": [], "stations_count": 0,
            "nearest_station": None,
            "iso_rating_lookup_url": _ISO_LOOKUP_URL,
            "fire_flow_status": "manual_verification_required",
            "advisory": (
                f"{county.title()} fire-station layer did "
                "not respond. Retry, or pull the ISO rating "
                f"manually at {_ISO_LOOKUP_URL}."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=[f"fire-station query failed: {error_msg}"[:500]],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_ISO_UPLOAD_TYPES),
            reason=(
                "Fire-station service did not respond. Look "
                f"up ISO rating at {_ISO_LOOKUP_URL}."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
