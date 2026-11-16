"""txdot_roadway adapter — TxDOT Roadway Inventory.

Ported from legacy_services/core/public_intel.py
::_fetch_txdot_roadway_info + fetch_txdot_roadway_extended.

Returns roadway classification, lanes, speed, minimum ROW width,
median/access-control indicators, and turn-lane presence indicators
where TxDOT publishes them. Stacking/storage length and site-specific
driveway/crossover approval still require plan, TIA, as-built, or
field evidence.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from itertools import pairwise

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.adapters._txdot_common import (
    _TXDOT_ROADWAY,
    arcgis_spatial_query,
    format_hwy_name,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "txdot_roadway"
MANUAL_REQUIRED = "manual_verification_required"
_MILES_TO_METERS = 1609.344

_MEDIAN_TYPE_LABELS = {
    0: "No median",
    2: "Unprotected median",
    3: "Curbed median",
    4: "Positive barrier - unspecified",
    5: "Positive barrier - flexible",
    6: "Positive barrier - semi-rigid",
    7: "Positive barrier - rigid",
}

_ACCESS_CONTROL_LABELS = {
    1: "Full access control",
    2: "Partial access control",
    3: "No access control",
}

_TURN_LANE_LABELS = {
    1: "No intersections exist on the segment",
    2: "Multiple exclusive turning lanes",
    3: "Continuous exclusive turning lane",
    4: "Single exclusive turning lane",
    5: "Turns permitted; no exclusive turning lanes",
    6: "Turns not permitted during the peak period",
}

_ACCEL_DECEL_LABELS = {
    0: "No acceleration / deceleration lane reported",
    1: "Acceleration / deceleration lane reported",
}


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for txdot_roadway")
    sess = http_session(session)
    try:
        features = _arcgis_spatial_features(
            sess, _TXDOT_ROADWAY,
            identity.lat, identity.lng,
            # legacy_services queried only HWY for the road name,
            # but HWY is null on local-street features (HSYS=LS)
            # and county roads (HSYS=CR) — STE_NAM is where the
            # actual street name lives there. Add HSYS+HNUM+
            # STE_NAM so the name fallback chain can resolve.
            # ROW_WDTH is not in the service schema, but the current
            # TxDOT Roadway Inventory exposes ROW_MIN plus access,
            # median, turn-lane, and lane-width indicators.
            out_fields=(
                "HWY,HSYS,HNUM,STE_NAM,"
                "NUM_LANES,SPD_MAX,F_SYSTEM,"
                "MED_TYPE,MED_WID,SRF_TYPE,ADT_CUR,ADT_YEAR,"
                "ACES_CTRL,ACCEL_DECEL_LANE,ROW_MIN,"
                "RT_TURN_LANE,LT_TURN_LANE,LANE_WIDTH"
            ),
            # Match the AADT nearby-context radius so state-system
            # frontage just outside the parcel centroid is available
            # for engineering screening.
            radius_meters=2000,
            max_records=50,
        )
    except Exception as exc:
        return _failed(f"txdot roadway query failed: {exc}")

    roads = []
    for feature in features:
        attr = feature.get("attributes") or {}
        # Name resolution chain — HWY first (state highway
        # records), HSYS+HNUM combo if HWY blank but the
        # system code is a known prefix (FM/IH/US/SH/SL/RM/SS),
        # then STE_NAM (local streets / county roads), then
        # "Unknown".
        hwy = (attr.get("HWY") or "").strip()
        hsys = (attr.get("HSYS") or "").strip().upper()
        hnum = str(attr.get("HNUM") or "").strip()
        ste_nam = (attr.get("STE_NAM") or "").strip()

        if hwy:
            road_name = format_hwy_name(hwy, "")
        elif hsys in ("IH", "US", "SH", "SL", "FM", "RM", "SS") and hnum:
            road_name = f"{hsys} {hnum.lstrip('0') or hnum}"
        elif ste_nam:
            road_name = ste_nam.title()
        else:
            road_name = "Unknown"
        distance_mi = _geometry_distance_mi(
            identity.lat,
            identity.lng,
            feature.get("geometry"),
        )
        map_geometry = _arcgis_paths_to_geojson(feature.get("geometry"))
        engineering = _engineering_details(attr)
        roads.append({
            "road_name": road_name,
            "distance_mi": distance_mi,
            "functional_class": attr.get("F_SYSTEM") or "",
            "speed_limit": attr.get("SPD_MAX") or "",
            "num_lanes": attr.get("NUM_LANES") or "",
            "surface_type": attr.get("SRF_TYPE") or "",
            "median_type": attr.get("MED_TYPE") or "",
            **({"map_geometry": map_geometry} if map_geometry else {}),
            **engineering,
        })

    seen: dict[str, dict] = {}
    for r in roads:
        key = r["road_name"]
        if key not in seen:
            seen[key] = r
        else:
            existing_lanes = seen[key].get("num_lanes") or 0
            new_lanes = r.get("num_lanes") or 0
            if (
                new_lanes > existing_lanes
                or _resolved_engineering_count(r)
                > _resolved_engineering_count(seen[key])
            ):
                seen[key] = r

    roadway_list = sorted(
        seen.values(),
        key=lambda row: (
            1 if row.get("distance_mi") is None else 0,
            float(row.get("distance_mi") or 999),
            -(_int_or_none(row.get("speed_limit")) or 0),
            -(_int_or_none(row.get("num_lanes")) or 0),
            str(row.get("road_name") or ""),
        ),
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT
            if seen else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "roadways": roadway_list,
            "engineering_details": _engineering_summary(roadway_list),
            "source": "TxDOT Roadway Inventory",
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
    )


def _arcgis_spatial_features(
    sess: requests.Session,
    url: str,
    lat: float,
    lng: float,
    *,
    radius_meters: int,
    out_fields: str,
    max_records: int = 50,
) -> list[dict]:
    """Geometry-returning spatial query. Delegates to the shared
    hardened helper (2026-07-14) so roadway gets the same
    backoff/retry policy and generalized geometry as every other
    TxDOT adapter instead of the old 45s + one-instant-retry copy."""
    return arcgis_spatial_query(
        sess,
        url,
        lat,
        lng,
        radius_meters=radius_meters,
        out_fields=out_fields,
        return_geometry=True,
        max_records=max_records,
    ) or []


def _geometry_distance_mi(
    lat: float,
    lng: float,
    geometry: dict | None,
) -> float | None:
    if not isinstance(geometry, dict):
        return None
    paths = geometry.get("paths") or []
    distances: list[float] = []
    for path in paths:
        if not isinstance(path, list):
            continue
        points = [
            (float(point[1]), float(point[0]))
            for point in path
            if isinstance(point, list) and len(point) >= 2
        ]
        if len(points) == 1:
            distances.append(_haversine_m(lat, lng, points[0][0], points[0][1]))
            continue
        for start, end in pairwise(points):
            distances.append(_point_to_segment_distance_m(lat, lng, start, end))
    if not distances:
        return None
    return round(min(distances) / _MILES_TO_METERS, 3)


def _arcgis_paths_to_geojson(geometry: dict | None) -> dict | None:
    if not isinstance(geometry, dict):
        return None
    lines: list[list[list[float]]] = []
    for path in geometry.get("paths") or []:
        if not isinstance(path, list):
            continue
        coordinates: list[list[float]] = []
        for point in path:
            if not isinstance(point, list) or len(point) < 2:
                continue
            try:
                lng = float(point[0])
                lat = float(point[1])
            except (TypeError, ValueError):
                continue
            if -180 <= lng <= 180 and -90 <= lat <= 90:
                coordinates.append([lng, lat])
        if len(coordinates) >= 2:
            lines.append(coordinates)
    if not lines:
        return None
    if len(lines) == 1:
        return {"type": "LineString", "coordinates": lines[0]}
    return {"type": "MultiLineString", "coordinates": lines}


def _point_to_segment_distance_m(
    lat: float,
    lng: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    scale = math.cos(math.radians(lat))

    def project(point_lat: float, point_lng: float) -> tuple[float, float]:
        return (
            math.radians(point_lng - lng) * scale * 6_371_000,
            math.radians(point_lat - lat) * 6_371_000,
        )

    sx, sy = project(start[0], start[1])
    ex, ey = project(end[0], end[1])
    seg_x = ex - sx
    seg_y = ey - sy
    seg_len_sq = seg_x * seg_x + seg_y * seg_y
    if seg_len_sq == 0:
        return math.hypot(sx, sy)
    t = max(0.0, min(1.0, (-(sx * seg_x + sy * seg_y)) / seg_len_sq))
    closest_x = sx + t * seg_x
    closest_y = sy + t * seg_y
    return math.hypot(closest_x, closest_y)


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


def _engineering_details(attr: dict) -> dict:
    row_width_ft = _int_or_none(attr.get("ROW_MIN"))
    median_type = _int_or_none(attr.get("MED_TYPE"))
    median_width_ft = _int_or_none(attr.get("MED_WID"))
    access_control = _int_or_none(attr.get("ACES_CTRL"))
    right_turn_lane = _int_or_none(attr.get("RT_TURN_LANE"))
    left_turn_lane = _int_or_none(attr.get("LT_TURN_LANE"))
    accel_decel = _int_or_none(attr.get("ACCEL_DECEL_LANE"))
    lane_width_ft = _int_or_none(attr.get("LANE_WIDTH"))

    row_width = row_width_ft if row_width_ft is not None else MANUAL_REQUIRED
    crossover = _crossover_indicator(
        median_type=median_type,
        median_width_ft=median_width_ft,
        access_control=access_control,
    )
    turn_lane = _turn_lane_indicator(
        right_turn_lane=right_turn_lane,
        left_turn_lane=left_turn_lane,
        accel_decel=accel_decel,
    )
    manual_fields = [
        key for key, value in (
            ("row_width", row_width),
            ("crossover_regulation", crossover["value"]),
            ("turn_lane_presence", turn_lane["value"]),
            ("stacking_length_ft", MANUAL_REQUIRED),
        )
        if value == MANUAL_REQUIRED
    ]

    return {
        "row_width": row_width,
        "row_width_ft": row_width_ft,
        "row_width_source_field": "ROW_MIN" if row_width_ft is not None else None,
        "lane_width_ft": lane_width_ft,
        "median_type_code": median_type,
        "median_type_label": _MEDIAN_TYPE_LABELS.get(median_type),
        "median_width_ft": median_width_ft,
        "access_control_code": access_control,
        "access_control_label": _ACCESS_CONTROL_LABELS.get(access_control),
        "accel_decel_lane_code": accel_decel,
        "accel_decel_lane_label": _ACCEL_DECEL_LABELS.get(accel_decel),
        "right_turn_lane_code": right_turn_lane,
        "right_turn_lane_label": _TURN_LANE_LABELS.get(right_turn_lane),
        "left_turn_lane_code": left_turn_lane,
        "left_turn_lane_label": _TURN_LANE_LABELS.get(left_turn_lane),
        "crossover_regulation": crossover["value"],
        "crossover_regulation_label": crossover["label"],
        "turn_lane_presence": turn_lane["value"],
        "turn_lane_presence_label": turn_lane["label"],
        "stacking_length_ft": MANUAL_REQUIRED,
        "stacking_length_status": (
            "not_in_txdot_inventory; requires TIA/site plan/as-built or field measurement"
        ),
        "engineering_detail_status": {
            "row_width": (
                "txdot_inventory_screened_fact"
                if row_width_ft is not None else MANUAL_REQUIRED
            ),
            "crossover_regulation": crossover["status"],
            "turn_lane_presence": turn_lane["status"],
            "stacking_length_ft": MANUAL_REQUIRED,
        },
        "manual_access_fields": manual_fields,
    }


def _crossover_indicator(
    *,
    median_type: int | None,
    median_width_ft: int | None,
    access_control: int | None,
) -> dict[str, str]:
    if access_control == 1:
        return {
            "value": "full_access_control_screened_indicator",
            "label": "Full access control; driveway/crossover movements are likely restricted.",
            "status": "txdot_inventory_screened_indicator",
        }
    if median_type in {4, 5, 6, 7}:
        return {
            "value": "positive_barrier_median_screened_indicator",
            "label": "Positive-barrier median; crossover openings are likely limited.",
            "status": "txdot_inventory_screened_indicator",
        }
    if median_type == 2:
        width = f" ({median_width_ft} ft)" if median_width_ft else ""
        return {
            "value": "unprotected_median_screened_indicator",
            "label": f"Unprotected median{width}; crossover feasibility still depends on access spacing and TxDOT approval.",
            "status": "txdot_inventory_screened_indicator",
        }
    if median_type in {0, 3}:
        median_label = _MEDIAN_TYPE_LABELS.get(median_type, "Known median")
        return {
            "value": "median_type_screened_indicator",
            "label": f"{median_label}; site-specific driveway movements still need access review.",
            "status": "txdot_inventory_screened_indicator",
        }
    if access_control in {2, 3}:
        return {
            "value": "access_control_screened_indicator",
            "label": f"{_ACCESS_CONTROL_LABELS[access_control]}; crossover detail still needs access review.",
            "status": "txdot_inventory_screened_indicator",
        }
    return {
        "value": MANUAL_REQUIRED,
        "label": "Median/crossover conditions are not resolved from TxDOT inventory.",
        "status": MANUAL_REQUIRED,
    }


def _turn_lane_indicator(
    *,
    right_turn_lane: int | None,
    left_turn_lane: int | None,
    accel_decel: int | None,
) -> dict[str, str]:
    present: list[str] = []
    if right_turn_lane in {2, 3, 4}:
        present.append(f"right: {_TURN_LANE_LABELS[right_turn_lane].lower()}")
    if left_turn_lane in {2, 3, 4}:
        present.append(f"left: {_TURN_LANE_LABELS[left_turn_lane].lower()}")
    if accel_decel == 1:
        present.append("acceleration/deceleration lane reported")
    if present:
        return {
            "value": "exclusive_or_auxiliary_turn_lane_reported",
            "label": "; ".join(present),
            "status": "txdot_inventory_screened_indicator",
        }

    known_codes = [
        code for code in (right_turn_lane, left_turn_lane, accel_decel)
        if code is not None
    ]
    if not known_codes:
        return {
            "value": MANUAL_REQUIRED,
            "label": "Turn-lane presence is not resolved from TxDOT inventory.",
            "status": MANUAL_REQUIRED,
        }
    if right_turn_lane == 1 and left_turn_lane == 1:
        return {
            "value": "no_intersections_on_segment_reported",
            "label": "TxDOT reports no intersections on this roadway segment.",
            "status": "txdot_inventory_screened_indicator",
        }
    if right_turn_lane == 6 or left_turn_lane == 6:
        return {
            "value": "turn_restriction_reported",
            "label": "TxDOT reports peak-period turn restrictions on at least one side.",
            "status": "txdot_inventory_screened_indicator",
        }
    if right_turn_lane == 5 or left_turn_lane == 5 or accel_decel == 0:
        labels = [
            label for label in (
                _TURN_LANE_LABELS.get(right_turn_lane),
                _TURN_LANE_LABELS.get(left_turn_lane),
                _ACCEL_DECEL_LABELS.get(accel_decel),
            )
            if label
        ]
        return {
            "value": "turns_permitted_no_exclusive_lane_reported",
            "label": "; ".join(labels),
            "status": "txdot_inventory_screened_indicator",
        }
    return {
        "value": "turn_lane_code_screened_indicator",
        "label": "TxDOT returned turn-lane codes; verify geometry before design reliance.",
        "status": "txdot_inventory_screened_indicator",
    }


def _engineering_summary(roads: list[dict]) -> dict:
    resolved = {"row_width": 0, "crossover_regulation": 0, "turn_lane_presence": 0}
    unresolved = {"row_width": 0, "crossover_regulation": 0, "turn_lane_presence": 0}
    for road in roads:
        status = road.get("engineering_detail_status") or {}
        if not isinstance(status, dict):
            continue
        for key in resolved:
            if status.get(key) == MANUAL_REQUIRED:
                unresolved[key] += 1
            elif status.get(key):
                resolved[key] += 1
    return {
        "source": "TxDOT Roadway Inventory",
        "source_fields": [
            "ROW_MIN",
            "MED_TYPE",
            "MED_WID",
            "ACES_CTRL",
            "ACCEL_DECEL_LANE",
            "RT_TURN_LANE",
            "LT_TURN_LANE",
            "LANE_WIDTH",
        ],
        "resolved_counts": resolved,
        "unresolved_counts": unresolved,
        "still_requires_plan_or_field_verification": [
            "stacking_length_ft",
            "turn_lane_storage_length",
            "site_specific_driveway_spacing_and_movements",
            "final TxDOT/local access approval",
        ],
        "read": (
            "TxDOT Roadway Inventory can screen ROW width, median/access-control, "
            "lane width, and turn-lane presence where populated. It does not "
            "provide stacking/storage length or approve site-specific driveway movements."
        ),
    }


def _resolved_engineering_count(row: dict) -> int:
    status = row.get("engineering_detail_status") or {}
    if not isinstance(status, dict):
        return 0
    return sum(
        1 for value in status.values()
        if value and value != MANUAL_REQUIRED
    )


def _int_or_none(value) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


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
