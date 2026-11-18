"""Colorado's traffic history for the Traffic card (spec 2.6).

Source: CDOT AADT_All_Years_Data (1997-2025, state highways only). Local
streets have no history here. One count station per state route: the
segment nearest the site. Road names come from HPMS (CDOT only publishes a
route code such as "040C").
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.regions import arcgis
from app.regions.co.sources import _roads as r
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "txdot_aadt_history"
PROVIDER_NAME = "Colorado Department of Transportation"
PROVIDER_SHORT = "CDOT"
VERSION = 1
LAYER = f"{r.CDOT_ROOT}/AADT_All_Years_Data/FeatureServer/0"
RADIUS_M = 5000
FIRST_YEAR, LAST_YEAR = 1997, 2025


def _pct(current, prior):
    if current is None or not prior:
        return None
    return round((current - prior) / prior * 100, 2)


def _direction(pct, band=3.0):
    if pct is None:
        return "unknown"
    return "rising" if pct >= band else "falling" if pct <= -band else "stable"


def _series(row: dict) -> list[dict]:
    points = []
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        value = r.int_or_none(row.get(f"AADT{year % 100:02d}"))
        if value and value > 0:  # 0 means no count that year
            points.append({"year": year, "aadt": value})
    return points


def _trend(points: list[dict]) -> dict:
    if not points:
        return {"available": False, "direction": "unknown", "points": []}
    by_year = {p["year"]: p["aadt"] for p in points}
    year, aadt = points[-1]["year"], points[-1]["aadt"]
    five = _pct(aadt, by_year.get(year - 5))
    return {
        "available": True,
        "latest_year": year,
        "latest_aadt": aadt,
        "change_1yr_pct": _pct(aadt, by_year.get(year - 1)),
        "change_3yr_pct": _pct(aadt, by_year.get(year - 3)),
        "change_5yr_pct": five,
        "direction": _direction(five),
        "points": points[-6:],
    }


def _route_names(codes: set[str], lat: float, lng: float, session) -> dict[str, str]:
    if not codes:
        return {}
    quoted = ",".join(f"'{c}'" for c in sorted(codes))
    try:
        rows = arcgis.query_near(
            r.HPMS_LAYER, lat, lng, RADIUS_M + 500,
            where=f"route_id IN ({quoted}) AND route_name IS NOT NULL",
            fields="route_id,route_name", session=session, max_records=2000,
        )
    except Exception:  # names are a nicety; the route code still identifies the road
        return {}
    names: dict[str, str] = {}
    for row in rows:  # nearest first
        names.setdefault(row["route_id"], r.clean_text(row["route_name"]))
    return names


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return r.failed(SOURCE_ID, "lat/lng required for txdot_aadt_history")
    lat, lng = identity.lat, identity.lng
    try:
        rows = arcgis.query_near(
            LAYER, lat, lng, RADIUS_M, keep_geometry=True,
            session=session, max_records=2000,
        )
    except Exception as exc:
        return r.failed(SOURCE_ID, f"CDOT historical AADT query failed: {exc}")

    nearest: dict[str, dict] = {}
    for row in rows:  # nearest first: keep one segment per route
        route = r.clean_text(row.get("route"))
        if route and route not in nearest and _series(row):
            nearest[route] = row
    names = _route_names(set(nearest), lat, lng, session)

    stations = []
    for route, row in nearest.items():
        series = _series(row)
        point = r.nearest_point(lat, lng, row.get("_geometry"))
        dist = r.distance_m(lat, lng, row.get("_geometry"))
        if point is None or dist is None:
            continue
        stations.append({
            "station_id": f"{route}@{row.get('refPt'):.3f}" if row.get("refPt") is not None else route,
            "on_road_raw": route,
            "on_road": names.get(route) or f"State route {route}",
            "lat": point[0],
            "lng": point[1],
            "distance_m": round(dist),
            "distance_mi": round(dist / r.MILE_M, 2),
            "latest_year": series[-1]["year"],
            "latest_aadt": series[-1]["aadt"],
            "history": series,
            "trend_summary": _trend(series),
        })
    stations.sort(key=lambda s: (s["distance_m"], -s["latest_aadt"]))
    representative = max(stations, key=lambda s: s["latest_aadt"], default=None)
    years = [str(s["latest_year"]) for s in stations]
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR if stations else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.COVERED if stations else CoverageStatus.PARTIAL,
        normalized_facts={
            "source_dataset": "CDOT AADT All Years Data (state highways, 1997-2025)",
            "source_url": LAYER,
            "query_radius_m": RADIUS_M,
            "stations": stations[:20],
            "representative_station": representative,
            "station_count": len(stations),
            "provider_name": PROVIDER_NAME,
            "provider_short": PROVIDER_SHORT,
            "data_as_of": r.year_range(years),
            "coverage_note": (
                "History is published for state highways only. Local streets "
                "have no yearly history in CDOT's data."
            ),
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
        advisories=[
            ("Traffic history comes from CDOT's state-highway counts near the "
             "site. Local streets have no yearly history, and counts dip in "
             "2020. Useful for trends, not proof of how many drivers would "
             "pull in here.")
        ],
    )
