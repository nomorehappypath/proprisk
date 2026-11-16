"""Free electric-grid infrastructure facts for Public Data.

HIFLD transmission-line and substation proximity fetcher, extracted from the
the reference app cross-module helper (`data_center_public_sources.py`) so the Site
Check package is self-contained. The `electric_power_infrastructure` adapter
is a thin wrapper around `fetch_hifld_power_paths`.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import requests
from shapely.geometry import LineString, box, mapping
from shapely.ops import unary_union

from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

DATA_CENTER_HIFLD_POWER_SOURCE_ID = "data_center_hifld_power"
PUBLIC_ELECTRIC_POWER_SOURCE_ID = "electric_power_infrastructure"

_TRANSMISSION_LINES_URL = (
    "https://services2.arcgis.com/LYMgRMwHfrWWEg3s/ArcGIS/rest/services/"
    "HIFLD_US_Electric_Power_Transmission_Lines/FeatureServer/0/query"
)
_SUBSTATIONS_URL = (
    "https://services.arcgis.com/G4S1dGvn7PIgYd6Y/ArcGIS/rest/services/"
    "HIFLD_electric_power_substations/FeatureServer/0/query"
)
_TIMEOUT = 25
_RADIUS_MI = 15.0
_EARTH_RADIUS_MI = 3958.7613


def _now() -> datetime:
    return datetime.now(UTC)


def _http_session(base: requests.Session | None = None) -> requests.Session:
    if base is not None:
        return base
    session = requests.Session()
    session.headers.update({
        "User-Agent": "PropDesk/1.0 (propdesk_control_plane site-check; support@reference.app)",
        "Accept": "application/json",
    })
    return session


def _error_result(source_id: str, message: str) -> dict[str, Any]:
    return AdapterResult(
        source_id=source_id,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={},
        fetched_at=_now(),
        errors=[message[:500]],
    ).model_dump(mode="json")


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return None if math.isnan(parsed) else parsed


def _distance_mi(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return _EARTH_RADIUS_MI * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bbox(lat: float, lng: float, radius_mi: float) -> dict[str, Any]:
    d_lat = radius_mi / 69.0
    d_lng = radius_mi / (69.0 * max(0.2, math.cos(math.radians(lat))))
    return {
        "xmin": lng - d_lng,
        "ymin": lat - d_lat,
        "xmax": lng + d_lng,
        "ymax": lat + d_lat,
        "spatialReference": {"wkid": 4326},
    }


def _arcgis_params(
    *,
    lat: float,
    lng: float,
    out_fields: str,
    radius_mi: float = _RADIUS_MI,
) -> dict[str, Any]:
    return {
        "f": "json",
        "where": "1=1",
        "outFields": out_fields,
        "returnGeometry": "true",
        "geometry": json.dumps(_bbox(lat, lng, radius_mi)),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "outSR": 4326,
        "geometryPrecision": 6,
        "spatialRel": "esriSpatialRelIntersects",
        "resultRecordCount": 80,
    }


def _line_map_geometry(
    geometry: dict[str, Any],
    *,
    lat: float,
    lng: float,
) -> dict[str, Any] | None:
    paths = geometry.get("paths") if isinstance(geometry, dict) else None
    if not isinstance(paths, list):
        return None
    lines = []
    for path in paths:
        if not isinstance(path, list):
            continue
        coordinates = []
        for point in path:
            if not isinstance(point, list) or len(point) < 2:
                continue
            try:
                coordinates.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                continue
        if len(coordinates) >= 2:
            lines.append(LineString(coordinates))
    if not lines:
        return None
    bounds = _bbox(lat, lng, _RADIUS_MI)
    clipped = unary_union(lines).intersection(box(
        bounds["xmin"], bounds["ymin"], bounds["xmax"], bounds["ymax"],
    ))
    if clipped.is_empty:
        return None
    simplified = clipped.simplify(0.00015, preserve_topology=True)
    geojson = mapping(simplified)
    if geojson.get("type") not in {"LineString", "MultiLineString"}:
        return None
    return geojson


def _line_distance(lat: float, lng: float, geometry: dict[str, Any]) -> float | None:
    paths = geometry.get("paths") if isinstance(geometry, dict) else None
    if not isinstance(paths, list):
        return None
    best: float | None = None
    for path in paths:
        if not isinstance(path, list):
            continue
        for point in path:
            if not isinstance(point, list) or len(point) < 2:
                continue
            distance = _distance_mi(lat, lng, float(point[1]), float(point[0]))
            if best is None or distance < best:
                best = distance
    return best


def _voltage_class(voltage: float | None) -> str:
    if voltage is None:
        return "unknown"
    if voltage >= 345:
        return "345kv_plus"
    if voltage >= 138:
        return "138kv"
    if voltage >= 69:
        return "69kv"
    return "below_transmission"


def _parse_lines(features: list[dict[str, Any]], *, lat: float, lng: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        attrs = feature.get("attributes") if isinstance(feature.get("attributes"), dict) else {}
        voltage = _num(attrs.get("VOLTAGE"))
        distance = _line_distance(lat, lng, feature.get("geometry") or {})
        if distance is None or distance > _RADIUS_MI:
            continue
        rows.append({
            "label": f"{round(voltage)} kV transmission line" if voltage else "transmission line",
            "voltage_kv": round(voltage) if voltage is not None else None,
            "voltage_class": attrs.get("VOLT_CLASS") or _voltage_class(voltage),
            "adequacy_class": _voltage_class(voltage),
            "distance_mi": round(distance, 2),
            "owner": attrs.get("OWNER"),
            "status": attrs.get("STATUS"),
            "type": attrs.get("TYPE"),
            "source": "HIFLD Electric Power Transmission Lines",
            "source_field": attrs.get("SOURCE"),
            "substation_1": attrs.get("SUB_1"),
            "substation_2": attrs.get("SUB_2"),
            "map_geometry": _line_map_geometry(
                feature.get("geometry") or {}, lat=lat, lng=lng,
            ),
        })
    return sorted(rows, key=lambda item: (item["distance_mi"], -(item.get("voltage_kv") or 0)))[:12]


def _parse_substations(features: list[dict[str, Any]], *, lat: float, lng: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        attrs = feature.get("attributes") if isinstance(feature.get("attributes"), dict) else {}
        geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
        x = geometry.get("x") if geometry.get("x") is not None else attrs.get("LONGITUDE")
        y = geometry.get("y") if geometry.get("y") is not None else attrs.get("LATITUDE")
        if x is None or y is None:
            continue
        distance = _distance_mi(lat, lng, float(y), float(x))
        if distance > _RADIUS_MI:
            continue
        voltage = _num(attrs.get("MAX_VOLT"))
        name = attrs.get("NAME") or "Unnamed substation"
        rows.append({
            "label": f"{name} substation",
            "name": name,
            "max_voltage_kv": round(voltage) if voltage is not None else None,
            "adequacy_class": _voltage_class(voltage),
            "distance_mi": round(distance, 2),
            "status": attrs.get("STATUS"),
            "type": attrs.get("TYPE"),
            "city": attrs.get("CITY"),
            "county": attrs.get("COUNTY"),
            "source": "HIFLD Electric Substations",
            "lat": float(y),
            "lng": float(x),
            "map_geometry": {
                "type": "Point",
                "coordinates": [float(x), float(y)],
            },
        })
    return sorted(rows, key=lambda item: (item["distance_mi"], -(item.get("max_voltage_kv") or 0)))[:12]


def fetch_hifld_power_paths(
    *,
    lat: float | None,
    lng: float | None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch nearby transmission and substation source-path proxies."""
    if lat is None or lng is None:
        return _error_result(DATA_CENTER_HIFLD_POWER_SOURCE_ID, "lat/lng required for HIFLD power query")
    sess = _http_session(session)
    try:
        lines_response = sess.get(
            _TRANSMISSION_LINES_URL,
            params=_arcgis_params(
                lat=lat,
                lng=lng,
                out_fields="VOLTAGE,VOLT_CLASS,OWNER,STATUS,TYPE,SOURCE,SUB_1,SUB_2",
            ),
            timeout=_TIMEOUT,
        )
        lines_response.raise_for_status()
        line_features = lines_response.json().get("features") or []
        substations_response = sess.get(
            _SUBSTATIONS_URL,
            params=_arcgis_params(
                lat=lat,
                lng=lng,
                out_fields="NAME,MAX_VOLT,MIN_VOLT,STATUS,TYPE,CITY,COUNTY,LATITUDE,LONGITUDE",
            ),
            timeout=_TIMEOUT,
        )
        substations_response.raise_for_status()
        substation_features = substations_response.json().get("features") or []
    except Exception as exc:
        return _error_result(DATA_CENTER_HIFLD_POWER_SOURCE_ID, f"HIFLD power query failed: {exc}")

    lines = _parse_lines(line_features if isinstance(line_features, list) else [], lat=lat, lng=lng)
    substations = _parse_substations(
        substation_features if isinstance(substation_features, list) else [],
        lat=lat,
        lng=lng,
    )
    voltages = [
        item.get("voltage_kv")
        for item in lines
        if isinstance(item.get("voltage_kv"), int | float)
    ] + [
        item.get("max_voltage_kv")
        for item in substations
        if isinstance(item.get("max_voltage_kv"), int | float)
    ]
    facts = {
        "radius_mi": _RADIUS_MI,
        "nearest_transmission_lines": lines,
        "nearest_substations": substations,
        "max_voltage_nearby_kv": max(voltages) if voltages else None,
        "nearest_345kv_mi": min(
            [item["distance_mi"] for item in lines + substations if (item.get("voltage_kv") or item.get("max_voltage_kv") or 0) >= 345],
            default=None,
        ),
        "nearest_138kv_mi": min(
            [item["distance_mi"] for item in lines + substations if (item.get("voltage_kv") or item.get("max_voltage_kv") or 0) >= 138],
            default=None,
        ),
        "cannot_confirm": [
            "available transmission headroom",
            "interconnection queue position",
            "upgrade cost",
            "energization timeline",
            "deliverable MW",
        ],
        "attribution": "HIFLD electric transmission lines and substations",
    }
    return AdapterResult(
        source_id=DATA_CENTER_HIFLD_POWER_SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts=facts,
        identifier_used="lat_lng_radius_15mi",
        fetched_at=_now(),
        advisories=[
            "HIFLD identifies infrastructure proximity only; it does not provide ATC, headroom, queue, cost, or MW availability."
        ],
    ).model_dump(mode="json")

