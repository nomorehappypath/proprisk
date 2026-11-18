"""Colorado's traffic counts for the Traffic card (spec 2.6).

Slot name is kept (`txdot_aadt`). Source: FHWA HPMS Colorado 2025 (every
public road with a count; one entry per named road, the count nearest the
site), plus DRCOG's named local counts for roads HPMS did not already list.
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

SOURCE_ID = "txdot_aadt"
PROVIDER_NAME = "FHWA Highway Performance Monitoring System (Colorado, submitted by CDOT)"
PROVIDER_SHORT = "FHWA HPMS"
VERSION = 1
LAYER = r.HPMS_LAYER
DRCOG_LAYER = "https://gis.drcog.org/server/rest/services/RDC/TRAFFIC_COUNTS_2025/MapServer/0"
RADIUS_M = 2000
FRONTAGE_M = 150


def _hpms_counts(rows: list[dict], lat: float, lng: float) -> list[dict]:
    frontage: set[str] = set()
    best: dict[str, dict] = {}
    for row in rows:
        name = r.clean_text(row.get("route_name"))
        if not name:
            continue
        dist = r.distance_m(lat, lng, row.get("_geometry"))
        if dist is not None and dist <= FRONTAGE_M:
            frontage.add(name)
        aadt = r.int_or_none(row.get("aadt"))
        if not aadt or aadt <= 0 or dist is None:
            continue
        if name not in best or dist < best[name]["_d"]:
            best[name] = {"_d": dist, "row": row, "aadt": aadt}
    counts = []
    for name, hit in best.items():
        geometry = hit["row"].get("_geometry")
        point = r.nearest_point(lat, lng, geometry)
        counts.append({
            "road_name": name,
            "aadt": hit["aadt"],
            "year": r.year_of(hit["row"].get("aadt_vd")),
            "source": "FHWA HPMS",
            "is_frontage": name in frontage,
            "map_geometry": r.line_geojson(geometry),
            "lat": point[0] if point else None,
            "lng": point[1] if point else None,
            "distance_mi": round(hit["_d"] / r.MILE_M, 2),
        })
    return counts


def _drcog_counts(rows: list[dict], skip_keys: set[str], lat: float, lng: float) -> list[dict]:
    best: dict[str, dict] = {}
    for row in rows:
        location = r.clean_text(row.get("CountLocat"))
        aadt = r.int_or_none(row.get("Latest_Cnt"))
        if not location or not aadt or aadt <= 0:
            continue
        name = location.split(":")[0].strip()
        key = r.name_key(name)
        if key in skip_keys or (r.route_keys(name) & skip_keys):
            continue
        dist = row.get("_distance_m")
        if key not in best or (dist or 1e9) < (best[key]["_distance_m"] or 1e9):
            best[key] = dict(row, _name=name)
    out = []
    for row in best.values():
        dist = row.get("_distance_m")
        out.append({
            "road_name": row["_name"],
            "aadt": r.int_or_none(row.get("Latest_Cnt")),
            "year": r.year_of(row.get("Latest_Yr")),
            "source": "DRCOG traffic counts",
            "is_frontage": False,
            "map_geometry": None,
            "lat": row.get("Lat"),
            "lng": row.get("Long"),
            "distance_mi": round(dist / r.MILE_M, 2) if dist is not None else None,
        })
    return out


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return r.failed(SOURCE_ID, "lat/lng required for txdot_aadt")
    lat, lng = identity.lat, identity.lng
    warnings: list[str] = []
    try:
        hpms_rows = arcgis.query_near(
            LAYER, lat, lng, RADIUS_M, where="route_name IS NOT NULL",
            fields="route_name,aadt,aadt_vd", keep_geometry=True,
            session=session, max_records=2000,
        )
    except Exception as exc:
        hpms_rows = []
        warnings.append(f"HPMS traffic counts unavailable: {str(exc)[:160]}")
    counts = _hpms_counts(hpms_rows, lat, lng)
    try:
        drcog_rows = arcgis.query_near(
            DRCOG_LAYER, lat, lng, RADIUS_M,
            fields="CountLocat,Latest_Cnt,Latest_Yr,Lat,Long",
            session=session, max_records=500,
        )
    except Exception as exc:
        drcog_rows = []
        warnings.append(f"DRCOG traffic counts unavailable: {str(exc)[:160]}")
    counts += _drcog_counts(drcog_rows, {r.name_key(c["road_name"]) for c in counts}, lat, lng)

    if not counts and warnings:
        return r.failed(SOURCE_ID, "; ".join(warnings))
    counts.sort(key=lambda c: (0 if c["is_frontage"] else 1, -(c["aadt"] or 0)))
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR if warnings
            else DataConfidenceStatus.VERIFIED_PUBLIC_FACT if counts
            else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.PARTIAL if warnings else CoverageStatus.COVERED,
        normalized_facts={
            "traffic_counts": counts,
            "source": PROVIDER_SHORT,
            "source_warnings": warnings,
            "provider_name": PROVIDER_NAME,
            "provider_short": PROVIDER_SHORT,
            "source_url": LAYER,
            "data_as_of": r.year_range([c["year"] for c in counts]),
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
        advisories=warnings or None,
    )
