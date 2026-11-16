"""USGS 3DEP parcel elevation screening via the free EPQS API."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import requests
from shapely.geometry import shape

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "usgs_elevation"
_EPQS_URL = "https://epqs.nationalmap.gov/v1/json"
_TIMEOUT_S = 20
_MAX_SAMPLES = 5


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for USGS elevation screening")

    sess = http_session(session)
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, (lat, lng) in enumerate(_sample_points(identity)):
        try:
            response = sess.get(
                _EPQS_URL,
                params={
                    "x": lng,
                    "y": lat,
                    "units": "Feet",
                    "wkid": 4326,
                    "includeDate": "false",
                },
                timeout=_TIMEOUT_S,
            )
            response.raise_for_status()
            payload = response.json()
            value = float(payload["value"])
            if value <= -1_000_000:
                raise ValueError("USGS returned a no-data elevation")
            samples.append({
                "kind": "center" if index == 0 else "parcel_boundary",
                "lat": round(lat, 8),
                "lng": round(lng, 8),
                "elevation_ft": round(value, 1),
                "resolution_m": payload.get("resolution"),
            })
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc)[:180])

    if not samples:
        return _failed(
            "USGS Elevation Point Query Service returned no usable samples",
            errors=errors,
        )

    elevations = [row["elevation_ft"] for row in samples]
    center = samples[0]
    max_grade = _maximum_sampled_grade_pct(samples)
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "screened",
            "center_elevation_ft": center["elevation_ft"],
            "min_elevation_ft": min(elevations),
            "max_elevation_ft": max(elevations),
            "relief_ft": round(max(elevations) - min(elevations), 1),
            "sampled_max_grade_pct": max_grade,
            "sampled_grade_basis": (
                "Maximum pairwise grade among stored public DEM samples"
                if max_grade is not None else None
            ),
            "sample_count": len(samples),
            "samples": samples,
            "source": "USGS 3DEP Elevation Point Query Service",
            "source_url": "https://apps.nationalmap.gov/epqs/",
            "advisory": (
                "Interpolated public DEM screening, not a boundary survey, "
                "topographic survey, finished-floor elevation, or drainage design."
            ),
        },
        advisories=(
            [f"{len(errors)} of {_MAX_SAMPLES} elevation samples were unavailable."]
            if errors else []
        ),
        fetched_at=datetime.now(UTC),
        identifier_used=(
            "parcel_geometry" if identity.parcel_geometry else "lat_lng"
        ),
    )


def _sample_points(identity: PropertyIdentity) -> list[tuple[float, float]]:
    geometry = identity.parcel_geometry or {}
    points: list[tuple[float, float]] = []
    if geometry:
        try:
            centroid = shape(geometry).centroid
            if not centroid.is_empty:
                points.append((float(centroid.y), float(centroid.x)))
        except (TypeError, ValueError):
            pass
    if not points:
        points.append((float(identity.lat), float(identity.lng)))
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon" and coordinates:
        ring = coordinates[0]
    elif geometry.get("type") == "MultiPolygon" and coordinates:
        ring = coordinates[0][0] if coordinates[0] else []
    else:
        ring = []
    valid = [
        (float(point[1]), float(point[0]))
        for point in ring
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if valid:
        step = max(1, len(valid) // (_MAX_SAMPLES - 1))
        for point in valid[::step]:
            if point not in points:
                points.append(point)
            if len(points) >= _MAX_SAMPLES:
                break
    return points


def _maximum_sampled_grade_pct(
    samples: list[dict[str, Any]],
) -> float | None:
    maximum: float | None = None
    for left_index, left in enumerate(samples):
        for right in samples[left_index + 1:]:
            horizontal_ft = _haversine_ft(
                float(left["lat"]),
                float(left["lng"]),
                float(right["lat"]),
                float(right["lng"]),
            )
            if horizontal_ft < 1:
                continue
            grade = (
                abs(float(left["elevation_ft"]) - float(right["elevation_ft"]))
                / horizontal_ft
                * 100
            )
            maximum = grade if maximum is None else max(maximum, grade)
    return round(maximum, 1) if maximum is not None else None


def _haversine_ft(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
) -> float:
    radius_ft = 20_902_231
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return 2 * radius_ft * math.asin(min(1.0, math.sqrt(a)))


def _failed(reason: str, *, errors: list[str] | None = None) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts={
            "status": "unavailable",
            "source": "USGS 3DEP Elevation Point Query Service",
            "source_url": "https://apps.nationalmap.gov/epqs/",
            "advisory": (
                "Numerical elevation screening is temporarily unavailable; "
                "the hillshade layer may still show relative terrain."
            ),
        },
        errors=[reason, *(errors or [])][:5],
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
    )


__all__ = ["fetch"]
