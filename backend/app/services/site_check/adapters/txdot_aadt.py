"""txdot_aadt adapter — TxDOT AADT traffic counts.

Ported from legacy_services/core/public_intel.py::_fetch_txdot_traffic.
Two-step probe: tight (~150m) frontage road query + wider 2km
nearby-context scan. Frontage records sorted to head of list.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.adapters._txdot_common import (
    _TXDOT_AADT,
    _TXDOT_ROADWAY,
    apply_geometry_generalization,
    arcgis_resilient_get,
    arcgis_spatial_query,
    format_hwy_name,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "txdot_aadt"


_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _clean_year(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    match = _YEAR_RE.search(text)
    return match.group(0) if match else ""


def _is_public_road_name(value: str) -> bool:
    road = (value or "").strip()
    if not road or road == "Unknown":
        return False
    if road.startswith("TxDOT local segment "):
        return False
    # TxDOT AADT sometimes returns internal route ids like
    # "246AA9954". Those are useful to TxDOT, not to brokers.
    return not re.fullmatch(r"\d+[A-Z]{2}\d+", road)


def _arcgis_spatial_features(
    sess,
    url: str,
    lat: float,
    lng: float,
    *,
    radius_meters: int,
    out_fields: str,
    return_geometry: bool = False,
    max_records: int = 50,
) -> list[dict]:
    d = radius_meters / 111_320.0
    envelope = json.dumps({
        "xmin": lng - d, "ymin": lat - d,
        "xmax": lng + d, "ymax": lat + d,
        "spatialReference": {"wkid": 4326},
    })
    params = {
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "outSR": 4326,
        "resultRecordCount": max_records,
        "f": "json",
    }
    if return_geometry:
        apply_geometry_generalization(params)
    # Shared hardened GET (2026-07-14): settings-driven attempts with
    # exponential backoff + jitter — replaces the old 45s + one instant
    # retry that failed every refresh when services.arcgis.com was slow.
    data = arcgis_resilient_get(sess, url, params)
    time.sleep(0.5)
    return data.get("features", []) or []


def _road_name_from_roadway_attrs(attr: dict) -> str:
    hwy = (attr.get("HWY") or "").strip()
    hsys = (attr.get("HSYS") or "").strip().upper()
    hnum = str(attr.get("HNUM") or "").strip()
    ste_nam = (attr.get("STE_NAM") or "").strip()
    if hwy:
        return format_hwy_name(hwy, "")
    if hsys in (
        "IH", "US", "SH", "SL", "SS", "FM", "RM",
        "PR", "BI", "BU", "BS", "BF", "LP",
    ) and hnum:
        return f"{hsys} {hnum.lstrip('0') or hnum}"
    if ste_nam:
        return " ".join(ste_nam.title().split())
    return "Unknown"


def _geometry_centroid(geometry: dict | None) -> tuple[float, float] | None:
    if not geometry:
        return None
    paths = geometry.get("paths") or []
    points = [
        point
        for path in paths
        for point in path
        if isinstance(point, list) and len(point) >= 2
    ]
    if not points:
        return None
    lng = sum(float(point[0]) for point in points) / len(points)
    lat = sum(float(point[1]) for point in points) / len(points)
    return lat, lng


def _haversine_m(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
) -> float:
    radius_m = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return 2 * radius_m * math.asin(min(1.0, math.sqrt(a)))


def _nearest_named_road(
    aadt_geometry: dict | None,
    roadway_features: list[dict],
    *,
    aadt: int | None,
) -> tuple[str, str] | None:
    centroid = _geometry_centroid(aadt_geometry)
    if not centroid:
        return None

    ranked: list[tuple[float, int, str]] = []
    for feature in roadway_features:
        attr = feature.get("attributes") or {}
        road = _road_name_from_roadway_attrs(attr)
        if not _is_public_road_name(road):
            continue
        rcentroid = _geometry_centroid(feature.get("geometry") or {})
        if not rcentroid:
            continue
        distance_m = _haversine_m(
            centroid[0], centroid[1], rcentroid[0], rcentroid[1],
        )
        # Prefer Roadway Inventory rows carrying the same ADT_CUR
        # when available, then fall back to closest named segment.
        adt_match = 0 if aadt and attr.get("ADT_CUR") == aadt else 1
        ranked.append((distance_m, adt_match, road))

    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[1], item[0]))
    distance_m, adt_match, road = ranked[0]
    if distance_m > 250 and adt_match != 0:
        return None
    source = (
        "TxDOT Roadway Inventory ADT match"
        if adt_match == 0
        else "TxDOT Roadway Inventory nearest named segment"
    )
    return road, source


def _display_road_name(
    raw_road: str,
    *,
    aadt_geometry: dict | None,
    roadway_features: list[dict],
    aadt: int | None,
) -> tuple[str | None, str | None]:
    if _is_public_road_name(raw_road):
        return raw_road, None
    resolved = _nearest_named_road(
        aadt_geometry,
        roadway_features,
        aadt=aadt,
    )
    if resolved:
        return resolved
    return None, None


def _fetch_traffic(sess, lat, lng, radius=2000):
    warnings: list[str] = []
    frontage_road_names: set[str] = set()
    try:
        frontage_features = arcgis_spatial_query(
            sess, _TXDOT_ROADWAY, lat, lng,
            radius_meters=150,
            out_fields="HWY,ADT_CUR,ADT_YEAR,NUM_LANES,SPD_MAX",
            max_records=5,
        )
        for fattr in frontage_features:
            fhwy = fattr.get("HWY", "")
            if fhwy:
                frontage_road_names.add(format_hwy_name(fhwy, ""))
    except Exception:  # noqa: S110 — ported: frontage probe is best-effort, main AADT query proceeds regardless
        pass

    counts = []

    try:
        roadway_features = _arcgis_spatial_features(
            sess, _TXDOT_ROADWAY, lat, lng,
            radius_meters=radius,
            out_fields=(
                "HWY,HSYS,HNUM,STE_NAM,ADT_CUR,ADT_YEAR,"
                "NUM_LANES,SPD_MAX"
            ),
            return_geometry=True,
            max_records=40,
        )
    except Exception as exc:
        roadway_features = []
        warnings.append(f"TxDOT Roadway Inventory unavailable: {str(exc)[:160]}")

    try:
        features = _arcgis_spatial_features(
            sess, _TXDOT_AADT, lat, lng,
            radius_meters=radius,
            out_fields="RTE_NM,RTE_PRFX,AADT_CUR,EXT_DATE",
            return_geometry=True,
        )
    except Exception as exc:
        features = []
        warnings.append(f"TxDOT AADT layer unavailable: {str(exc)[:160]}")
    for feature in features:
        attr = feature.get("attributes") or {}
        prefix = attr.get("RTE_PRFX", "")
        rte = attr.get("RTE_NM", "")
        raw_road = format_hwy_name(rte, prefix)
        aadt = attr.get("AADT_CUR") or 0
        map_geometry = _line_geometry(feature.get("geometry") or {})
        centroid = _geometry_centroid(feature.get("geometry") or {})
        road, resolved_source = _display_road_name(
            raw_road,
            aadt_geometry=feature.get("geometry") or {},
            roadway_features=roadway_features,
            aadt=int(aadt) if aadt else None,
        )
        date = attr.get("EXT_DATE", "")
        if aadt and road:
            item = {
                "road_name": road,
                "aadt": int(aadt),
                "year": _clean_year(date),
                "source": "TxDOT AADT",
                "is_frontage": road in frontage_road_names,
                "map_geometry": map_geometry,
                "lat": centroid[0] if centroid else None,
                "lng": centroid[1] if centroid else None,
                "distance_mi": (
                    round(_haversine_m(lat, lng, centroid[0], centroid[1]) / 1609.344, 2)
                    if centroid else None
                ),
            }
            if resolved_source:
                item["road_name_source"] = resolved_source
                item["raw_txdot_route"] = raw_road
            counts.append(item)

    for feature in roadway_features:
        attr = feature.get("attributes") or {}
        road = _road_name_from_roadway_attrs(attr)

        adt = attr.get("ADT_CUR") or 0
        year = attr.get("ADT_YEAR", "")
        map_geometry = _line_geometry(feature.get("geometry") or {})
        centroid = _geometry_centroid(feature.get("geometry") or {})
        if adt and _is_public_road_name(road):
            counts.append({
                "road_name": road,
                "aadt": int(adt),
                "year": _clean_year(year),
                "source": "TxDOT Roadway Inventory",
                "is_frontage": road in frontage_road_names,
                "map_geometry": map_geometry,
                "lat": centroid[0] if centroid else None,
                "lng": centroid[1] if centroid else None,
                "distance_mi": (
                    round(_haversine_m(lat, lng, centroid[0], centroid[1]) / 1609.344, 2)
                    if centroid else None
                ),
            })

    seen: dict[str, dict] = {}
    for c in counts:
        key = c["road_name"]
        if key not in seen or c["aadt"] > seen[key]["aadt"]:
            prior_frontage = seen.get(key, {}).get("is_frontage", False)
            seen[key] = c
            if prior_frontage:
                seen[key]["is_frontage"] = True

    traffic = sorted(
        seen.values(),
        key=lambda x: (
            0 if x.get("is_frontage") else 1,
            -int(x.get("aadt") or 0),
        ),
    )
    if not traffic and warnings:
        raise RuntimeError("; ".join(warnings))
    return traffic, warnings


def _line_geometry(geometry: dict | None) -> dict | None:
    paths = (geometry or {}).get("paths") or []
    valid_paths = [
        [
            [float(point[0]), float(point[1])]
            for point in path
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        for path in paths
        if isinstance(path, list)
    ]
    valid_paths = [path for path in valid_paths if len(path) >= 2]
    if not valid_paths:
        return None
    if len(valid_paths) == 1:
        return {"type": "LineString", "coordinates": valid_paths[0]}
    return {"type": "MultiLineString", "coordinates": valid_paths}


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for txdot_aadt")
    sess = http_session(session)
    try:
        traffic, warnings = _fetch_traffic(sess, identity.lat, identity.lng)
    except Exception as exc:
        return _failed(f"txdot aadt query failed: {exc}")

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if warnings
            else DataConfidenceStatus.VERIFIED_PUBLIC_FACT
            if traffic
            else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=(
            CoverageStatus.PARTIAL if warnings else CoverageStatus.COVERED
        ),
        normalized_facts={
            "traffic_counts": traffic,
            "source": "TxDOT",
            "source_warnings": warnings,
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
        advisories=warnings or None,
    )


def _failed(reason: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={},
        errors=[reason],
        fetched_at=datetime.now(UTC),
    )
