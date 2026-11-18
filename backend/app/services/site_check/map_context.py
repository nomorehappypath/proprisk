"""Read-only map projection for a completed Public Data run.

This module performs no provider calls and no persistence. It normalizes the
latest stored run into a bounded map contract, computes property proximity in
a local projected CRS, and returns only allowlisted public tile definitions.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point, mapping, shape
from shapely.ops import nearest_points, unary_union
from shapely.ops import transform as shapely_transform

from app.models import SiteCheckRun
from app.regions import states as state_names

from .map_contract import (
    SiteCheckMapBasemap,
    SiteCheckMapBounds,
    SiteCheckMapCategory,
    SiteCheckMapContext,
    SiteCheckMapCoordinate,
    SiteCheckMapFeature,
    SiteCheckMapLayer,
    SiteCheckMapPlace,
)

_BASEMAPS = (
    SiteCheckMapBasemap(
        id="aerial_roads",
        label="Aerial + roads",
        kind="aerial",
        tile_url=(
            "https://basemap.nationalmap.gov/arcgis/rest/services/"
            "USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
        ),
        max_zoom=16,
        attribution=(
            "USDA, USGS The National Map: orthoimagery; OpenFreeMap / "
            "OpenMapTiles / OpenStreetMap contributors: roads"
        ),
        provider_label="USGS National Map / USDA NAIP public mosaic",
        resolution_label="Public source resolution varies by location and capture",
        detail_advisory=(
            "At close zoom, the map enlarges source pixels from the best "
            "unrestricted public imagery available; parcel and road overlays "
            "remain vector-sharp."
        ),
        road_vector_url="https://tiles.openfreemap.org/planet",
        road_glyphs_url=(
            "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf"
        ),
    ),
    SiteCheckMapBasemap(
        id="aerial",
        label="Aerial",
        kind="aerial",
        tile_url=(
            "https://basemap.nationalmap.gov/arcgis/rest/services/"
            "USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
        ),
        max_zoom=16,
        attribution="USDA, USGS The National Map: orthoimagery",
        provider_label="USGS National Map / USDA NAIP public mosaic",
        resolution_label="Public source resolution varies by location and capture",
        detail_advisory=(
            "At close zoom, the map enlarges source pixels from the best "
            "unrestricted public imagery available; parcel overlays remain "
            "vector-sharp."
        ),
    ),
    SiteCheckMapBasemap(
        id="terrain",
        label="Terrain",
        kind="terrain",
        tile_url=(
            "https://basemap.nationalmap.gov/arcgis/rest/services/"
            "USGSTopo/MapServer/tile/{z}/{y}/{x}"
        ),
        max_zoom=16,
        attribution="USGS The National Map",
        provider_label="USGS The National Map",
        resolution_label="Cartographic terrain reference",
        detail_advisory=(
            "Terrain is a reference basemap, not close-range aerial imagery."
        ),
    ),
)

_FEMA_TILE = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/export"
    "?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857&size=256%2C256"
    "&format=png32&transparent=true&f=image&layers=show%3A28"
)
_WETLANDS_TILE = (
    "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/"
    "Wetlands/MapServer/export?bbox={bbox-epsg-3857}&bboxSR=3857"
    "&imageSR=3857&size=256%2C256&format=png32&transparent=true"
    "&f=image&layers=show%3A0"
)
_ELEVATION_TILE = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/"
    "ImageServer/exportImage?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857"
    "&size=256%2C256&format=png32&transparent=true&f=image"
    "&renderingRule=%7B%22rasterFunction%22%3A%22Hillshade%20Gray%22%7D"
)

_CATEGORY_META: dict[str, tuple[str, str, str]] = {
    "gas_station": ("Gas stations", "auto", "fuel"),
    "ev_charging": ("EV charging", "auto", "zap"),
    "car_wash": ("Car washes", "auto", "car"),
    "auto_dealer": ("Auto dealers", "auto", "car"),
    "auto_parts": ("Auto parts", "auto", "wrench"),
    "restaurant": ("Restaurants", "food_service", "utensils"),
    "fast_food": ("Fast food", "food_service", "sandwich"),
    "cafe": ("Cafes", "food_service", "coffee"),
    "bar": ("Bars", "food_service", "martini"),
    "hotel": ("Hotels", "lodging", "bed"),
    "hospital": ("Hospitals", "healthcare", "hospital"),
    "clinic": ("Clinics", "healthcare", "stethoscope"),
    "pharmacy": ("Pharmacies", "healthcare", "cross"),
    "dentist": ("Dentists", "healthcare", "heart-pulse"),
    "veterinary": ("Veterinary", "healthcare", "paw-print"),
    "supermarket": ("Grocery", "retail", "shopping-cart"),
    "convenience": ("Convenience", "retail", "store"),
    "shopping_center": ("Shopping", "retail", "shopping-bag"),
    "department": ("Department stores", "retail", "store"),
    "hardware": ("Hardware", "retail", "hammer"),
    "bank": ("Banks", "banking", "landmark"),
    "atm": ("ATMs", "banking", "credit-card"),
    "school": ("Schools", "education", "school"),
    "university": ("Universities", "education", "graduation-cap"),
    "daycare": ("Daycare", "education", "baby"),
    "bus_stop": ("Bus stops", "transit", "bus"),
    "train_station": ("Rail stations", "transit", "train"),
    "parking": ("Parking", "transit", "parking"),
    "apartments": ("Apartments & houses", "residential", "building"),
    "park": ("Parks", "recreation", "trees"),
    "gym": ("Gyms", "recreation", "dumbbell"),
    "sports_facility": ("Sports", "recreation", "trophy"),
    "cinema": ("Cinemas", "entertainment", "film"),
    "theater": ("Theaters", "entertainment", "drama"),
    "event_venue": ("Event venues", "entertainment", "ticket"),
    "truck_stop": ("Truck stops", "industrial", "truck"),
    "self_storage": ("Self storage", "storage", "warehouse"),
    "other": ("Other places", "other", "map-pin"),
}
_DEFAULT_CATEGORIES = {
    "gas_station",
    "restaurant",
    "hotel",
    "school",
    "apartments",
    "parking",
    "bus_stop",
}
_MAX_PLACES = 750


def _facts(run: SiteCheckRun, source_id: str) -> dict[str, Any]:
    result = (run.adapter_results or {}).get(source_id) or {}
    value = result.get("normalized_facts") or {}
    return value if isinstance(value, dict) else {}


def _parcel_shape(run: SiteCheckRun):
    for source_id in ("wcad_parcel", "tcad_parcel"):
        candidate = _facts(run, source_id).get("parcel_polygon")
        if not isinstance(candidate, dict):
            continue
        if candidate.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        try:
            geometry = shape(candidate)
        except (TypeError, ValueError):
            continue
        if geometry.is_empty:
            continue
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if not geometry.is_empty and geometry.geom_type in {"Polygon", "MultiPolygon"}:
            return geometry
    return None


def _center(run: SiteCheckRun, parcel) -> tuple[float, float]:
    if parcel is not None:
        centroid = parcel.centroid
        return float(centroid.y), float(centroid.x)
    if run.lat is None or run.lng is None:
        raise ValueError("Public Data map requires coordinates")
    return float(run.lat), float(run.lng)


def _bounds(parcel, lat: float, lng: float) -> SiteCheckMapBounds:
    min_lat_half = 0.009
    min_lng_half = min_lat_half / max(math.cos(math.radians(lat)), 0.25)
    if parcel is None:
        return SiteCheckMapBounds(
            west=lng - min_lng_half,
            south=lat - min_lat_half,
            east=lng + min_lng_half,
            north=lat + min_lat_half,
        )
    west, south, east, north = parcel.bounds
    center_lng = (west + east) / 2
    center_lat = (south + north) / 2
    half_lng = max((east - west) * 0.75, min_lng_half)
    half_lat = max((north - south) * 0.75, min_lat_half)
    return SiteCheckMapBounds(
        west=center_lng - half_lng,
        south=center_lat - half_lat,
        east=center_lng + half_lng,
        north=center_lat + half_lat,
    )


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_m = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius_m * 2 * math.asin(min(1.0, math.sqrt(value)))


def _parcel_distance(
    parcel,
    *,
    parcel_lat: float,
    parcel_lng: float,
    point_lat: float,
    point_lng: float,
) -> tuple[float, str, SiteCheckMapCoordinate]:
    if parcel is None:
        return (
            _haversine_m(parcel_lat, parcel_lng, point_lat, point_lng),
            "address_pin",
            SiteCheckMapCoordinate(lat=parcel_lat, lng=parcel_lng),
        )

    zone = int((parcel_lng + 180) // 6) + 1
    epsg = 32600 + max(1, min(zone, 60))
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inverse = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    projected_parcel = shapely_transform(forward.transform, parcel)
    projected_point = Point(*forward.transform(point_lng, point_lat))
    distance_m = float(projected_parcel.distance(projected_point))
    _point_on_place, point_on_subject = nearest_points(
        projected_point,
        projected_parcel,
    )
    nearest_lng, nearest_lat = inverse.transform(
        point_on_subject.x,
        point_on_subject.y,
    )
    return (
        distance_m,
        "parcel_boundary",
        SiteCheckMapCoordinate(lat=nearest_lat, lng=nearest_lng),
    )


def _valid_point(row: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat = float(row.get("lat"))
        lng_raw = row.get("lng", row.get("lon"))
        lng = float(lng_raw)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def _official_points(run: SiteCheckRun) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for index, row in enumerate(_facts(run, "schools_intel").get("schools") or []):
        if not isinstance(row, dict) or _valid_point(row) is None:
            continue
        points.append(
            {
                **row,
                "id": f"tea-school:{row.get('school_number') or index}",
                "group": "education",
                "category": "school",
                "source": "Texas Education Agency",
                "confidence": "screened_indicator",
            }
        )
    transit = _facts(run, "transit_intel")
    for key, category, source_label in (
        ("bus_stops", "bus_stop", "Capital Metro"),
        ("rail_stations", "train_station", "Capital Metro"),
    ):
        for index, row in enumerate(transit.get(key) or []):
            if not isinstance(row, dict) or _valid_point(row) is None:
                continue
            points.append(
                {
                    **row,
                    "id": f"cmta:{category}:{row.get('stop_id') or index}",
                    "group": "transit",
                    "category": category,
                    "source": source_label,
                    "confidence": "screened_indicator",
                }
            )
    return points


def _housing_project_points(run: SiteCheckRun) -> list[dict[str, Any]]:
    """Map county residential supply separately from literal apartments."""
    entitlement = _facts(run, "entitlement_pipeline")
    source = str(
        entitlement.get("county_gis_source") or "County subdivision records"
    )[:120]
    points: list[dict[str, Any]] = []
    for index, row in enumerate(entitlement.get("subdivision_cases") or []):
        if not isinstance(row, dict) or _valid_point({
            "lat": row.get("map_lat"), "lng": row.get("map_lng"),
        }) is None:
            continue
        lot_count = row.get("lot_count")
        status = str(row.get("status") or "Recorded subdivision").strip()
        detail = status
        if isinstance(lot_count, int) and lot_count > 0:
            detail = f"{detail} · {lot_count:,} platted lots"
        points.append({
            "id": f"county-subdivision:{row.get('document_number') or row.get('name') or index}",
            "group": "residential",
            "category": "apartments",
            "name": str(row.get("name") or "Nearby housing project")[:160],
            "lat": row["map_lat"],
            "lng": row["map_lng"],
            "address": detail[:240],
            "source": source,
            "confidence": "screened_indicator",
        })
    return points


def _legacy_osm_points(run: SiteCheckRun) -> list[dict[str, Any]]:
    osm = _facts(run, "osm_overpass")
    existing = osm.get("map_points")
    if isinstance(existing, list):
        return [row for row in existing if isinstance(row, dict)]

    points: list[dict[str, Any]] = []
    groups = osm.get("category_groups") or {}
    if not isinstance(groups, dict):
        return points
    for group_name, group_value in groups.items():
        categories = (group_value or {}).get("categories") or {}
        if not isinstance(categories, dict):
            continue
        for category, category_value in categories.items():
            rows = (category_value or {}).get("top_5_within_5mi") or []
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or _valid_point(row) is None:
                    continue
                points.append(
                    {
                        **row,
                        "id": f"legacy-osm:{group_name}:{category}:{index}",
                        "group": group_name,
                        "category": category,
                        "source": "OpenStreetMap",
                        "confidence": "screened_indicator",
                    }
                )
    return points


def _dedupe_points(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[tuple[str, str, int, int]] = set()
    for row in rows:
        coordinate = _valid_point(row)
        if coordinate is None:
            continue
        lat, lng = coordinate
        category = str(row.get("category") or "other")
        name = str(row.get("name") or "Nearby place").strip()[:160]
        stable_id = str(row.get("id") or f"point:{category}:{lat:.7f}:{lng:.7f}")
        name_key = (category, name.casefold(), round(lat * 10_000), round(lng * 10_000))
        if stable_id in seen_ids or name_key in seen_names:
            continue
        seen_ids.add(stable_id)
        seen_names.add(name_key)
        output.append({**row, "id": stable_id, "name": name, "lat": lat, "lng": lng})
        if len(output) >= _MAX_PLACES:
            break
    return output


def _zoning_value(run: SiteCheckRun) -> str | None:
    facts = _facts(run, "austin_zoning")
    jurisdiction = _facts(run, "jurisdiction_resolver")
    jurisdiction_kind = str(
        jurisdiction.get("jurisdiction_kind")
        or facts.get("jurisdiction_kind")
        or ""
    ).casefold()
    if jurisdiction_kind == "etj":
        return None
    for key in ("base_zone", "zoning_type", "zone"):
        value = facts.get(key)
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if cleaned and cleaned.casefold() not in {
            "unknown", "no_city_zoning", "etj",
        } and not cleaned.casefold().endswith("_etj"):
            return cleaned
    return None


def _zoning_jurisdiction_label(run: SiteCheckRun) -> str:
    facts = _facts(run, "austin_zoning")
    resolved = _facts(run, "jurisdiction_resolver")
    jurisdiction_kind = str(
        resolved.get("jurisdiction_kind")
        or facts.get("jurisdiction_kind")
        or "unknown"
    ).casefold()
    raw_city = str(
        resolved.get("city") or facts.get("jurisdiction") or ""
    ).strip()
    if raw_city.casefold().endswith(" etj"):
        raw_city = raw_city[:-4].strip()
    if raw_city.casefold().startswith("city of "):
        raw_city = raw_city[8:].strip()
    city = raw_city.title() if raw_city.isupper() else raw_city

    if jurisdiction_kind == "etj":
        return (
            f"{city} ETJ - outside city limits"
            if city else "ETJ - outside city limits"
        )
    if jurisdiction_kind in {"city", "full_purpose_city"}:
        return f"Inside {city} city limits" if city else "Inside city limits"
    if jurisdiction_kind == "limited_purpose":
        return (
            f"{city} limited-purpose jurisdiction"
            if city else "Limited-purpose city jurisdiction"
        )
    if jurisdiction_kind in {"unincorporated", "unincorporated_county"}:
        return "Outside incorporated city limits"
    raw_jurisdiction = facts.get("jurisdiction")
    if isinstance(raw_jurisdiction, str) and raw_jurisdiction.strip():
        return raw_jurisdiction.strip()
    return "Jurisdiction not verified"


def _zoning_overlay_label(run: SiteCheckRun) -> str | None:
    facts = _facts(run, "austin_zoning")
    resolved = _facts(run, "jurisdiction_resolver")
    jurisdiction_kind = str(
        resolved.get("jurisdiction_kind")
        or facts.get("jurisdiction_kind")
        or "unknown"
    ).casefold()
    jurisdiction_label = _zoning_jurisdiction_label(run)
    zoning = _zoning_value(run)
    if zoning:
        return f"{zoning} - {jurisdiction_label}"
    if jurisdiction_kind == "etj":
        return jurisdiction_label
    if jurisdiction_kind in {"city", "full_purpose_city", "limited_purpose"}:
        return f"{jurisdiction_label}; zoning district not verified"
    return (
        jurisdiction_label
        if jurisdiction_label != "Jurisdiction not verified" else None
    )


def _geojson_geometry(
    row: dict[str, Any],
    allowed_types: set[str],
) -> dict[str, Any] | None:
    geometry = row.get("map_geometry") or row.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in allowed_types:
        return None
    return geometry


def _fmt_number(value: Any, *, digits: int = 0, suffix: str = "") -> str | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    formatted = f"{numeric:,.{digits}f}"
    return f"{formatted}{suffix}"


def _screen_value(value: Any, missing: str = "Not published") -> str:
    if value is None or value == "":
        return missing
    if value == "manual_verification_required":
        return "Manual verification required"
    return str(value)


def _dimension_numeric(standards: dict[str, Any], key: str) -> float | None:
    row = standards.get(key)
    value = row.get("value") if isinstance(row, dict) else row
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dimension_label(standards: dict[str, Any], key: str) -> str | None:
    row = standards.get(key)
    if not isinstance(row, dict):
        return _fmt_number(row)
    value = _fmt_number(row.get("value"), digits=0)
    if value is None:
        return None
    unit = str(row.get("unit") or "").strip()
    code_ref = str(row.get("code_ref") or "").strip()
    parts = [f"{value} {unit}".strip()]
    if code_ref:
        parts.append(code_ref)
    return " / ".join(parts)


def _projected_area_acres(geometry, lat: float, lng: float) -> float:
    zone = int((lng + 180) // 6) + 1
    epsg = 32600 + max(1, min(zone, 60))
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    projected = shapely_transform(forward.transform, geometry)
    return float(projected.area) / 4046.8564224


def _setback_envelope_geojson(
    parcel,
    *,
    lat: float,
    lng: float,
    setback_ft: float,
) -> tuple[dict[str, Any] | None, float | None]:
    zone = int((lng + 180) // 6) + 1
    epsg = 32600 + max(1, min(zone, 60))
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inverse = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    projected = shapely_transform(forward.transform, parcel)
    envelope = projected.buffer(-(setback_ft * 0.3048)) if setback_ft > 0 else projected
    if envelope.is_empty:
        return None, None
    if not envelope.is_valid:
        envelope = envelope.buffer(0)
    if envelope.is_empty:
        return None, None
    output = shapely_transform(inverse.transform, envelope)
    return mapping(output), float(envelope.area) / 4046.8564224


def _roadway_access_features(run: SiteCheckRun) -> list[SiteCheckMapFeature]:
    output: list[SiteCheckMapFeature] = []
    for index, row in enumerate((_facts(run, "txdot_roadway").get("roadways") or [])[:30]):
        if not isinstance(row, dict):
            continue
        geometry = _geojson_geometry(row, {"LineString", "MultiLineString"})
        if geometry is None:
            continue
        road = str(row.get("road_name") or "TxDOT roadway")
        details = [
            {
                "label": "Roadway inventory",
                "value": (
                    f"{_screen_value(row.get('num_lanes'))} lanes / "
                    f"{_screen_value(row.get('speed_limit'))} mph / "
                    f"class {_screen_value(row.get('functional_class'))}"
                ),
            },
            {
                "label": "ROW width",
                "value": (
                    f"{int(row['row_width_ft']):,} ft"
                    if isinstance(row.get("row_width_ft"), (int, float))
                    else "Manual verification required"
                ),
            },
            {
                "label": "Median / access control",
                "value": (
                    f"{_screen_value(row.get('median_type_label'))}; "
                    f"{_screen_value(row.get('access_control_label'))}"
                ),
            },
            {
                "label": "Turn-lane screen",
                "value": _screen_value(
                    row.get("turn_lane_presence_label")
                    or row.get("turn_lane_presence")
                ),
            },
            {
                "label": "Driveways, signals, stacking",
                "value": "Not approved by inventory; verify with plans and field review",
            },
            {
                "label": "Future ROW / dedication",
                "value": "Screen TxDOT projects, plats, and final road authority comments",
            },
        ]
        if row.get("distance_mi") is not None:
            details.insert(0, {
                "label": "Approx. distance",
                "value": f"{float(row['distance_mi']):.2f} mi",
            })
        output.append(SiteCheckMapFeature(
            id=f"txdot-roadway:{index}:{road}",
            kind="access_roadway",
            title=road,
            subtitle="TxDOT Roadway Inventory access screen",
            source=str(row.get("source") or "TxDOT Roadway Inventory"),
            confidence="screened_indicator",
            geometry=geometry,
            details=details,
            map_properties={
                "road_name": road,
                "row_width_ft": row.get("row_width_ft"),
                "access_class": "roadway_inventory",
            },
        ))
    return output


def _linear_parts(geometry) -> list[Any]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        return [
            part
            for child in geometry.geoms
            for part in _linear_parts(child)
        ]
    return []


def _subject_frontage_features(
    parcel,
    road_features: list[SiteCheckMapFeature],
    *,
    lat: float,
    lng: float,
) -> list[SiteCheckMapFeature]:
    """Screen parcel-edge proximity to published road centerlines.

    The output deliberately says "mapped frontage" rather than legal access:
    public road centerlines do not establish a curb cut, access easement, or
    driveway permit.
    """
    zone = int((lng + 180) // 6) + 1
    epsg = 32600 + max(1, min(zone, 60))
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inverse = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    projected_parcel = shapely_transform(forward.transform, parcel)
    if projected_parcel.is_empty:
        return []

    # TxDOT AADT and Roadway Inventory often contain the same road with slightly
    # different geometry. Keep the geometry closest to the subject per road.
    nearest_by_road: dict[str, tuple[float, SiteCheckMapFeature, Any]] = {}
    for feature in road_features:
        road_name = str(feature.map_properties.get("road_name") or feature.title).strip()
        if not road_name:
            continue
        try:
            projected_road = shapely_transform(
                forward.transform, shape(feature.geometry),
            )
        except (TypeError, ValueError):
            continue
        if projected_road.is_empty:
            continue
        distance_m = float(projected_parcel.distance(projected_road))
        key = " ".join(road_name.casefold().split())
        current = nearest_by_road.get(key)
        if current is None or distance_m < current[0]:
            nearest_by_road[key] = (distance_m, feature, projected_road)

    output: list[SiteCheckMapFeature] = []
    for index, (distance_m, feature, projected_road) in enumerate(
        sorted(nearest_by_road.values(), key=lambda item: item[0])
    ):
        row_width = feature.map_properties.get("row_width_ft")
        width_ft = float(row_width) if isinstance(row_width, (int, float)) else 0.0
        screen_distance_ft = max(125.0, min(250.0, width_ft / 2 + 35.0))
        if feature.map_properties.get("is_frontage"):
            screen_distance_ft = max(screen_distance_ft, 175.0)
        if distance_m > screen_distance_ft * 0.3048:
            continue

        screened_edge = projected_parcel.boundary.intersection(
            projected_road.buffer(screen_distance_ft * 0.3048),
        )
        parts = [
            part for part in _linear_parts(screened_edge)
            if part.length >= 15 * 0.3048
        ]
        if not parts:
            continue
        merged = unary_union(parts)
        frontage_length_ft = float(sum(part.length for part in parts)) / 0.3048
        road_name = str(feature.map_properties.get("road_name") or feature.title)
        mapped_geometry = shapely_transform(inverse.transform, merged)
        output.append(SiteCheckMapFeature(
            id=f"subject-frontage:{index}:{road_name}",
            kind="access_subject_frontage",
            title=f"Mapped frontage on {road_name}",
            subtitle=f"About {frontage_length_ft:,.0f} ft of parcel edge near roadway",
            source=feature.source,
            confidence="screened_indicator",
            geometry=mapping(mapped_geometry),
            details=[
                {
                    "label": "Approx. mapped frontage",
                    "value": f"{frontage_length_ft:,.0f} ft",
                },
                {
                    "label": "Road centerline proximity",
                    "value": f"Nearest mapped line is {distance_m / 0.3048:,.0f} ft from parcel",
                },
                {
                    "label": "What this establishes",
                    "value": "Parcel-edge proximity to published roadway geometry only",
                },
                {
                    "label": "What still requires verification",
                    "value": "Legal access, curb-cut location, permit, spacing, median, and future ROW",
                },
            ],
            map_properties={
                "road_name": road_name,
                "access_class": "subject_frontage",
                "frontage_length_ft": round(frontage_length_ft),
                "display_label": road_name,
            },
        ))
    return output


def _physical_utility_features(run: SiteCheckRun) -> list[SiteCheckMapFeature]:
    output: list[SiteCheckMapFeature] = []
    candidates = [
        ("water_mains", "utility_main_water", "Water main"),
        ("wastewater_mains", "utility_main_wastewater", "Wastewater main"),
        ("sewer_mains", "utility_main_wastewater", "Sewer main"),
        ("gravity_mains", "utility_main_wastewater", "Gravity sewer main"),
        ("force_mains", "utility_main_wastewater", "Force main"),
        ("gas_pipelines", "utility_gas_pipeline", "Gas pipeline"),
    ]
    for source_id in ("utility_infrastructure", "water_wastewater_infrastructure"):
        facts = _facts(run, source_id)
        for key, kind, default_title in candidates:
            for index, row in enumerate((facts.get(key) or [])[:30]):
                if not isinstance(row, dict):
                    continue
                geometry = _geojson_geometry(row, {"LineString", "MultiLineString"})
                if geometry is None:
                    continue
                output.append(SiteCheckMapFeature(
                    id=f"{source_id}:{key}:{index}",
                    kind=kind,
                    title=str(row.get("label") or row.get("name") or default_title)[:240],
                    subtitle=str(row.get("provider") or row.get("owner") or "") or None,
                    source=str(row.get("source") or source_id),
                    confidence=str(row.get("confidence") or "screened_indicator"),
                    geometry=geometry,
                    details=[
                        {"label": "Published utility geometry", "value": default_title},
                        {
                            "label": "Capacity / tie-in",
                            "value": "Not established by map geometry",
                        },
                    ],
                ))
        for index, row in enumerate((facts.get("lift_stations") or [])[:20]):
            if not isinstance(row, dict):
                continue
            geometry = _geojson_geometry(row, {"Point"})
            if geometry is None:
                continue
            output.append(SiteCheckMapFeature(
                id=f"{source_id}:lift_station:{index}",
                kind="utility_lift_station",
                title=str(row.get("label") or row.get("name") or "Lift station")[:240],
                subtitle=str(row.get("provider") or row.get("owner") or "") or None,
                source=str(row.get("source") or source_id),
                confidence=str(row.get("confidence") or "screened_indicator"),
                geometry=geometry,
                details=[
                    {"label": "Published utility geometry", "value": "Lift station"},
                    {
                        "label": "Capacity / service availability",
                        "value": "Not established by map geometry",
                    },
                ],
            ))
    return output


def _site_constraint_features(run: SiteCheckRun, *, parcel=None) -> list[SiteCheckMapFeature]:
    facts = _facts(run, "site_constraints")
    output: list[SiteCheckMapFeature] = []

    def map_kind(row: dict[str, Any], default_kind: str) -> str:
        raw_kind = str(row.get("kind") or default_kind)
        return {
            "gas_transmission_pipeline": "utility_gas_pipeline",
            "edwards_aquifer": "environment_edwards_aquifer",
            "critical_habitat": "environment_critical_habitat",
        }.get(raw_kind, raw_kind)

    def add_rows(
        rows: Any,
        *,
        default_kind: str,
        allowed_types: set[str],
    ) -> None:
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            geometry = _geojson_geometry(row, allowed_types)
            if geometry is None:
                continue
            details: list[dict[str, str]] = []
            if isinstance(row.get("distance_ft"), (int, float)):
                details.append({
                    "label": "Approx. distance from subject",
                    "value": f"{float(row['distance_ft']):,.0f} ft",
                })
            for label, key in (
                ("Operator / provider", "operator"),
                ("Pipeline / facility type", "pipeline_type"),
                ("Published pipe size", "pipe_size"),
                ("Published pressure", "pressure"),
                ("Published flow type", "flow_type"),
                ("Designation", "designation"),
            ):
                if row.get(key) not in (None, ""):
                    details.append({"label": label, "value": str(row[key])})
            attributes = row.get("attributes")
            if isinstance(attributes, dict):
                for key, value in list(attributes.items())[:5]:
                    details.append({
                        "label": str(key).replace("_", " ").title(),
                        "value": str(value),
                    })
            if row.get("easement_status"):
                details.append({
                    "label": "Recorded easement",
                    "value": "Requires title instruments and survey plotting",
                })
            if row.get("source_url"):
                details.append({
                    "label": "Official verification source",
                    "value": "Open public source",
                    "url": str(row["source_url"]),
                })
            kind = map_kind(row, default_kind)
            if kind == "access_driveway":
                crosses_subject_edge = False
                lies_on_subject = False
                if parcel is not None:
                    try:
                        driveway_shape = shape(geometry)
                        crosses_subject_edge = driveway_shape.intersects(parcel.boundary)
                        lies_on_subject = driveway_shape.intersects(parcel)
                    except (TypeError, ValueError):
                        crosses_subject_edge = False
                        lies_on_subject = False
                if crosses_subject_edge:
                    kind = "access_subject_driveway"
                elif lies_on_subject:
                    kind = "access_on_site_driveway"
                else:
                    kind = "access_nearby_driveway"
                details.append({
                    "label": "Map meaning",
                    "value": (
                        "Community-mapped driveway crosses the subject boundary"
                        if crosses_subject_edge
                        else (
                            "Community-mapped driveway lies on the subject but no "
                            "parcel-edge crossing was resolved"
                            if lies_on_subject
                            else "Community-mapped driveway near, but not on, the subject"
                        )
                    ),
                })
                details.append({
                    "label": "Access status",
                    "value": "Screening only; verify physical condition, legal right, and permit",
                })
            elif kind == "access_signal":
                details.append({
                    "label": "Map meaning",
                    "value": "Community-mapped traffic signal; field verification required",
                })
            title = str(row.get("title") or default_kind.replace("_", " ").title())[:240]
            if kind == "access_subject_driveway":
                title = "Mapped subject curb cut"
            elif kind == "access_on_site_driveway":
                title = "Mapped on-site driveway"
            elif kind == "access_nearby_driveway":
                title = "Nearby mapped driveway"
            output.append(SiteCheckMapFeature(
                id=str(row.get("id") or f"site-constraint:{default_kind}:{index}"),
                kind=kind,
                title=title,
                subtitle=(
                    f"{float(row['distance_ft']):,.0f} ft from subject"
                    if isinstance(row.get("distance_ft"), (int, float)) else None
                ),
                source=str(row.get("source") or "Public site-constraints screen"),
                confidence=str(row.get("confidence") or "screened_indicator"),
                geometry=geometry,
                details=details,
                map_properties={
                    "constraint_kind": kind,
                    "access_class": kind.removeprefix("access_"),
                },
            ))

    add_rows(
        facts.get("gas_pipelines"),
        default_kind="utility_gas_pipeline",
        allowed_types={"LineString", "MultiLineString"},
    )
    add_rows(
        facts.get("utility_lines"),
        default_kind="utility_main_other",
        allowed_types={"LineString", "MultiLineString"},
    )
    add_rows(
        facts.get("utility_facilities"),
        default_kind="utility_facility",
        allowed_types={"Point", "LineString", "Polygon"},
    )
    add_rows(
        facts.get("access_features"),
        default_kind="access_feature",
        allowed_types={"Point", "LineString", "MultiLineString"},
    )
    add_rows(
        facts.get("edwards_aquifer_zones"),
        default_kind="environment_edwards_aquifer",
        allowed_types={"Polygon", "MultiPolygon"},
    )
    add_rows(
        facts.get("critical_habitat"),
        default_kind="environment_critical_habitat",
        allowed_types={"Polygon", "MultiPolygon"},
    )
    buildable = facts.get("buildable_envelope")
    gross_sqft = buildable.get("gross_area_sqft") if isinstance(buildable, dict) else None
    gross_acres = (
        float(gross_sqft) / 43560
        if isinstance(gross_sqft, (int, float)) and gross_sqft > 0 else None
    )
    screened_acres = (
        buildable.get("screened_area_acres") if isinstance(buildable, dict) else None
    )
    setback_ft = (
        buildable.get("uniform_setback_offset_ft")
        if isinstance(buildable, dict) else None
    )
    meaningful_buildable = (
        isinstance(buildable, dict)
        and isinstance(buildable.get("map_geometry"), dict)
        and str(buildable.get("completeness") or "").lower() != "parcel_only"
        and (
            bool(buildable.get("constraints_ledger"))
            or
            (isinstance(setback_ft, (int, float)) and setback_ft > 0)
            or (
                isinstance(screened_acres, (int, float))
                and gross_acres is not None
                and screened_acres < gross_acres - 0.01
            )
        )
    )
    if meaningful_buildable:
        feature_details = [
            {
                "label": "Gross parcel area",
                "value": f"{float(buildable.get('gross_area_sqft') or 0) / 43560:,.2f} ac",
            },
            {
                "label": "Area outside applied mapped exclusions",
                "value": f"{float(buildable.get('screened_area_acres') or 0):,.2f} ac",
            },
            {
                "label": "FEMA SFHA screening exclusion",
                "value": (
                    f"{float(buildable.get('sfha_excluded_acres') or 0):,.2f} ac "
                    "outside the preliminary envelope; not a final "
                    "unbuildable-area determination"
                ),
            },
            {
                "label": "NWI wetland screening exclusion",
                "value": (
                    f"{float(buildable.get('nwi_wetlands_excluded_acres') or 0):,.2f} ac "
                    "outside the preliminary envelope; not a jurisdictional "
                    "wetland or final unbuildable-area determination"
                ),
            },
            {
                "label": "Applied constraints",
                "value": "; ".join(buildable.get("applied_constraints") or []),
            },
        ]
        if buildable.get("floodway_intersection_acres") is not None:
            feature_details.append({
                "label": "Regulatory floodway intersection",
                "value": (
                    f"{float(buildable.get('floodway_intersection_acres') or 0):,.2f} ac"
                ),
            })
        for row in (buildable.get("constraints_ledger") or [])[:12]:
            if not isinstance(row, dict):
                continue
            feature_details.append({
                "label": str(row.get("category") or "Constraint"),
                "value": (
                    f"{str(row.get('status') or 'unresolved').replace('_', ' ').title()}: "
                    f"{row.get('result') or 'No result'}"
                ),
            })
        feature_details.append({
            "label": "Still missing",
            "value": "; ".join(buildable.get("missing_constraints") or []),
        })
        output.append(SiteCheckMapFeature(
            id="site-constraints:buildable-envelope",
            kind="buildable_envelope",
            title="Preliminary development envelope",
            subtitle=f"Completeness: {str(buildable.get('completeness') or 'incomplete').replace('_', ' ')}",
            source="Verified CAD parcel and available published constraints",
            confidence="screened_indicator",
            geometry=buildable["map_geometry"],
            details=feature_details,
            map_properties={
                "setback_ft": float(setback_ft or 0),
            },
        ))
    return output


def _district_features(run: SiteCheckRun) -> list[SiteCheckMapFeature]:
    facts = _facts(run, "district_overlays")
    output: list[SiteCheckMapFeature] = []
    district_groups = (
        ("subdivisions", "subdivision", "Subdivision"),
        ("mud_districts", "mud", "Municipal utility district"),
        ("wcid_districts", "wcid", "Water control and improvement district"),
        ("esd_districts", "esd", "Emergency services district"),
        ("pid_districts", "pid", "Public improvement district"),
        ("tirz_districts", "tirz", "Tax increment reinvestment zone"),
        ("metro_districts", "metro", "Metropolitan district"),
    )
    for key, kind, label in district_groups:
        rows = facts.get(key)
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            geometry = _geojson_geometry(row, {"Polygon", "MultiPolygon"})
            if geometry is None:
                continue
            name = str(row.get("name") or label)
            identifier = row.get("id")
            details = [{"label": "District type", "value": label}]
            if identifier not in (None, ""):
                details.append({
                    "label": "Published identifier",
                    "value": str(identifier),
                })
            details.append({
                "label": "Reliance",
                "value": (
                    "Confirm the current boundary, assessment, and obligations "
                    "with the governing entity"
                ),
            })
            output.append(SiteCheckMapFeature(
                id=f"district:{kind}:{identifier or index}",
                kind=f"regulatory_district_{kind}",
                title=name[:240],
                subtitle=label,
                source="County public GIS district overlay",
                confidence="verified_public_fact",
                geometry=geometry,
                details=details,
                map_properties={
                    "district_kind": kind,
                    "display_label": name[:120],
                },
            ))
    return output


def _buildable_envelope_feature(
    run: SiteCheckRun,
    parcel,
    *,
    lat: float,
    lng: float,
) -> SiteCheckMapFeature | None:
    standards = _facts(run, "zoning_standards").get("dimensional_standards")
    if parcel is None or not isinstance(standards, dict) or not standards:
        return None
    setbacks = [
        value for value in (
            _dimension_numeric(standards, "front_setback"),
            _dimension_numeric(standards, "side_setback"),
            _dimension_numeric(standards, "rear_setback"),
            _dimension_numeric(standards, "side_setback_corner"),
        )
        if value is not None
    ]
    uniform_setback_ft = max(setbacks) if setbacks else 0.0
    if uniform_setback_ft <= 0:
        return None
    envelope, envelope_acres = _setback_envelope_geojson(
        parcel,
        lat=lat,
        lng=lng,
        setback_ft=uniform_setback_ft,
    )
    if envelope is None or envelope_acres is None:
        return None
    gross_acres = _projected_area_acres(parcel, lat, lng)
    impervious_pct = _dimension_numeric(standards, "max_impervious_cover")
    impervious_value = (
        f"{impervious_pct:.0f}% / {gross_acres * impervious_pct / 100:,.2f} ac cap"
        if impervious_pct is not None
        else "Not stored in dimensional standards"
    )
    zoning_facts = _facts(run, "zoning_standards")
    district = zoning_facts.get("district") or _zoning_value(run)
    details = [
        {"label": "Gross parcel area", "value": f"{gross_acres:,.2f} ac"},
        {
            "label": "Uniform setback screen",
            "value": f"{uniform_setback_ft:,.0f} ft from stored zoning setbacks",
        },
        {
            "label": "After-setback envelope",
            "value": f"{envelope_acres:,.2f} ac",
        },
        {"label": "Maximum impervious cover", "value": impervious_value},
        {
            "label": "Detention area",
            "value": "Not computed from public data; civil drainage design required",
        },
        {
            "label": "Completeness ledger",
            "value": (
                "Parcel geometry and zoning dimensional standards present; "
                "survey, easements, utilities, floodplain, wetlands, trees, "
                "and drainage exclusions remain unverified"
            ),
        },
    ]
    if district:
        details.insert(0, {"label": "Zoning district", "value": str(district)})
    return SiteCheckMapFeature(
        id="buildable-envelope:subject",
        kind="buildable_envelope",
        title="Preliminary development envelope",
        subtitle="Screening geometry from parcel and stored dimensional standards",
        source="County parcel geometry and curated zoning standards",
        confidence="screened_indicator",
        geometry=envelope,
        details=details,
        map_properties={
            "gross_acres": round(gross_acres, 3),
            "after_setback_acres": round(envelope_acres, 3),
            "setback_ft": round(uniform_setback_ft, 1),
        },
    )


def _context_features(
    run: SiteCheckRun,
    *,
    parcel=None,
    lat: float | None = None,
    lng: float | None = None,
) -> list[SiteCheckMapFeature]:
    output: list[SiteCheckMapFeature] = []
    for source_id, feature_prefix, source_label in (
        ("wcad_parcel", "wcad", "Williamson Central Appraisal District parcel layer"),
        ("tcad_parcel", "tcad", "County appraisal district parcel layer"),
    ):
        parcel_facts = _facts(run, source_id)
        subject_id = str(parcel_facts.get("parcel_id") or "")
        for row in (parcel_facts.get("neighboring_parcels") or [])[:40]:
            if not isinstance(row, dict):
                continue
            parcel_id = str(row.get("parcel_id") or "").strip()
            geometry = row.get("parcel_polygon")
            if not parcel_id or parcel_id == subject_id or not isinstance(geometry, dict):
                continue
            if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
                continue
            acres = row.get("acreage")
            cad_account = row.get("cad_account")
            distance_ft = row.get("distance_from_subject_centroid_ft")
            details = []
            if acres is not None:
                details.append({"label": "CAD acreage", "value": f"{float(acres):,.2f} ac"})
            if cad_account:
                details.append({"label": "CAD account", "value": str(cad_account)})
            if distance_ft is not None:
                details.append({
                    "label": "Approx. centroid spacing",
                    "value": f"{float(distance_ft):,.0f} ft",
                })
            output.append(SiteCheckMapFeature(
                id=f"{feature_prefix}-neighbor:{parcel_id}",
                kind="neighbor_parcel",
                title=f"Parcel {parcel_id}",
                subtitle=(f"{float(acres):,.2f} acres" if acres is not None else None),
                source=str(parcel_facts.get("source") or source_label),
                confidence="verified_public_fact",
                geometry=geometry,
                details=details,
            ))

    seen_projects: set[str] = set()
    projects = (
        _facts(run, "txdot_projects").get("items")
        or _facts(run, "txdot_projects").get("projects")
        or []
    )
    for index, row in enumerate(projects[:100]):
        if not isinstance(row, dict):
            continue
        coordinate = _valid_point(row)
        if coordinate is None:
            continue
        lat, lng = coordinate
        csj = str(row.get("csj") or index)
        if csj in seen_projects:
            continue
        seen_projects.add(csj)
        details = []
        for label, key in (
            ("Phase", "phase"),
            ("Work scope", "work_type"),
            ("Project stage", "project_stage"),
            ("Project status", "project_status"),
            ("Scheduled let", "scheduled_let_date"),
            ("Actual let", "actual_let_date"),
            ("Commission award", "commission_award_date"),
            ("County", "county"),
            ("Let type", "let_type"),
        ):
            if row.get(key):
                details.append({"label": label, "value": str(row[key])})
        limits = " to ".join(
            str(row[key]).strip()
            for key in ("limits_from", "limits_to")
            if row.get(key)
        )
        if limits:
            details.append({"label": "Project limits", "value": limits})
        length = row.get("project_length_mi")
        if isinstance(length, (int, float)):
            details.append({"label": "Project length", "value": f"{length:,.2f} mi"})
        cost = row.get("estimated_cost_usd")
        if isinstance(cost, (int, float)):
            details.append({"label": "Engineer estimate", "value": f"${cost:,.2f}"})
        award = row.get("award") if isinstance(row.get("award"), dict) else {}
        for label, key in (
            ("Awarded contractor", "contractor"),
            ("Contract", "contract_number"),
            ("Contract letting", "letting_date"),
            ("Contract award", "award_date"),
            ("Notice to proceed", "notice_to_proceed_date"),
            ("Work began", "work_begin_date"),
            ("Accepted", "accepted_date"),
        ):
            if award.get(key):
                details.append({"label": label, "value": str(award[key])})
        award_amount = award.get("award_amount_usd")
        variance = award.get("award_variance_pct")
        if isinstance(award_amount, (int, float)):
            variance_note = (
                f" ({float(variance):+.2f}% vs estimate)"
                if isinstance(variance, (int, float)) else ""
            )
            details.append({
                "label": "Award amount",
                "value": f"${float(award_amount):,.2f}{variance_note}",
            })
        completion = award.get("percent_complete")
        if isinstance(completion, (int, float)):
            details.append({"label": "Reported complete", "value": f"{completion:,.1f}%"})
        addenda = row.get("addenda") if isinstance(row.get("addenda"), list) else []
        for addendum in addenda[:2]:
            if isinstance(addendum, dict) and addendum.get("summary"):
                details.append({
                    "label": f"Addendum {addendum.get('number') or ''}".strip(),
                    "value": str(addendum["summary"]),
                })
        documents = row.get("documents") if isinstance(row.get("documents"), list) else []
        for document in documents[:6]:
            if not isinstance(document, dict) or not document.get("url"):
                continue
            details.append({
                "label": str(document.get("label") or "Official TxDOT document"),
                "value": "Open verified PDF",
                "url": str(document["url"]),
            })
        lookup_links = (
            row.get("official_lookup_links")
            if isinstance(row.get("official_lookup_links"), list) else []
        )
        for link in lookup_links[:2]:
            if not isinstance(link, dict) or not link.get("url"):
                continue
            details.append({
                "label": str(link.get("label") or "Official TxDOT lookup"),
                "value": f"Open and search CSJ {csj}",
                "url": str(link["url"]),
            })
        lookup = row.get("document_lookup")
        if isinstance(lookup, dict) and lookup.get("direct_plan_advisory"):
            details.append({
                "label": "Plans / schematic",
                "value": str(lookup["direct_plan_advisory"]),
            })
        output.append(SiteCheckMapFeature(
            id=f"txdot-project:{csj}",
            kind="public_project",
            title=str(row.get("description") or "TxDOT project")[:240],
            subtitle=f"TxDOT CSJ {csj}" if csj else None,
            source="Texas Department of Transportation",
            confidence="verified_public_fact",
            geometry={"type": "Point", "coordinates": [lng, lat]},
            details=details,
        ))

    for index, row in enumerate(
        (_facts(run, "twdb_ccn").get("service_territories") or [])[:30]
    ):
        if not isinstance(row, dict):
            continue
        geometry = row.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {
            "Polygon", "MultiPolygon",
        }:
            continue
        service = str(row.get("service") or "utility")
        authority = str(row.get("authority") or "service_area")
        provider = str(row.get("provider") or "Provider not named")
        output.append(SiteCheckMapFeature(
            id=f"twdb-ccn:{service}:{authority}:{index}",
            kind=f"utility_ccn_{service}",
            title=f"{service.title()} service territory",
            subtitle=provider,
            source=str(row.get("source") or "TWDB public utility service area"),
            confidence="verified_public_fact",
            geometry=geometry,
            details=[
                {"label": "Provider", "value": provider},
                {"label": "Service", "value": service.title()},
                {
                    "label": "Authority basis",
                    "value": authority.replace("_", " ").title(),
                },
                {
                    "label": "Capacity / tie-in",
                    "value": "Not established by CCN boundary",
                },
            ],
            map_properties={
                "display_label": provider[:120],
                "service": service,
                "authority": authority,
            },
        ))

    electric_facts = _facts(run, "electric_power_infrastructure")
    for index, row in enumerate(
        (electric_facts.get("nearest_transmission_lines") or [])[:12]
    ):
        if not isinstance(row, dict):
            continue
        geometry = row.get("map_geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {
            "LineString", "MultiLineString",
        }:
            continue
        voltage = row.get("voltage_kv")
        voltage_label = f"{int(voltage):,} kV" if voltage else "Voltage not published"
        details = [
            {"label": "Voltage", "value": voltage_label},
            {
                "label": "Approx. distance",
                "value": (
                    f"{float(row['distance_mi']):.2f} mi"
                    if row.get("distance_mi") is not None
                    else "Not calculated"
                ),
            },
            {"label": "Owner", "value": str(row.get("owner") or "Not published")},
            {
                "label": "Capacity / interconnection",
                "value": "Not established by infrastructure proximity",
            },
        ]
        output.append(SiteCheckMapFeature(
            id=f"hifld-transmission:{index}:{voltage_label}",
            kind="utility_electric_transmission",
            title=str(row.get("label") or "Electric transmission line")[:240],
            subtitle=(
                f"{float(row['distance_mi']):.2f} mi from subject"
                if row.get("distance_mi") is not None else None
            ),
            source=str(row.get("source") or "HIFLD Electric Power Transmission Lines"),
            confidence="screened_indicator",
            geometry=geometry,
            details=details,
            map_properties={
                "display_label": voltage_label,
                "voltage_kv": int(voltage) if voltage else None,
            },
        ))

    for index, row in enumerate(
        (electric_facts.get("nearest_substations") or [])[:12]
    ):
        if not isinstance(row, dict):
            continue
        geometry = row.get("map_geometry")
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            continue
        voltage = row.get("max_voltage_kv")
        details = [
            {
                "label": "Maximum published voltage",
                "value": f"{int(voltage):,} kV" if voltage else "Not published",
            },
            {
                "label": "Approx. distance",
                "value": (
                    f"{float(row['distance_mi']):.2f} mi"
                    if row.get("distance_mi") is not None
                    else "Not calculated"
                ),
            },
            {"label": "Status", "value": str(row.get("status") or "Not published")},
            {
                "label": "Service / headroom",
                "value": "Not established by substation proximity",
            },
        ]
        output.append(SiteCheckMapFeature(
            id=f"hifld-substation:{index}:{row.get('name') or 'unnamed'}",
            kind="utility_electric_substation",
            title=str(row.get("label") or "Electric substation")[:240],
            subtitle=(
                f"{float(row['distance_mi']):.2f} mi from subject"
                if row.get("distance_mi") is not None else None
            ),
            source=str(row.get("source") or "HIFLD Electric Substations"),
            confidence="screened_indicator",
            geometry=geometry,
            details=details,
            map_properties={
                "display_label": str(row.get("name") or "Substation")[:120],
                "voltage_kv": int(voltage) if voltage else None,
            },
        ))

    for index, row in enumerate(
        (_facts(run, "txdot_aadt").get("traffic_counts") or [])[:30]
    ):
        if not isinstance(row, dict):
            continue
        geometry = row.get("map_geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {
            "LineString", "MultiLineString",
        }:
            continue
        road = str(row.get("road_name") or "TxDOT road")
        aadt = row.get("aadt")
        details = [
            {
                "label": "AADT",
                "value": (
                    f"{int(aadt):,} vehicles/day"
                    if aadt else "Not published"
                ),
            },
            {
                "label": "Count year",
                "value": str(row.get("year") or "Not published"),
            },
            {
                "label": "Frontage screen",
                "value": "Yes" if row.get("is_frontage") else "Nearby",
            },
        ]
        if row.get("distance_mi") is not None:
            details.append({
                "label": "Approx. distance",
                "value": f"{float(row['distance_mi']):.2f} mi",
            })
        output.append(SiteCheckMapFeature(
            id=f"txdot-aadt:{index}:{road}",
            kind="traffic_count",
            title=road,
            subtitle=(
                f"{int(aadt):,} vehicles/day (AADT)"
                if aadt else "TxDOT traffic count"
            ),
            source=str(
                row.get("source") or "Texas Department of Transportation"
            ),
            confidence="verified_public_fact",
            geometry=geometry,
            details=details,
            map_properties={
                **({"aadt": int(aadt)} if aadt is not None else {}),
                "count_year": str(row.get("year") or ""),
                "road_name": road,
                "is_frontage": bool(row.get("is_frontage")),
                "display_label": (
                    f"{int(aadt):,}/day" if aadt is not None else road
                ),
            },
        ))
    for index, row in enumerate(
        (_facts(run, "txdot_aadt_history").get("stations") or [])[:30]
    ):
        if not isinstance(row, dict):
            continue
        station_lat = row.get("lat")
        station_lng = row.get("lng")
        aadt = row.get("latest_aadt")
        if not all(isinstance(value, (int, float)) for value in (station_lat, station_lng)):
            continue
        road = str(row.get("on_road") or "TxDOT count station")
        station_id = str(row.get("station_id") or index)
        trend = row.get("trend_summary") if isinstance(row.get("trend_summary"), dict) else {}
        details = [
            {
                "label": "AADT",
                "value": f"{int(aadt):,} vehicles/day" if aadt else "Not published",
            },
            {"label": "Count year", "value": str(row.get("latest_year") or "Not published")},
            {"label": "Station", "value": station_id},
            {
                "label": "Approx. distance",
                "value": (
                    f"{float(row['distance_mi']):.2f} mi"
                    if row.get("distance_mi") is not None else "Not calculated"
                ),
            },
        ]
        if trend.get("change_5yr_pct") is not None:
            details.append({
                "label": "Five-year change",
                "value": f"{float(trend['change_5yr_pct']):+.1f}%",
            })
        output.append(SiteCheckMapFeature(
            id=f"txdot-aadt-station:{station_id}",
            kind="traffic_station",
            title=road,
            subtitle=(
                f"{int(aadt):,} vehicles/day (AADT)"
                if aadt else "TxDOT count station"
            ),
            source=str(
                _facts(run, "txdot_aadt_history").get("source_dataset")
                or "Texas Department of Transportation"
            ),
            confidence="verified_public_fact",
            geometry={
                "type": "Point",
                "coordinates": [float(station_lng), float(station_lat)],
            },
            details=details,
            map_properties={
                **({"aadt": int(aadt)} if aadt is not None else {}),
                "count_year": str(row.get("latest_year") or ""),
                "road_name": road,
                "display_label": f"{int(aadt):,}/day" if aadt else road,
                "station_id": station_id,
            },
        ))
    output.extend(_roadway_access_features(run))
    output.extend(_physical_utility_features(run))
    output.extend(_site_constraint_features(run, parcel=parcel))
    if parcel is not None and lat is not None and lng is not None:
        output.extend(
            _subject_frontage_features(
                parcel,
                [
                    feature
                    for feature in output
                    if feature.kind in {"traffic_count", "access_roadway"}
                ],
                lat=lat,
                lng=lng,
            )
        )
    output.extend(_district_features(run))
    if (
        parcel is not None and lat is not None and lng is not None
        and not any(feature.kind == "buildable_envelope" for feature in output)
    ):
        buildable = _buildable_envelope_feature(run, parcel, lat=lat, lng=lng)
        if buildable is not None:
            output.append(buildable)
    return output


def _access_facts_details(
    site_constraint_facts: dict | None,
) -> list[dict[str, str]]:
    """Per-frontage authority + mapped facts from the access_facts block
    (Task B). Snapshots that predate the block contribute no lines —
    the card renders exactly as before."""
    if not isinstance(site_constraint_facts, dict):
        return []
    block = site_constraint_facts.get("access_facts")
    if not isinstance(block, dict):
        return []
    lines: list[dict[str, str]] = []
    for row in (block.get("frontages") or [])[:4]:
        name = str(row.get("road_name") or "Road")
        parts: list[str] = [f"authority: {row.get('authority')}"]
        if row.get("posted_speed_mph"):
            parts.append(f"{row['posted_speed_mph']} mph posted")
        if row.get("lanes"):
            parts.append(f"{row['lanes']} lanes")
        if row.get("median_type"):
            parts.append(f"median: {row['median_type']}")
        if row.get("amm_reference_spacing_ft"):
            parts.append(
                f"TxDOT reference spacing {row['amm_reference_spacing_ft']} ft"
            )
        lines.append({
            "label": f"Frontage facts — {name}",
            "value": "; ".join(parts),
        })
        lines.append({
            "label": f"Approval status — {name}",
            "value": str(row.get("status") or ""),
        })
    if not lines and block.get("status"):
        lines.append({
            "label": "Access facts",
            "value": str(block["status"]),
        })
    return lines
def _drainage_layer(run: SiteCheckRun) -> SiteCheckMapLayer:
    """Fixed Drainage control (after Flood). Old snapshots that predate
    the drainage screen render the schema-stale state — the control is
    always present, never omitted, and an empty result is never
    fabricated (spec §8)."""
    result = (
        run.adapter_results.get("drainage_screen")
        if isinstance(run.adapter_results, dict) else None
    )
    facts = (
        result.get("normalized_facts") if isinstance(result, dict) else None
    )
    legend = [
        {"label": "Screened flow path", "color": "#0EA5E9"},
        {"label": "Low spot (fill-to-spill depth)", "color": "#3B82F6"},
        {"label": "Detention candidate", "color": "#10B981"},
        {"label": "Water enters here (run-on)", "color": "#F59E0B"},
    ]
    if not isinstance(facts, dict):
        return SiteCheckMapLayer(
            id="drainage",
            label="Drainage",
            kind="geojson",
            available=False,
            status="refresh_required",
            attribution="USGS 3DEP elevation",
            advisory=(
                "Refresh Public Data to compute the drainage screen for "
                "this property."
            ),
            legend=legend,
        )
    status = str(facts.get("status") or "")
    if status == "region_not_supported":
        return SiteCheckMapLayer(
            id="drainage",
            label="Drainage",
            kind="geojson",
            available=False,
            status="not_supported_in_region",
            attribution="USGS 3DEP elevation",
            advisory=(
                "Drainage screening is not yet supported in this region; "
                "coverage is currently limited to the contiguous United "
                "States."
            ),
            legend=legend,
        )
    if status in {"dem_unavailable", "coordinates_required"}:
        return SiteCheckMapLayer(
            id="drainage",
            label="Drainage",
            kind="geojson",
            available=False,
            status="unavailable",
            attribution="USGS 3DEP elevation",
            advisory=(
                "The public elevation service did not return usable data; "
                "refresh this source when it is responding again."
            ),
            legend=legend,
        )

    features: list[dict[str, Any]] = []

    def add(rows, kind, props=()):
        for row in rows or []:
            geometry = row.get("map_geometry") if isinstance(row, dict) else None
            if not isinstance(geometry, dict):
                continue
            properties: dict[str, Any] = {"kind": kind}
            for key in props:
                if row.get(key) is not None:
                    properties[key] = row[key]
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": properties,
            })

    add(facts.get("flow_paths"), "drainage_flow_path",
        ("contributing_acres_at_exit",))
    add(facts.get("topographic_depressions"), "drainage_depression",
        ("max_depth_ft", "area_acres"))
    add(facts.get("detention_candidates"), "drainage_detention",
        ("area_acres",))
    add(facts.get("runon_entries"), "drainage_runon",
        ("contributing_acres", "window_bounded"))

    ddf = facts.get("atlas14_ddf_inches") or {}
    storm_100yr = (ddf.get("24-hr") or {}).get("100yr")
    details = [
        {"label": "Low spots (fill-to-spill)",
         "value": str(sum(1 for f in features
                          if f["properties"]["kind"] == "drainage_depression"))},
        {"label": "Detention candidates on the parcel",
         "value": str(sum(1 for f in features
                          if f["properties"]["kind"] == "drainage_detention"))},
        {"label": "Water entry points (run-on)",
         "value": str(sum(1 for f in features
                          if f["properties"]["kind"] == "drainage_runon"))},
        {"label": "Stream-network cross-check",
         "value": {
             "agrees": "Computed paths match the published stream network",
             "partial": "Partially verified; treat direction as unverified",
             "no_mapped_streams": "No published streams in the window",
         }.get(str(facts.get("nhd_cross_check")), "Not evaluated")},
    ]
    if isinstance(storm_100yr, (int, float)):
        details.append({
            "label": "100-year 24-hour rainfall (NOAA Atlas 14)",
            "value": f"{storm_100yr:.2f} in (context only)",
        })
    return SiteCheckMapLayer(
        id="drainage",
        label="Drainage",
        kind="geojson",
        available=bool(features),
        status="screening" if features else "covered_empty",
        geojson={
            "type": "FeatureCollection", "features": features,
        } if features else None,
        attribution=(
            "USGS 3DEP elevation; USGS National Hydrography Dataset; "
            "NOAA Atlas 14"
        ),
        advisory=str(facts.get("advisory") or ""),
        legend=legend,
        details=details,
    )


def _advisory(run: SiteCheckRun, source_id: str) -> str | None:
    value = _facts(run, source_id).get("advisory")
    return str(value).strip() if value else None


def _first_list_count(facts: dict[str, Any], keys: tuple[str, ...]) -> int:
    total = 0
    for key in keys:
        value = facts.get(key)
        if isinstance(value, list):
            total += len(value)
    return total


def _regulatory_details(run: SiteCheckRun) -> list[dict[str, str]]:
    zoning_facts = _facts(run, "austin_zoning")
    zoning_standards = _facts(run, "zoning_standards")
    district_facts = _facts(run, "district_overlays")
    plat_facts = _facts(run, "plats_intel")
    entitlement_facts = _facts(run, "entitlement_pipeline")
    standards = zoning_standards.get("dimensional_standards")
    details: list[dict[str, str]] = []
    label = _zoning_overlay_label(run)
    if label:
        details.append({"label": "Zoning / jurisdiction", "value": label})
    if zoning_facts.get("jurisdiction"):
        details.append({"label": "Jurisdiction", "value": str(zoning_facts["jurisdiction"])})
    if isinstance(standards, dict):
        for label_text, key in (
            ("Front setback", "front_setback"),
            ("Side setback", "side_setback"),
            ("Rear setback", "rear_setback"),
            ("Maximum height", "max_height"),
            ("Maximum lot coverage", "max_lot_coverage"),
            ("Maximum impervious cover", "max_impervious_cover"),
        ):
            value = _dimension_label(standards, key)
            if value:
                details.append({"label": label_text, "value": value})
    if not any(row["label"] == "Maximum lot coverage" for row in details):
        details.append({
            "label": "Maximum lot coverage",
            "value": "Not published in the resolved dimensional standard",
        })
    details.append({
        "label": "Detention requirement",
        "value": (
            "Confirm current drainage criteria, downstream analysis, and civil "
            "detention design with the governing authority"
        ),
    })
    district_count = _first_list_count(district_facts, (
        "subdivisions", "mud_districts", "wcid_districts", "esd_districts",
        "pid_districts", "tirz_districts", "school_districts",
        "metro_districts",
    ))
    details.append({
        "label": "District overlays",
        "value": f"{district_count} stored matches" if district_facts else "Not stored",
    })
    plat_status = next(
        (
            str(value)
            for key, value in plat_facts.items()
            if key.endswith("_subdivision_gis_status") and value
        ),
        None,
    )
    details.append({
        "label": "Plat / subdivision screen",
        "value": plat_status.replace("_", " ").title() if plat_status else "Not stored",
    })
    case_count = _first_list_count(entitlement_facts, (
        "cases", "items", "active_cases", "nearby_cases",
    ))
    details.append({
        "label": "Permit / entitlement pipeline",
        "value": f"{case_count} stored records" if entitlement_facts else "Not stored",
    })
    return details


def _market_details(run: SiteCheckRun) -> list[dict[str, str]]:
    census = _facts(run, "census_acs")
    osm = _facts(run, "osm_overpass")
    site_market = _facts(run, "site_constraints").get("market_context")
    site_market = site_market if isinstance(site_market, dict) else {}
    details: list[dict[str, str]] = []
    rings = census.get("rings") if isinstance(census.get("rings"), dict) else {}
    for ring_key in ("1mi", "3mi", "5mi"):
        ring = rings.get(ring_key) if isinstance(rings, dict) else None
        if not isinstance(ring, dict):
            continue
        pieces = []
        if ring.get("population") is not None:
            pieces.append(f"pop {int(float(ring['population'])):,}")
        if ring.get("median_household_income") is not None:
            pieces.append(f"income ${int(float(ring['median_household_income'])):,}")
        if pieces:
            details.append({
                "label": f"ACS {ring_key} ring",
                "value": " / ".join(pieces),
            })
    map_points = osm.get("map_points")
    if isinstance(map_points, list):
        details.append({"label": "Mapped POIs", "value": f"{len(map_points)} stored points"})
    if site_market:
        details.append({
            "label": "Published permit / project density",
            "value": (
                f"{float(site_market.get('published_permit_project_record_density_per_sqmi') or 0):,.2f} "
                f"records/sq mi ({int(site_market.get('published_record_count') or 0)} records)"
            ),
        })
        details.append({
            "label": "Drive-time trade area",
            "value": "Not calculated; requires a network-routed travel-time source",
        })
    details.append({
        "label": "Market geometry",
        "value": "Census rings and POIs are screening context, not drive-time trade areas",
    })
    return details or [{
        "label": "Market snapshot",
        "value": "No census or POI market context stored in this snapshot",
    }]


def _population_density_geojson(
    run: SiteCheckRun,
    *,
    parcel,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Build non-overlapping ACS radial density bands from stored facts."""
    census = _facts(run, "census_acs")
    rings = census.get("rings") if isinstance(census.get("rings"), dict) else {}
    center_lat, center_lng = _center(run, parcel)
    zone = int((center_lng + 180) // 6) + 1
    epsg = 32600 + max(1, min(zone, 60))
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inverse = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    center = Point(*forward.transform(center_lng, center_lat))

    cumulative: list[tuple[float, float]] = []
    for radius_mi, key in ((1.0, "1mi"), (3.0, "3mi"), (5.0, "5mi")):
        row = rings.get(key) if isinstance(rings, dict) else None
        population = row.get("population") if isinstance(row, dict) else None
        try:
            population_value = float(population)
        except (TypeError, ValueError):
            continue
        cumulative.append((radius_mi, max(0.0, population_value)))

    features = []
    details: list[dict[str, str]] = []
    prior_radius = 0.0
    prior_population = 0.0
    for radius_mi, population in cumulative:
        incremental_population = max(0.0, population - prior_population)
        band_area_sqmi = math.pi * (radius_mi ** 2 - prior_radius ** 2)
        density = incremental_population / band_area_sqmi if band_area_sqmi else 0.0
        if density < 500:
            density_class, color = "low", "#2563EB"
        elif density < 1500:
            density_class, color = "moderate", "#EAB308"
        else:
            density_class, color = "high", "#DC2626"
        outer = center.buffer(radius_mi * 1609.344, resolution=64)
        band = (
            outer.difference(center.buffer(prior_radius * 1609.344, resolution=64))
            if prior_radius > 0 else outer
        )
        geometry = mapping(shapely_transform(inverse.transform, band))
        label = f"{prior_radius:g}-{radius_mi:g} mi density"
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "id": f"population-density:{prior_radius:g}-{radius_mi:g}",
                "kind": "population_density",
                "title": label,
                "display_label": f"{density:,.0f} people/sq mi",
                "density_class": density_class,
                "density_color": color,
                "population_density_per_sqmi": round(density),
                "incremental_population": round(incremental_population),
                "inner_radius_mi": prior_radius,
                "outer_radius_mi": radius_mi,
                "acs_year": census.get("acs_year"),
            },
        })
        details.append({
            "label": label,
            "value": (
                f"{density:,.0f} people/sq mi ({incremental_population:,.0f} "
                f"estimated people in band; {density_class})"
            ),
        })
        prior_radius = radius_mi
        prior_population = population

    if not features:
        return None, [{
            "label": "Population density",
            "value": "No usable ACS population rings are stored in this snapshot",
        }]
    details.append({
        "label": "Method",
        "value": (
            "Incremental population divided by each radial band's land area; "
            "ACS block-group centroid screening, not a drive-time trade area"
        ),
    })
    return {"type": "FeatureCollection", "features": features}, details


def _environmental_details(run: SiteCheckRun) -> list[dict[str, str]]:
    environmental = _facts(run, "environmental_screening")
    site_constraints = _facts(run, "site_constraints")
    details: list[dict[str, str]] = []
    edwards = site_constraints.get("edwards_aquifer_zones") or []
    habitat = site_constraints.get("critical_habitat") or []

    def mapped_summary(rows: Any, *, absent: str) -> str:
        mapped = [row for row in rows if isinstance(row, dict)]
        if not mapped:
            return absent
        distances = [
            float(row["distance_ft"])
            for row in mapped
            if isinstance(row.get("distance_ft"), (int, float))
        ]
        if not distances:
            return f"{len(mapped)} mapped feature(s); subject intersection not resolved"
        nearest = min(distances)
        relationship = "intersects subject" if nearest <= 1 else "does not intersect subject"
        distance = (
            f"{nearest / 5280:.2f} mi away" if nearest >= 1320 else f"{nearest:,.0f} ft away"
        )
        return f"{len(mapped)} mapped feature(s); nearest {distance}; {relationship}"

    details.extend([
        {
            "label": "Mapped Edwards protection zones",
            "value": mapped_summary(
                edwards,
                absent=(
                    _screen_value(
                        environmental.get("edwards_aquifer_status"),
                        "No mapped zone returned; verify in the governing viewer",
                    ) if _in_texas(run)
                    else f"Not connected for {_state_name(run)} yet"
                ),
            ),
        },
        {
            "label": "Mapped critical habitat",
            "value": mapped_summary(
                habitat,
                absent=_screen_value(
                    environmental.get("habitat_status"),
                    "No mapped critical habitat returned; run USFWS IPaC",
                ),
            ),
        },
        {
            "label": "Karst",
            "value": "No complete nationwide parcel layer; geology/IPaC/field verification required",
        },
    ])
    return details


def _feature_geojson(feature: SiteCheckMapFeature) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": feature.geometry,
        "properties": {
            **feature.map_properties,
            "id": feature.id,
            "kind": feature.kind,
            "title": feature.title,
            "subtitle": feature.subtitle or "",
            "display_label": (
                feature.map_properties.get("display_label") or feature.title
            ),
        },
    }


def _buildable_details(run: SiteCheckRun) -> list[dict[str, str]]:
    buildable = _facts(run, "site_constraints").get("buildable_envelope")
    if isinstance(buildable, dict):
        details: list[dict[str, str]] = []
        if buildable.get("gross_area_sqft") is not None:
            details.append({
                "label": "Gross parcel area",
                "value": f"{float(buildable.get('gross_area_sqft') or 0) / 43560:,.2f} ac",
            })
        if buildable.get("screened_area_acres") is not None:
            details.append({
                "label": "Area outside applied mapped exclusions",
                "value": f"{float(buildable.get('screened_area_acres') or 0):,.2f} ac",
            })
        if buildable.get("sfha_excluded_acres") is not None:
            details.append({
                "label": "FEMA SFHA screening exclusion",
                "value": (
                    f"{float(buildable.get('sfha_excluded_acres') or 0):,.2f} ac "
                    "outside the preliminary envelope; not a final "
                    "unbuildable-area determination"
                ),
            })
        if buildable.get("nwi_wetlands_excluded_acres") is not None:
            details.append({
                "label": "NWI wetland screening exclusion",
                "value": (
                    f"{float(buildable.get('nwi_wetlands_excluded_acres') or 0):,.2f} ac "
                    "outside the preliminary envelope; not a jurisdictional "
                    "wetland or final unbuildable-area determination"
                ),
            })
        if buildable.get("floodway_intersection_acres") is not None:
            details.append({
                "label": "Regulatory floodway intersection",
                "value": f"{float(buildable.get('floodway_intersection_acres') or 0):,.2f} ac",
            })
        if buildable.get("max_impervious_cover_pct") is not None:
            details.append({
                "label": "Impervious-cover screen",
                "value": (
                    f"{float(buildable['max_impervious_cover_pct']):g}% / "
                    f"approximately {float(buildable.get('max_impervious_area_acres') or 0):,.2f} ac"
                ),
            })
        details.append({
            "label": "Utility serviceability",
            "value": (
                "Provider verification required; public CCN, mains, and grid proximity "
                "do not establish capacity, tie-in right, route, three-phase service, or cost"
            ),
        })
        for row in (buildable.get("constraints_ledger") or [])[:12]:
            if not isinstance(row, dict):
                continue
            details.append({
                "label": str(row.get("category") or "Constraint"),
                "value": f"{str(row.get('status') or 'unresolved').replace('_', ' ').title()}: {row.get('result') or 'No result'}",
            })
        details.append({
            "label": "Geometry basis",
            "value": str(buildable.get("method") or "Public parcel and available dimensional standards"),
        })
        details.append({
            "label": "Completeness ledger",
            "value": "; ".join(buildable.get("missing_constraints") or [])
            or "Survey, title/easements, utilities, drainage, floodplain/wetlands, and civil design remain unverified",
        })
        return details
    return [
        {
            "label": "Computed envelope",
            "value": "Unavailable until parcel geometry and zoning setbacks are stored",
        },
        {
            "label": "Completeness ledger",
            "value": (
                "Requires survey, title/easements, utility routing, drainage, "
                "floodplain/wetlands, and civil design before yield reliance"
            ),
        },
    ]


def _in_texas(run: Any) -> bool:
    return (getattr(run, "state", None) or "TX") == "TX"


def _state_name(run: Any) -> str:
    return state_names.name_of(getattr(run, "state", None) or "TX")


def _layers(
    run: SiteCheckRun,
    parcel,
    context_features: list[SiteCheckMapFeature],
) -> list[SiteCheckMapLayer]:
    parcel_geojson = mapping(parcel) if parcel is not None else None
    zoning_facts = _facts(run, "austin_zoning")
    ccn_facts = _facts(run, "twdb_ccn")
    parcel_facts = next(
        (
            facts
            for source_id in ("wcad_parcel", "tcad_parcel")
            if (facts := _facts(run, source_id)).get("parcel_polygon")
        ),
        {},
    )
    parcel_details: list[dict[str, str]] = []
    if parcel_facts.get("parcel_id"):
        parcel_details.append({
            "label": "CAD property ID",
            "value": str(parcel_facts["parcel_id"]),
        })
    if isinstance(parcel_facts.get("acreage"), (int, float)):
        parcel_details.append({
            "label": "CAD acreage",
            "value": f"{float(parcel_facts['acreage']):,.2f} ac",
        })
    zoning = _zoning_value(run)
    zoning_label = _zoning_overlay_label(run)
    jurisdiction_facts = _facts(run, "jurisdiction_resolver")
    municipal_wastewater_candidate = str(
        ccn_facts.get("municipal_wastewater_candidate") or ""
    ).strip()
    if not municipal_wastewater_candidate and str(
        jurisdiction_facts.get("jurisdiction_kind") or ""
    ).casefold() in {"city", "full_purpose_city"}:
        city = str(jurisdiction_facts.get("city") or "").strip()
        if city:
            municipal_wastewater_candidate = f"City of {city}"
    zoning_jurisdiction_kind = str(
        jurisdiction_facts.get("jurisdiction_kind")
        or zoning_facts.get("jurisdiction_kind")
        or "unknown"
    ).casefold()
    zoning_geojson = None
    if parcel_geojson and zoning_label:
        zoning_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": parcel_geojson,
                    "properties": {
                        "zone": zoning,
                        "label": f"Subject jurisdiction: {zoning_label}",
                        "display_label": zoning_label,
                        "jurisdiction_kind": (
                            zoning_jurisdiction_kind
                            if zoning_jurisdiction_kind != "unknown"
                            else "zoning"
                        ),
                    },
                }
            ],
        }
    neighbors = [feature for feature in context_features if feature.kind == "neighbor_parcel"]
    projects = [feature for feature in context_features if feature.kind == "public_project"]
    utilities = [
        feature
        for feature in context_features
        if feature.kind.startswith("utility_")
    ]
    electric_lines = [
        feature for feature in utilities
        if feature.kind == "utility_electric_transmission"
    ]
    electric_substations = [
        feature for feature in utilities
        if feature.kind == "utility_electric_substation"
    ]
    # Dedicated sewer / water CCN toggles (spec sewer_availability_screening
    # §3, Admin: "i need it also as a layer") — same features that feed the
    # combined Utilities layer, split by service so each screens on its own.
    water_ccn_features = [
        feature for feature in utilities if feature.kind == "utility_ccn_water"
    ]
    sewer_ccn_features = [
        feature for feature in utilities
        if feature.kind == "utility_ccn_wastewater"
    ]
    sewer_ccn_is_nearest_only = bool(sewer_ccn_features) and not any(
        feature.map_properties.get("authority") == "legal_ccn"
        for feature in sewer_ccn_features
    )
    nearest_sewer_providers = sorted({
        feature.subtitle
        for feature in sewer_ccn_features
        if feature.subtitle
        and feature.map_properties.get("authority") == "nearest_ccn"
    })
    district_ccn_features = [
        feature for feature in utilities
        if feature.kind == "utility_ccn_district"
    ]
    traffic = [
        feature
        for feature in context_features
        if feature.kind in {"traffic_count", "traffic_station"}
    ]
    access = [feature for feature in context_features if feature.kind.startswith("access_")]
    access_roadways = [feature for feature in access if feature.kind == "access_roadway"]
    subject_frontages = [feature for feature in access if feature.kind == "access_subject_frontage"]
    subject_driveways = [feature for feature in access if feature.kind == "access_subject_driveway"]
    on_site_driveways = [feature for feature in access if feature.kind == "access_on_site_driveway"]
    nearby_driveways = [feature for feature in access if feature.kind == "access_nearby_driveway"]
    access_signals = [feature for feature in access if feature.kind == "access_signal"]
    site_constraint_result = (
        run.adapter_results.get("site_constraints")
        if isinstance(run.adapter_results, dict)
        else None
    )
    site_constraint_facts = (
        site_constraint_result.get("normalized_facts")
        if isinstance(site_constraint_result, dict)
        else None
    )
    driveway_screen_present = (
        isinstance(site_constraint_facts, dict) and "access_features" in site_constraint_facts
    )
    environmental_geometry = [
        feature for feature in context_features if feature.kind.startswith("environment_")
    ]
    regulatory_geometry = [
        feature
        for feature in context_features
        if feature.kind.startswith("regulatory_district_")
    ]
    buildable = [
        feature for feature in context_features if feature.kind == "buildable_envelope"
    ]
    elevation_facts = _facts(run, "usgs_elevation")
    flood_facts = _facts(run, "fema_nfhl")
    soil_facts = _facts(run, "soil_intel")
    environmental_details = _environmental_details(run)
    buildable_details = buildable[0].details if buildable else _buildable_details(run)
    population_density, population_density_details = _population_density_geojson(
        run, parcel=parcel,
    )
    zoning_details = [{
        "label": "Jurisdiction",
        "value": _zoning_jurisdiction_label(run),
    }]
    zoning_details.append({
        "label": "Base zoning district",
        "value": (
            zoning
            or (
                "Not assigned from ETJ status"
                if zoning_jurisdiction_kind == "etj"
                else "Not verified from published city records"
            )
        ),
    })
    use_action = zoning_facts.get("zoning_use_action")
    if isinstance(use_action, str) and use_action.strip():
        zoning_details.append({
            "label": "Recorded land-use action",
            "value": use_action.strip(),
        })
        use_status = str(zoning_facts.get("zoning_use_status") or "").strip()
        zoning_details.append({
            "label": "Land-use action outcome",
            "value": (
                use_status.replace("_", " ").title()
                if use_status and use_status.casefold() != "unknown"
                else "Not published in the retrieved record"
            ),
        })
    if jurisdiction_facts.get("broker_summary"):
        zoning_details.append({
            "label": "Review context",
            "value": str(jurisdiction_facts["broker_summary"]).strip(),
        })
    elevation_details = []
    if elevation_facts.get("center_elevation_ft") is not None:
        elevation_details.append({
            "label": "Subject elevation",
            "value": f"{float(elevation_facts['center_elevation_ft']):,.1f} ft",
        })
        elevation_details.append({
            "label": "Sampled parcel range",
            "value": (
                f"{float(elevation_facts['min_elevation_ft']):,.1f}-"
                f"{float(elevation_facts['max_elevation_ft']):,.1f} ft "
                f"({int(elevation_facts.get('sample_count') or 1)} samples)"
            ),
        })
        elevation_details.append({
            "label": "Screened relief",
            "value": f"{float(elevation_facts.get('relief_ft') or 0):,.1f} ft",
        })
    else:
        elevation_details.append({
            "label": "Numerical elevation",
            "value": "Not stored in this Public Data snapshot",
        })
    if elevation_facts.get("sampled_max_grade_pct") is not None:
        elevation_details.append({
            "label": "Maximum sampled DEM grade",
            "value": f"{float(elevation_facts['sampled_max_grade_pct']):,.1f}%",
        })
    if soil_facts.get("max_component_slope_pct") is not None:
        elevation_details.append({
            "label": "SSURGO component slope",
            "value": (
                f"Up to {float(soil_facts['max_component_slope_pct']):,.1f}%"
            ),
        })
    elevation_details.append({
        "label": "Hydric soil component",
        "value": (
            "Present"
            if soil_facts.get("any_hydric_component")
            else "Not indicated"
        ),
    })

    flood_details = []
    zones = flood_facts.get("zones_found") or []
    if zones:
        flood_details.append({
            "label": "FEMA zones", "value": ", ".join(map(str, zones)),
        })
    flood_details.extend([
        {
            "label": "Regulatory floodway",
            "value": (
                "Intersected"
                if flood_facts.get("in_regulatory_floodway")
                else "Not indicated"
            ),
        },
        {
            "label": "1% annual-chance / SFHA",
            "value": (
                "Intersected" if flood_facts.get("has_sfha") else "Not indicated"
            ),
        },
        {
            "label": "0.2% annual-chance / 500-year",
            "value": (
                "Intersected"
                if flood_facts.get("has_shaded_x")
                else "Not indicated"
            ),
        },
        {
            "label": "Base flood elevation",
            "value": (
                f"{float(flood_facts['base_flood_elevation']):,.1f} ft"
                if flood_facts.get("base_flood_elevation") is not None
                else "Not published in intersected feature"
            ),
        },
    ])
    if flood_facts.get("sfha_intersection_acres") is not None:
        flood_details.append({
            "label": "SFHA area on subject",
            "value": f"{float(flood_facts.get('sfha_intersection_acres') or 0):,.2f} ac",
        })
    if flood_facts.get("floodway_intersection_acres") is not None:
        flood_details.append({
            "label": "Floodway area on subject",
            "value": f"{float(flood_facts.get('floodway_intersection_acres') or 0):,.2f} ac",
        })
    effective_lomrs = [
        row for row in (flood_facts.get("lomr_cases") or [])
        if isinstance(row, dict) and row.get("case_no")
    ]
    if effective_lomrs:
        lomr = max(
            effective_lomrs,
            key=lambda row: str(row.get("effective_date") or ""),
        )
        lomr_parts = [f"LOMR {lomr['case_no']}"]
        if lomr.get("effective_date"):
            lomr_parts.append(f"effective {lomr['effective_date']}")
        if lomr.get("status"):
            lomr_parts.append(str(lomr["status"]))
        if lomr.get("source_citation"):
            lomr_parts.append(f"source {lomr['source_citation']}")
        lomr_value = "; ".join(lomr_parts)
        provenance_rows = [
            {"label": "Current FEMA map revision", "value": lomr_value},
            {
                "label": "Map-version warning",
                "value": (
                    "Current NFHL geometry reflects this effective revision. "
                    "A plat, survey, or flood certificate dated earlier may show "
                    "a prior zone; compare record dates and confirm with the "
                    "community floodplain administrator or FEMA case record."
                ),
            },
        ]
        flood_details.extend(provenance_rows)
        buildable_details.extend(provenance_rows)

    layers = [
        SiteCheckMapLayer(
            id="parcel",
            label="Parcel",
            kind="geojson",
            available=parcel is not None,
            status="verified" if parcel is not None else "address_pin_only",
            geojson=(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": parcel_geojson,
                            "properties": {
                                "label": "Subject property",
                                "parcel_id": str(parcel_facts.get("parcel_id") or ""),
                                "acreage": parcel_facts.get("acreage"),
                            },
                        }
                    ],
                }
                if parcel_geojson
                else None
            ),
            attribution="County appraisal district parcel geometry",
            advisory=(None if parcel is not None else "Verified parcel boundary unavailable."),
            legend=[{"label": "Subject property", "color": "#F2C94C"}],
            details=parcel_details,
        ),
        SiteCheckMapLayer(
            id="neighbors",
            label="Neighboring land",
            kind="geojson",
            available=bool(neighbors),
            status="verified" if neighbors else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(feature) for feature in neighbors],
            } if neighbors else None,
            attribution="County appraisal district parcel layer",
            advisory=(
                "Neighbor details are limited to fields published by the CAD GIS layer; "
                "owner, deed, survey, and development plans require their governing records."
                if neighbors else "Neighboring parcel geometry is unavailable in this snapshot."
            ),
            legend=[{"label": "Neighboring CAD parcels", "color": "#64748B"}],
            details=[{"label": "Clickable parcels", "value": str(len(neighbors))}],
        ),
        SiteCheckMapLayer(
            id="projects",
            label="Public projects",
            kind="geojson",
            available=bool(projects),
            status="verified" if projects else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(feature) for feature in projects],
            } if projects else None,
            attribution="Texas Department of Transportation",
            advisory=(
                "Project markers use the center of public project geometry for map context; "
                "open the project record for exact limits."
                if projects else "No stored public project geometry is available."
            ),
            legend=[{"label": "Public infrastructure project (P)", "color": "#172033"}],
            details=[{"label": "Clickable projects", "value": str(len(projects))}],
        ),
        SiteCheckMapLayer(
            id="utilities",
            label="Utilities",
            kind="geojson",
            available=bool(utilities),
            status="screening" if utilities else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(feature) for feature in utilities],
            } if utilities else None,
            attribution=(
                "TWDB / PUC / TCEQ service areas; HIFLD electric infrastructure"
                if _in_texas(run) else "HIFLD electric infrastructure; OpenStreetMap"
            ),
            advisory=(
                "CCN and operating-area boundaries identify service authority, not "
                "capacity, main location, pipe size, gravity/force-main path, "
                "extension cost, or a right to connect. HIFLD transmission and "
                "substation proximity does not establish three-phase service, "
                "headroom, interconnection, easements, upgrade cost, or deliverable "
                "power. Obtain provider maps, will-serve/SER, and recorded evidence."
                if utilities
                else "No map-ready utility service-area or electric-grid geometry is stored."
            ),
            legend=[
                {"label": "Water service territory", "color": "#0284C7"},
                {"label": "Wastewater service territory", "color": "#059669"},
                {"label": "Electric transmission line", "color": "#A21CAF"},
                {"label": "Electric substation", "color": "#EA580C"},
                {"label": "Mapped water / wastewater main", "color": "#0F766E"},
                {"label": "Gas pipeline", "color": "#92400E"},
            ],
            details=[
                {
                    "label": "Clickable utility features",
                    "value": str(len(utilities)),
                },
                {
                    "label": "Electric infrastructure",
                    "value": (
                        f"{len(electric_lines)} lines / "
                        f"{len(electric_substations)} substations"
                    ),
                },
                {
                    "label": "Water / wastewater authority",
                    "value": (
                        "Public CCN or operating-area screen" if _in_texas(run)
                        else f"Not connected for {_state_name(run)} yet"
                    ),
                },
                {
                    "label": "Mains, capacity, and tie-in",
                    "value": "Unverified - obtain utility maps and will-serve/SER",
                },
                {
                    "label": "Electric and pipeline constraints",
                    "value": "Unverified - obtain provider and recorded-easement evidence",
                },
            ],
        ),
        SiteCheckMapLayer(
            id="sewer_ccn",
            label=(
                "Nearest certificated sewer area (context only)"
                if sewer_ccn_is_nearest_only
                else "Sewer service areas"
            ),
            kind="geojson",
            available=bool(sewer_ccn_features),
            status="screening" if sewer_ccn_features else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(f) for f in sewer_ccn_features],
            } if sewer_ccn_features else None,
            attribution="PUCT CCN sewer service areas",
            advisory=(
                f"No state-certificated sewer area covers this parcel. It is inside "
                f"{municipal_wastewater_candidate} city limits, so contact "
                f"{municipal_wastewater_candidate} first. The mapped "
                f"{', '.join(nearest_sewer_providers) or 'nearby'} area is the nearest "
                "CCN and is context only; it does not serve this parcel. This public "
                "source does not publish City sewer-main geometry."
                if municipal_wastewater_candidate and sewer_ccn_is_nearest_only
                else "State-certificated sewer service areas (CCNs). When none "
                "covers the parcel, the NEAREST areas found are shown so you "
                "can see where organized sewer service is — a shown area does "
                "not mean this parcel is served. Authority, not capacity: only "
                "the provider's written availability answer is authoritative."
                if sewer_ccn_features
                else "No sewer service area covers this parcel or was found "
                     "within the search radius (up to ~5 km)."
            ),
            legend=[
                {
                    "label": (
                        "Nearest sewer CCN - context only"
                        if sewer_ccn_is_nearest_only
                        else "Sewer service area"
                    ),
                    "color": "#059669",
                },
            ],
            details=[
                {
                    "label": (
                        "Nearest CCN areas shown"
                        if sewer_ccn_is_nearest_only
                        else "Sewer areas shown"
                    ),
                    "value": str(len(sewer_ccn_features)),
                },
                *([{
                    "label": "First city sewer contact",
                    "value": municipal_wastewater_candidate,
                }] if municipal_wastewater_candidate else []),
                *([{
                    "label": "Mapped City sewer mains",
                    "value": "Not published by this public source",
                }] if municipal_wastewater_candidate and sewer_ccn_is_nearest_only else []),
                {"label": "Capacity / tie-in", "value": "Not established by a CCN boundary"},
            ],
        ),
        SiteCheckMapLayer(
            id="water_districts",
            label="Utility districts (sewer check)",
            kind="geojson",
            available=bool(district_ccn_features),
            status="screening" if district_ccn_features else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(f) for f in district_ccn_features],
            } if district_ccn_features else None,
            attribution="TCEQ Water Districts (MUD / SUD / WCID / FWSD)",
            advisory=(
                "Utility districts covering the parcel where NO certificated "
                "sewer area exists — districts of this kind commonly provide "
                "sewer inside their boundary. Only the district's written "
                "availability answer is authoritative."
                if district_ccn_features
                else "Shown only when no certificated sewer area covers the "
                     "parcel and a TCEQ utility district does."
            ),
            legend=[
                {"label": "Utility district boundary", "color": "#7C3AED"},
            ],
            details=[
                {"label": "Districts shown", "value": str(len(district_ccn_features))},
            ],
        ),
        SiteCheckMapLayer(
            id="water_ccn",
            label="Water service areas",
            kind="geojson",
            available=bool(water_ccn_features),
            status="screening" if water_ccn_features else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(f) for f in water_ccn_features],
            } if water_ccn_features else None,
            attribution="TWDB / PUCT CCN water service areas",
            advisory=(
                "State-certificated water service areas (CCNs). Authority, not "
                "capacity — only the provider's written availability answer is "
                "authoritative."
                if water_ccn_features
                else "No water service area covers or borders this parcel in "
                     "the state CCN records."
            ),
            legend=[
                {"label": "Water service area", "color": "#0284C7"},
            ],
            details=[
                {"label": "Water areas shown", "value": str(len(water_ccn_features))},
            ],
        ),
        SiteCheckMapLayer(
            id="traffic",
            label="Traffic",
            kind="geojson",
            available=bool(traffic),
            status="verified" if traffic else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(feature) for feature in traffic],
            } if traffic else None,
            attribution=(
                "Texas Department of Transportation AADT and Roadway Inventory"
            ),
            advisory=(
                "AADT lines are public traffic-count context. Driveways, median "
                "openings, signals, access classification, and final spacing "
                "require current plans, field review, and agency confirmation."
                if traffic
                else "No map-ready TxDOT traffic-count geometry is stored."
            ),
            legend=[
                {"label": "Under 5,000 vehicles/day", "color": "#16A34A"},
                {"label": "5,000-14,999 vehicles/day", "color": "#CA8A04"},
                {"label": "15,000-29,999 vehicles/day", "color": "#EA580C"},
                {"label": "30,000-59,999 vehicles/day", "color": "#DC2626"},
                {"label": "60,000+ vehicles/day", "color": "#7F1D1D"},
                {"label": "Count unavailable", "color": "#64748B"},
            ],
            details=[
                {
                    "label": "Mapped AADT observations", "value": str(len(traffic)),
                },
                {
                    "label": "Density measure",
                    "value": "TxDOT AADT (annual average vehicles per day)",
                },
                {
                    "label": "Driveways, medians, signals, future ROW",
                    "value": "Verify with current plans, field review, and road authority",
                },
            ],
        ),
        SiteCheckMapLayer(
            id="access",
            label="Access",
            kind="geojson",
            available=bool(access),
            status="screening" if access else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(feature) for feature in access],
            } if access else None,
            attribution=(
                "Texas Department of Transportation Roadway Inventory; "
                "OpenStreetMap contributors"
            ),
            advisory=(
                "Mapped frontage means the parcel edge is close to published road "
                "geometry. It does not establish legal access or an approved curb "
                "cut. Community-mapped driveways and signals may be incomplete; "
                "verify permits, spacing, medians, future ROW, and field conditions."
                if access else "No map-ready roadway inventory geometry is stored."
            ),
            legend=[
                {"label": "Mapped subject frontage", "color": "#059669"},
                {"label": "Mapped subject curb cut", "color": "#BE185D"},
                {"label": "Mapped on-site driveway", "color": "#D97706"},
                {"label": "Nearby mapped driveway", "color": "#7C3AED"},
                {"label": "Traffic signal", "color": "#DC2626"},
                {"label": "Roadway inventory", "color": "#2563EB"},
            ],
            details=[
                *_access_facts_details(site_constraint_facts),
                {
                    "label": "Mapped subject frontage",
                    "value": (
                        f"{len(subject_frontages)} road relationship(s); click green lines"
                        if subject_frontages
                        else "No frontage relationship resolved from stored public road geometry"
                    ),
                },
                {
                    "label": "Mapped subject curb cuts",
                    "value": (
                        f"{len(subject_driveways)} community-mapped feature(s); verify in field"
                        if subject_driveways
                        else (
                            "Not evaluated: this snapshot has no community-mapped "
                            "driveway screen"
                            if not driveway_screen_present
                            else (
                                "No mapped subject curb cut found. This does not mean the "
                                "parcel lacks physical or legal access."
                            )
                        )
                    ),
                },
                {
                    "label": "Mapped on-site driveways",
                    "value": (
                        f"{len(on_site_driveways)} feature(s); no parcel-edge crossing resolved"
                        if on_site_driveways else "0"
                    ),
                },
                {
                    "label": "Nearby mapped driveways",
                    "value": str(len(nearby_driveways)),
                },
                {
                    "label": "Mapped traffic signals",
                    "value": str(len(access_signals)),
                },
                {
                    "label": "Roadway inventory segments",
                    "value": str(len(access_roadways)),
                },
                {
                    "label": "Decision limit",
                    "value": "Legal access and driveway approval require title, survey, field, and road-authority review",
                },
            ],
        ),
        SiteCheckMapLayer(
            id="population_density",
            label="Population density",
            kind="geojson",
            available=population_density is not None,
            status="screening" if population_density else "unavailable",
            geojson=population_density,
            attribution="U.S. Census Bureau American Community Survey",
            advisory=(
                "Density uses stored ACS population rings and fixed radial areas. "
                "Blue is under 500, yellow is 500-1,499, and red is 1,500 or more "
                "people per square mile. It is descriptive screening, not a "
                "drive-time trade area or demand forecast."
            ),
            legend=[
                {"label": "Under 500 people/sq mi", "color": "#2563EB"},
                {"label": "500-1,499 people/sq mi", "color": "#EAB308"},
                {"label": "1,500+ people/sq mi", "color": "#DC2626"},
            ],
            details=population_density_details,
        ),
        SiteCheckMapLayer(
            id="regulatory",
            label="Special districts",
            kind="geojson",
            available=bool(regulatory_geometry),
            status="screening" if regulatory_geometry else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [
                    _feature_geojson(feature) for feature in regulatory_geometry
                ],
            } if regulatory_geometry else None,
            attribution="Municipal, county, and public entitlement records",
            advisory=(
                "Regulatory facts are public-record screening. Current planner "
                "confirmation, official zoning verification, plat documents, and "
                "permit file review control entitlement reliance."
            ),
            legend=[
                {"label": "MUD / WCID", "color": "#7C3AED"},
                {"label": "PID / TIRZ", "color": "#BE185D"},
                {"label": "Subdivision / other district", "color": "#475569"},
            ],
            details=[
                {
                    "label": "Mapped district boundaries",
                    "value": str(len(regulatory_geometry)),
                },
                *[
                    {"label": feature.subtitle or "District", "value": feature.title}
                    for feature in regulatory_geometry[:6]
                ],
            ] if regulatory_geometry else [],
        ),
        SiteCheckMapLayer(
            id="environmental",
            label="Environmental",
            kind="geojson",
            available=bool(environmental_geometry),
            status="screening" if environmental_geometry else "unavailable",
            geojson={
                "type": "FeatureCollection",
                "features": [
                    _feature_geojson(feature) for feature in environmental_geometry
                ],
            } if environmental_geometry else None,
            attribution=(
                "FEMA / USFWS / TCEQ / NWI / USDA SSURGO / public environmental records"
                if _in_texas(run)
                else "FEMA / USFWS / NWI / USDA SSURGO / EPA ECHO"
            ),
            advisory=(
                "Environmental layers are desktop screening only. Phase I, "
                "wetland delineation, endangered-species, Edwards aquifer, karst, "
                "and jurisdictional determinations require specialist review."
            ),
            legend=[
                {"label": "Edwards aquifer protection zone", "color": "#0891B2"},
                {"label": "Critical habitat", "color": "#16A34A"},
            ],
            details=environmental_details,
        ),
        SiteCheckMapLayer(
            id="buildable_envelope",
            label="Preliminary development envelope",
            kind="geojson",
            available=bool(buildable),
            status="screening" if buildable else "incomplete",
            geojson={
                "type": "FeatureCollection",
                "features": [_feature_geojson(feature) for feature in buildable],
            } if buildable else None,
            attribution="County parcel geometry / zoning standards",
            advisory=(
                "This is a preliminary public-data development screen, not a site "
                "plan or legal building envelope. Parcel-clipped public constraints "
                "are applied where defensible; land outside this conservative screen "
                "is not automatically unbuildable. The details state which ROW, "
                "easement, utility, drainage, wetland, access, and civil inputs remain "
                "unresolved."
            ),
            legend=[{"label": "Public-data screening envelope", "color": "#D97706"}],
            details=buildable_details,
        ),
        SiteCheckMapLayer(
            id="flood",
            label="Flood",
            kind="raster",
            available=run.lat is not None and run.lng is not None,
            status="screening",
            tile_url=_FEMA_TILE,
            attribution="FEMA National Flood Hazard Layer",
            advisory=_advisory(run, "fema_nfhl"),
            legend=[{"label": "Official FEMA flood symbology", "color": "#2563EB"}],
            details=flood_details,
        ),
        _drainage_layer(run),
        SiteCheckMapLayer(
            id="wetlands",
            label="Wetlands",
            kind="raster",
            available=run.lat is not None and run.lng is not None,
            status="screening",
            tile_url=_WETLANDS_TILE,
            attribution="U.S. Fish and Wildlife Service NWI",
            advisory=_advisory(run, "wetlands_nwi"),
            legend=[{"label": "Mapped wetland indicators", "color": "#16A34A"}],
        ),
        SiteCheckMapLayer(
            id="zoning",
            label="Zoning / jurisdiction",
            kind="geojson",
            available=zoning_geojson is not None,
            status="subject_only" if zoning_geojson else "unavailable",
            geojson=zoning_geojson,
            attribution="Official municipal zoning record",
            advisory=(
                "This subject-parcel overlay separates jurisdiction from the base "
                "zoning district. A colored parcel does not prove a zoning district; "
                "use the details and Public Data zoning card for the governing record."
                if zoning_geojson
                else "Programmatic subject zoning is unavailable."
            ),
            details=zoning_details,
            legend=(
                [{
                    "label": zoning_label or "Subject zoning",
                    "color": (
                        "#0891B2"
                        if zoning_jurisdiction_kind == "etj"
                        else "#7C3AED"
                    ),
                }]
                if zoning_geojson
                else []
            ),
        ),
        SiteCheckMapLayer(
            id="elevation",
            label="Elevation",
            kind="raster",
            available=run.lat is not None and run.lng is not None,
            status="screening",
            tile_url=_ELEVATION_TILE,
            opacity=0.48,
            attribution="USGS 3D Elevation Program",
            advisory=(
                str(elevation_facts.get("advisory"))
                if elevation_facts.get("advisory")
                else "Hillshade is terrain screening, not a boundary or survey elevation."
            ),
            legend=[{"label": "USGS 3DEP hillshade", "color": "#64748B"}],
            details=elevation_details,
        ),
    ]
    if not _in_texas(run):
        layers = [_outside_texas_layer(layer, run) for layer in layers]
    return layers


# Layers drawn only from Texas agencies (TxDOT, TWDB/PUC, TCEQ). Outside
# Texas they are not connected: no shapes, no "0 shown", no Texas credit.
_TEXAS_ONLY_LAYERS = {"projects", "sewer_ccn", "water_districts", "water_ccn", "traffic"}


def _outside_texas_layer(layer: SiteCheckMapLayer, run: Any) -> SiteCheckMapLayer:
    not_connected = f"Not connected for {_state_name(run)} yet"
    if layer.id in _TEXAS_ONLY_LAYERS:
        return layer.model_copy(update={
            "available": False, "status": "unavailable", "geojson": None,
            "attribution": "", "advisory": f"{not_connected}.", "legend": [],
            "details": [{"label": "Status", "value": not_connected}],
        })
    if layer.id == "access":
        # Road frontage and inventory come from TxDOT; OSM features remain.
        return layer.model_copy(update={
            "attribution": "OpenStreetMap contributors",
            "details": [
                row for row in layer.details
                if row.get("label") != "Roadway inventory segments"
            ] + [{"label": "Road inventory", "value": not_connected}],
        })
    return layer


def build_map_context(run: SiteCheckRun) -> SiteCheckMapContext:
    """Build map context from one already-authorized stored run."""
    parcel = _parcel_shape(run)
    lat, lng = _center(run, parcel)
    raw_points = _dedupe_points(
        [
            *_official_points(run),
            *_housing_project_points(run),
            *_legacy_osm_points(run),
        ]
    )
    context_features = _context_features(run, parcel=parcel, lat=lat, lng=lng)
    population_geojson, _population_details = _population_density_geojson(
        run, parcel=parcel,
    )
    if population_geojson is not None:
        for feature in population_geojson.get("features") or []:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            geometry = feature.get("geometry")
            if not isinstance(properties, dict) or not isinstance(geometry, dict):
                continue
            density = properties.get("population_density_per_sqmi")
            population = properties.get("incremental_population")
            context_features.append(SiteCheckMapFeature(
                id=str(properties.get("id") or "population-density"),
                kind="population_density",
                title=str(properties.get("title") or "Population density"),
                subtitle=str(properties.get("display_label") or "ACS density screen"),
                source="U.S. Census Bureau American Community Survey",
                confidence="screened_indicator",
                geometry=geometry,
                details=[
                    {
                        "label": "Population density",
                        "value": f"{float(density or 0):,.0f} people/sq mi",
                    },
                    {
                        "label": "Estimated population in band",
                        "value": f"{float(population or 0):,.0f}",
                    },
                    {
                        "label": "ACS year",
                        "value": str(properties.get("acs_year") or "Not stored"),
                    },
                    {
                        "label": "Method",
                        "value": "ACS block-group centroid radial screening; not drive-time routed",
                    },
                ],
                map_properties=dict(properties),
            ))

    places: list[SiteCheckMapPlace] = []
    for row in raw_points:
        distance_m, basis, nearest = _parcel_distance(
            parcel,
            parcel_lat=lat,
            parcel_lng=lng,
            point_lat=row["lat"],
            point_lng=row["lng"],
        )
        places.append(
            SiteCheckMapPlace(
                id=row["id"],
                group=str(row.get("group") or "other"),
                category=str(row.get("category") or "other"),
                name=row["name"],
                lat=row["lat"],
                lng=row["lng"],
                address=(str(row.get("address"))[:240] if row.get("address") else None),
                brand=(str(row.get("brand"))[:120] if row.get("brand") else None),
                source=str(row.get("source") or "Public Data")[:120],
                confidence=str(row.get("confidence") or "screened_indicator")[:40],
                distance_m=round(distance_m, 2),
                distance_basis=basis,
                nearest_subject=nearest,
            )
        )
    places.sort(key=lambda place: (place.distance_m, place.name.casefold(), place.id))

    counts = Counter(place.category for place in places)
    categories: list[SiteCheckMapCategory] = []
    for category, count in counts.items():
        label, group, icon = _CATEGORY_META.get(
            category,
            (category.replace("_", " ").title(), "other", "map-pin"),
        )
        categories.append(
            SiteCheckMapCategory(
                id=category,
                label=label,
                group=group,
                icon=icon,
                count=count,
                default_selected=category in _DEFAULT_CATEGORIES,
            )
        )
    categories.sort(key=lambda item: (-int(item.default_selected), item.label))

    advisories = []
    if parcel is None:
        advisories.append(
            "Verified parcel geometry is unavailable; proximity is measured from the address pin."
        )
    if not places:
        advisories.append("No map-ready nearby places were stored in this Public Data snapshot.")

    return SiteCheckMapContext(
        run_id=run.id,
        fetched_at=run.finished_at,
        address=run.address,
        center=SiteCheckMapCoordinate(lat=lat, lng=lng),
        bounds=_bounds(parcel, lat, lng),
        subject_anchor="parcel" if parcel is not None else "address_pin",
        parcel_geojson=mapping(parcel) if parcel is not None else None,
        basemaps=list(_BASEMAPS),
        layers=_layers(run, parcel, context_features),
        categories=categories,
        places=places,
        context_features=context_features,
        advisories=advisories,
    )


__all__ = ["build_map_context"]
