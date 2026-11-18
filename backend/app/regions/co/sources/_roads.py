"""Small helpers shared by Colorado's five road source modules (spec 2.6).

Not a source itself (the leading underscore keeps `modules_for` away from it).
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

HPMS_LAYER = "https://geo.dot.gov/server/rest/services/Hosted/HPMS_FULL_CO_2025/FeatureServer/0"
CDOT_ROOT = "https://services.arcgis.com/yzB9WM8W0BO3Ql7d/ArcGIS/rest/services"
MILE_M = 1609.344


def failed(slot: str, reason: str) -> AdapterResult:
    return AdapterResult(
        source_id=slot,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={},
        errors=[reason],
        fetched_at=datetime.now(UTC),
    )


def paths(geometry: dict | None) -> list[list[tuple[float, float]]]:
    """ArcGIS polyline/point geometry as lists of (lng, lat)."""
    if not isinstance(geometry, dict):
        return []
    if "x" in geometry and "y" in geometry:
        return [[(float(geometry["x"]), float(geometry["y"]))]]
    out = []
    for path in geometry.get("paths") or []:
        pts = [(float(p[0]), float(p[1])) for p in path if len(p) >= 2]
        if pts:
            out.append(pts)
    return out


def _segment_m(lat: float, lng: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    scale = math.cos(math.radians(lat))

    def proj(p: tuple[float, float]) -> tuple[float, float]:
        return (
            math.radians(p[0] - lng) * scale * 6_371_000,
            math.radians(p[1] - lat) * 6_371_000,
        )

    ax, ay = proj(a)
    bx, by = proj(b)
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length == 0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
    return math.hypot(ax + t * dx, ay + t * dy)


def distance_m(lat: float, lng: float, geometry: dict | None) -> float | None:
    """Shortest distance from the site to a line (point-to-segment)."""
    best = None
    for pts in paths(geometry):
        if len(pts) == 1:
            cands = [_segment_m(lat, lng, pts[0], pts[0])]
        else:
            cands = [_segment_m(lat, lng, a, b) for a, b in pairwise(pts)]
        for d in cands:
            best = d if best is None or d < best else best
    return best


def nearest_point(lat: float, lng: float, geometry: dict | None) -> tuple[float, float] | None:
    """(lat, lng) of the line's vertex closest to the site: a point on the line."""
    best = None
    for pts in paths(geometry):
        for x, y in pts:
            d = (y - lat) ** 2 + ((x - lng) * math.cos(math.radians(lat))) ** 2
            if best is None or d < best[0]:
                best = (d, y, x)
    return (best[1], best[2]) if best else None


def line_geojson(geometry: dict | None) -> dict | None:
    lines = [[[x, y] for x, y in pts] for pts in paths(geometry) if len(pts) >= 2]
    if not lines:
        return None
    if len(lines) == 1:
        return {"type": "LineString", "coordinates": lines[0]}
    return {"type": "MultiLineString", "coordinates": lines}


def epoch_date(value: Any) -> str | None:
    """Epoch milliseconds as YYYY-MM-DD, or None."""
    try:
        if not value:
            return None
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def year_of(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def year_range(years: list[str]) -> str:
    ys = sorted(y for y in years if y)
    if not ys:
        return "unknown"
    return ys[0] if ys[0] == ys[-1] else f"{ys[0]}–{ys[-1]}"


def int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def name_key(name: str) -> str:
    """A road name reduced for matching: 'N LINCOLN ST' == 'LINCOLN ST (SH-40)'."""
    text = re.sub(r"\(.*?\)", " ", (name or "").upper().replace(".", ""))
    text = text.split(":")[0]
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    words = [w for w in text.split() if w not in {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}]
    swaps = {"STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR", "ROAD": "RD"}
    return " ".join(swaps.get(w, w) for w in words)


def route_keys(name: str) -> set[str]:
    """Route labels inside parentheses: 'COLFAX AVE (SH-40)' -> {'SH 40'}."""
    return {
        re.sub(r"[^A-Z0-9]+", " ", m.upper().replace(".", "")).strip()
        for m in re.findall(r"\((.*?)\)", name or "")
    }
