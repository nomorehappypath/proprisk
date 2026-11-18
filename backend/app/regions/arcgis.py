"""One shared query for a public ArcGIS map layer (spec 2.6).

State and county source modules call `query_point` (what contains the
site) or `query_near` (what lies within a radius). Results come back as
plain dicts of attributes, with `_distance_m` (from the site to the
feature, metres) and, when asked, `_geometry` in longitude/latitude.
"""

from __future__ import annotations

import math
from typing import Any

import requests

from app.services.site_check.adapters._base import http_session
from app.services.site_check.adapters._resilient_http import (
    arcgis_json_error,
    resilient_json,
)

_MAX_PAGES = 10


def _query(
    url: str,
    params: dict[str, Any],
    session: requests.Session | None,
    max_records: int,
) -> list[dict[str, Any]]:
    sess = http_session(session)
    rows: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        page = dict(params, resultOffset=offset)
        payload = resilient_json(
            sess, "GET", url.rstrip("/") + ("" if url.rstrip("/").endswith("/query") else "/query"),
            params=page, json_error=arcgis_json_error,
        )
        features = payload.get("features") or []
        rows.extend(features)
        if len(rows) >= max_records or not payload.get("exceededTransferLimit") or not features:
            break
        offset += len(features)
    return rows[:max_records]


def _point(lat: float, lng: float) -> str:
    return f"{lng},{lat}"


def _distance_m(lat: float, lng: float, geometry: dict | None) -> float | None:
    if not geometry:
        return None
    pts: list[tuple[float, float]] = []
    if "x" in geometry and "y" in geometry:
        pts = [(geometry["x"], geometry["y"])]
    for key in ("paths", "rings"):
        for part in geometry.get(key) or []:
            pts.extend((p[0], p[1]) for p in part)
    if not pts:
        return None
    best = None
    for x, y in pts:
        d = haversine_m(lat, lng, y, x)
        best = d if best is None or d < best else best
    if "rings" in geometry and point_in_rings(lng, lat, geometry["rings"]):
        return 0.0
    return best


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def point_in_rings(x: float, y: float, rings: list) -> bool:
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
                inside = not inside
    return inside


def _rows(features: list[dict], lat: float, lng: float, keep_geometry: bool) -> list[dict]:
    out = []
    for feature in features:
        row = dict(feature.get("attributes") or {})
        geometry = feature.get("geometry")
        row["_distance_m"] = _distance_m(lat, lng, geometry)
        if keep_geometry and geometry:
            row["_geometry"] = geometry
        out.append(row)
    out.sort(key=lambda r: (r["_distance_m"] is None, r["_distance_m"] or 0))
    return out


def query_point(
    url: str,
    lat: float,
    lng: float,
    *,
    fields: str = "*",
    where: str = "1=1",
    keep_geometry: bool = False,
    session: requests.Session | None = None,
    max_records: int = 50,
) -> list[dict[str, Any]]:
    """Features of the layer that contain (intersect) the site."""
    params = {
        "f": "json", "where": where, "outFields": fields,
        "geometry": _point(lat, lng), "geometryType": "esriGeometryPoint",
        "inSR": 4326, "outSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "returnGeometry": "true" if keep_geometry else "false",
    }
    return _rows(_query(url, params, session, max_records), lat, lng, keep_geometry)


def query_near(
    url: str,
    lat: float,
    lng: float,
    radius_m: float,
    *,
    fields: str = "*",
    where: str = "1=1",
    keep_geometry: bool = False,
    session: requests.Session | None = None,
    max_records: int = 500,
) -> list[dict[str, Any]]:
    """Features within radius_m of the site, nearest first, with distance."""
    params = {
        "f": "json", "where": where, "outFields": fields,
        "geometry": _point(lat, lng), "geometryType": "esriGeometryPoint",
        "inSR": 4326, "outSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_m, "units": "esriSRUnit_Meter",
        "returnGeometry": "true",
    }
    rows = _rows(_query(url, params, session, max_records), lat, lng, True)
    if not keep_geometry:
        for row in rows:
            row.pop("_geometry", None)
    return rows
