"""Drainage screen — free public elevation turned into an honest local
drainage read (approved contract: docs/specs/feasibility_screens_free_data.md §4).

Sources (all anonymous, $0):
- USGS 3DEP DEM via exportImage (metric EPSG:5070 grid, hard caps).
- USGS NHD flowlines fetched by THIS adapter for the cross-check (the
  stored hydro_intel result has no geometry).
- NOAA Atlas 14 depth-duration-frequency table, attached as design-storm
  CONTEXT only — never combined with depression depth into a storm claim.

Every output is a screened indicator. "Ponding" is deliberately named
topographic depression depth (fill-to-spill): DEM fill minus raw DEM.
Raw elevation ignores culverts, so flow that pools against a road
embankment is flagged "possible culvert/crossing — field check", not
reported as ponding fact. Outfall LEGAL availability is a title/agency
question and always remains an open diligence item.

CONUS-only in v1: EPSG:5070's area of use is the contiguous states.
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np
import requests
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import LineString, Point, box, mapping, shape
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

from app.config.settings import get_settings
from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.adapters._dem_hydrology import (
    HydrologyResult,
    compute_hydrology,
    label_regions,
    slope_percent,
    trace_flow_polylines,
)
from app.services.site_check.adapters._resilient_http import (
    arcgis_json_error,
    resilient_json,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    ManualUploadRequest,
    SourceAccessStatus,
)

log = logging.getLogger("propdesk.site_check.drainage_screen")

SOURCE_ID = "drainage_screen"

_3DEP_EXPORT = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/"
    "3DEPElevation/ImageServer/exportImage"
)
_NHD_FLOWLINE_QUERY = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/"
    "MapServer/6/query"
)
_ATLAS14_CSV = "https://hdsc.nws.noaa.gov/cgi-bin/new/fe_text_mean.csv"

_TO_5070 = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
_TO_4326 = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)

_SQM_PER_ACRE = 4046.8564224


class _DemFetchError(RuntimeError):
    """DEM could not be fetched/validated; message is user-safe."""


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _unavailable(
            "coordinates_required",
            ["lat/lng required for the drainage screen"],
        )

    settings = get_settings()
    lat, lng = float(identity.lat), float(identity.lng)

    if not _in_conus(lat, lng, settings.drainage_screen_conus_bounds):
        return _not_supported_region()

    sess = http_session(session)
    results = (config or {}).get("upstream_results") or {}
    parcel = _parcel_shape(identity, results)

    notes: list[str] = []
    errors: list[str] = []

    # ── Adaptive analysis window ─────────────────────────────────
    window_m = int(settings.drainage_screen_window_m)
    window_max = int(settings.drainage_screen_window_max_m)
    grid = None
    window_bounded = False
    while True:
        try:
            grid = _fetch_dem(sess, lat=lat, lng=lng, window_m=window_m)
        except _DemFetchError as exc:
            errors.append(str(exc))
            return _unavailable("dem_unavailable", errors)
        hydro = compute_hydrology(grid.dem, grid.nodata)
        parcel_mask = _parcel_mask(grid, parcel, lat=lat, lng=lng)
        window_bounded = _parcel_flow_is_window_bounded(
            hydro, parcel_mask,
            min_accum=int(settings.drainage_screen_flowpath_min_accum_cells),
        )
        if not window_bounded or window_m >= window_max:
            break
        window_m = min(window_max, window_m * 2)

    if grid.nodata.any():
        notes.append(
            "Elevation data has gaps in the analysis window; affected "
            "cells were excluded."
        )

    cell = grid.cell_size_m

    # ── Topographic depressions (fill-to-spill) ──────────────────
    depression_mask = (
        hydro.depression_depth
        >= settings.drainage_screen_depression_min_depth_m
    )
    depressions = _regions_to_features(
        grid, depression_mask,
        min_area_m2=settings.drainage_screen_depression_min_area_m2,
        value_grid=hydro.depression_depth,
        max_regions=settings.drainage_screen_max_regions_per_kind,
    )
    # Road-embankment heuristic: depressions whose spill sits against a
    # strong flow path get the culvert advisory downstream in the card.

    # ── Detention candidates inside the parcel ───────────────────
    detention: list[dict[str, Any]] = []
    if parcel_mask is not None and parcel_mask.any():
        slope = slope_percent(grid.dem, cell)
        candidate_mask = (
            parcel_mask
            & ~grid.nodata
            & (slope < settings.drainage_screen_detention_max_slope_pct)
            & (
                hydro.accumulation
                < settings.drainage_screen_detention_max_accum_cells
            )
            & ~depression_mask
        )
        detention = _regions_to_features(
            grid, candidate_mask,
            min_area_m2=settings.drainage_screen_detention_min_area_m2,
            value_grid=None,
            max_regions=settings.drainage_screen_max_regions_per_kind,
        )

    # ── Flow paths + run-on entries ──────────────────────────────
    min_accum = int(settings.drainage_screen_flowpath_min_accum_cells)
    paths = trace_flow_polylines(hydro, min_accumulation=min_accum)
    flow_features = _paths_to_features(
        grid, paths, hydro,
        max_regions=settings.drainage_screen_max_regions_per_kind,
    )
    runon = _runon_entries(grid, hydro, parcel_mask, min_accum=min_accum)
    if window_bounded:
        notes.append(
            "Water enters from an upstream area larger than this screen "
            "analyzed; contributing acreage is a lower bound."
        )

    # ── NHD cross-check (this adapter's own geometry fetch) ──────
    nhd_status, nhd_note = _nhd_cross_check(
        sess, grid, flow_features,
        record_cap=int(settings.drainage_screen_nhd_record_cap),
        agree_distance_m=settings.drainage_screen_nhd_agree_distance_m,
    )
    if nhd_note:
        notes.append(nhd_note)

    # ── Atlas 14 context ─────────────────────────────────────────
    ddf, ddf_note = _atlas14(sess, lat=lat, lng=lng)
    if ddf_note:
        notes.append(ddf_note)

    partial = window_bounded or nhd_status == "partial" or bool(
        grid.nodata.any(),
    )
    status = "partial" if partial else "screened"

    facts: dict[str, Any] = {
        "status": status,
        "analysis_window_m": window_m,
        "cell_size_m": round(cell, 2),
        "crs": "EPSG:5070",
        "topographic_depressions": depressions,
        "detention_candidates": detention,
        "flow_paths": flow_features,
        "runon_entries": runon,
        "runon_window_bounded": window_bounded,
        "nhd_cross_check": nhd_status,
        "atlas14_ddf_inches": ddf,
        "notes": notes,
        "advisory": _ADVISORY,
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=(
            CoverageStatus.PARTIAL if partial else CoverageStatus.COVERED
        ),
        normalized_facts=facts,
        identifier_used="parcel_polygon" if parcel is not None else "lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[_ADVISORY, *notes],
        errors=errors or None,
        manual_upload_request=ManualUploadRequest(
            document_types=[
                "drainage study or preliminary drainage report",
                "grading plan",
                "detention/water-quality design",
                "FEMA elevation certificate",
            ],
            reason=(
                "Bind engineered drainage evidence to the public "
                "elevation screen."
            ),
        ),
    )


_ADVISORY = (
    "Preliminary drainage screen from public elevation data only. "
    "Low spots are fill-to-spill depths, not modeled storm ponding; "
    "public elevation ignores culverts, so flow shown pooling at a road "
    "may pass through an unmapped crossing. Rainfall depths are official "
    "NOAA Atlas 14 context, not a drainage design. Outfall availability, "
    "detention sizing, and floodplain effects require a drainage "
    "engineer and the road/drainage authority."
)


# ─────────────────────────── DEM fetch ───────────────────────────


class _Grid:
    def __init__(self, dem, nodata, x0, y0, dx, dy):
        self.dem = dem
        self.nodata = nodata
        self.x0 = x0  # 5070 x of the LEFT edge
        self.y0 = y0  # 5070 y of the TOP edge
        self.dx = dx
        self.dy = dy

    @property
    def cell_size_m(self) -> float:
        return (self.dx + self.dy) / 2.0

    def cell_center_xy(self, rows, cols):
        x = self.x0 + (np.asarray(cols) + 0.5) * self.dx
        y = self.y0 - (np.asarray(rows) + 0.5) * self.dy
        return x, y


def _fetch_dem(sess, *, lat: float, lng: float, window_m: int) -> _Grid:
    settings = get_settings()
    cell = settings.drainage_screen_cell_size_m
    size_px = max(2, round(window_m / cell))
    if size_px * size_px > settings.drainage_screen_max_pixels:
        raise _DemFetchError(
            "requested analysis grid exceeds the pixel cap "
            f"({size_px}x{size_px})"
        )
    # The bbox is stated DIRECTLY in EPSG:5070 as a true metric square:
    # a 4326 bbox cannot pin an exact metric extent (the server envelopes
    # the reprojected corners, inflating cells ~1.5-3.3% — fails the 1%
    # contract). Live-verified 2026-07-15: bboxSR=5070 echoes the exact
    # requested extent.
    cx, cy = _TO_5070.transform(lng, lat)
    half = window_m / 2.0
    params = {
        "bbox": f"{cx - half},{cy - half},{cx + half},{cy + half}",
        "bboxSR": "5070",
        "imageSR": "5070",
        "size": f"{size_px},{size_px}",
        "format": "tiff",
        "pixelType": "F32",
        "f": "json",
    }
    meta = resilient_json(
        sess, "GET", _3DEP_EXPORT, params=params,
        json_error=arcgis_json_error,
    )
    href = str(meta.get("href") or "")
    extent = meta.get("extent") or {}
    width = int(meta.get("width") or 0)
    height = int(meta.get("height") or 0)
    wkid = ((extent.get("spatialReference") or {}).get("wkid"))
    if wkid != 5070:
        raise _DemFetchError(
            f"elevation service returned CRS {wkid}, expected EPSG:5070"
        )
    if width != size_px or height != size_px:
        raise _DemFetchError("elevation service returned unexpected grid size")

    from urllib.parse import urlparse

    parsed = urlparse(href)
    if parsed.scheme != "https" or parsed.hostname != settings.drainage_screen_dem_host:
        raise _DemFetchError(
            "elevation download host was not the allowlisted USGS server"
        )

    # Redirects DISABLED; hard streamed byte cap.
    send_sess = getattr(sess, "no_transport_retry", sess)
    resp = send_sess.get(href, timeout=None, stream=True, allow_redirects=False)
    if 300 <= resp.status_code < 400:
        raise _DemFetchError("elevation download attempted a redirect")
    resp.raise_for_status()
    cap = settings.drainage_screen_max_download_bytes
    buf = io.BytesIO()
    read = 0
    for chunk in resp.iter_content(chunk_size=1 << 16):
        read += len(chunk)
        if read > cap:
            raise _DemFetchError("elevation download exceeded the byte cap")
        buf.write(chunk)

    import tifffile

    arr = tifffile.imread(io.BytesIO(buf.getvalue()))
    if arr.ndim != 2 or arr.shape != (height, width):
        raise _DemFetchError("decoded elevation grid had an unexpected shape")
    dem = arr.astype(np.float32)

    xmin = float(extent["xmin"])
    xmax = float(extent["xmax"])
    ymin = float(extent["ymin"])
    ymax = float(extent["ymax"])
    dx = (xmax - xmin) / width
    dy = (ymax - ymin) / height
    if abs(dx - cell) / cell > 0.01 or abs(dy - cell) / cell > 0.01:
        raise _DemFetchError(
            "elevation grid cell size deviates from the requested metric "
            f"cell ({dx:.2f}m x {dy:.2f}m vs {cell}m)"
        )
    nodata = ~np.isfinite(dem) | (
        dem <= settings.drainage_screen_dem_nodata_sentinel / 2
    )
    return _Grid(dem, nodata, xmin, ymax, dx, dy)


# ───────────────────── parcel + window helpers ─────────────────────


def _in_conus(lat: float, lng: float, bounds_csv: str) -> bool:
    try:
        lat_min, lat_max, lng_min, lng_max = (
            float(v) for v in bounds_csv.split(",")
        )
    except ValueError:
        lat_min, lat_max, lng_min, lng_max = 24.0, 49.5, -125.0, -66.5
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _parcel_shape(identity: PropertyIdentity, results: dict[str, Any]):
    candidates = [getattr(identity, "parcel_geometry", None)]
    for source_id in ("wcad_parcel", "tcad_parcel"):
        raw = results.get(source_id) if isinstance(results, dict) else None
        facts = raw.get("normalized_facts") if isinstance(raw, dict) else None
        if isinstance(facts, dict):
            candidates.append(facts.get("parcel_polygon"))
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("type") not in {
            "Polygon", "MultiPolygon",
        }:
            continue
        try:
            geom = shape(candidate)
            if not geom.is_valid:
                geom = geom.buffer(0)
            if not geom.is_empty:
                return geom
        except (TypeError, ValueError):
            continue
    return None


def _parcel_mask(grid: _Grid, parcel, *, lat: float, lng: float):
    rows, cols = grid.dem.shape
    if parcel is None:
        # Fall back to a small square around the anchor so detention/
        # run-on logic still has a subject region.
        parcel = Point(lng, lat).buffer(0.0009)  # ~100 m
    projected = shapely_transform(_TO_5070.transform, parcel)
    rr, cc = np.mgrid[0:rows, 0:cols]
    x, y = grid.cell_center_xy(rr.ravel(), cc.ravel())
    inside = contains_xy(projected, x, y).reshape(rows, cols)
    return inside


def _parcel_flow_is_window_bounded(
    hydro: HydrologyResult, parcel_mask, *, min_accum: int,
) -> bool:
    if parcel_mask is None or not parcel_mask.any():
        return False
    strong_on_parcel = parcel_mask & (hydro.accumulation >= min_accum)
    if not strong_on_parcel.any():
        return False
    return bool(hydro.border_influenced[strong_on_parcel].any())


# ───────────────────── geometry extraction ─────────────────────


def _cells_to_polygon(grid: _Grid, labels, label_id):
    rows, cols = np.nonzero(labels == label_id)
    boxes = [
        box(
            grid.x0 + c * grid.dx,
            grid.y0 - (r + 1) * grid.dy,
            grid.x0 + (c + 1) * grid.dx,
            grid.y0 - r * grid.dy,
        )
        for r, c in zip(rows.tolist(), cols.tolist(), strict=True)
    ]
    merged = unary_union(boxes).simplify(grid.cell_size_m / 2)
    return merged


def _to_4326_geojson(geom) -> dict[str, Any]:
    return mapping(shapely_transform(_TO_4326.transform, geom))


def _regions_to_features(
    grid: _Grid,
    mask,
    *,
    min_area_m2: float,
    value_grid,
    max_regions: int,
) -> list[dict[str, Any]]:
    labels, count = label_regions(np.asarray(mask, dtype=bool))
    cell_area = grid.dx * grid.dy
    regions = []
    for label_id in range(1, count + 1):
        area = float((labels == label_id).sum()) * cell_area
        if area < min_area_m2:
            continue
        regions.append((area, label_id))
    regions.sort(reverse=True)
    truncated = len(regions) > max_regions
    output = []
    for area, label_id in regions[:max_regions]:
        feature: dict[str, Any] = {
            "area_acres": round(area / _SQM_PER_ACRE, 3),
        }
        if value_grid is not None:
            region_mask = labels == label_id
            feature["max_depth_ft"] = round(
                float(value_grid[region_mask].max()) * 3.28084, 1,
            )
        feature["map_geometry"] = _to_4326_geojson(
            _cells_to_polygon(grid, labels, label_id),
        )
        output.append(feature)
    if truncated:
        output.append({"truncated": True, "region_cap": max_regions})
    return output


def _paths_to_features(
    grid: _Grid,
    paths: list[list[tuple[int, int]]],
    hydro: HydrologyResult,
    *,
    max_regions: int,
) -> list[dict[str, Any]]:
    cell_area = grid.dx * grid.dy
    ranked = sorted(
        paths,
        key=lambda p: int(hydro.accumulation[p[-1][0], p[-1][1]]),
        reverse=True,
    )
    output = []
    for path in ranked[:max_regions]:
        rows = [r for r, _c in path]
        cols = [c for _r, c in path]
        x, y = grid.cell_center_xy(rows, cols)
        line = LineString(
            list(zip(x.tolist(), y.tolist(), strict=True)),
        ).simplify(grid.cell_size_m)
        end = path[-1]
        output.append({
            "contributing_acres_at_exit": round(
                float(hydro.accumulation[end[0], end[1]])
                * cell_area / _SQM_PER_ACRE, 2,
            ),
            "map_geometry": _to_4326_geojson(line),
        })
    if len(ranked) > max_regions:
        output.append({"truncated": True, "region_cap": max_regions})
    return output


def _runon_entries(
    grid: _Grid, hydro: HydrologyResult, parcel_mask, *, min_accum: int,
) -> list[dict[str, Any]]:
    if parcel_mask is None or not parcel_mask.any():
        return []
    from app.services.site_check.adapters._dem_hydrology import _OFFSETS

    cell_area = grid.dx * grid.dy
    entries = []
    rows, cols = grid.dem.shape
    strong = hydro.accumulation >= min_accum
    for r, c in zip(*np.nonzero(strong & ~parcel_mask), strict=True):
        d = hydro.flow_dir[r, c]
        if d < 0:
            continue
        dr, dc = _OFFSETS[d]
        nr, nc = r + dr, c + dc
        if not (0 <= nr < rows and 0 <= nc < cols):
            continue
        if parcel_mask[nr, nc]:
            x, y = grid.cell_center_xy([nr], [nc])
            point = Point(float(x[0]), float(y[0]))
            entries.append({
                "contributing_acres": round(
                    float(hydro.accumulation[r, c])
                    * cell_area / _SQM_PER_ACRE, 2,
                ),
                "window_bounded": bool(hydro.border_influenced[r, c]),
                "map_geometry": _to_4326_geojson(point),
            })
    entries.sort(key=lambda e: e["contributing_acres"], reverse=True)
    return entries[:10]


# ───────────────────── cross-checks + context ─────────────────────


def _nhd_cross_check(
    sess, grid: _Grid, flow_features, *, record_cap: int,
    agree_distance_m: float,
) -> tuple[str, str | None]:
    """Fetch NHD flowline geometry over the window and compare against
    computed flow paths. Returns (status, note): status is 'agrees',
    'partial', or 'no_mapped_streams'."""
    xmin = grid.x0
    ymax = grid.y0
    xmax = grid.x0 + grid.dem.shape[1] * grid.dx
    ymin = grid.y0 - grid.dem.shape[0] * grid.dy
    sw = _TO_4326.transform(xmin, ymin)
    ne = _TO_4326.transform(xmax, ymax)
    try:
        data = resilient_json(
            sess, "GET", _NHD_FLOWLINE_QUERY,
            params={
                "geometry": f"{sw[0]},{sw[1]},{ne[0]},{ne[1]}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "outSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "returnGeometry": "true",
                "outFields": "gnis_name,ftype",
                "resultRecordCount": record_cap,
                "f": "json",
            },
            json_error=arcgis_json_error,
        )
    except Exception as exc:  # honest degrade, never a silent pass
        log.warning("NHD cross-check failed: %s", exc)
        return "partial", (
            "The public stream network could not be fetched, so computed "
            "flow paths were not cross-checked."
        )
    if data.get("exceededTransferLimit"):
        return "partial", (
            "The public stream network was truncated by the provider, so "
            "the cross-check is incomplete."
        )
    features = data.get("features") or []
    if not features:
        return "no_mapped_streams", None

    computed = [
        shapely_transform(_TO_5070.transform, shape(f["map_geometry"]))
        for f in flow_features
        if isinstance(f.get("map_geometry"), dict)
    ]
    if not computed:
        return "partial", (
            "NHD maps streams in this window but the screen computed no "
            "strong flow path; treat drainage direction as unverified."
        )
    computed_union = unary_union(computed)
    total = 0
    near = 0
    for feat in features:
        paths = (feat.get("geometry") or {}).get("paths") or []
        for path in paths:
            for lng_pt, lat_pt in path[:: max(1, len(path) // 20)]:
                x, y = _TO_5070.transform(lng_pt, lat_pt)
                total += 1
                if computed_union.distance(Point(x, y)) <= agree_distance_m:
                    near += 1
    if total == 0:
        return "no_mapped_streams", None
    if near / total >= 0.5:
        return "agrees", None
    return "partial", (
        "Computed flow paths disagree with the published stream network "
        "in part of the window; treat drainage direction as unverified."
    )


def _atlas14(sess, *, lat: float, lng: float) -> tuple[
    dict[str, dict[str, float]] | None, str | None,
]:
    try:
        send_sess = getattr(sess, "no_transport_retry", sess)
        resp = send_sess.get(
            _ATLAS14_CSV,
            params={
                "lat": f"{lat:.4f}", "lon": f"{lng:.4f}",
                "data": "depth", "units": "english", "series": "pds",
            },
            timeout=None,
        )
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:
        log.warning("Atlas 14 fetch failed: %s", exc)
        return None, (
            "Official rainfall context (NOAA Atlas 14) was unavailable; "
            "drainage geometry is unaffected."
        )
    aris: list[str] = []
    table: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("by duration for ARI"):
            aris = [v.strip() for v in line.split(":,")[1].split(",")]
        elif aris and ":" in line and "," in line:
            duration, _sep, rest = line.partition(":,")
            try:
                depths = [float(v) for v in rest.split(",")]
            except ValueError:
                continue
            if len(depths) == len(aris):
                table[duration.strip()] = {
                    f"{ari}yr": depth
                    for ari, depth in zip(aris, depths, strict=True)
                }
    if not table:
        return None, (
            "Official rainfall context (NOAA Atlas 14) could not be "
            "parsed; drainage geometry is unaffected."
        )
    return table, None


# ───────────────────── unavailable states ─────────────────────


def _unavailable(status: str, errors: list[str]) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={"status": status},
        fetched_at=datetime.now(UTC),
        errors=errors,
    )


def _not_supported_region() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
        normalized_facts={
            "status": "region_not_supported",
            "advisory": (
                "Drainage screening is not yet supported in this region; "
                "coverage is currently limited to the contiguous United "
                "States."
            ),
        },
        fetched_at=datetime.now(UTC),
        advisories=[
            "Drainage screening is not yet supported in this region.",
        ],
    )


__all__ = ["SOURCE_ID", "fetch"]
