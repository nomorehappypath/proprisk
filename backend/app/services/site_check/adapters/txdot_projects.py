"""txdot_projects adapter — TxDOT planned/active construction.

Ported from legacy_services/core/public_intel.py
::fetch_txdot_all_projects (the EXTENDED layer legacy_services
actually surfaces in its public-intel dialog). The earlier
basic port returned 5 keys with TYPE_OF_WORK as the
description; legacy_services renders descriptions like
"SH 29 — Intersection & Operational Imprv (At Corridor C
Phase I to .)" with PROJ_CLASS as the descriptor and
LIMITS_FROM/TO appended in parens.

Output dict keys mirror legacy_services items[] exactly so the
reference_app renderer can read the same shape: csj, description,
phase, estimated_let_date (string), actual_let_date,
estimated_cost_usd (float), district, county, distance_mi.

max_records=100 to match legacy_services. The TxDOT_Projects
layer caps server-side around 100 anyway.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.adapters._txdot_common import (
    _TXDOT_PROJECTS,
    arcgis_spatial_query,
    format_epoch,
)
from app.services.site_check.adapters.txdot_project_records import (
    enrich_project_official_records,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "txdot_projects"


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for txdot_projects")
    sess = http_session(session)
    try:
        features = arcgis_spatial_query(
            sess, _TXDOT_PROJECTS,
            identity.lat, identity.lng,
            radius_meters=8047,  # 5 mi to match legacy_services
            return_geometry=True,
            max_records=100,
        )
    except Exception as exc:
        return _failed(f"txdot projects query failed: {exc}")

    items = []
    for feature in features:
        attr = feature.get("attributes") or {}
        coordinate = _geometry_center(feature.get("geometry") or {})
        # legacy_services formula:
        #   desc = f"{HIGHWAY_NUMBER} — {PROJ_CLASS}" if hwy
        #          else (TYPE_OF_WORK or PROJ_CLASS)
        #   if LIMITS_FROM: desc += f" ({LIMITS_FROM} to {LIMITS_TO})"
        hwy = (attr.get("HIGHWAY_NUMBER") or "").strip()
        proj_class = (attr.get("PROJ_CLASS") or "").strip()
        tow = (attr.get("TYPE_OF_WORK") or "").strip()
        lf = (attr.get("LIMITS_FROM") or "").strip()
        lt = (attr.get("LIMITS_TO") or "").strip()

        if hwy:
            desc = f"{hwy} — {proj_class}" if proj_class else hwy
        else:
            desc = tow or proj_class or "Unknown project"

        if lf:
            limits = f"{lf} to {lt}" if lt else lf
            desc = f"{desc} ({limits})"

        phase = (
            (attr.get("PT_PHASE") or "").strip()
            or (attr.get("PROJ_STG") or "").strip()
            or (attr.get("PROJ_STAT") or "").strip()
        )

        let_raw = attr.get("PROJ_ESTMTD_LET_D")
        actual_let = attr.get("ACTUAL_LET_DATE")
        cost = attr.get("EST_CONSTRUCTION_COST")

        item = {
            "csj": str(
                attr.get("CONTROL_SECT_JOB")
                or attr.get("CSJ_TEXT")
                or ""
            ),
            "description": str(desc).strip(),
            "phase": phase,
            "work_type": tow or None,
            "limits_from": lf or None,
            "limits_to": lt or None,
            "estimated_let_date": format_epoch(let_raw),
            "scheduled_let_date": _format_date(let_raw),
            "actual_let_date": _format_date(actual_let),
            "commission_award_date": _format_date(
                attr.get("COMMISSION_AWARD_OF_CONTRACT")
            ),
            "estimated_cost_usd": (
                float(cost)
                if isinstance(cost, (int, float)) and cost
                else None
            ),
            "district": (attr.get("DISTRICT_NAME") or "").strip(),
            "district_number": attr.get("DISTRICT_NUMBER"),
            "county": (attr.get("COUNTY_NAME") or "").strip(),
            "project_id": (attr.get("PROJECT_ID") or "").strip(),
            "project_stage": (attr.get("PROJ_STG") or "").strip() or None,
            "project_status": (attr.get("PROJ_STAT") or "").strip() or None,
            "let_type": (attr.get("LET_TYPE_DSCR") or "").strip() or None,
            "project_length_mi": (
                float(attr["PROJ_LENGTH"])
                if isinstance(attr.get("PROJ_LENGTH"), (int, float))
                else None
            ),
            "mpo": (attr.get("MPO_NM") or "").strip() or None,
            "distance_mi": (
                round(_haversine_mi(
                    identity.lat, identity.lng, coordinate[0], coordinate[1],
                ), 2)
                if coordinate else None
            ),
            "lat": coordinate[0] if coordinate else None,
            "lng": coordinate[1] if coordinate else None,
            "geometry_basis": "public_project_geometry_center" if coordinate else None,
        }
        items.append(enrich_project_official_records(item, sess))

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT
            if items else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            # legacy_services-parity shape: count_within_radius +
            # items[] keyed by description / phase / etc. The
            # earlier `projects` key is kept as an alias so any
            # downstream consumer that read the old dict shape
            # doesn't break — but new consumers (reference_app
            # accordion, AI-gateway prompts) should read items.
            "count_within_radius": len(items),
            "items": items,
            "projects": items,  # legacy alias
            "source": "TxDOT",
        },
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


def _geometry_center(geometry: dict) -> tuple[float, float] | None:
    coordinates = []
    if "x" in geometry and "y" in geometry:
        return float(geometry["y"]), float(geometry["x"])
    for key in ("paths", "rings", "points"):
        groups = geometry.get(key) or []
        if key == "points":
            groups = [groups]
        for group in groups:
            coordinates.extend(
                point for point in group
                if isinstance(point, (list, tuple)) and len(point) >= 2
            )
    if not coordinates:
        return None
    lng = sum(float(point[0]) for point in coordinates) / len(coordinates)
    lat = sum(float(point[1]) for point in coordinates) / len(coordinates)
    return lat, lng


def _haversine_mi(
    lat1: float, lng1: float, lat2: float, lng2: float,
) -> float:
    radius_mi = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return radius_mi * 2 * math.asin(min(1.0, math.sqrt(value)))


def _format_date(value: object) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return (
                    datetime.strptime(stripped[:10], pattern)
                    .replace(tzinfo=UTC)
                    .date()
                    .isoformat()
                )
            except ValueError:
                continue
        return stripped[:10]
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None
