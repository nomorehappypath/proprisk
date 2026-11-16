"""fema_nfhl adapter — FEMA National Flood Hazard Layer.

Ported from legacy_services/core/public_intel.py
::_fetch_fema_flood_zone + _select_flood_geometry +
_fema_query_with_retry + _assemble_flood_payload + helpers.

Algorithm + buffer formula + multi-layer query (zones / LOMA /
LOMR) + status taxonomy preserved verbatim.

NOT ported in v0: _apply_flood_contradiction_guard. That helper
requires the project document corpus (it scans uploaded text
for LOMA references that contradict the public answer). It
wires in when Quick Analysis ships and the orchestrator gets
a path to the project's documents. For now the adapter never
returns `contradicted_by_uploaded_evidence`.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Any

import requests
from pyproj import Geod
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import unary_union

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
    map_legacy_status,
)

SOURCE_ID = "fema_nfhl"

_FEMA_NFHL_BASE = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/"
    "MapServer"
)
_FEMA_NFHL_FLOOD = f"{_FEMA_NFHL_BASE}/28/query"   # zones
_FEMA_NFHL_LOMA = f"{_FEMA_NFHL_BASE}/34/query"   # LOMA
_FEMA_NFHL_LOMR = f"{_FEMA_NFHL_BASE}/1/query"    # LOMR

_FLOOD_BUFFER_MIN_M = 100
_FLOOD_BUFFER_MAX_M = 500
_FLOOD_BUFFER_DEFAULT_M = 300
_DEG_PER_METRE = 1.0 / 111_320.0
_TIMEOUT_S = 20

_FLOOD_ZONE_DESC = {
    "A":   "100-year floodplain (no BFE determined)",
    "AE":  "100-year floodplain (BFE determined)",
    "AH":  "100-year shallow flooding (ponding)",
    "AO":  "100-year shallow flooding (sheet flow)",
    "AR":  "100-year floodplain (temporarily increased risk)",
    "A99": "100-year floodplain (federal flood protection system under construction)",
    "V":   "Coastal 100-year flood with velocity hazard (waves)",
    "VE":  "Coastal 100-year flood with velocity hazard (BFE determined)",
    "X":   "Area of minimal flood hazard",
    "D":   "Undetermined flood hazard",
}
_SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}
_FLOOD_ADVISORY_BY_STATUS = {
    "clear": (
        "No high-risk flood zone touches the mapped property area."
    ),
    "mixed": (
        "Parts of this property fall under different FEMA flood "
        "designations — some of it may be in a floodplain."
    ),
    "unknown": (
        "FEMA's map didn't give a clear answer for this exact spot."
    ),
}
_GEOD = Geod(ellps="WGS84")
_SQM_PER_ACRE = 4046.8564224


# ── Buffer envelope ───────────────────────────────────────────────


def _select_flood_geometry(lat, lng, acreage) -> dict:
    """Pick the buffer envelope to intersect against FEMA.

    Buffer formula:  radius_m = clamp(100, ceil(sqrt(acres * 4047)), 500)
    Default when acreage unknown:  radius_m = 300
    """
    radius_m = _FLOOD_BUFFER_DEFAULT_M
    if acreage is not None and acreage > 0:
        sq_m = float(acreage) * 4047.0
        radius_m = max(
            _FLOOD_BUFFER_MIN_M,
            min(_FLOOD_BUFFER_MAX_M, math.ceil(math.sqrt(sq_m))),
        )
    dlat = radius_m * _DEG_PER_METRE
    dlng = radius_m * _DEG_PER_METRE / max(0.01, math.cos(math.radians(lat)))
    return {
        "geometry_type": "buffer",
        "confidence_level": "buffer",
        "query_radius_m": radius_m,
        "envelope": {
            "xmin": lng - dlng,
            "xmax": lng + dlng,
            "ymin": lat - dlat,
            "ymax": lat + dlat,
        },
    }


def _select_query_geometry(identity: PropertyIdentity) -> dict:
    parcel = identity.parcel_geometry
    if isinstance(parcel, dict) and parcel.get("type") in {"Polygon", "MultiPolygon"}:
        try:
            parcel_shape = shape(parcel)
            if not parcel_shape.is_valid:
                parcel_shape = parcel_shape.buffer(0)
            if not parcel_shape.is_empty:
                xmin, ymin, xmax, ymax = parcel_shape.bounds
                return {
                    "geometry_type": "parcel",
                    "confidence_level": "parcel_intersection",
                    "query_radius_m": None,
                    "envelope": {
                        "xmin": xmin,
                        "xmax": xmax,
                        "ymin": ymin,
                        "ymax": ymax,
                    },
                }
        except Exception:  # noqa: S110 — ported: parcel-geometry probe is best-effort, falls back to point geometry below
            pass
    return _select_flood_geometry(identity.lat, identity.lng, identity.acreage)


def _attributes(feature: dict[str, Any]) -> dict[str, Any]:
    value = feature.get("attributes") or feature.get("properties")
    return value if isinstance(value, dict) else {}


def _geodesic_acres(geometry) -> float:
    if geometry is None or geometry.is_empty:
        return 0.0
    area_sqm, _ = _GEOD.geometry_area_perimeter(geometry)
    return round(abs(area_sqm) / _SQM_PER_ACRE, 3)


def _feature_shape(feature: dict[str, Any]):
    """Read either GeoJSON or the native ArcGIS polygon response shape."""
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


def _parcel_flood_geometries(
    parcel_geojson: dict[str, Any] | None,
    zone_features: list[dict],
) -> dict[str, Any]:
    """Clip FEMA polygons to the verified parcel for screening math."""
    if not isinstance(parcel_geojson, dict):
        return {
            "sfha_map_geometry": None,
            "floodway_map_geometry": None,
            "shaded_x_map_geometry": None,
            "sfha_intersection_acres": None,
            "floodway_intersection_acres": None,
            "shaded_x_intersection_acres": None,
            "parcel_zone_features": [],
        }
    try:
        parcel = shape(parcel_geojson)
        if not parcel.is_valid:
            parcel = parcel.buffer(0)
        if parcel.is_empty:
            raise ValueError("empty parcel")
    except Exception:
        return {
            "sfha_map_geometry": None,
            "floodway_map_geometry": None,
            "shaded_x_map_geometry": None,
            "sfha_intersection_acres": None,
            "floodway_intersection_acres": None,
            "shaded_x_intersection_acres": None,
            "parcel_zone_features": [],
        }

    sfha_parts = []
    floodway_parts = []
    shaded_x_parts = []
    zone_rows: list[dict[str, Any]] = []
    for feature in zone_features:
        zone_shape = _feature_shape(feature)
        if zone_shape is None:
            continue
        try:
            clipped = zone_shape.intersection(parcel)
        except Exception:  # noqa: S112 — ported: per-zone-feature isolation, invalid geometry skipped
            continue
        if clipped.is_empty:
            continue
        attrs = _attributes(feature)
        zone = str(attrs.get("FLD_ZONE") or "").upper()
        subtype = str(attrs.get("ZONE_SUBTY") or "").strip()
        sfha_flag = str(attrs.get("SFHA_TF") or "").upper()
        is_sfha = zone in _SFHA_ZONES or sfha_flag in {"T", "TRUE", "Y", "YES"}
        is_floodway = zone == "FLOODWAY" or "FLOODWAY" in subtype.upper()
        is_shaded_x = zone == "X" and "0.2" in subtype
        if is_sfha:
            sfha_parts.append(clipped)
        if is_floodway:
            floodway_parts.append(clipped)
        if is_shaded_x:
            shaded_x_parts.append(clipped)
        zone_rows.append({
            "zone": zone or None,
            "subtype": subtype or None,
            "is_sfha": is_sfha,
            "is_floodway": is_floodway,
            "is_500_year": is_shaded_x,
            "base_flood_elevation": attrs.get("STATIC_BFE"),
            "feature_id": attrs.get("FLD_AR_ID"),
            "source_citation": attrs.get("SOURCE_CIT"),
            "intersection_acres": _geodesic_acres(clipped),
            "map_geometry": mapping(clipped),
        })

    def merged(parts):
        value = unary_union(parts) if parts else None
        return value, mapping(value) if value is not None and not value.is_empty else None

    sfha, sfha_geojson = merged(sfha_parts)
    floodway, floodway_geojson = merged(floodway_parts)
    shaded_x, shaded_x_geojson = merged(shaded_x_parts)
    return {
        "sfha_map_geometry": sfha_geojson,
        "floodway_map_geometry": floodway_geojson,
        "shaded_x_map_geometry": shaded_x_geojson,
        "sfha_intersection_acres": _geodesic_acres(sfha),
        "floodway_intersection_acres": _geodesic_acres(floodway),
        "shaded_x_intersection_acres": _geodesic_acres(shaded_x),
        "parcel_zone_features": zone_rows,
    }


def _fema_query_with_retry(
    sess: requests.Session, url: str, params: dict,
) -> dict:
    """3 attempts, 1s/2s/4s backoff on 429/5xx."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = sess.get(url, params=params, timeout=_TIMEOUT_S)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(
                    f"FEMA transient {resp.status_code}",
                    response=resp,
                )
            resp.raise_for_status()
            data = resp.json()
            # ArcGIS often answers HTTP 200 with an error envelope
            # ({"error": {"code": 504, "message": ...}}) instead of a 5xx.
            # Untreated, that reads as "zero features found" — the
            # false-negative this guards against. Treat it as transient.
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                code = err.get("code") if isinstance(err, dict) else None
                msg = err.get("message") if isinstance(err, dict) else err
                raise requests.HTTPError(
                    f"FEMA ArcGIS error {code or ''}: {msg or 'unspecified'}"
                    .strip()
                )
            return data
        except (requests.ConnectionError, requests.Timeout,
                requests.HTTPError) as exc:
            last_exc = exc
            if attempt == 2:
                raise
            time.sleep(1 * (2 ** attempt))
    raise last_exc or RuntimeError("FEMA fetch failed")


def _summarise_lomc_features(
    features: list[dict],
    *,
    case_field_candidates=("CASE_NO", "CASE_NUMBER", "CASE_NUM", "CASENO"),
    date_field_candidates=(
        "EFF_DATE", "STATUS_DAT", "EFFDATE", "EFFECTIVE", "DATE_EFF",
    ),
) -> list[dict]:
    def normalized_date(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 100_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp, tz=UTC).date().isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return normalized_date(int(text))
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            return text

    out = []
    for feat in features:
        attrs = _attributes(feat)
        case_no = next(
            (attrs.get(k) for k in case_field_candidates if attrs.get(k)),
            None,
        )
        date_raw = next(
            (attrs.get(k) for k in date_field_candidates if attrs.get(k)),
            None,
        )
        out.append({
            "case_no": case_no,
            "effective_date": normalized_date(date_raw),
            "status": attrs.get("STATUS"),
            "source_citation": attrs.get("SOURCE_CIT"),
        })
    return out


def _assemble_payload(
    *,
    geom: dict,
    zone_features: list[dict],
    loma_features: list[dict],
    lomr_features: list[dict],
) -> dict:
    """Roll the three FEMA layer responses into the v1 payload
    shape. Computes a partial status — `clear` / `mixed` /
    `unknown` — based on layer data alone."""
    zones_found: list[str] = []
    sfha_zones_found: list[str] = []
    has_shaded_x = False
    bfe: float | None = None
    max_depth: float | None = None
    max_velocity: float | None = None
    in_floodway = False
    zone_subtypes_seen: set[str] = set()
    zone_engineering_detail: list[dict] = []

    for feat in zone_features:
        attrs = _attributes(feat)
        zone = attrs.get("FLD_ZONE")
        subtype = (attrs.get("ZONE_SUBTY") or "").strip()
        sfha_flag = str(attrs.get("SFHA_TF") or "").upper()

        if zone and zone not in zones_found:
            zones_found.append(zone)
        if subtype:
            zone_subtypes_seen.add(subtype.upper())

        is_sfha = (
            zone in _SFHA_ZONES
            or sfha_flag in ("T", "TRUE", "Y", "YES")
        )
        if is_sfha and zone and zone not in sfha_zones_found:
            sfha_zones_found.append(zone)

        if zone == "X" and "0.2" in subtype:
            has_shaded_x = True

        static_bfe = attrs.get("STATIC_BFE")
        real_bfe = None
        if static_bfe is not None and static_bfe != -9999 and static_bfe > 0:
            real_bfe = float(static_bfe)
            if bfe is None:
                bfe = real_bfe

        depth = attrs.get("DEPTH")
        real_depth = None
        if depth is not None and depth != -9999 and depth > 0:
            real_depth = float(depth)
            if max_depth is None or real_depth > max_depth:
                max_depth = real_depth

        velocity = attrs.get("VELOCITY")
        if velocity is not None and velocity != -9999 and velocity > 0:
            real_v = float(velocity)
            if max_velocity is None or real_v > max_velocity:
                max_velocity = real_v

        zone_upper = str(zone or "").upper()
        this_floodway = (
            zone_upper == "FLOODWAY"
            or "FLOODWAY" in subtype.upper()
        )
        if this_floodway:
            in_floodway = True

        if zone:
            zone_engineering_detail.append({
                "zone": zone,
                "subtype": subtype or None,
                "bfe": real_bfe,
                "depth": real_depth,
                "in_floodway": this_floodway,
            })

    loma_cases = _summarise_lomc_features(loma_features)
    lomr_cases = _summarise_lomc_features(lomr_features)

    has_sfha = bool(sfha_zones_found)
    multiple_zones = len(zones_found) > 1
    # Sort zones for deterministic output — ArcGIS returns
    # features in arrival order which flips ["AE","X"] vs
    # ["X","AE"] between runs; that was painting harmless
    # parity REVIEW_VALUES on every flood-mixed property.
    zones_found = sorted(zones_found)
    sfha_zones_found = sorted(sfha_zones_found)

    if not zones_found:
        status = "unknown"
    elif has_sfha or has_shaded_x or multiple_zones or loma_cases:
        status = "mixed"
    elif zones_found == ["X"]:
        status = "clear"
    else:
        status = "mixed"

    advisory = _FLOOD_ADVISORY_BY_STATUS.get(status, "")

    legacy_zone = "Unknown"
    if sfha_zones_found:
        legacy_zone = sfha_zones_found[0]
    elif zones_found:
        legacy_zone = zones_found[0]

    legacy_desc = _FLOOD_ZONE_DESC.get(legacy_zone, "")
    if zone_subtypes_seen:
        legacy_desc = (
            f"{legacy_desc} ({', '.join(sorted(zone_subtypes_seen))})"
            if legacy_desc
            else ", ".join(sorted(zone_subtypes_seen))
        )

    return {
        "result_schema_version": 3,
        "status": status,
        "geometry_type": geom["geometry_type"],
        "confidence_level": geom["confidence_level"],
        "query_radius_m": geom["query_radius_m"],
        "zones_found": zones_found,
        "sfha_zones_found": sfha_zones_found,
        "has_sfha": has_sfha,
        "has_shaded_x": has_shaded_x,
        "multiple_zones": multiple_zones,
        "loma_cases": loma_cases,
        "lomr_cases": lomr_cases,
        "advisory": advisory,
        "base_flood_elevation": bfe,
        "max_flood_depth_ft": max_depth,
        "max_flood_velocity_fps": max_velocity,
        "in_regulatory_floodway": in_floodway,
        "zone_engineering_detail": zone_engineering_detail,
        "zone_subtypes": sorted(zone_subtypes_seen),
        "zone": legacy_zone,
        "in_floodplain": has_sfha,
        "zone_description": legacy_desc or "Unknown zone type",
        "source": "FEMA NFHL",
    }


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for fema_nfhl")

    sess = http_session(session)
    geom = _select_query_geometry(identity)
    env = geom["envelope"]
    envelope_param = (
        f"{env['xmin']},{env['ymin']},{env['xmax']},{env['ymax']}"
    )
    common_params = {
        "geometry": envelope_param,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }

    errors: list[str] = []

    try:
        zone_data = _fema_query_with_retry(
            sess, _FEMA_NFHL_FLOOD,
            params={
                **common_params,
                "outFields": (
                    "FLD_ZONE,ZONE_SUBTY,SFHA_TF,"
                    "STATIC_BFE,DEPTH,VELOCITY,FLD_AR_ID,SOURCE_CIT"
                ),
            },
        )
    except Exception as exc:
        return _failed(f"FEMA layer 28 (zones) failed: {exc}")

    time.sleep(0.5)

    try:
        loma_data = _fema_query_with_retry(
            sess, _FEMA_NFHL_LOMA,
            params={**common_params, "outFields": "*"},
        )
    except Exception as exc:
        errors.append(f"FEMA LOMA layer failed: {exc}")
        loma_data = {"features": []}

    time.sleep(0.3)

    try:
        lomr_data = _fema_query_with_retry(
            sess, _FEMA_NFHL_LOMR,
            params={**common_params, "outFields": "*"},
        )
    except Exception as exc:
        errors.append(f"FEMA LOMR layer failed: {exc}")
        lomr_data = {"features": []}

    payload = _assemble_payload(
        geom=geom,
        zone_features=zone_data.get("features", []),
        loma_features=loma_data.get("features", []),
        lomr_features=lomr_data.get("features", []),
    )
    payload.update(_parcel_flood_geometries(
        identity.parcel_geometry,
        zone_data.get("features", []),
    ))

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=map_legacy_status(payload["status"]),
        coverage_status=CoverageStatus.COVERED,
        normalized_facts=payload,
        errors=errors or None,
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
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
