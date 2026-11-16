"""austin_zoning adapter — TX-Central city zoning dispatcher.

Spec source_id is `austin_zoning` per docs/specs/public_data.md
§4 matrix, but the implementation is the full legacy_services
multi-city zoning dispatcher (11 wired cities + Manor as
manual). Austin is one of the wired cities; the dispatcher's
job is to find the right city / ETJ / unincorporated answer
for any TX-Central coordinate.

Ported from legacy_services/core/zoning_fetchers.py per
public_data.md §5. Algorithm + city configs + 4-state status
taxonomy + ETJ handling preserved verbatim.
"""

from __future__ import annotations

import html
import io
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from xml.etree import ElementTree

import requests
from shapely.geometry import Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
    map_legacy_status,
)

log = logging.getLogger("propdesk.site_check.austin_zoning")
SOURCE_ID = "austin_zoning"

_TIMEOUT = 20

_ZONING_ADVISORIES = {
    "clear": "Property is in a zoning district; see record for details.",
    "mixed": (
        "Property intersects multiple zoning districts (split-zoned). "
        "Each district's rules apply to the portion within it."
    ),
    "contradicted": (
        "Uploaded documents reference zoning that conflicts with the "
        "public result; manual review required."
    ),
    "unknown": "Zoning determination incomplete.",
}
_NO_CITY_ZONING_ADVISORY = (
    "No city zoning applies. Subdivision, platting, ETJ, drainage, "
    "environmental, utility, and access rules may still govern this parcel."
)


# ── City configs (verbatim from legacy_services) ─────────────────────


@dataclass(frozen=True)
class _City:
    name: str
    arcgis_url: str
    base_zone_field: str
    zoning_field: str
    description_field: str | None
    bbox: tuple[float, float, float, float]
    jurisdiction_url: str | None = None
    jurisdiction_where: str | None = None


_AUSTIN = _City(
    name="Austin",
    arcgis_url=(
        "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/"
        "services/Publish_Zoning_AGOL/FeatureServer/0/query"
    ),
    base_zone_field="BASE_ZONE",
    zoning_field="ZONING_ZTYPE",
    description_field="BASE_ZONE_CATEGORY",
    bbox=(30.10, 30.55, -97.95, -97.55),
    jurisdiction_url=(
        "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/"
        "services/BOUNDARIES_jurisdictions/FeatureServer/0/query"
    ),
)
_PFLUGERVILLE = _City(
    name="Pflugerville",
    arcgis_url=(
        "https://maps.pflugervilletx.gov/arcgis/rest/services/"
        "Planning/Zoning_Districts/MapServer/0/query"
    ),
    base_zone_field="ZONING",
    zoning_field="ZONING",
    description_field="DESCRIPTION",
    bbox=(30.40, 30.51, -97.70, -97.55),
)
_CEDAR_PARK = _City(
    name="Cedar Park",
    arcgis_url=(
        "https://gisrest.cedarparktexas.gov/cpgis/rest/services/"
        "Planning/Zoning/MapServer/3/query"
    ),
    base_zone_field="ZoningAbbrev",
    zoning_field="ZoningType",
    description_field="ZoningDescription",
    bbox=(30.48, 30.58, -97.86, -97.74),
)
_GEORGETOWN = _City(
    name="Georgetown",
    arcgis_url=(
        "https://gis.georgetowntexas.gov/arcgis/rest/services/"
        "SystemsEngineering/DevelopmentPipelineBase_External_WebMap/"
        "MapServer/0/query"
    ),
    base_zone_field="ZONING",
    zoning_field="ZONING",
    description_field=None,
    bbox=(30.60, 30.74, -97.78, -97.60),
)
_HUTTO = _City(
    name="Hutto",
    arcgis_url=(
        "https://services.arcgis.com/YZhxlqU7ABWQBGTG/arcgis/rest/"
        "services/Hutto_Zoning_Districts/FeatureServer/0/query"
    ),
    base_zone_field="ZONING",
    zoning_field="ZONING",
    description_field=None,
    bbox=(30.50, 30.60, -97.60, -97.45),
)
_ROUND_ROCK = _City(
    name="Round Rock",
    arcgis_url=(
        "https://maps.roundrocktexas.gov/arcgis/rest/services/"
        "Planning/Planning_Multi/MapServer/12/query"
    ),
    base_zone_field="BASE_ZONIN",
    zoning_field="BASE_ZONIN",
    description_field="LOCATION",
    bbox=(30.46, 30.58, -97.74, -97.64),
)
_BUDA = _City(
    name="Buda",
    arcgis_url=(
        "https://services6.arcgis.com/vXZW4vAaPRr14z2s/arcgis/rest/"
        "services/Zoning/FeatureServer/0/query"
    ),
    base_zone_field="Zoning_Category",
    zoning_field="Zoning_Category",
    description_field="Zoning_Description",
    bbox=(30.05, 30.11, -97.86, -97.80),
)
_KYLE = _City(
    name="Kyle",
    arcgis_url=(
        "https://utility.arcgis.com/usrsvcs/servers/"
        "cb715452b5464cd08d53449e26fa913d/rest/services/"
        "KCH-ESRI/Zoning/FeatureServer/0/query"
    ),
    base_zone_field="Z_Code",
    zoning_field="Z_Code",
    description_field=None,
    bbox=(29.95, 30.04, -97.95, -97.85),
)
_DRIPPING_SPRINGS = _City(
    name="Dripping Springs",
    arcgis_url=(
        "https://services6.arcgis.com/XnTA1N5QxtOFa9o8/arcgis/rest/"
        "services/CODS_Zoning/FeatureServer/0/query"
    ),
    base_zone_field="Zoning_Abbreviation",
    zoning_field="Zoning_Abbreviation",
    description_field="Zoning_District",
    bbox=(30.16, 30.22, -98.13, -98.05),
)
_SAN_MARCOS = _City(
    name="San Marcos",
    arcgis_url=(
        "https://services1.arcgis.com/M68M8H7oABBFs1Pf/arcgis/rest/"
        "services/Parcel_by_Zoning_SpatialJoin/FeatureServer/21/query"
    ),
    base_zone_field="ZONECODE_Zoning",
    zoning_field="ZONECODE_Zoning",
    description_field="ZONINGDIST_Zoning",
    bbox=(29.75, 30.00, -98.15, -97.85),
)
_LEANDER = _City(
    name="Leander",
    arcgis_url=(
        "https://services1.arcgis.com/L0MLvN0Ay0iEjnCT/arcgis/rest/"
        "services/Leander_Current_Zoning/FeatureServer/3/query"
    ),
    base_zone_field="Use_",
    zoning_field="Use_",
    description_field=None,
    bbox=(30.51, 30.65, -97.95, -97.77),
)
_TAYLOR = _City(
    name="Taylor",
    arcgis_url=(
        "https://services7.arcgis.com/SQVxkeGOcRYhZqOD/arcgis/rest/"
        "services/Zoning_011720/FeatureServer/46/query"
    ),
    base_zone_field="First_Plac",
    zoning_field="First_Plac",
    description_field=None,
    bbox=(30.53, 30.63, -97.51, -97.36),
)
_ELGIN = _City(
    name="Elgin",
    arcgis_url=(
        "https://services3.arcgis.com/wdTkTU0MdZbNBEZy/arcgis/rest/"
        "services/Elgin_Zoning/FeatureServer/0/query"
    ),
    base_zone_field="Zone_Code",
    zoning_field="Zoning",
    description_field="Zoning",
    bbox=(30.25, 30.43, -97.50, -97.25),
)


_WIRED_CITIES: tuple[_City, ...] = (
    _AUSTIN, _PFLUGERVILLE, _CEDAR_PARK, _GEORGETOWN,
    _HUTTO, _ROUND_ROCK,
    _BUDA, _KYLE, _DRIPPING_SPRINGS, _SAN_MARCOS, _LEANDER, _TAYLOR,
    _ELGIN,
)


@dataclass(frozen=True)
class _ManualCity:
    name: str
    bbox: tuple[float, float, float, float]
    portal_url: str


@dataclass(frozen=True)
class _DownloadableCity:
    """Official downloadable polygon source, independent of GIS vendor."""

    name: str
    archive_url: str
    archive_member: str
    base_zone_field: str
    zoning_field: str
    description_field: str | None
    bbox: tuple[float, float, float, float]
    portal_url: str
    source_label: str


_DOWNLOADABLE_CITIES: tuple[_DownloadableCity, ...] = (
    _DownloadableCity(
        name="Manor",
        archive_url=(
            "https://www.manortx.gov/DocumentCenter/View/2340/KMZ-Files"
        ),
        archive_member="Zoning.kmz",
        base_zone_field="ZONE",
        zoning_field="ZONE",
        description_field=None,
        bbox=(30.32, 30.39, -97.58, -97.46),
        portal_url="https://www.manortx.gov/266/Maps-Exhibits",
        source_label="Manor official downloadable zoning GIS",
    ),
)

_DOWNLOADABLE_CACHE_TTL_SECONDS = 24 * 60 * 60
_DOWNLOADABLE_CACHE: dict[str, tuple[float, bytes]] = {}
_DOWNLOADABLE_CACHE_LOCK = Lock()
_MAX_DOWNLOADABLE_ARCHIVE_BYTES = 25_000_000


_MANUAL_CITIES: tuple[_ManualCity, ...] = (
    _ManualCity(
        name="Manor",
        bbox=(30.32, 30.39, -97.58, -97.52),
        portal_url="https://www.manortx.gov/252/Zoning",
    ),
    _ManualCity(
        name="Elgin",
        bbox=(30.25, 30.43, -97.50, -97.25),
        portal_url=(
            "https://www.elgintexas.gov/DocumentCenter/View/13814/"
            "342026-Zoning-Map"
        ),
    ),
    _ManualCity(
        name="Blanco",
        bbox=(30.06, 30.16, -98.48, -98.36),
        portal_url="https://www.cityofblancotx.gov/1297/Maps",
    ),
)


_AUSTIN_ZONES_PARCEL = frozenset({"FULL", "LTD"})
_AUSTIN_IS_ETJ = frozenset({"2MILE", "5MILE", "ETJ"})


# ── Helpers ──────────────────────────────────────────────────────


def _bbox_contains(bbox, lat, lng) -> bool:
    lat_min, lat_max, lng_min, lng_max = bbox
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _arcgis_query_with_retry(
    sess: requests.Session, url: str, params: dict,
) -> dict:
    """3 attempts, 1s/2s/4s backoff on 429/5xx."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = sess.get(url, params=params, timeout=_TIMEOUT)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(
                    f"transient {resp.status_code}", response=resp,
                )
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout,
                requests.HTTPError) as exc:
            last_exc = exc
            if attempt == 2:
                raise
            time.sleep(1 * (2 ** attempt))
    raise last_exc or RuntimeError("ArcGIS fetch failed")


def _query_city_zoning(sess, city, lat, lng) -> dict | None:
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        return _arcgis_query_with_retry(sess, city.arcgis_url, params)
    except Exception as exc:
        log.warning("zoning fetch failed for %s: %s", city.name, exc)
        return None


def _downloadable_city_by_name(name: str | None) -> _DownloadableCity | None:
    key = _city_key(name)
    if not key:
        return None
    return next(
        (
            city for city in _DOWNLOADABLE_CITIES
            if _city_key(city.name) == key
        ),
        None,
    )


def _download_archive(sess: requests.Session, url: str) -> bytes:
    now = time.monotonic()
    with _DOWNLOADABLE_CACHE_LOCK:
        cached = _DOWNLOADABLE_CACHE.get(url)
        if cached and now - cached[0] < _DOWNLOADABLE_CACHE_TTL_SECONDS:
            return cached[1]

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            response = sess.get(
                url,
                timeout=_TIMEOUT,
                headers={"User-Agent": "PropDesk/1.0 public-zoning-gis"},
            )
            response.raise_for_status()
            content = bytes(response.content)
            if not content or len(content) > _MAX_DOWNLOADABLE_ARCHIVE_BYTES:
                raise ValueError("downloadable zoning archive has invalid size")
            with _DOWNLOADABLE_CACHE_LOCK:
                _DOWNLOADABLE_CACHE[url] = (now, content)
            return content
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1 * (2 ** attempt))
    raise last_exc or RuntimeError("downloadable zoning archive failed")


def _nested_kml(archive: bytes, member_name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive)) as outer:
        member = next(
            (
                name
                for name in outer.namelist()
                if name.casefold().endswith(member_name.casefold())
                and not name.startswith("__MACOSX/")
            ),
            None,
        )
        if member is None:
            raise ValueError(f"{member_name} missing from zoning archive")
        nested = outer.read(member)
    if member.casefold().endswith(".kml"):
        return nested
    with zipfile.ZipFile(io.BytesIO(nested)) as kmz:
        kml_name = next(
            (
                name for name in kmz.namelist()
                if name.casefold().endswith(".kml")
            ),
            None,
        )
        if kml_name is None:
            raise ValueError("KML missing from zoning KMZ")
        return kmz.read(kml_name)


def _kml_coordinates(text: str | None) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return points


def _description_fields(description: str | None) -> dict[str, str]:
    markup = html.unescape(description or "")
    fields: dict[str, str] = {}
    for label, value in re.findall(
        r"<td[^>]*>\s*([^<]+?)\s*</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>",
        markup,
        flags=re.IGNORECASE,
    ):
        key = re.sub(r"\s+", " ", label).strip().upper()
        fields[key] = re.sub(r"\s+", " ", value).strip()
    return fields


def _placemark_geometry(
    placemark: ElementTree.Element,
) -> BaseGeometry | None:
    namespace = {"k": "http://www.opengis.net/kml/2.2"}
    polygons: list[BaseGeometry] = []
    for polygon in placemark.findall(".//k:Polygon", namespace):
        outer_node = polygon.find(
            "./k:outerBoundaryIs/k:LinearRing/k:coordinates",
            namespace,
        )
        shell = _kml_coordinates(
            outer_node.text if outer_node is not None else None
        )
        if len(shell) < 4:
            continue
        holes = []
        for inner_node in polygon.findall(
            "./k:innerBoundaryIs/k:LinearRing/k:coordinates",
            namespace,
        ):
            ring = _kml_coordinates(inner_node.text)
            if len(ring) >= 4:
                holes.append(ring)
        candidate: BaseGeometry = Polygon(shell, holes)
        if not candidate.is_valid:
            candidate = candidate.buffer(0)
        if not candidate.is_empty:
            polygons.append(candidate)
    if not polygons:
        return None
    geometry = polygons[0]
    for polygon in polygons[1:]:
        geometry = geometry.union(polygon)
    return geometry


def _downloadable_features(
    city: _DownloadableCity,
    kml: bytes,
    *,
    lat: float,
    lng: float,
    parcel_geometry: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(kml)
    namespace = {"k": "http://www.opengis.net/kml/2.2"}
    target = shape(parcel_geometry) if parcel_geometry else Point(lng, lat)
    features: list[dict[str, Any]] = []
    for placemark in root.findall(".//k:Placemark", namespace):
        geometry = _placemark_geometry(placemark)
        if geometry is None or not geometry.intersects(target):
            continue
        description = placemark.find("./k:description", namespace)
        fields = _description_fields(
            description.text if description is not None else None
        )
        name = placemark.find("./k:name", namespace)
        fields["NAME"] = (
            (name.text or "").strip() if name is not None else ""
        )
        zone = fields.get(city.base_zone_field.upper()) or fields.get("ZONE")
        if not zone:
            continue
        fields[city.base_zone_field] = zone
        fields[city.zoning_field] = zone
        features.append({"attributes": fields})
    return features


def _query_downloadable_city_zoning(
    sess: requests.Session,
    city: _DownloadableCity,
    lat: float,
    lng: float,
    parcel_geometry: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    try:
        archive = _download_archive(sess, city.archive_url)
        kml = _nested_kml(archive, city.archive_member)
        return _downloadable_features(
            city,
            kml,
            lat=lat,
            lng=lng,
            parcel_geometry=parcel_geometry,
        )
    except Exception as exc:
        log.warning(
            "downloadable zoning fetch failed for %s: %s",
            city.name,
            exc,
        )
        return None


def _query_city_zoning_envelope(
    sess, city, lat, lng, radius_m: int = 30,
) -> dict | None:
    """Small-envelope fallback used when the point-in-polygon
    zoning query returns 0 features for an address whose
    jurisdiction layer DID confirm the city zones this point
    (i.e. FULL/LTD purpose). Geocoded address centroids often
    land in road right-of-way ~10-30m off the parcel polygon;
    a tight envelope catches the adjacent parcel without
    risking the legacy_services false-Austin-on-rural-land
    regression — that regression came from envelopes used
    BEFORE jurisdiction confirmation, where rural land could
    pull in zoning records from a distant city's edge. Here
    the city has already claimed this exact point.
    """
    d = radius_m / 111_320.0
    params = {
        "geometry": json.dumps({
            "xmin": lng - d, "ymin": lat - d,
            "xmax": lng + d, "ymax": lat + d,
            "spatialReference": {"wkid": 4326},
        }),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": 5,
        "f": "json",
    }
    try:
        return _arcgis_query_with_retry(sess, city.arcgis_url, params)
    except Exception as exc:
        log.warning(
            "envelope zoning fetch failed for %s: %s", city.name, exc,
        )
        return None


def _parcel_rings(
    parcel_geometry: dict[str, Any] | None,
) -> list[list[list[float]]] | None:
    if not isinstance(parcel_geometry, dict):
        return None
    rings = parcel_geometry.get("rings")
    if isinstance(rings, list) and rings:
        return rings
    coords = parcel_geometry.get("coordinates")
    geom_type = str(parcel_geometry.get("type") or "").lower()
    if geom_type == "polygon" and isinstance(coords, list) and coords:
        return coords
    if (
        geom_type == "multipolygon"
        and isinstance(coords, list)
        and coords
        and isinstance(coords[0], list)
    ):
        return coords[0]
    return None


def _parcel_centroid(
    parcel_geometry: dict[str, Any] | None,
) -> tuple[float, float] | None:
    rings = _parcel_rings(parcel_geometry)
    if not rings:
        return None
    pts = [
        p for p in rings[0]
        if isinstance(p, (list, tuple)) and len(p) >= 2
    ]
    if not pts:
        return None
    lng = sum(float(p[0]) for p in pts) / len(pts)
    lat = sum(float(p[1]) for p in pts) / len(pts)
    return lng, lat


def _query_city_zoning_polygon(
    sess, city, parcel_geometry: dict[str, Any] | None,
) -> dict | None:
    rings = _parcel_rings(parcel_geometry)
    if not rings:
        return None
    params = {
        "geometry": json.dumps({
            "rings": rings,
            "spatialReference": {"wkid": 4326},
        }),
        "geometryType": "esriGeometryPolygon",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": 20,
        "f": "json",
    }
    try:
        return _arcgis_query_with_retry(sess, city.arcgis_url, params)
    except Exception as exc:
        log.warning(
            "parcel-polygon zoning fetch failed for %s: %s",
            city.name,
            exc,
        )
        return None


def _feature_key(feature: dict) -> str:
    attrs = feature.get("attributes") or {}
    return json.dumps(attrs, sort_keys=True, default=str)


def _prioritize_features(
    features: list[dict], primary_features: list[dict],
) -> list[dict]:
    if not primary_features:
        return features
    ordered: list[dict] = []
    seen: set[str] = set()
    for feature in [*primary_features, *features]:
        key = _feature_key(feature)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(feature)
    return ordered


def _query_city_zoning_parcel(
    sess, city, parcel_geometry: dict[str, Any] | None,
) -> list[dict]:
    """Query the exact parcel polygon, then use the parcel centroid only to
    order a split-zoned result so the card's primary district is the district
    covering the parcel's center while still surfacing all intersecting zones.
    """
    polygon_data = _query_city_zoning_polygon(sess, city, parcel_geometry)
    features = (polygon_data or {}).get("features") or []
    if not features:
        return []

    centroid = _parcel_centroid(parcel_geometry)
    if centroid is None:
        return features
    lng, lat = centroid
    centroid_data = _query_city_zoning(sess, city, lat, lng)
    centroid_features = (centroid_data or {}).get("features") or []
    return _prioritize_features(features, centroid_features)


def _query_jurisdiction(sess, city, lat, lng) -> dict | None:
    if not city.jurisdiction_url:
        return None
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    if city.jurisdiction_where:
        params["where"] = city.jurisdiction_where
    try:
        return _arcgis_query_with_retry(sess, city.jurisdiction_url, params)
    except Exception as exc:
        log.warning("jurisdiction query failed for %s: %s", city.name, exc)
        return None


def _summarise_features(
    city, features,
) -> tuple[list[str], list[dict]]:
    zones: list[str] = []
    records: list[dict] = []
    for feat in features:
        attrs = feat.get("attributes", {}) or {}
        z = (
            attrs.get(city.zoning_field)
            or attrs.get(city.base_zone_field)
        )
        if z and z not in zones:
            zones.append(str(z))
        records.append({
            "zoning_type": attrs.get(city.zoning_field) or "",
            "base_zone": attrs.get(city.base_zone_field) or "",
            "description": (
                attrs.get(city.description_field) or ""
                if city.description_field else ""
            ),
            "jurisdiction": city.name,
            "raw": attrs,
        })
    return zones, records


def _austin_jurisdiction_type(features) -> tuple[str, str]:
    if not features:
        return "UNKNOWN", "Unknown"
    a = features[0].get("attributes", {}) or {}
    return (
        (a.get("JURISDICTION_TYPE") or "").upper() or "UNKNOWN",
        a.get("JURISDICTION_LABEL")
        or a.get("JURISDICTION_TYPE_SPECIFICS")
        or "Unknown",
    )


_PORTAL_URLS = {
    "Austin": "https://www.austintexas.gov/development-services",
    "Pflugerville": "https://www.pflugervilletx.gov/city-government/development-services",
    "Georgetown": "https://gus.georgetown.org/",
    "Cedar Park": "https://www.cedarparktexas.gov/",
    "Hutto": "https://www.huttotx.gov/310/Zoning-Information",
    "Round Rock": (
        "https://www.roundrocktexas.gov/city-departments/"
        "planning-and-development-services/"
    ),
    "San Marcos": "https://www.sanmarcostx.gov/4746/Interactive-Maps",
    "Elgin": "https://www.elgintexas.gov/936/Maps",
}


def _make_etj_payload(city, jurisdiction_label) -> dict:
    portal = _PORTAL_URLS.get(city.name)
    advisory = (
        f"Property is in {city.name}'s ETJ ({jurisdiction_label}). "
        f"{city.name} has subdivision / development-review authority "
        f"over the parcel but does NOT zone it. County rules also "
        f"apply. No specific zoning code."
    )
    return {
        "status": "clear",
        "jurisdiction": f"{city.name} ETJ",
        "jurisdiction_kind": "etj",
        "zoning_type": f"{city.name.upper().replace(' ', '_')}_ETJ",
        "base_zone": "ETJ",
        "description": jurisdiction_label,
        "zones_found": [f"{city.name.upper().replace(' ', '_')}_ETJ"],
        "raw_records": [],
        "advisory": advisory,
        "manual_lookup_url": portal,
        "contradiction_flags": [],
        "source": f"{city.name} BOUNDARIES_jurisdictions",
    }


def _make_verified_etj_payload(
    *, city_name: str, county_lc: str, source: str | None,
) -> dict:
    manual_city = _manual_city_by_name(city_name)
    portal = (
        manual_city.portal_url if manual_city is not None
        else _portal_url_for_city(city_name)
    )
    label = city_name or "the city"
    county_label = county_lc.title() if county_lc else "the county"
    return {
        "status": "clear",
        "jurisdiction": f"{label} ETJ",
        "jurisdiction_kind": "etj",
        "zoning_type": f"{label.upper().replace(' ', '_')}_ETJ",
        "base_zone": "ETJ",
        "description": (
            f"Verified {label} extraterritorial jurisdiction; "
            "city zoning is not assigned from this ETJ verdict."
        ),
        "zones_found": [f"{label.upper().replace(' ', '_')}_ETJ"],
        "raw_records": [],
        "advisory": (
            f"PropDesk verified the parcel is in {label}'s ETJ using "
            f"{source or 'a public jurisdiction layer'}. City zoning is not "
            f"assigned from this ETJ verdict, but {label} subdivision or "
            f"development review and {county_label} permitting rules may "
            "still apply."
        ),
        "manual_lookup_url": portal,
        "contradiction_flags": [],
        "source": source or "verified jurisdiction resolver ETJ",
        "query_anchor": "jurisdiction_verdict",
    }


def _make_clear_or_mixed(
    city, features, *, source_suffix: str = "", query_anchor: str = "lat_lng",
) -> dict:
    zones, records = _summarise_features(city, features)
    primary = records[0] if records else {}
    multiple_zones = len(zones) > 1
    status = "mixed" if multiple_zones else "clear"
    return {
        "status": status,
        "jurisdiction": city.name,
        "jurisdiction_kind": "city",
        "zoning_type": (
            primary.get("zoning_type") or (zones[0] if zones else "")
        ),
        "base_zone": primary.get("base_zone") or "",
        "description": primary.get("description") or "",
        "zones_found": zones,
        "raw_records": records,
        "advisory": _ZONING_ADVISORIES[status],
        "manual_lookup_url": None,
        "contradiction_flags": [],
        "source": (
            f"{city.source_label}{source_suffix}"
            if getattr(city, "source_label", None)
            else (
                f"Cedar Park official zoning GIS{source_suffix}"
                if city.name == "Cedar Park"
                else f"{city.name} ArcGIS{source_suffix}"
            )
        ),
        "query_anchor": query_anchor,
    }


def _make_unknown(
    *, jurisdiction, jurisdiction_kind, reason, source,
    manual_lookup_url=None,
) -> dict:
    return {
        "status": "unknown",
        "jurisdiction": jurisdiction,
        "jurisdiction_kind": jurisdiction_kind,
        "zoning_type": "",
        "base_zone": "",
        "description": "",
        "zones_found": [],
        "raw_records": [],
        "advisory": reason,
        "manual_lookup_url": manual_lookup_url,
        "contradiction_flags": [],
        "source": source,
    }


def _make_unincorporated_payload(county_lc: str) -> dict:
    county_label = county_lc.title()
    return {
        "status": "clear",
        "jurisdiction": f"{county_label} County (unincorporated)",
        "jurisdiction_kind": "unincorporated",
        "zoning_type": "NO_CITY_ZONING",
        "base_zone": "NO_CITY_ZONING",
        "description": (
            "No city zoning applies — property is in unincorporated "
            "county land."
        ),
        "zones_found": ["NO_CITY_ZONING"],
        "raw_records": [],
        "advisory": _NO_CITY_ZONING_ADVISORY,
        "manual_lookup_url": None,
        "contradiction_flags": [],
        "source": f"{county_label} County (statutory)",
        "query_anchor": "jurisdiction_verdict",
    }


def _city_key(name: str | None) -> str:
    return (name or "").strip().lower().replace("_", " ")


def _city_by_name(name: str | None) -> _City | None:
    key = _city_key(name)
    if not key:
        return None
    return next((city for city in _WIRED_CITIES if _city_key(city.name) == key), None)


def _manual_city_by_name(name: str | None) -> _ManualCity | None:
    key = _city_key(name)
    if not key:
        return None
    return next(
        (city for city in _MANUAL_CITIES if _city_key(city.name) == key),
        None,
    )


def _portal_url_for_city(name: str | None) -> str | None:
    key = _city_key(name)
    if not key:
        return None
    for city_name, url in _PORTAL_URLS.items():
        if _city_key(city_name) == key:
            return url
    return None


def _manual_city_for_point(lat: float, lng: float) -> _ManualCity | None:
    return next(
        (city for city in _MANUAL_CITIES if _bbox_contains(city.bbox, lat, lng)),
        None,
    )


def _verdict_confirms_city(
    jurisdiction: dict[str, Any] | None, city: _City,
) -> bool:
    if not isinstance(jurisdiction, dict):
        return False
    if jurisdiction.get("jurisdiction_kind") not in {
        "full_purpose_city", "limited_purpose",
    }:
        return False
    return _city_key(jurisdiction.get("city")) == _city_key(city.name)


def _dispatch(
    sess: requests.Session,
    lat: float,
    lng: float,
    county: str,
    *,
    jurisdiction: dict[str, Any] | None = None,
    parcel_geometry: dict[str, Any] | None = None,
) -> dict:
    """Top-level legacy_services dispatch — verbatim port of
    fetch_zoning_intel."""
    county_lc = (county or "").lower().strip()
    verdict_kind = (
        jurisdiction.get("jurisdiction_kind")
        if isinstance(jurisdiction, dict) else None
    )
    if verdict_kind == "unincorporated_county":
        return _make_unincorporated_payload(county_lc)
    if verdict_kind == "etj" and isinstance(jurisdiction, dict):
        return _make_verified_etj_payload(
            city_name=str(jurisdiction.get("city") or "the city"),
            county_lc=county_lc,
            source=jurisdiction.get("source"),
        )

    verified_city = (
        _city_by_name(jurisdiction.get("city"))
        if verdict_kind in {"full_purpose_city", "limited_purpose"}
        and isinstance(jurisdiction, dict)
        else None
    )
    if isinstance(jurisdiction, dict) and verdict_kind == "unknown":
        manual = _manual_city_for_point(lat, lng)
        if manual is not None:
            return _make_unknown(
                jurisdiction=manual.name,
                jurisdiction_kind="unknown",
                reason=(
                    "PropDesk could not verify whether this parcel is inside "
                    "city limits, in an ETJ, or unincorporated from a wired "
                    f"jurisdiction layer. Because it is near {manual.name}'s "
                    "mapped planning area, verify city limits / ETJ and zoning "
                    "manually via the official city map. The app will not "
                    "assume county-only zoning."
                ),
                manual_lookup_url=manual.portal_url,
                source="jurisdiction unresolved; nearby official city map",
            )
        return _make_unknown(
            jurisdiction=county_lc.title() if county_lc else "Unknown",
            jurisdiction_kind="unknown",
            reason=(
                "PropDesk could not verify whether this parcel is inside city "
                "limits, in an ETJ, or unincorporated from a wired jurisdiction "
                "layer. The app will not assume county-only zoning. Verify the "
                "jurisdiction on the official city/county map before relying "
                "on a zoning conclusion."
            ),
            source="jurisdiction unresolved",
        )

    candidates = [verified_city] if verified_city else [
        c for c in _WIRED_CITIES if _bbox_contains(c.bbox, lat, lng)
    ]
    if county_lc == "williamson":
        # Austin's coarse bbox overlaps several Williamson County cities.
        # Prefer the tighter local city layers before falling back to Austin.
        candidates.sort(key=lambda c: 1 if c.name == "Austin" else 0)
    fetch_failures: list[str] = []
    empty_candidates: list[str] = []
    for candidate in candidates:
        city_zoning_confirmed = _verdict_confirms_city(
            jurisdiction, candidate,
        )
        if candidate.jurisdiction_url and not city_zoning_confirmed:
            jur_payload = _query_jurisdiction(sess, candidate, lat, lng)
            if jur_payload is None:
                fetch_failures.append(candidate.name)
                continue
            jur_features = jur_payload.get("features") or []
            if not jur_features:
                continue

            jur_type, jur_label = _austin_jurisdiction_type(jur_features)
            if jur_type in _AUSTIN_IS_ETJ:
                return _make_etj_payload(candidate, jur_label)
            city_zoning_confirmed = True

        if city_zoning_confirmed and parcel_geometry:
            parcel_features = _query_city_zoning_parcel(
                sess, candidate, parcel_geometry,
            )
            if parcel_features:
                return _make_clear_or_mixed(
                    candidate,
                    parcel_features,
                    source_suffix=" (parcel geometry)",
                    query_anchor="parcel_geometry",
                )

        data = _query_city_zoning(sess, candidate, lat, lng)
        if data is None:
            fetch_failures.append(candidate.name)
            continue
        features = data.get("features", []) or []
        if features:
            return _make_clear_or_mixed(candidate, features)
        empty_candidates.append(candidate.name)

        # Point query empty. If the jurisdiction layer already
        # confirmed the city zones this exact point (FULL/LTD —
        # ETJ would have returned earlier), the geocoded address
        # almost certainly fell in a road right-of-way ~10-30m
        # off the parcel. Try a tight envelope before giving up.
        if candidate.jurisdiction_url or city_zoning_confirmed:
            env_data = _query_city_zoning_envelope(
                sess, candidate, lat, lng, radius_m=30,
            )
            env_features = (
                (env_data or {}).get("features") or []
            )
            if env_features:
                return _make_clear_or_mixed(
                    candidate, env_features,
                    source_suffix=" (ROW fallback)",
                )

            return _make_unknown(
                jurisdiction=candidate.name,
                jurisdiction_kind="city",
                reason=(
                    f"Property is inside {candidate.name} city limits / "
                    f"zoning jurisdiction, but no specific zoning code is "
                    f"published for this parcel. Verify the actual "
                    f"zoning code at the city's planning portal."
                ),
                manual_lookup_url=(
                    _PORTAL_URLS.get(candidate.name)
                ),
                source=f"{candidate.name} ArcGIS (sliver)",
            )

    if (
        isinstance(jurisdiction, dict)
        and verdict_kind in {"full_purpose_city", "limited_purpose"}
        and not verified_city
    ):
        city_name = str(jurisdiction.get("city") or "the verified city")
        downloadable_city = _downloadable_city_by_name(city_name)
        if downloadable_city is not None:
            downloadable_features = _query_downloadable_city_zoning(
                sess,
                downloadable_city,
                lat,
                lng,
                parcel_geometry,
            )
            if downloadable_features:
                return _make_clear_or_mixed(
                    downloadable_city,
                    downloadable_features,
                    source_suffix=" (parcel intersection)",
                    query_anchor=(
                        "parcel_geometry" if parcel_geometry else "lat_lng"
                    ),
                )
        manual_city = _manual_city_by_name(city_name)
        return _make_unknown(
            jurisdiction=city_name,
            jurisdiction_kind="city",
            reason=(
                f"PropDesk verified the parcel is inside {city_name} city "
                "limits, but an automated zoning layer is not wired for that "
                "city yet. Use the city's official zoning map or request a "
                "zoning verification letter."
            ),
            manual_lookup_url=(
                manual_city.portal_url if manual_city is not None else None
            ),
            source="verified jurisdiction with no wired zoning layer",
        )

    if empty_candidates:
        label = ", ".join(empty_candidates)
        return _make_unknown(
            jurisdiction=label,
            jurisdiction_kind="unknown",
            reason=(
                f"{label} zoning source was checked because the parcel falls "
                "near that jurisdiction, but no zoning feature intersected "
                "the point. Verify city limits / ETJ and the city zoning map "
                "before relying on county-only rules."
            ),
            manual_lookup_url=(
                "https://www.cedarparktexas.gov/680/Find-View"
                if "Cedar Park" in empty_candidates else None
            ),
            source="city zoning source returned no intersecting feature",
        )

    if candidates and fetch_failures and not any(
        c.name not in fetch_failures for c in candidates
    ):
        return _make_unknown(
            jurisdiction=", ".join(fetch_failures),
            jurisdiction_kind="city",
            reason=(
                f"Zoning fetch failed for: {', '.join(fetch_failures)}. "
                "Transient or service issue — retry later."
            ),
            source="(fetch failure)",
        )

    manual = _manual_city_for_point(lat, lng)
    if manual is not None:
        return _make_unknown(
            jurisdiction=manual.name,
            jurisdiction_kind="unknown",
            reason=(
                f"The parcel is near {manual.name}'s mapped planning area, "
                "but PropDesk has not verified an automated jurisdiction or "
                "zoning layer for that city. Verify city limits / ETJ and "
                "zoning manually via the city portal. The app will not assume "
                "county-only zoning."
            ),
            manual_lookup_url=manual.portal_url,
            source=f"{manual.name} (no public REST)",
        )

    return _make_unknown(
        jurisdiction=county_lc.title() if county_lc else "Unknown",
        jurisdiction_kind="unknown",
        reason=(
            "PropDesk could not verify whether this parcel is inside city "
            "limits, in an ETJ, or unincorporated from a wired jurisdiction "
            "layer. The app will not assume county-only zoning. Verify the "
            "jurisdiction on the official city/county map before relying on "
            "a zoning conclusion."
        ),
        source="jurisdiction unresolved",
    )


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    qlat, qlng = identity.query_point()
    if qlat is None or qlng is None:
        return _failed("lat/lng required for austin_zoning")
    sess = http_session(session)
    cfg = config if isinstance(config, dict) else {}
    payload = _dispatch(
        sess,
        qlat,
        qlng,
        identity.county or "",
        jurisdiction=cfg.get("jurisdiction"),
        parcel_geometry=identity.parcel_geometry,
    )

    # Coverage axis from the dispatcher result. unincorporated
    # NO_CITY_ZONING and ETJ-only answers are honest signal —
    # they're "covered" with a clear answer that there's no city
    # zoning. Manual-lookup fallback maps to
    # MANUAL_VERIFICATION_REQUIRED.
    jurisdiction_kind = payload.get("jurisdiction_kind")
    if jurisdiction_kind == "manual_lookup":
        coverage = CoverageStatus.MANUAL_VERIFICATION_REQUIRED
    elif jurisdiction_kind in {
        "city", "etj", "unincorporated",
        "full_purpose_city", "limited_purpose",
        "unincorporated_county",
    }:
        coverage = CoverageStatus.COVERED
    else:
        coverage = CoverageStatus.PARTIAL

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=map_legacy_status(payload["status"]),
        coverage_status=coverage,
        normalized_facts=payload,
        fetched_at=datetime.now(UTC),
        identifier_used=payload.get("query_anchor") or "lat_lng",
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
