"""Colorado's road inventory for the Roads card (spec 2.6).

Source: FHWA HPMS Colorado 2025 (class, lanes, speed, ownership, median,
access control, turn lanes). One entry per named road, the section nearest
the site. HPMS has no right-of-way width, so it is "not published". Code
tables are the HPMS Field Manual's (median type differs from Texas's).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.regions import arcgis
from app.regions.co.sources import _roads as r
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "txdot_roadway"
PROVIDER_NAME = "FHWA Highway Performance Monitoring System (Colorado, submitted by CDOT)"
PROVIDER_SHORT = "FHWA HPMS"
VERSION = 1
LAYER = r.HPMS_LAYER
RADIUS_M = 2000
MANUAL = "manual_verification_required"
_FIELDS = (
    "route_name,f_system,facility_type,ownership,through_lanes,speed_limit,"
    "lane_width,median_type,median_width,turn_lanes_r,turn_lanes_l,"
    "access_control,surface_type,data_year"
)

MEDIAN = {
    1: "No median", 2: "Unprotected median", 3: "Curbed median",
    4: "Positive barrier - unspecified", 5: "Positive barrier - flexible",
    6: "Positive barrier - semi-rigid", 7: "Positive barrier - rigid",
}
ACCESS = {1: "Full access control", 2: "Partial access control", 3: "No access control"}
TURN = {
    1: "No intersections exist on the segment",
    2: "Multiple exclusive turning lanes",
    3: "Continuous exclusive turning lane",
    4: "Single exclusive turning lane",
    5: "Turns permitted; no exclusive turning lanes",
    6: "Turns not permitted during the peak period",
}
OWNERSHIP = {
    1: "State highway agency", 2: "County highway agency",
    3: "Town or township highway agency", 4: "City or municipal highway agency",
}


def _good(value, low, high):
    number = r.int_or_none(value)
    return number if number is not None and low <= number <= high else None


def _engineering(row: dict) -> dict:
    name = (row.get("route_name") or "").upper()
    f_system = r.int_or_none(row.get("f_system"))
    median = _good(row.get("median_type"), 1, 7)
    median_w = _good(row.get("median_width"), 1, 200)
    access = _good(row.get("access_control"), 1, 3)
    freeway = f_system in (1, 2) or bool(re.search(r"\b(FWY|FREEWAY|EXPY|EXPRESSWAY)\b", name))
    if access == 3 and freeway:
        access = None  # a freeway cannot have "no access control": bad record
    right = _good(row.get("turn_lanes_r"), 1, 6)
    left = _good(row.get("turn_lanes_l"), 1, 6)
    lane_w = _good(row.get("lane_width"), 6, 20)

    crossover = _crossover(median, median_w, access)
    turn = _turn_lane(right, left)
    manual = [k for k, v in (
        ("row_width", MANUAL), ("crossover_regulation", crossover["value"]),
        ("turn_lane_presence", turn["value"]), ("stacking_length_ft", MANUAL),
    ) if v == MANUAL]
    return {
        "row_width": MANUAL,
        "row_width_ft": None,
        "row_width_source_field": None,
        "lane_width_ft": lane_w,
        "median_type_code": median,
        "median_type_label": MEDIAN.get(median),
        "median_width_ft": median_w,
        "access_control_code": access,
        "access_control_label": ACCESS.get(access),
        "accel_decel_lane_code": None,
        "accel_decel_lane_label": None,
        "right_turn_lane_code": right,
        "right_turn_lane_label": TURN.get(right),
        "left_turn_lane_code": left,
        "left_turn_lane_label": TURN.get(left),
        "crossover_regulation": crossover["value"],
        "crossover_regulation_label": crossover["label"],
        "turn_lane_presence": turn["value"],
        "turn_lane_presence_label": turn["label"],
        "stacking_length_ft": MANUAL,
        "stacking_length_status": (
            "not_in_hpms; requires TIA/site plan/as-built or field measurement"
        ),
        "engineering_detail_status": {
            "row_width": MANUAL,
            "crossover_regulation": crossover["status"],
            "turn_lane_presence": turn["status"],
            "stacking_length_ft": MANUAL,
        },
        "manual_access_fields": manual,
    }


def _crossover(median, width, access) -> dict:
    ind = "hpms_screened_indicator"
    if access == 1:
        return {"value": "full_access_control_screened_indicator", "status": ind,
                "label": "Full access control; driveway/crossover movements are likely restricted."}
    if median in {4, 5, 6, 7}:
        return {"value": "positive_barrier_median_screened_indicator", "status": ind,
                "label": "Positive-barrier median; crossover openings are likely limited."}
    if median == 2:
        w = f" ({width} ft)" if width else ""
        return {"value": "unprotected_median_screened_indicator", "status": ind,
                "label": f"Unprotected median{w}; crossover feasibility still depends on access spacing and approval."}
    if median in {1, 3}:
        return {"value": "median_type_screened_indicator", "status": ind,
                "label": f"{MEDIAN[median]}; site-specific driveway movements still need access review."}
    if access in {2, 3}:
        return {"value": "access_control_screened_indicator", "status": ind,
                "label": f"{ACCESS[access]}; crossover detail still needs access review."}
    return {"value": MANUAL, "status": MANUAL,
            "label": "Median/crossover conditions are not published in the HPMS inventory."}


def _turn_lane(right, left) -> dict:
    ind = "hpms_screened_indicator"
    present = []
    if right in {2, 3, 4}:
        present.append(f"right: {TURN[right].lower()}")
    if left in {2, 3, 4}:
        present.append(f"left: {TURN[left].lower()}")
    if present:
        return {"value": "exclusive_or_auxiliary_turn_lane_reported", "status": ind,
                "label": "; ".join(present)}
    if right is None and left is None:
        return {"value": MANUAL, "status": MANUAL,
                "label": "Turn-lane presence is not published in the HPMS inventory."}
    if right == 1 and left == 1:
        return {"value": "no_intersections_on_segment_reported", "status": ind,
                "label": "HPMS reports no intersections on this roadway section."}
    if 6 in (right, left):
        return {"value": "turn_restriction_reported", "status": ind,
                "label": "HPMS reports peak-period turn restrictions on at least one side."}
    if 5 in (right, left):
        return {"value": "turns_permitted_no_exclusive_lane_reported", "status": ind,
                "label": "; ".join(TURN[c] for c in (right, left) if c)}
    return {"value": "turn_lane_code_screened_indicator", "status": ind,
            "label": "HPMS returned turn-lane codes; verify geometry before design reliance."}


def _summary(roads: list[dict]) -> dict:
    keys = ("row_width", "crossover_regulation", "turn_lane_presence")
    resolved = dict.fromkeys(keys, 0)
    unresolved = dict.fromkeys(keys, 0)
    for road in roads:
        status = road.get("engineering_detail_status") or {}
        for key in keys:
            if status.get(key) == MANUAL:
                unresolved[key] += 1
            elif status.get(key):
                resolved[key] += 1
    return {
        "source": "FHWA HPMS (Colorado)",
        "source_fields": [
            "median_type", "median_width", "access_control", "turn_lanes_r",
            "turn_lanes_l", "lane_width",
        ],
        "resolved_counts": resolved,
        "unresolved_counts": unresolved,
        "still_requires_plan_or_field_verification": [
            "row_width_ft", "stacking_length_ft", "turn_lane_storage_length",
            "site_specific_driveway_spacing_and_movements",
            "final CDOT/local access approval",
        ],
        "read": (
            "The federal road inventory for Colorado (HPMS) can screen class, "
            "lanes, speed limit, ownership and, where sampled, median, access "
            "control, lane width and turn lanes. It does not publish "
            "right-of-way width, stacking length, or driveway approvals."
        ),
    }


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return r.failed(SOURCE_ID, "lat/lng required for txdot_roadway")
    lat, lng = identity.lat, identity.lng
    try:
        rows = arcgis.query_near(
            LAYER, lat, lng, RADIUS_M, where="route_name IS NOT NULL",
            fields=_FIELDS, keep_geometry=True, session=session, max_records=2000,
        )
    except Exception as exc:
        return r.failed(SOURCE_ID, f"HPMS roadway query failed: {exc}")

    best: dict[str, tuple[float, dict]] = {}
    data_year = ""
    for row in rows:
        if r.int_or_none(row.get("facility_type")) in (4, 6, 7):  # ramp, non-inventory, unbuilt
            continue
        name = r.clean_text(row.get("route_name"))
        dist = r.distance_m(lat, lng, row.get("_geometry"))
        if not name or dist is None:
            continue
        data_year = data_year or r.year_of(row.get("data_year"))
        if name not in best or dist < best[name][0]:
            best[name] = (dist, row)

    roads = []
    for name, (dist, row) in best.items():
        speed = _good(row.get("speed_limit"), 5, 85)  # 999 and 0 are not usable speeds
        lanes = _good(row.get("through_lanes"), 1, 20)
        ownership = r.int_or_none(row.get("ownership"))
        eng = _engineering(row)
        geometry = r.line_geojson(row.get("_geometry"))
        roads.append({
            "road_name": name,
            "distance_mi": round(dist / r.MILE_M, 3),
            "functional_class": r.int_or_none(row.get("f_system")) or "",
            "speed_limit": speed or "",
            "num_lanes": lanes or "",
            "surface_type": r.int_or_none(row.get("surface_type")) or "",
            "median_type": eng["median_type_code"] or "",
            "ownership_code": ownership,
            "ownership_label": OWNERSHIP.get(ownership),
            **({"map_geometry": geometry} if geometry else {}),
            **eng,
        })
    roads.sort(key=lambda x: (
        x["distance_mi"], -(x["speed_limit"] or 0), -(x["num_lanes"] or 0), x["road_name"],
    ))
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT if roads else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "roadways": roads,
            "engineering_details": _summary(roads),
            "source": "FHWA HPMS (Colorado)",
            "provider_name": PROVIDER_NAME,
            "provider_short": PROVIDER_SHORT,
            "source_url": LAYER,
            "data_as_of": data_year or "unknown",
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
    )
