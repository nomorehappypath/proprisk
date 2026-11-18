"""tcad_parcel adapter — Texas CAD parcel polygons by supplied ID.

Per docs/specs/public_data.md §4 + legacy_services May-2026
decision: live TCAD parcel auto-derivation by address was REMOVED in
legacy_services because a free-text address (especially
intersections like 'CR 130 & Chandler Road') cannot be
resolved to one specific parcel — the geocoder picks one and
guesses, real-world failures documented (Chandler 10.39 ac
R482369 misresolved to neighbours R675045 then R543369).

Replacement: the user supplies `property_tax_id` at project creation.
When the id is supplied, this adapter queries a county-specific parcel
layer where one is wired (Travis first), then the TxGIO statewide parcel
layer. It does NOT guess from address.

This adapter:
  1. Resolves user-provided parcel_id against public parcel layers when
     possible.
  2. Returns acreage and parcel polygon for downstream parcel-specific
     checks.
  3. Sets coverage_status=MANUAL_VERIFICATION_REQUIRED when
     no parcel_id was supplied (honest signal: we can't
     know without it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests

from app.regions import loader as regions
from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    ManualUploadRequest,
    SourceAccessStatus,
)

SOURCE_ID = "tcad_parcel"

_TCAD_PROPERTY_SEARCH = "https://traviscad.org/property-search"
_TCAD_CURRENT_PORTAL = "https://travis.prodigycad.com"
_TCAD_CURRENT_API = "https://prod-container.trueprodigyapi.com"
_TCAD_CURRENT_TOKEN = f"{_TCAD_CURRENT_API}/trueprodigy/cadpublic/auth/token"
_TCAD_CURRENT_SEARCH = f"{_TCAD_CURRENT_API}/public/property/search"
_TCAD_CURRENT_SHAPES = f"{_TCAD_CURRENT_API}/gama/parcelshapes"
_TCAD_PARCELS_QUERY = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/services/"
    "EXTERNAL_tcad_parcel/FeatureServer/0/query"
)
_TX_STATEWIDE_PARCELS_LAYER = (
    "https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/"
    "stratmap_land_parcels_48_most_recent/MapServer/0"
)
_TX_STATEWIDE_PARCELS_QUERY = f"{_TX_STATEWIDE_PARCELS_LAYER}/query"
_TXGIO_LAND_PARCELS_PAGE = "https://geographic.texas.gov/stratmap/land-parcels/"
_TIMEOUT = 20
_SQFT_PER_ACRE = 43_560.0
_SQM_PER_ACRE = 4_046.8564224
# From each county file's `appraisal_search_url` (app/regions/tx/counties/*.yaml).
_CAD_PROPERTY_SEARCH_BY_COUNTY: dict[str, str] = regions.all_county_sections(
    "appraisal_search_url",
)

# From each county file's `parcels:` section (app/regions/tx/counties/*.yaml).
_COUNTY_PARCEL_LAYERS: dict[str, dict[str, Any]] = regions.all_county_sections(
    "parcels",
)


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    """Resolve user-supplied parcel id through public parcel layers.

    No address-derived parcel guessing happens here. If the user did not
    supply property_tax_id, we still return manual guidance.
    """
    if identity.state != "TX":
        return _fetch_outside_texas(identity, session=session)
    county = _normalize_county(identity.county) or ""
    if county == "williamson":
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.JURISDICTION_NOT_COVERED,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
            normalized_facts={
                "county": county,
                "status": "delegated_to_wcad_parcel",
                "note": (
                    "Williamson County parcel lookup is handled by the "
                    "dedicated WCAD Public Data source."
                ),
            },
            identifier_used="county",
            fetched_at=datetime.now(UTC),
        )
    county_cfg = _COUNTY_PARCEL_LAYERS.get(county)
    if county_cfg is not None:
        if county == "travis" and identity.parcel_id:
            current = _fetch_current_travis_parcel(
                identity,
                sess=session or requests.Session(),
                legacy_cfg=county_cfg,
            )
            if current is not None:
                return current
        return _fetch_configured_county_parcel(
            identity,
            sess=session or requests.Session(),
            county=county,
            cfg=county_cfg,
        )

    in_travis = county == "travis"

    sess = session or requests.Session()

    if not in_travis and identity.parcel_id:
        try:
            parcels = _query_statewide_by_parcel_id(
                sess, identity.parcel_id, county=county or None,
            )
        except Exception as exc:
            url = _CAD_PROPERTY_SEARCH_BY_COUNTY.get(county)
            return AdapterResult(
                source_id=SOURCE_ID,
                source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
                data_confidence_status=DataConfidenceStatus.UNKNOWN,
                coverage_status=CoverageStatus.PARTIAL,
                normalized_facts={
                    "county": county or None,
                    "parcel_id": identity.parcel_id,
                    "manual_lookup_url": url or _TXGIO_LAND_PARCELS_PAGE,
                    "statewide_layer_url": _TX_STATEWIDE_PARCELS_LAYER,
                    "note": (
                        "Texas statewide parcel lookup failed. The supplied "
                        "parcel ID is still stored as the anchor; verify "
                        "full CAD details through the county property search."
                    ),
                },
                errors=[f"Texas parcel lookup failed: {exc}"[:500]],
                identifier_used="parcel_id",
                fetched_at=datetime.now(UTC),
            )
        if len(parcels) == 1:
            return _statewide_exact_result(
                parcels[0], identifier_used="parcel_id",
            )
        if len(parcels) > 1:
            return _statewide_ambiguous_result(
                parcels, parcel_id=identity.parcel_id, county=county,
            )

    if not in_travis:
        # Outside Travis — use the statewide layer first. If a supplied
        # ID did not resolve there, give the user the right county CAD
        # path when we know it and keep the supplied parcel anchor visible.
        url = _CAD_PROPERTY_SEARCH_BY_COUNTY.get(county)
        note = (
            "The supplied parcel ID did not match the TxGIO statewide "
            "parcel layer. The ID is still stored as the anchor; verify "
            "full CAD details through the county property search."
            if identity.parcel_id and url else
            "No county parcel ID was supplied. Add the county parcel ID "
            "to let PropDesk query the TxGIO statewide parcel layer and "
            "anchor downstream parcel-specific checks."
            if url else
            "The supplied parcel ID did not match the TxGIO statewide "
            "parcel layer, and a county-specific CAD lookup URL is not "
            "wired for this jurisdiction yet."
            if identity.parcel_id else
            "County CAD parcel lookup is not wired for this jurisdiction "
            "yet. Add the county parcel ID when available; PropDesk will "
            "try the TxGIO statewide parcel layer."
        )
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED,
            coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            normalized_facts={
                "county": county or None,
                "parcel_id": identity.parcel_id,
                "status": (
                    "no_statewide_parcel_match"
                    if identity.parcel_id else "parcel_id_not_supplied"
                ),
                "manual_lookup_url": url or _TXGIO_LAND_PARCELS_PAGE,
                "statewide_layer_url": _TX_STATEWIDE_PARCELS_LAYER,
                "note": note,
            },
            identifier_used="parcel_id" if identity.parcel_id else "county",
            fetched_at=datetime.now(UTC),
        )

    if not identity.parcel_id:
        # User didn't supply a property_tax_id at project
        # creation. Honest signal — we can't fetch what we
        # don't have an anchor for.
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.HTML_OR_PDF_ONLY,
            data_confidence_status=DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED,
            coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            normalized_facts={
                "manual_lookup_url": _TCAD_PROPERTY_SEARCH,
                "note": (
                    "No property_tax_id supplied. Look up the "
                    "parcel manually at traviscad.org and add it "
                    "in project settings to anchor downstream "
                    "queries (subdivision overlays, official "
                    "records, future TCAD API integrations)."
                ),
            },
            manual_upload_request=ManualUploadRequest(
                document_types=["tax_assessment", "deed"],
                reason=(
                    "Project's property_tax_id is empty. Add the "
                    "TCAD parcel ID from the deed or tax notice "
                    "in project settings."
                ),
            ),
            fetched_at=datetime.now(UTC),
            identifier_used="county",
        )

    errors: list[str] = []
    try:
        parcels = _query_by_parcel_id(sess, identity.parcel_id)
    except Exception as exc:
        errors.append(f"Travis CAD parcel lookup failed: {exc}"[:500])
        parcels = []
    try:
        if not parcels:
            parcels = _query_statewide_by_parcel_id(
                sess, identity.parcel_id, county="travis",
            )
    except Exception as exc:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.PARTIAL,
            normalized_facts={
                "parcel_id": identity.parcel_id,
                "county": "travis",
                "manual_lookup_url": _TCAD_PROPERTY_SEARCH,
                "manual_lookup_with_id": (
                    f"{_TCAD_PROPERTY_SEARCH}?keywords={identity.parcel_id}"
                ),
                "note": (
                    "TCAD parcel lookup failed. The supplied parcel ID "
                    "is still stored as the anchor; use the manual TCAD "
                    "link to verify acreage."
                ),
            },
            errors=[*errors, f"Texas parcel lookup failed: {exc}"[:500]],
            identifier_used="parcel_id",
            fetched_at=datetime.now(UTC),
        )

    if not parcels:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            normalized_facts={
                "parcel_id": identity.parcel_id,
                "county": "travis",
                "status": "no_parcel_match",
                "manual_lookup_url": _TCAD_PROPERTY_SEARCH,
                "manual_lookup_with_id": (
                    f"{_TCAD_PROPERTY_SEARCH}?keywords={identity.parcel_id}"
                ),
                "note": (
                    "The public TCAD parcel layer did not return the "
                    "supplied parcel ID. Verify the ID in TCAD property "
                    "search before relying on parcel-specific checks."
                ),
            },
            manual_upload_request=ManualUploadRequest(
                document_types=["tax_assessment", "deed", "survey"],
                reason=(
                    "Verify the Travis CAD parcel ID and upload the "
                    "tax assessment, deed, or survey if the public "
                    "parcel layer does not match."
                ),
            ),
            errors=errors,
            identifier_used="parcel_id",
            fetched_at=datetime.now(UTC),
        )

    if len(parcels) > 1:
        return _statewide_ambiguous_result(
            parcels, parcel_id=identity.parcel_id, county="travis",
        )

    if _is_statewide_feature(parcels[0]):
        return _statewide_exact_result(
            parcels[0], identifier_used="parcel_id",
        )
    return _tcad_exact_result(parcels[0], identifier_used="parcel_id")


def _fetch_current_travis_parcel(
    identity: PropertyIdentity,
    *,
    sess: requests.Session,
    legacy_cfg: dict[str, Any],
) -> AdapterResult | None:
    """Resolve current TCAD IDs before consulting the legacy tax-map layer.

    TCAD's 2026 public portal can assign a new property ID when legacy tax-map
    parcels are consolidated. The older ArcGIS layer may not contain that ID,
    so point fallback can select only one constituent tract. The current portal
    is authoritative for the ID/acreage and its GAMA endpoint returns the
    matching current parcel geometry.
    """
    post = getattr(sess, "post", None)
    if not callable(post) or not identity.parcel_id:
        return None

    cleaned = identity.parcel_id.strip()
    try:
        token_resp = post(
            _TCAD_CURRENT_TOKEN,
            json={"office": "Travis"},
            timeout=_TIMEOUT,
            headers={"User-Agent": "PropDesk/1.0 public-parcel"},
        )
        token_resp.raise_for_status()
        token = str(
            ((token_resp.json() or {}).get("user") or {}).get("token") or ""
        ).strip()
        if not token:
            return None

        auth_headers = {
            "Authorization": token,
            "User-Agent": "PropDesk/1.0 public-parcel",
        }
        search_resp = post(
            _TCAD_CURRENT_SEARCH,
            params={"page": "1", "pageSize": "10"},
            json={"pid": {"operator": "=", "value": cleaned}},
            timeout=_TIMEOUT,
            headers=auth_headers,
        )
        search_resp.raise_for_status()
        matches = [
            row
            for row in (search_resp.json() or {}).get("results") or []
            if str(row.get("pid") or "").strip() == cleaned
        ]
        # Once TCAD opens the next tax year, the same pid comes back once per
        # roll year (e.g. 2026 and 2027). Keep only the newest year's row.
        newest_year = max((str(r.get("pYear") or "") for r in matches), default="")
        matches = [r for r in matches if str(r.get("pYear") or "") == newest_year]
        if len(matches) != 1:
            return None
        row = matches[0]

        shape_resp = post(
            _TCAD_CURRENT_SHAPES,
            json={"pIDList": [row.get("pid")]},
            timeout=_TIMEOUT,
            headers=auth_headers,
        )
        shape_resp.raise_for_status()
        feature_collections = [
            item.get("row_to_json")
            for item in (shape_resp.json() or {}).get("results") or []
            if isinstance(item, dict)
        ]
        features = [
            feature
            for collection in feature_collections
            if isinstance(collection, dict)
            for feature in collection.get("features") or []
            if isinstance(feature, dict)
            and str((feature.get("properties") or {}).get("pid") or "").strip()
            == cleaned
        ]
        if len(features) != 1:
            return None
        geometry = features[0].get("geometry")
        if not isinstance(geometry, dict):
            return None
    except Exception:
        # The current service is preferred but remains a public, fragile
        # dependency. Exact-ID lookup in the county/state layers remains the
        # fallback; geocoded-point substitution is prohibited below.
        return None

    acreage = _parse_number(row.get("legalAcreage"))
    lat = _parse_number(row.get("latitude"))
    lng = _parse_number(row.get("longitude"))
    legacy_neighbors = _current_travis_legacy_neighbors(
        sess,
        legacy_cfg,
        lat=lat if lat is not None else identity.lat,
        lng=lng if lng is not None else identity.lng,
        subject_geometry=geometry,
    )
    year = str(row.get("pYear") or "").strip() or None
    detail_url = f"{_TCAD_CURRENT_PORTAL}/property-detail/{cleaned}"
    if year:
        detail_url = f"{detail_url}/{year}"

    advisories = [
        (
            "Subject parcel ID, acreage, and geometry come from TCAD's current "
            "public portal. Neighbor attributes come from the county tax-map "
            "layer and may lag parcel consolidations."
        )
    ]
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "county": "travis",
            "status": "parcel_found_from_current_tcad_id",
            "parcel_id": cleaned,
            "cad_account": _clean_string(row.get("geoID")),
            "owner_name": _clean_string(row.get("displayName") or row.get("name")),
            "situs": _clean_string(row.get("streetPrimary") or row.get("fullSitus")),
            "legal_description": _clean_string(row.get("legalDescription")),
            "acreage": acreage,
            "acres": acreage,
            "acreage_source": "TCAD legalAcreage",
            "land_value": _parse_number(row.get("landValue")),
            "market_value": _parse_number(row.get("marketValue")),
            "deed_date": _clean_string(row.get("deedDt")),
            "tax_year": year,
            "parcel_polygon": geometry,
            "neighboring_parcels": legacy_neighbors,
            "neighboring_parcel_count": len(legacy_neighbors),
            "source": "Travis Central Appraisal District current public portal",
            "source_url": detail_url,
            "parcel_geometry_source": _TCAD_CURRENT_SHAPES,
            "source_status": "official_current_public_portal",
            "manual_lookup_url": _TCAD_PROPERTY_SEARCH,
            "note": (
                f"TCAD parcel {cleaned} resolved by supplied current property "
                "ID. Acreage and map geometry refer to the same TCAD record."
            ),
            "source_advisories": advisories,
        },
        identifier_used="parcel_id",
        fetched_at=datetime.now(UTC),
        advisories=advisories,
    )


def _current_travis_legacy_neighbors(
    session: requests.Session,
    cfg: dict[str, Any],
    *,
    lat: float | None,
    lng: float | None,
    subject_geometry: dict[str, Any],
) -> list[dict[str, Any]]:
    if lat is None or lng is None:
        return []
    try:
        features = _query_configured(cfg, session, {
            "where": "1=1",
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "distance": "3000",
            "units": "esriSRUnit_Foot",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": cfg["out_fields"],
            "returnGeometry": "true",
            "outSR": "4326",
            "resultRecordCount": "40",
            "f": "json",
        })
    except Exception:
        return []

    neighbors = _configured_neighboring_parcels(
        features,
        cfg,
        subject_parcel_id=None,
    )
    return [
        parcel
        for parcel in neighbors
        if not _geometry_contains_representative_point(
            subject_geometry,
            parcel.get("parcel_polygon"),
        )
    ]


def _geometry_contains_representative_point(
    subject: dict[str, Any],
    candidate: Any,
) -> bool:
    if not isinstance(candidate, dict):
        return False
    points = list(_geometry_points(candidate))
    if not points:
        return False
    point = (
        sum(value[0] for value in points) / len(points),
        sum(value[1] for value in points) / len(points),
    )
    return _point_in_geojson(point, subject)


def _geometry_points(geometry: dict[str, Any]):
    coordinates = geometry.get("coordinates") or []
    geometry_type = geometry.get("type")
    polygons = [coordinates] if geometry_type == "Polygon" else coordinates
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        return
    for polygon in polygons:
        for ring in polygon or []:
            for point in ring or []:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    try:
                        yield float(point[0]), float(point[1])
                    except (TypeError, ValueError):
                        continue


def _point_in_geojson(
    point: tuple[float, float],
    geometry: dict[str, Any],
) -> bool:
    coordinates = geometry.get("coordinates") or []
    geometry_type = geometry.get("type")
    polygons = [coordinates] if geometry_type == "Polygon" else coordinates
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        return False
    for polygon in polygons:
        rings = polygon or []
        if not rings or not _point_in_ring(point, rings[0]):
            continue
        if not any(_point_in_ring(point, hole) for hole in rings[1:]):
            return True
    return False


def _point_in_ring(point: tuple[float, float], ring: Any) -> bool:
    x, y = point
    vertices = [
        (float(value[0]), float(value[1]))
        for value in ring or []
        if isinstance(value, (list, tuple)) and len(value) >= 2
    ]
    if len(vertices) < 3:
        return False
    inside = False
    previous = vertices[-1]
    for current in vertices:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _clean_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _fetch_outside_texas(
    identity: PropertyIdentity, *, session: requests.Session | None,
) -> AdapterResult:
    """Outside Texas: the county file's parcel layer only (the statewide
    parcel layer and the Travis/Williamson paths are Texas's)."""
    key = identity.county_key
    cfg = _COUNTY_PARCEL_LAYERS.get(key)
    if cfg is None:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.JURISDICTION_NOT_COVERED,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.NOT_SUPPORTED_IN_JURISDICTION,
            normalized_facts={
                "county": identity.county,
                "state": identity.state,
                "advisory": "No parcel map is connected for this county yet.",
            },
            identifier_used="county",
            fetched_at=datetime.now(UTC),
        )
    result = _fetch_configured_county_parcel(
        identity, sess=session or requests.Session(), county=key, cfg=cfg,
    )
    facts = result.normalized_facts or {}
    if facts.get("county") == key:
        facts["county"] = identity.county
    facts["state"] = identity.state
    return result


def _fetch_configured_county_parcel(
    identity: PropertyIdentity,
    *,
    sess: requests.Session,
    county: str,
    cfg: dict[str, Any],
) -> AdapterResult:
    """Query a county-specific parcel polygon layer.

    Exact point intersection with one parcel is treated as resolved. Multiple
    or nearby matches are candidates only; PropDesk does not choose among them.
    """
    try:
        if identity.parcel_id:
            matches = _query_configured_by_parcel_id(
                sess, cfg, identity.parcel_id,
            )
            if len(matches) == 1:
                neighboring_features = _best_effort_configured_neighbors(
                    sess, cfg, identity, matches[0],
                )
                return _configured_exact_result(
                    matches[0],
                    county=county,
                    cfg=cfg,
                    status="parcel_found_from_supplied_id",
                    identifier_used="parcel_id",
                    neighboring_features=neighboring_features,
                )
            if len(matches) > 1:
                return _configured_candidate_result(
                    matches,
                    county=county,
                    cfg=cfg,
                    status="multiple_parcel_matches",
                    identifier_used="parcel_id",
                    note=(
                        "The supplied parcel ID matched multiple public "
                        "parcel records. Select the correct record before "
                        "using parcel-specific conclusions."
                    ),
                    supplied_parcel_id=identity.parcel_id,
                )

            # A supplied CAD ID is the parcel anchor. Never replace it with a
            # parcel found at the geocoded address point; that can silently
            # select a legacy sub-parcel or an adjacent tract.
            return _configured_not_found_result(
                identity,
                county=county,
                cfg=cfg,
            )

        if identity.lat is not None and identity.lng is not None:
            point_matches = _query_configured_point(
                sess, cfg, identity.lat, identity.lng,
            )
            if len(point_matches) == 1:
                neighboring_features = _best_effort_configured_neighbors(
                    sess, cfg, identity, point_matches[0],
                )
                return _configured_exact_result(
                    point_matches[0],
                    county=county,
                    cfg=cfg,
                    status="parcel_found_at_geocoded_point",
                    identifier_used="lat_lng",
                    neighboring_features=neighboring_features,
                )
            if len(point_matches) > 1:
                return _configured_candidate_result(
                    point_matches,
                    county=county,
                    cfg=cfg,
                    status="multiple_parcels_at_geocoded_point",
                    identifier_used="lat_lng",
                    note=(
                        "The public parcel layer returned multiple parcels at "
                        "the geocoded point. Select the correct parcel ID "
                        "before using parcel-specific conclusions."
                    ),
                )

            nearby = _query_configured_nearby(
                sess, cfg, identity.lat, identity.lng,
            )
            if nearby:
                return _configured_candidate_result(
                    nearby,
                    county=county,
                    cfg=cfg,
                    status="nearby_candidates_only",
                    identifier_used="lat_lng",
                    note=(
                        "No parcel polygon contains the geocoded point in the "
                        "public county layer. PropDesk found nearby parcel "
                        "candidates but did not choose one automatically."
                    ),
                )

        return _configured_not_found_result(
            identity,
            county=county,
            cfg=cfg,
        )
    except Exception as exc:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=(
                SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
            ),
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.PARTIAL,
            normalized_facts={
                "county": county,
                "status": "fetch_failed",
                "manual_lookup_url": _CAD_PROPERTY_SEARCH_BY_COUNTY.get(county),
                "source": cfg["source_label"],
                "source_url": cfg["source_url"],
                "note": (
                    f"{cfg['source_label']} failed. Retry Public Data or "
                    "verify the parcel directly in the county CAD search."
                ),
            },
            errors=[f"{cfg['source_label']} failed: {exc}"[:500]],
            identifier_used=(
                "parcel_id" if identity.parcel_id else "lat_lng"
            ),
            fetched_at=datetime.now(UTC),
        )


def _query_configured_by_parcel_id(
    session: requests.Session,
    cfg: dict[str, Any],
    parcel_id: str,
) -> list[dict[str, Any]]:
    cleaned = parcel_id.strip().upper()
    escaped = cleaned.replace("'", "''")
    clauses: list[str] = []
    numeric_fields = set(cfg.get("numeric_id_fields") or [])
    for field in cfg.get("id_fields") or []:
        if field in numeric_fields and cleaned.isdigit():
            clauses.append(f"{field} = {int(cleaned)}")
        else:
            clauses.append(f"{field} = '{escaped}'")
    if not clauses:
        return []
    return _query_configured(cfg, session, {
        "where": " OR ".join(clauses),
        "outFields": cfg["out_fields"],
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": "10",
        "f": "json",
    })


def _query_configured_point(
    session: requests.Session,
    cfg: dict[str, Any],
    lat: float,
    lng: float,
) -> list[dict[str, Any]]:
    return _query_configured(cfg, session, {
        "where": "1=1",
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": cfg["out_fields"],
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": "10",
        "f": "json",
    })


def _query_configured_nearby(
    session: requests.Session,
    cfg: dict[str, Any],
    lat: float,
    lng: float,
    *,
    distance_ft: int = 200,
) -> list[dict[str, Any]]:
    return _query_configured(cfg, session, {
        "where": "1=1",
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "distance": str(distance_ft),
        "units": "esriSRUnit_Foot",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": cfg["out_fields"],
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": "20",
        "f": "json",
    })


def _best_effort_configured_neighbors(
    session: requests.Session,
    cfg: dict[str, Any],
    identity: PropertyIdentity,
    subject_feature: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fetch map context without making parcel resolution depend on it."""
    lat = identity.lat
    lng = identity.lng
    if lat is None or lng is None:
        center = _arcgis_geometry_center(subject_feature.get("geometry") or {})
        if center is None:
            return []
        lat, lng = center
    try:
        return _query_configured(cfg, session, {
            "where": "1=1",
            "geometry": f"{lng},{lat}",
            "geometryType": "esriGeometryPoint",
            "distance": "3000",
            "units": "esriSRUnit_Foot",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": cfg["out_fields"],
            "returnGeometry": "true",
            "outSR": "4326",
            "resultRecordCount": "40",
            "f": "json",
        })
    except Exception:
        return []


def _query_configured(
    cfg: dict[str, Any],
    session: requests.Session,
    params: dict[str, str],
) -> list[dict[str, Any]]:
    resp = session.get(
        cfg["query_url"],
        params=params,
        timeout=_TIMEOUT,
        headers={"User-Agent": "PropDesk/1.0 public-parcel"},
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", data["error"]))
    return data.get("features", []) or []


def _configured_exact_result(
    feature: dict[str, Any],
    *,
    county: str,
    cfg: dict[str, Any],
    status: str,
    identifier_used: str,
    neighboring_features: list[dict[str, Any]] | None = None,
) -> AdapterResult:
    parcel = _configured_feature_to_parcel(feature, cfg)
    parcel_id = parcel.get("parcel_id")
    acreage = parcel.get("acreage")
    advisories = _configured_advisories(cfg)
    neighboring_parcels = _configured_neighboring_parcels(
        neighboring_features or [], cfg, subject_parcel_id=parcel_id,
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.SCREENED_INDICATOR
            if cfg.get("source_status") else
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT
        ),
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "county": county,
            "status": status,
            "parcel_id": parcel_id,
            "cad_account": parcel.get("cad_account"),
            "owner_name": parcel.get("owner_name"),
            "situs": parcel.get("situs"),
            "legal_description": parcel.get("legal_description"),
            "acreage": acreage,
            "acres": acreage,
            "acreage_source": parcel.get("acreage_source"),
            "land_value": parcel.get("land_value"),
            "market_value": parcel.get("market_value"),
            "deed_number": parcel.get("deed_number"),
            "deed_date": parcel.get("deed_date"),
            "tax_year": parcel.get("tax_year"),
            "parcel_polygon": parcel.get("parcel_polygon"),
            "neighboring_parcels": neighboring_parcels,
            "neighboring_parcel_count": len(neighboring_parcels),
            "source": cfg["source_label"],
            "source_url": cfg["source_url"],
            "source_status": cfg.get("source_status", "official_rest_verified"),
            "manual_lookup_url": _CAD_PROPERTY_SEARCH_BY_COUNTY.get(county),
            "note": _configured_exact_note(parcel_id, cfg),
            **({"source_advisories": advisories} if advisories else {}),
        },
        identifier_used=identifier_used,
        fetched_at=datetime.now(UTC),
        advisories=advisories or None,
    )


def _configured_candidate_result(
    features: list[dict[str, Any]],
    *,
    county: str,
    cfg: dict[str, Any],
    status: str,
    identifier_used: str,
    note: str,
    supplied_parcel_id: str | None = None,
) -> AdapterResult:
    candidates = _configured_candidates(features, cfg)
    advisories = _configured_advisories(cfg)
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "county": county,
            "status": status,
            "candidate_count": len(candidates),
            "nearby_candidates": candidates,
            "manual_lookup_url": _CAD_PROPERTY_SEARCH_BY_COUNTY.get(county),
            "source": cfg["source_label"],
            "source_url": cfg["source_url"],
            "source_status": cfg.get("source_status", "official_rest_verified"),
            "note": note,
            **(
                {"parcel_id": supplied_parcel_id}
                if supplied_parcel_id else {}
            ),
            **({"source_advisories": advisories} if advisories else {}),
        },
        manual_upload_request=ManualUploadRequest(
            document_types=["tax_assessment", "deed", "survey"],
            reason=(
                "Select the correct county parcel ID or upload a tax "
                "assessment, deed, or survey before relying on parcel-specific "
                "public-data conclusions."
            ),
        ),
        identifier_used=identifier_used,
        fetched_at=datetime.now(UTC),
        advisories=advisories or None,
    )


def _configured_not_found_result(
    identity: PropertyIdentity,
    *,
    county: str,
    cfg: dict[str, Any],
) -> AdapterResult:
    has_anchor = bool(identity.parcel_id or (
        identity.lat is not None and identity.lng is not None
    ))
    status = "no_parcel_match" if has_anchor else "parcel_anchor_missing"
    note = (
        "The public county parcel layer did not return a parcel at the "
        "supplied ID or geocoded point. Verify the parcel in the county CAD "
        "search before relying on parcel-specific checks."
        if has_anchor else
        "No parcel ID or coordinates were available for the county parcel "
        "lookup. Add the county parcel ID or rerun after geocoding."
    )
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "county": county,
            "status": status,
            "parcel_id": identity.parcel_id,
            "manual_lookup_url": _CAD_PROPERTY_SEARCH_BY_COUNTY.get(county),
            "source": cfg["source_label"],
            "source_url": cfg["source_url"],
            "source_status": cfg.get("source_status", "official_rest_verified"),
            "note": note,
        },
        manual_upload_request=ManualUploadRequest(
            document_types=["tax_assessment", "deed", "survey"],
            reason=(
                "Verify the county parcel ID and upload the tax assessment, "
                "deed, or survey if the public parcel layer does not match."
            ),
        ),
        identifier_used="parcel_id" if identity.parcel_id else "lat_lng",
        fetched_at=datetime.now(UTC),
    )


def _query_by_parcel_id(
    session: requests.Session,
    parcel_id: str,
) -> list[dict]:
    cleaned = parcel_id.strip().upper()
    escaped = cleaned.replace("'", "''")
    where_parts = [f"PID_10 = '{escaped}'"]
    if cleaned.isdigit():
        where_parts.append(f"PROP_ID = {int(cleaned)}")
    return _query(session, {
        "where": " OR ".join(where_parts),
        "outFields": (
            "OBJECTID,PID_10,PROP_ID,SITUS,SDE_TCAD_PARCELS_AREA,"
            "LAND_VALUE,Shape__Area"
        ),
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": "2",
        "f": "json",
    })


def _query_statewide_by_parcel_id(
    session: requests.Session,
    parcel_id: str,
    *,
    county: str | None,
) -> list[dict]:
    cleaned = parcel_id.strip().upper()
    escaped = cleaned.replace("'", "''")
    id_clause = f"(prop_id = '{escaped}' OR geo_id = '{escaped}')"
    features = _query_statewide(session, {
        "where": (
            f"county = '{county.strip().upper()}' AND {id_clause}"
            if county else id_clause
        ),
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": "10",
        "f": "json",
    })
    if features or not county:
        return features
    return _query_statewide(session, {
        "where": id_clause,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": "10",
        "f": "json",
    })


def _query(
    session: requests.Session,
    params: dict[str, str],
) -> list[dict]:
    resp = session.get(_TCAD_PARCELS_QUERY, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", data["error"]))
    return data.get("features", []) or []


def _query_statewide(
    session: requests.Session,
    params: dict[str, str],
) -> list[dict]:
    resp = session.get(
        _TX_STATEWIDE_PARCELS_QUERY, params=params, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", data["error"]))
    return data.get("features", []) or []


def _tcad_exact_result(
    feature: dict,
    *,
    identifier_used: str,
) -> AdapterResult:
    parcel = _tcad_feature_to_parcel(feature)
    parcel_id = parcel.get("parcel_id")
    acreage = parcel.get("acreage")
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "county": "travis",
            "status": "parcel_found_from_supplied_id",
            "parcel_id": parcel_id,
            "cad_account": parcel.get("cad_account"),
            "situs": parcel.get("situs"),
            "acreage": acreage,
            "acres": acreage,
            "acreage_source": parcel.get("acreage_source"),
            "land_value": parcel.get("land_value"),
            "parcel_polygon": parcel.get("parcel_polygon"),
            "manual_lookup_url": _TCAD_PROPERTY_SEARCH,
            "manual_lookup_with_id": (
                f"{_TCAD_PROPERTY_SEARCH}?keywords={parcel_id}"
                if parcel_id else _TCAD_PROPERTY_SEARCH
            ),
            "note": _exact_note(parcel_id, "Travis County TCAD"),
        },
        identifier_used=identifier_used,
        fetched_at=datetime.now(UTC),
    )


def _statewide_exact_result(
    feature: dict,
    *,
    identifier_used: str,
) -> AdapterResult:
    parcel = _statewide_feature_to_parcel(feature)
    parcel_id = parcel.get("parcel_id")
    county = parcel.get("county")
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "county": county,
            "status": "parcel_found_from_supplied_id",
            "parcel_id": parcel_id,
            "cad_account": parcel.get("cad_account"),
            "owner_name": parcel.get("owner_name"),
            "situs": parcel.get("situs"),
            "legal_description": parcel.get("legal_description"),
            "acreage": parcel.get("acreage"),
            "acres": parcel.get("acreage"),
            "acreage_source": parcel.get("acreage_source"),
            "land_value": parcel.get("land_value"),
            "market_value": parcel.get("market_value"),
            "year_built": parcel.get("year_built"),
            "parcel_polygon": parcel.get("parcel_polygon"),
            "source": parcel.get("source"),
            "tax_year": parcel.get("tax_year"),
            "manual_lookup_url": _CAD_PROPERTY_SEARCH_BY_COUNTY.get(
                county or "",
            ),
            "note": _exact_note(parcel_id, "Texas statewide parcel layer"),
        },
        identifier_used=identifier_used,
        fetched_at=datetime.now(UTC),
    )


def _statewide_ambiguous_result(
    features: list[dict],
    *,
    parcel_id: str,
    county: str | None,
) -> AdapterResult:
    candidates = [
        _statewide_feature_to_candidate(feature)
        for feature in features[:10]
    ]
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.MANUAL_VERIFICATION_REQUIRED,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "county": county or None,
            "status": "multiple_parcel_matches",
            "parcel_id": parcel_id,
            "candidate_count": len(features),
            "nearby_candidates": candidates,
            "manual_lookup_url": _CAD_PROPERTY_SEARCH_BY_COUNTY.get(
                county or "",
            ),
            "note": (
                "The supplied parcel ID matched multiple Texas parcel "
                "records. Select the correct county/CAD record before "
                "treating parcel-specific checks as verified."
            ),
        },
        manual_upload_request=ManualUploadRequest(
            document_types=["tax_assessment", "deed", "survey"],
            reason=(
                "The supplied parcel ID matched multiple parcel records. "
                "Upload the tax assessment, deed, or survey to identify "
                "the correct parcel."
            ),
        ),
        identifier_used="parcel_id",
        fetched_at=datetime.now(UTC),
    )


def _configured_feature_to_parcel(
    feature: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    attrs = feature.get("attributes") or {}
    geometry = feature.get("geometry") or {}
    acreage, acreage_source = _configured_acreage(attrs, cfg)
    return {
        "parcel_id": _first_attr_string(attrs, cfg.get("id_fields") or []),
        "cad_account": _first_attr_string(
            attrs, cfg.get("cad_account_fields") or [],
        ),
        "owner_name": _first_attr_string(attrs, cfg.get("owner_fields") or []),
        "situs": _configured_situs(attrs, cfg),
        "legal_description": _first_attr_string(
            attrs, cfg.get("legal_fields") or [],
        ),
        "acreage": acreage,
        "acreage_source": acreage_source,
        "land_value": _first_number(attrs, cfg.get("land_value_fields") or []),
        "market_value": _first_number(attrs, cfg.get("value_fields") or []),
        "deed_number": _first_attr_string(attrs, ["deed_num", "deed_seq"]),
        "deed_date": _first_attr_string(attrs, ["deed_date"]),
        "tax_year": _first_attr_string(attrs, ["TAX_YEAR", "tax_year"]),
        "parcel_polygon": _arcgis_polygon_to_geojson(geometry),
    }


def _configured_candidates(
    features: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for feature in features:
        parcel = _configured_feature_to_parcel(feature, cfg)
        key = (parcel.get("parcel_id"), parcel.get("cad_account"))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "parcel_id": parcel.get("parcel_id"),
            "cad_account": parcel.get("cad_account"),
            "owner_name": parcel.get("owner_name"),
            "situs": parcel.get("situs"),
            "legal_description": parcel.get("legal_description"),
            "acreage": parcel.get("acreage"),
        })
        if len(candidates) >= 12:
            break
    return candidates


def _configured_neighboring_parcels(
    features: list[dict[str, Any]],
    cfg: dict[str, Any],
    *,
    subject_parcel_id: str | None,
) -> list[dict[str, Any]]:
    neighbors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for feature in features:
        parcel = _configured_feature_to_parcel(feature, cfg)
        parcel_id = parcel.get("parcel_id")
        polygon = parcel.get("parcel_polygon")
        if (
            not parcel_id
            or parcel_id == subject_parcel_id
            or parcel_id in seen
            or not isinstance(polygon, dict)
        ):
            continue
        seen.add(parcel_id)
        neighbors.append(parcel)
        if len(neighbors) >= 40:
            break
    return neighbors


def _arcgis_geometry_center(geometry: dict[str, Any]) -> tuple[float, float] | None:
    points = [
        point
        for ring in geometry.get("rings") or []
        for point in ring
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if not points:
        return None
    lng = sum(float(point[0]) for point in points) / len(points)
    lat = sum(float(point[1]) for point in points) / len(points)
    return lat, lng


def _configured_acreage(
    attrs: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[float | None, str | None]:
    for field in cfg.get("acre_fields") or []:
        value = _parse_number(attrs.get(field))
        if value is not None and value > 0:
            return value, field
    for field in cfg.get("sqft_area_fields") or []:
        value = _parse_number(attrs.get(field))
        if value is not None and value > 0:
            return value / _SQFT_PER_ACRE, f"{field}_sqft"
    return None, None


def _configured_situs(attrs: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    fields = cfg.get("situs_fields") or []
    direct = _first_attr_string(attrs, fields[:1])
    if direct:
        return direct
    parts = [
        _first_attr_string(attrs, [field])
        for field in fields[1:]
    ]
    text = " ".join(part for part in parts if part)
    return text or None


def _first_attr_string(
    attrs: dict[str, Any],
    fields: list[str],
) -> str | None:
    for field in fields:
        value = attrs.get(field)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _first_number(
    attrs: dict[str, Any],
    fields: list[str],
) -> float | None:
    for field in fields:
        value = _parse_number(attrs.get(field))
        if value is not None:
            return value
    return None


def _configured_advisories(cfg: dict[str, Any]) -> list[str]:
    advisory = cfg.get("source_advisory")
    return [advisory] if isinstance(advisory, str) and advisory else []


def _configured_exact_note(
    parcel_id: str | None,
    cfg: dict[str, Any],
) -> str:
    return (
        f"Parcel {parcel_id} resolved from {cfg['source_label']}. PropDesk "
        "uses this only as parcel evidence; jurisdiction, zoning, and "
        "entitlement still come from their own official sources."
    )


def _tcad_feature_to_parcel(feature: dict) -> dict:
    attrs = feature.get("attributes") or {}
    geometry = feature.get("geometry") or {}
    prop_id = attrs.get("PROP_ID")
    pid_10 = attrs.get("PID_10")
    acreage, acreage_source = _acreage_from_attrs(attrs)
    return {
        "parcel_id": str(prop_id) if prop_id is not None else (
            str(pid_10).strip().upper() if pid_10 else None
        ),
        "cad_account": str(pid_10).strip().upper() if pid_10 else None,
        "situs": attrs.get("SITUS"),
        "acreage": acreage,
        "acreage_source": acreage_source,
        "land_value": attrs.get("LAND_VALUE"),
        "parcel_polygon": _arcgis_polygon_to_geojson(geometry),
    }


def _statewide_feature_to_parcel(feature: dict) -> dict:
    attrs = feature.get("attributes") or {}
    geometry = feature.get("geometry") or {}
    prop_id = attrs.get("prop_id")
    geo_id = attrs.get("geo_id")
    acreage, acreage_source = _statewide_acreage_from_attrs(attrs)
    return {
        "parcel_id": str(prop_id).strip().upper() if prop_id else None,
        "cad_account": str(geo_id).strip().upper() if geo_id else None,
        "county": _normalize_county(attrs.get("county")),
        "owner_name": attrs.get("owner_name"),
        "situs": attrs.get("situs_addr"),
        "legal_description": attrs.get("legal_desc"),
        "acreage": acreage,
        "acreage_source": acreage_source,
        "land_value": _parse_number(attrs.get("land_value")),
        "market_value": _parse_number(attrs.get("mkt_value")),
        "year_built": attrs.get("year_built"),
        "source": attrs.get("source"),
        "tax_year": attrs.get("tax_year"),
        "parcel_polygon": _arcgis_polygon_to_geojson(geometry),
    }


def _statewide_feature_to_candidate(feature: dict) -> dict:
    parcel = _statewide_feature_to_parcel(feature)
    return {
        "parcel_id": parcel.get("parcel_id"),
        "cad_account": parcel.get("cad_account"),
        "county": parcel.get("county"),
        "acreage": parcel.get("acreage"),
        "situs": parcel.get("situs"),
        "owner_name": parcel.get("owner_name"),
    }


def _acreage_from_attrs(attrs: dict) -> tuple[float | None, str | None]:
    for field in ("SDE_TCAD_PARCELS_AREA", "Shape__Area"):
        value = attrs.get(field)
        if isinstance(value, (int, float)) and value > 0:
            return (float(value) / _SQFT_PER_ACRE, f"{field}_sqft")
    return None, None


def _statewide_acreage_from_attrs(
    attrs: dict,
) -> tuple[float | None, str | None]:
    for field, unit_field in (
        ("gis_area", "gis_area_unit"),
        ("legal_area", "lgl_area_unit"),
    ):
        value = _parse_number(attrs.get(field))
        unit = str(attrs.get(unit_field) or "").strip().lower()
        if value is not None and value > 0:
            if unit.startswith("acre") or unit in {"ac", "acres"}:
                return value, field
            if unit in {"sqft", "square feet", "ft2"}:
                return value / _SQFT_PER_ACRE, field
            if unit in {"sqm", "square meters", "m2"}:
                return value / _SQM_PER_ACRE, field
            if not unit:
                return value, field
    value = attrs.get("st_area(shape)")
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / _SQM_PER_ACRE, "st_area(shape)_sqm"
    return None, None


def _parse_number(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace(",", "").replace("$", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _is_statewide_feature(feature: dict) -> bool:
    attrs = feature.get("attributes") or {}
    return "prop_id" in attrs or "geo_id" in attrs


def _normalize_county(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    cleaned = cleaned.removesuffix(" county")
    return cleaned or None


def _exact_note(parcel_id: str | None, source_label: str) -> str:
    return (
        f"Parcel {parcel_id} resolved from the {source_label} by supplied "
        "parcel ID. Acreage comes from the public parcel layer; a survey "
        "or recorded plat controls for legal acreage."
    )


def _arcgis_polygon_to_geojson(
    geometry: dict,
) -> dict | None:
    rings = geometry.get("rings")
    if not rings:
        return None
    return {
        "type": "Polygon",
        "coordinates": rings,
    }
