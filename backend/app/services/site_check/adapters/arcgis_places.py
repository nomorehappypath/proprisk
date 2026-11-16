"""arcgis_places adapter — ArcGIS Location Platform Places.

NEW adapter (no legacy_services source). Per docs/specs/public_data.md
§8.2-§8.4. POI discovery only — NOT cv2 brand authority.

Why NOT brand authority: ArcGIS docs verify `chains` field is
currently null ("Due to limitations with the underlying data
source, the following fields will currently return null"). The
lightweight Nearby response has no `brand` field at all.

cv2 brand path (per spec §8.4) lives elsewhere — curated brand
dictionary + OSM brand=* tags + uploaded evidence. This adapter
only contributes locations + names + categories.

Constraints (verified live 2026-05-04):
  - radius max 10,000m (5 mi = 8,047m fits)
  - pageSize max 20 — pagination is required for any 5-mi
    trade area in dense markets
  - offset max 199 → max pageable 200 results total per query
  - storage policy: raw place records cannot be permanently
    stored. Caching handled by app/services/site_check/usage.py
    + arcgis_places_cache table (TTL 30d default).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime

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

log = logging.getLogger("propdesk.site_check.arcgis_places")
SOURCE_ID = "arcgis_places"

_PLACES_NEAR_POINT = (
    "https://places-api.arcgis.com/arcgis/rest/services/"
    "places-service/v1/places/near-point"
)
_PLACES_DETAILS_BASE = (
    "https://places-api.arcgis.com/arcgis/rest/services/"
    "places-service/v1/places"
)

_RADIUS_MAX_M = 10_000           # ArcGIS hard limit per docs
_RADIUS_5MI_M = 8_047            # ~5 mi in meters
_PAGE_SIZE_MAX = 20              # ArcGIS hard limit per docs
_OFFSET_MAX = 199                # → 200 total pageable results
_TIMEOUT_S = 10

# v0 categories of interest. ArcGIS uses a different category
# taxonomy than ours; this list is OUR enum, mapped to ArcGIS
# categoryId (per spec §8 Q8 the mapping table is its own
# tracked artefact — added when first concrete need lands).
# For v0 we hit Nearby with NO category filter (returns all
# nearby places), then filter client-side on response category.
_V0_CATEGORIES_OF_INTEREST = (
    "gas_station",
    "convenience_store",
    "fast_food",
    "restaurant",
    "bank",
    "hotel",
    "supermarket",
)


def _api_key() -> str | None:
    from app.core import secret_store

    key = secret_store.get_secret("arcgis")
    return key.get_secret_value() if key else None


def _cache_key(endpoint: str, params: dict) -> str:
    canonical = json.dumps(
        sorted(params.items(), key=lambda kv: kv[0]),
        separators=(",", ":"),
    )
    raw = f"{endpoint}|{canonical}".encode()
    return hashlib.sha256(raw).hexdigest()


def _fetch_nearby(
    sess: requests.Session,
    api_key: str,
    lat: float,
    lng: float,
    radius_m: int = _RADIUS_5MI_M,
) -> tuple[list[dict], list[str]]:
    """Paginate Nearby until offset cap. Returns (places, errors).

    Each place is a dict with placeId, name, categories,
    location {x, y}, distance, icon. NO brand field — that's
    the documented ArcGIS limitation we work around at the
    consumer layer.
    """
    radius_m = min(radius_m, _RADIUS_MAX_M)
    places: list[dict] = []
    errors: list[str] = []
    offset = 0
    while offset <= _OFFSET_MAX:
        params = {
            "f": "json",
            "x": lng,
            "y": lat,
            "radius": radius_m,
            "pageSize": _PAGE_SIZE_MAX,
            "offset": offset,
            "token": api_key,
        }
        try:
            resp = sess.get(
                _PLACES_NEAR_POINT, params=params, timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(
                f"arcgis places nearby (offset={offset}): {exc}"
            )
            break
        if "error" in data:
            errors.append(
                f"arcgis places error at offset={offset}: "
                f"{data['error'].get('message')}"
            )
            break
        results = data.get("results") or []
        places.extend(results)
        if len(results) < _PAGE_SIZE_MAX:
            # Last page — fewer than pageSize returned.
            break
        offset += _PAGE_SIZE_MAX
        time.sleep(0.2)
    if offset > _OFFSET_MAX and len(places) >= 200:
        errors.append(
            "arcgis places hit 200-result pagination cap — "
            "trade area may have more places than we can see"
        )
    return places, errors


def _normalize_place(place: dict) -> dict:
    """Trim ArcGIS Nearby record to OUR derived shape.
    NEVER copy verbatim — strip provider-proprietary fields,
    round coordinates per spec §7.3."""
    location = place.get("location") or {}
    lat = location.get("y")
    lng = location.get("x")
    if lat is not None:
        lat = round(float(lat), 4)
    if lng is not None:
        lng = round(float(lng), 4)
    categories = place.get("categories") or []
    primary_cat = (
        categories[0].get("label") if categories else None
    )
    return {
        "place_id": place.get("placeId"),
        "name": place.get("name"),
        "category": primary_cat,
        "lat": lat,
        "lng": lng,
        "distance_m": place.get("distance"),
    }


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for arcgis_places")
    api_key = _api_key()
    if not api_key:
        return _failed(
            "ARCGIS_API_KEY not configured — arcgis_places unavailable",
            access=SourceAccessStatus.NO_USABLE_PUBLIC_SOURCE_FOUND,
        )

    sess = http_session(session)
    places, errors = _fetch_nearby(
        sess, api_key, identity.lat, identity.lng,
    )

    normalized = [_normalize_place(p) for p in places]

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if normalized
            else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=(
            CoverageStatus.PARTIAL
            if any("200-result" in e for e in errors)
            else CoverageStatus.COVERED
        ),
        normalized_facts={
            "places": normalized,
            "raw_count": len(places),
            "radius_m": _RADIUS_5MI_M,
            "attribution": "Powered by Esri",
            "source": "ArcGIS Location Platform Places",
            "v0_categories_of_interest": list(_V0_CATEGORIES_OF_INTEREST),
            "brand_note": (
                "ArcGIS Places does not provide reliable brand "
                "attribution today (chains field is null per ArcGIS "
                "docs). cv2 brand verification flows through the "
                "curated brand dictionary + OSM brand=* tag + "
                "uploaded evidence per public_data.md §8.4."
            ),
        },
        errors=errors or None,
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
    )


def _failed(
    reason: str,
    *,
    access: SourceAccessStatus = (
        SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
    ),
) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=access,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={},
        errors=[reason],
        fetched_at=datetime.now(UTC),
    )
