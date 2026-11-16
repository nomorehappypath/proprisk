"""transit_intel adapter — Capital Metro bus stops + rail.

Ported verbatim from legacy_services/core/public_intel_adapters/
transit_intel.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field
added.

Travis + Williamson counties (CMTA service area) only —
Hays / Bastrop / Burnet etc. return JURISDICTION_NOT_COVERED
with pointer to local agency (SPAN, CARTS).

Confidence always SCREENED_INDICATOR — distance is straight-
line, not walking; TOD-overlay eligibility + headway require
manual verification with CMTA / city.
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

log = logging.getLogger("propdesk.site_check.transit_intel")

SOURCE_ID = "transit_intel"

_CMTA_STOPS_QUERY = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/arcgis/rest/"
    "services/EXTERNAL_cmta_stops/FeatureServer/0/query"
)
_CMTA_RAIL_STATIONS_QUERY = (
    "https://services7.arcgis.com/uFAr0LUPy14bDaLg/arcgis/rest/"
    "services/CapMetro_Rail_Stations/FeatureServer/0/query"
)

_TIMEOUT = 25
_BUS_BUFFER_MI = 1.0
_RAIL_BUFFER_MI = 3.0

_TRANSIT_UPLOAD_TYPES = [
    "CMTA Project Connect map",
    "TOD overlay confirmation",
    "city transit study",
]

_CMTA_COUNTIES = {"travis", "williamson"}


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    bus_buffer_mi: float = _BUS_BUFFER_MI,
    rail_buffer_mi: float = _RAIL_BUFFER_MI,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    county = (identity.county or "").lower().strip()
    if county and county not in _CMTA_COUNTIES:
        return _jurisdiction_not_covered_result(county)

    sess = http_session(session)

    bus_features, bus_error = _query_layer(
        sess, _CMTA_STOPS_QUERY,
        identity.lat, identity.lng,
        buffer_mi=bus_buffer_mi,
        where="STATUS = 'Active'",
    )
    rail_features, rail_error = _query_layer(
        sess, _CMTA_RAIL_STATIONS_QUERY,
        identity.lat, identity.lng,
        buffer_mi=rail_buffer_mi,
    )

    bus_stops = _summarize_bus_stops(
        bus_features, identity.lat, identity.lng,
    )
    rail_stations = _summarize_rail_stations(
        rail_features, identity.lat, identity.lng,
    )
    nearest_bus = bus_stops[0] if bus_stops else None
    nearest_rail = rail_stations[0] if rail_stations else None

    advisory = _build_advisory(
        bus_stops, rail_stations,
        nearest_bus, nearest_rail,
        bus_buffer_mi, rail_buffer_mi,
    )

    errors: list[str] = []
    if bus_error:
        errors.append(f"CMTA stops: {bus_error}")
    if rail_error:
        errors.append(f"CMTA rail: {rail_error}")

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": (
                "clear"
                if (bus_stops or rail_stations)
                else "no_transit_in_buffer"
            ),
            "bus_stops_count": len(bus_stops),
            "rail_stations_count": len(rail_stations),
            "nearest_bus_stop": nearest_bus,
            "nearest_rail_station": nearest_rail,
            "bus_stops": bus_stops[:10],
            "rail_stations": rail_stations[:5],
            "bus_buffer_mi": bus_buffer_mi,
            "rail_buffer_mi": rail_buffer_mi,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors if errors else None,
        manual_upload_request=ManualUploadRequest(
            document_types=list(_TRANSIT_UPLOAD_TYPES),
            reason=(
                "Stop / station proximity is screening — "
                "binding TOD-overlay eligibility, headway, "
                "and Project Connect alignment require "
                "verification with CMTA + the city's "
                "transit / planning department."
            ),
        ),
    )


def _query_layer(
    session: requests.Session, url: str,
    lat: float, lng: float,
    buffer_mi: float,
    where: str | None = None,
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
    if where:
        params["where"] = where
    try:
        resp = session.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return [], f"non-JSON content-type ({ct[:40]})"
        return resp.json().get("features", []) or [], None
    except Exception as exc:
        log.warning("CMTA layer query failed (%s): %s", url, exc)
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


def _summarize_bus_stops(
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
            "stop_id": attrs.get("STOP_ID"),
            "name": attrs.get("STOP_NAME") or "Unnamed",
            "on_street": attrs.get("ON_STREET") or "",
            "at_street": attrs.get("AT_STREET") or "",
            "stop_type": attrs.get("STOP_TYPE") or "Bus Stop",
            "shelter": bool(attrs.get("SHELTERS")),
            "bench": bool(attrs.get("BENCHES")),
            "bike_rack": bool(attrs.get("BIKERACKS")),
            "distance_mi": round(dist, 2),
            "lat": slat, "lng": slng,
        })
    out.sort(key=lambda s: s["distance_mi"])
    return out


def _summarize_rail_stations(
    features: list[dict[str, Any]],
    parcel_lat: float, parcel_lng: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in features or []:
        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}
        slat = geom.get("y") or attrs.get("LATITUDE")
        slng = geom.get("x") or attrs.get("LONGITUDE")
        if slat is None or slng is None:
            continue
        try:
            dist = _haversine_mi(
                parcel_lat, parcel_lng, slat, slng,
            )
        except (TypeError, ValueError):
            continue
        out.append({
            "stop_id": attrs.get("STOP_ID"),
            "name": attrs.get("STOP_NAME") or "Unnamed Station",
            "city": attrs.get("City") or "",
            "zip": attrs.get("Zipcode"),
            "distance_mi": round(dist, 2),
            "lat": slat, "lng": slng,
        })
    out.sort(key=lambda s: s["distance_mi"])
    return out


def _build_advisory(
    bus_stops: list[dict[str, Any]],
    rail_stations: list[dict[str, Any]],
    nearest_bus: dict[str, Any] | None,
    nearest_rail: dict[str, Any] | None,
    bus_buffer_mi: float, rail_buffer_mi: float,
) -> str:
    parts: list[str] = []
    if not bus_stops and not rail_stations:
        return (
            f"No CMTA bus stops within {bus_buffer_mi:.1f} mi "
            f"or rail stations within {rail_buffer_mi:.1f} mi. "
            "Parcel is outside CMTA walkshed."
        )
    if bus_stops:
        parts.append(
            f"{len(bus_stops)} active CMTA bus stop(s) within "
            f"{bus_buffer_mi:.1f} mi"
        )
        if nearest_bus:
            parts.append(
                f"— nearest: {nearest_bus['name']} "
                f"({nearest_bus['distance_mi']} mi)."
            )
        else:
            parts.append(".")
    else:
        parts.append(
            f"No active bus stops within {bus_buffer_mi:.1f} mi."
        )
    if nearest_rail:
        parts.append(
            f"Nearest rail: {nearest_rail['name']} "
            f"({nearest_rail['distance_mi']} mi) — Red Line."
        )
    elif rail_buffer_mi > 0:
        parts.append(
            f"No rail stations within {rail_buffer_mi:.1f} mi."
        )
    parts.append(
        "Distance is straight-line, not walking. TOD-overlay "
        "eligibility + headway require manual verification "
        "with CMTA / city."
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
            "bus_stops": [], "rail_stations": [],
            "bus_stops_count": 0, "rail_stations_count": 0,
            "nearest_bus_stop": None,
            "nearest_rail_station": None,
            "advisory": (
                "Coordinates required for CMTA proximity lookup."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for CMTA query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_TRANSIT_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable. Pull a CMTA "
                "service-area map or city transit study "
                "manually."
            ),
        ),
    )


def _jurisdiction_not_covered_result(county: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.JURISDICTION_NOT_COVERED,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": "unknown",
            "bus_stops": [], "rail_stations": [],
            "bus_stops_count": 0, "rail_stations_count": 0,
            "nearest_bus_stop": None,
            "nearest_rail_station": None,
            "advisory": (
                f"Parcel sits in {county.title()} County, "
                "outside Capital Metro's service area. "
                "Check the local transit agency directly "
                "(SPAN in Hays, CARTS in rural counties)."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        manual_upload_request=ManualUploadRequest(
            document_types=list(_TRANSIT_UPLOAD_TYPES),
            reason=(
                f"CMTA does not serve {county.title()} "
                "County. Look up the local transit agency "
                "+ upload their service map."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
