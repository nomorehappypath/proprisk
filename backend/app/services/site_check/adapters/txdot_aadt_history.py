"""txdot_aadt_history adapter — historical TxDOT traffic-count stations.

This is gas-station feasibility supplemental context. The default Public Intel
AADT adapter reports current nearby traffic; this adapter queries TxDOT's
public annual AADT station layer to read same-source historical counts for
nearby stations.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.adapters._txdot_common import format_hwy_name
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "txdot_aadt_history"
TXDOT_AADT_HISTORY_URL = (
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services/"
    "TxDOT_TPP_Annual_AADT_Data_(SPM_View)/FeatureServer/0/query"
)
TXDOT_AADT_HISTORY_SERVICE_URL = TXDOT_AADT_HISTORY_URL.rsplit("/query", 1)[0]
_TIMEOUT_S = 45
_HIST_FIELD_RE = re.compile(r"AADT_RPT_HIST_(\d{2})_QTY")


def _num(value, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int | None = None) -> int | None:
    parsed = _num(value)
    if parsed is None:
        return default
    return round(parsed)


def _pct_change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior in {None, 0}:
        return None
    return round(((float(current) - float(prior)) / float(prior)) * 100, 2)


def _trend_direction(pct: float | None, *, stable_band: float = 3.0) -> str:
    if pct is None:
        return "unknown"
    if pct >= stable_band:
        return "rising"
    if pct <= -stable_band:
        return "falling"
    return "stable"


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
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


def _road_label(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return "Unknown"
    return format_hwy_name(value, "")


def _series_from_attrs(attr: dict) -> list[dict]:
    latest_year = _int(attr.get("LATEST_AADT_YR")) or _int(attr.get("AADT_RPT_YEAR"))
    latest_aadt = _int(attr.get("LATEST_AADT_QTY")) or _int(attr.get("AADT_RPT_QTY"))
    points: dict[int, int] = {}
    if latest_year and latest_aadt:
        points[latest_year] = latest_aadt
    report_year = _int(attr.get("AADT_RPT_YEAR")) or latest_year
    report_aadt = _int(attr.get("AADT_RPT_QTY"))
    if report_year and report_aadt:
        points[report_year] = report_aadt
    if report_year:
        for key, value in attr.items():
            match = _HIST_FIELD_RE.fullmatch(str(key))
            if not match:
                continue
            offset = int(match.group(1))
            aadt = _int(value)
            if aadt:
                points[report_year - offset] = aadt
    return [{"year": year, "aadt": points[year]} for year in sorted(points)]


def _trend_summary(points: list[dict]) -> dict:
    if not points:
        return {"available": False, "direction": "unknown", "points": []}
    latest = points[-1]
    by_year = {int(point["year"]): point for point in points}
    latest_year = int(latest["year"])
    prior_1 = by_year.get(latest_year - 1)
    prior_3 = by_year.get(latest_year - 3)
    prior_5 = by_year.get(latest_year - 5)
    pct_5yr = _pct_change(latest.get("aadt"), prior_5.get("aadt") if prior_5 else None)
    return {
        "available": True,
        "latest_year": latest_year,
        "latest_aadt": latest.get("aadt"),
        "change_1yr_pct": _pct_change(latest.get("aadt"), prior_1.get("aadt") if prior_1 else None),
        "change_3yr_pct": _pct_change(latest.get("aadt"), prior_3.get("aadt") if prior_3 else None),
        "change_5yr_pct": pct_5yr,
        "direction": _trend_direction(pct_5yr),
        "points": points[-6:],
    }


def _query_features(
    sess: requests.Session,
    *,
    lat: float,
    lng: float,
    radius_meters: int,
) -> list[dict]:
    d = radius_meters / 111_320.0
    envelope = json.dumps({
        "xmin": lng - d,
        "ymin": lat - d,
        "xmax": lng + d,
        "ymax": lat + d,
        "spatialReference": {"wkid": 4326},
    })
    hist_fields = ",".join(f"AADT_RPT_HIST_{i:02d}_QTY" for i in range(1, 20))
    params = {
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outSR": 4326,
        "outFields": (
            "DIST_NM,CNTY_NM,TRFC_STATN_ID,CATEGORY,ACTIVE,"
            "LATEST_AADT_YR,LATEST_AADT_QTY,AADT_RPT_YEAR,AADT_RPT_QTY,"
            f"{hist_fields},ON_ROAD,COUNT_CYCLE,LATITUDE,LONGITUDE"
        ),
        "returnGeometry": "false",
        "resultRecordCount": 40,
        "f": "json",
    }
    resp = sess.get(TXDOT_AADT_HISTORY_URL, params=params, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error'].get('message', data['error'])}")
    return data.get("features", []) or []


def _station_from_feature(feature: dict, *, lat: float, lng: float) -> dict | None:
    attr = feature.get("attributes") or {}
    station_lat = _num(attr.get("LATITUDE"))
    station_lng = _num(attr.get("LONGITUDE"))
    if station_lat is None or station_lng is None:
        return None
    series = _series_from_attrs(attr)
    if not series:
        return None
    distance_m = _haversine_m(lat, lng, station_lat, station_lng)
    return {
        "station_id": attr.get("TRFC_STATN_ID"),
        "district": attr.get("DIST_NM"),
        "county": attr.get("CNTY_NM"),
        "category": attr.get("CATEGORY"),
        "active": bool(attr.get("ACTIVE")),
        "on_road_raw": attr.get("ON_ROAD"),
        "on_road": _road_label(attr.get("ON_ROAD")),
        "count_cycle": attr.get("COUNT_CYCLE"),
        "lat": station_lat,
        "lng": station_lng,
        "distance_m": round(distance_m),
        "distance_mi": round(distance_m / 1609.344, 2),
        "latest_year": series[-1]["year"],
        "latest_aadt": series[-1]["aadt"],
        "history": series,
        "trend_summary": _trend_summary(series),
    }


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            normalized_facts={},
            errors=["lat/lng required for txdot_aadt_history"],
            fetched_at=datetime.now(UTC),
        )
    sess = http_session(session)
    try:
        features = _query_features(
            sess,
            lat=identity.lat,
            lng=identity.lng,
            radius_meters=5000,
        )
        stations = [
            station
            for feature in features
            if (station := _station_from_feature(feature, lat=identity.lat, lng=identity.lng))
        ]
    except Exception as exc:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.PARTIAL,
            normalized_facts={},
            errors=[f"TxDOT historical AADT query failed: {exc}"],
            fetched_at=datetime.now(UTC),
        )

    stations.sort(
        key=lambda item: (
            0 if item.get("active") else 1,
            item.get("distance_m") or 999_999,
            -int(item.get("latest_aadt") or 0),
        )
    )
    representative = max(
        stations,
        key=lambda item: int(item.get("latest_aadt") or 0),
        default=None,
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if stations else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.COVERED if stations else CoverageStatus.PARTIAL,
        normalized_facts={
            "source_dataset": "TxDOT TPP Annual AADT Data (SPM View)",
            "source_url": TXDOT_AADT_HISTORY_SERVICE_URL,
            "query_radius_m": 5000,
            "stations": stations[:20],
            "representative_station": representative,
            "station_count": len(stations),
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
        advisories=[
            # Plain English (§2/§14): renders on the Traffic card.
            ("Traffic history comes from count stations near the site — "
            "useful for spotting trends, not proof of how many drivers "
            "would actually pull in here.")
        ],
    )


__all__ = ["SOURCE_ID", "fetch"]
