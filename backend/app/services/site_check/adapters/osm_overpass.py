"""osm_overpass adapter — OpenStreetMap POI fetch via the
Overpass API.

Ported from legacy_services/core/poi_intel.py per
docs/specs/public_data.md §5. 16 categories x 3 ring distances
(1/3/5 mi). Algorithm + categorization rules + ring aggregation
preserved verbatim. Changes from legacy_services:
  - Removed Google Places fallback (Tier B) — paid provider, not
    in v0; ArcGIS Places is the v0 supplement, not Google
  - new fetch(identity, ...) -> AdapterResult contract wrapper

OSM data is ODbL — attribution required wherever it's rendered.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from typing import Any

import requests

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
from app.services.site_check.usage import logical_request

SOURCE_ID = "osm_overpass"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "PropDesk/1.0 (propdesk_control_plane site-check)"
QUERY_TIMEOUT_SEC = 90
BBOX_RADIUS_MILES = 5
RINGS_MILES = (1, 3, 5)
ATTRIBUTION = "© OpenStreetMap contributors"
MAX_MAP_POINTS_PER_CATEGORY = 40
MAX_MAP_POINTS_TOTAL = 600

_EARTH_RADIUS_MI = 3958.8
_MAX_RETRIES = 3
_RETRY_DELAYS = (10, 20, 30)
SUSPICIOUS_EMPTY_THRESHOLD_SEC = 30


# ── Category definitions (verbatim from legacy_services) ───────────


POI_CATEGORIES: dict[str, dict[str, list[dict]]] = {
    "healthcare": {
        "hospital": [{"amenity": "hospital"}],
        "clinic": [{"amenity": "clinic"}],
        "pharmacy": [{"amenity": "pharmacy"}],
        "dentist": [{"amenity": "dentist"}],
        "veterinary": [{"amenity": "veterinary"}],
    },
    "retail": {
        "supermarket": [{"shop": "supermarket"}],
        "convenience": [{"shop": "convenience"}],
        "shopping_center": [{"shop": "mall"}, {"amenity": "marketplace"}],
        "department": [{"shop": "department_store"}],
        "hardware": [{"shop": "hardware"}, {"shop": "doityourself"}],
    },
    "food_service": {
        "restaurant": [{"amenity": "restaurant"}],
        "fast_food": [{"amenity": "fast_food"}],
        "cafe": [{"amenity": "cafe"}],
        "bar": [{"amenity": "bar"}, {"amenity": "pub"}],
    },
    "auto": {
        "gas_station": [{"amenity": "fuel"}],
        "car_wash": [{"amenity": "car_wash"}],
        "auto_dealer": [{"shop": "car"}],
        "auto_parts": [{"shop": "car_parts"}],
        "ev_charging": [{"amenity": "charging_station"}],
    },
    "lodging": {
        "hotel": [{"tourism": "hotel"}, {"tourism": "motel"}],
    },
    "storage": {
        "self_storage": [
            {"amenity": "storage_rental"},
            {"shop": "storage_rental"},
        ],
    },
    "banking": {
        "bank": [{"amenity": "bank"}],
        "atm": [{"amenity": "atm"}],
    },
    "education": {
        "school": [{"amenity": "school"}],
        "university": [{"amenity": "university"}, {"amenity": "college"}],
        "daycare": [{"amenity": "childcare"}, {"amenity": "kindergarten"}],
    },
    "recreation": {
        "park": [{"leisure": "park"}],
        "gym": [
            {"leisure": "fitness_centre"},
            {"leisure": "sports_centre"},
        ],
        "sports_facility": [
            {"leisure": "stadium"},
            {"leisure": "pitch"},
        ],
    },
    "transit": {
        "bus_stop": [
            {"highway": "bus_stop"},
            {"public_transport": "stop_position"},
        ],
        "train_station": [
            {"railway": "station"},
            {"public_transport": "station"},
        ],
        "parking": [{"amenity": "parking"}],
    },
    "industrial": {
        "truck_stop": [{"highway": "services"}],
    },
    "religious": {
        "place_of_worship": [{"amenity": "place_of_worship"}],
    },
    "civic": {
        "post_office": [{"amenity": "post_office"}],
        "library": [{"amenity": "library"}],
        "fire_station": [{"amenity": "fire_station"}],
        "police": [{"amenity": "police"}],
    },
    "entertainment": {
        "cinema": [{"amenity": "cinema"}],
        "theater": [{"amenity": "theatre"}],
        "event_venue": [
            {"amenity": "events_venue"},
            {"amenity": "community_centre"},
        ],
    },
    "pet_services": {
        "pet_store": [{"shop": "pet"}],
        "pet_boarding": [{"amenity": "animal_boarding"}],
    },
    "agriculture": {
        "feed_store": [{"shop": "agrarian"}],
        "equipment": [{"shop": "farm"}, {"shop": "trade"}],
    },
    "residential": {
        "apartments": [{"building": "apartments"}],
    },
}


# ── Helpers ─────────────────────────────────────────────────────


def _build_bbox(lat, lon, radius_mi):
    d_lat = radius_mi / 69.0
    d_lon = radius_mi / (69.0 * math.cos(math.radians(lat)))
    return (lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon)


def _haversine_mi(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return _EARTH_RADIUS_MI * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _build_overpass_query(bbox) -> str:
    south, west, north, east = bbox
    bb = f"{south},{west},{north},{east}"
    lines = [f"[out:json][timeout:{QUERY_TIMEOUT_SEC}];", "("]
    seen_tags: set[str] = set()
    for group in POI_CATEGORIES.values():
        for cat_filters in group.values():
            for tag_dict in cat_filters:
                for k, v in tag_dict.items():
                    tag_key = f"{k}={v}"
                    if tag_key in seen_tags:
                        continue
                    seen_tags.add(tag_key)
                    lines.append(f'  node["{k}"="{v}"]({bb});')
                    lines.append(f'  way["{k}"="{v}"]({bb});')
    lines.append(");")
    lines.append("out center tags;")
    return "\n".join(lines)


def _categorize_element(tags: dict) -> tuple[str, str] | None:
    for group_name, categories in POI_CATEGORIES.items():
        for cat_key, filters in categories.items():
            for tag_dict in filters:
                if all(tags.get(k) == v for k, v in tag_dict.items()):
                    return (group_name, cat_key)
    return None


def _aggregate_results(elements, center_lat, center_lon) -> dict:
    result: dict[str, Any] = {}
    for group_name, categories in POI_CATEGORIES.items():
        cats: dict[str, Any] = {}
        for cat_key in categories:
            cats[cat_key] = {
                "count_1mi": 0,
                "count_3mi": 0,
                "count_5mi": 0,
                "nearest_distance_mi": None,
                "nearest_name": None,
                "top_5_within_5mi": [],
            }
        result[group_name] = {
            "categories": cats,
            "group_total_5mi": 0,
        }

    top5_lists: dict[tuple[str, str], list[dict]] = {}

    for el in elements:
        tags = el.get("tags", {})
        match = _categorize_element(tags)
        if match is None:
            continue
        group_name, cat_key = match
        el_lat = el.get("lat") or el.get("center", {}).get("lat")
        el_lon = el.get("lon") or el.get("center", {}).get("lon")
        if el_lat is None or el_lon is None:
            continue
        dist = _haversine_mi(
            center_lat,
            center_lon,
            float(el_lat),
            float(el_lon),
        )
        cat = result[group_name]["categories"][cat_key]
        if dist <= 1:
            cat["count_1mi"] += 1
        if dist <= 3:
            cat["count_3mi"] += 1
        if dist <= 5:
            cat["count_5mi"] += 1
            name = tags.get("name")
            if name:
                key = (group_name, cat_key)
                if key not in top5_lists:
                    top5_lists[key] = []
                top5_lists[key].append(
                    {
                        "name": name,
                        "distance_mi": round(dist, 2),
                        "lat": float(el_lat),
                        "lon": float(el_lon),
                        "address": tags.get("addr:street", None),
                        "brand": tags.get("brand"),
                        # OSM brand=* tag — feeds the cv2 brand path
                        # per docs/specs/public_data.md §8.4.
                    }
                )
        if cat["nearest_distance_mi"] is None or dist < cat["nearest_distance_mi"]:
            cat["nearest_distance_mi"] = round(dist, 2)
            cat["nearest_name"] = tags.get("name") or None

    for group_name, group_data in result.items():
        total = 0
        for cat_key, cat in group_data["categories"].items():
            total += cat["count_5mi"]
            key = (group_name, cat_key)
            if key in top5_lists:
                sorted_all = sorted(
                    top5_lists[key],
                    key=lambda x: (
                        x["distance_mi"],
                        x.get("name") or "",
                    ),
                )
                seen_names: set[str] = set()
                deduped: list[dict] = []
                for e in sorted_all:
                    n = e.get("name", "")
                    if n and n not in seen_names:
                        seen_names.add(n)
                        deduped.append(e)
                cat["top_5_within_5mi"] = deduped[:5]
                if deduped and deduped[0].get("name"):
                    cat["nearest_name"] = deduped[0]["name"]
                    cat["nearest_distance_mi"] = deduped[0]["distance_mi"]
        group_data["group_total_5mi"] = total

    return result


def _normalized_map_points(
    elements: list[dict],
    center_lat: float,
    center_lon: float,
) -> list[dict[str, Any]]:
    """Build the bounded, persistence-safe marker payload.

    Dense urban queries can contain thousands of parking and building
    features. Public Data retains only deterministic display fields and caps
    each category so the stored run remains suitable for API delivery.
    """
    by_category: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()

    for element in elements:
        tags = element.get("tags") or {}
        match = _categorize_element(tags)
        if match is None:
            continue
        group_name, category = match
        lat_raw = element.get("lat") or (element.get("center") or {}).get("lat")
        lng_raw = element.get("lon") or (element.get("center") or {}).get("lon")
        if lat_raw is None or lng_raw is None:
            continue
        try:
            lat = float(lat_raw)
            lng = float(lng_raw)
            distance_mi = _haversine_mi(center_lat, center_lon, lat, lng)
        except (TypeError, ValueError):
            continue
        if distance_mi > BBOX_RADIUS_MILES:
            continue

        element_type = str(element.get("type") or "feature")
        raw_element_id = element.get("id")
        element_id = str(raw_element_id if raw_element_id is not None else f"{lat:.7f}:{lng:.7f}")
        stable_id = f"osm:{element_type}:{element_id}"
        if stable_id in seen_ids:
            continue
        seen_ids.add(stable_id)

        fallback_name = {
            "parking": "Parking",
        }.get(category)
        name = str(tags.get("name") or fallback_name or "").strip()
        if not name:
            continue
        address = (
            " ".join(
                part
                for part in (
                    str(tags.get("addr:housenumber") or "").strip(),
                    str(tags.get("addr:street") or "").strip(),
                )
                if part
            )
            or None
        )
        by_category.setdefault(category, []).append(
            {
                "id": stable_id,
                "group": group_name,
                "category": category,
                "name": name[:160],
                "lat": lat,
                "lng": lng,
                "address": address,
                "brand": str(tags.get("brand"))[:120] if tags.get("brand") else None,
                "access": str(tags.get("access"))[:32] if tags.get("access") else None,
                "feature_type": (
                    str(tags.get("parking"))[:32]
                    if category == "parking" and tags.get("parking")
                    else None
                ),
                "distance_from_pin_mi": round(distance_mi, 3),
                "source": "OpenStreetMap",
                "confidence": "screened_indicator",
            }
        )

    selected: list[dict[str, Any]] = []
    for category in sorted(by_category):
        rows = sorted(
            by_category[category],
            key=lambda row: (
                row["distance_from_pin_mi"],
                row["name"].lower(),
                row["id"],
            ),
        )
        selected.extend(rows[:MAX_MAP_POINTS_PER_CATEGORY])
    return selected[:MAX_MAP_POINTS_TOTAL]


def _overpass_fetch(
    query: str,
    sess: requests.Session,
    errors: list[str],
) -> tuple[list[dict], float]:
    t0 = time.time()
    elements: list[dict] = []
    # One §8.6 logical request: Overpass sees every attempt below, so
    # each one is metered, and attempts 2+ are billed retries.
    with logical_request():
        for attempt in range(_MAX_RETRIES):
            try:
                resp = sess.post(
                    OVERPASS_URL,
                    data={"data": query},
                    timeout=QUERY_TIMEOUT_SEC,
                )
                if resp.status_code in (429, 504, 503):
                    delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                elements = data.get("elements", [])
                break
            except requests.Timeout:
                errors.append(
                    f"overpass: timeout after {QUERY_TIMEOUT_SEC}s "
                    f"(attempt {attempt + 1})"
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
            except requests.HTTPError as exc:
                errors.append(f"overpass: HTTP {exc.response.status_code}")
                break
            except json.JSONDecodeError as exc:
                errors.append(f"overpass: JSON parse error: {exc}")
                break
            except Exception as exc:
                errors.append(f"overpass: {type(exc).__name__}: {exc}")
                break
    return elements, round(time.time() - t0, 1)


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for osm_overpass")

    sess = http_session(session)
    sess.headers.setdefault("User-Agent", USER_AGENT)

    errors: list[str] = []
    bbox = _build_bbox(identity.lat, identity.lng, BBOX_RADIUS_MILES)
    query = _build_overpass_query(bbox)
    elements, first_query_s = _overpass_fetch(query, sess, errors)

    if len(elements) == 0 and first_query_s > SUSPICIOUS_EMPTY_THRESHOLD_SEC:
        time.sleep(15)
        retry_elements, retry_s = _overpass_fetch(query, sess, [])
        if retry_elements:
            elements = retry_elements
        else:
            errors.append(
                f"overpass: empty result confirmed after retry "
                f"(first: {first_query_s:.1f}s, retry: "
                f"{retry_s:.1f}s)"
            )
    elif len(elements) == 0 and 10 < first_query_s <= SUSPICIOUS_EMPTY_THRESHOLD_SEC:
        errors.append(
            f"overpass: empty result after {first_query_s:.1f}s "
            f"(ambiguous — may be genuine or server-side issue)"
        )

    category_groups = _aggregate_results(
        elements,
        identity.lat,
        identity.lng,
    )
    map_points = _normalized_map_points(
        elements,
        identity.lat,
        identity.lng,
    )

    has_pois = sum(g["group_total_5mi"] for g in category_groups.values()) > 0

    # ArcGIS Places fallback — fires ONLY when Overpass came
    # back fully empty AND recorded an error (504 / timeout /
    # silent-empty). On the happy path or when a property is
    # genuinely in a POI desert, ArcGIS is never called.
    # Mirrors legacy_services Google fallback in poi_intel.py
    # ::fetch_poi_intel; substitutes ArcGIS for Google because
    # ArcGIS is ~10x cheaper for this use case ($0.05/1K
    # Place Attributes vs $17/1K Google Place Details).
    # Metering (§8.6): the ArcGIS call goes out on the SAME session,
    # so the run's ledger gets BOTH an osm row (the Overpass attempts
    # that failed, including their billed retries) and an arcgis row
    # for the Places requests actually made — the fallback is never
    # invisible, and a fallback that returns nothing still counts.
    fallback_used = False
    fallback_count = 0
    if not elements and errors:
        try:
            from app.services.site_check.adapters import (
                arcgis_places,
            )

            arc_result = arcgis_places.fetch(identity, session=sess)
            arc_places = arc_result.normalized_facts.get("places") or []
            if arc_places:
                fallback_used = True
                fallback_count = len(arc_places)
                # Inject ArcGIS places into a synthetic
                # "fallback" group so downstream consumers see
                # them under category_groups too. We don't try
                # to re-categorize — ArcGIS categories don't
                # map cleanly to OSM ones.
                category_groups["arcgis_fallback"] = {
                    "categories": {
                        "all": {
                            "count_1mi": 0,  # unknown without per-place dist filter
                            "count_3mi": 0,
                            "count_5mi": fallback_count,
                            "nearest_distance_mi": (
                                round(arc_places[0].get("distance_m", 0) / 1609.344, 2)
                                if arc_places
                                else None
                            ),
                            "nearest_name": (arc_places[0].get("name") if arc_places else None),
                            "top_5_within_5mi": [
                                {
                                    "name": p.get("name"),
                                    "distance_mi": round(
                                        (p.get("distance_m") or 0) / 1609.344,
                                        2,
                                    ),
                                    "lat": p.get("lat"),
                                    "lon": p.get("lng"),
                                    "address": None,
                                    "brand": None,  # ArcGIS chains is null
                                }
                                for p in arc_places[:5]
                            ],
                        }
                    },
                    "group_total_5mi": fallback_count,
                }
                has_pois = True
                if not map_points:
                    map_points = [
                        {
                            "id": f"arcgis:{place.get('place_id') or index}",
                            "group": "other",
                            "category": "other",
                            "name": str(place.get("name") or "Nearby place")[:160],
                            "lat": place.get("lat"),
                            "lng": place.get("lng"),
                            "address": None,
                            "brand": None,
                            "access": None,
                            "feature_type": None,
                            "distance_from_pin_mi": round(
                                (place.get("distance_m") or 0) / 1609.344,
                                3,
                            ),
                            "source": "ArcGIS Places fallback",
                            "confidence": "screened_indicator",
                        }
                        for index, place in enumerate(arc_places[:MAX_MAP_POINTS_TOTAL])
                        if place.get("lat") is not None and place.get("lng") is not None
                    ]
                errors.append(f"arcgis_fallback: used ({fallback_count} places)")
        except Exception as exc:
            errors.append(f"arcgis_fallback: {exc}")

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR if has_pois else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "center_lat": identity.lat,
            "center_lon": identity.lng,
            "rings_miles": list(RINGS_MILES),
            "category_groups": category_groups,
            "map_points": map_points,
            "raw_element_count": len(elements),
            "attribution": (
                "Powered by Esri (fallback) + " + ATTRIBUTION if fallback_used else ATTRIBUTION
            ),
            "source": ("ArcGIS Places (fallback)" if fallback_used else "OpenStreetMap (Overpass)"),
            "fallback_used": fallback_used,
        },
        errors=errors or None,
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
