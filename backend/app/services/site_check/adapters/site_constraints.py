"""Consolidated, free public-source site constraints resolution.

This adapter runs after the ordinary Public Data adapters.  It promotes their
facts into one diligence-oriented contract and supplements them with public
pipeline, aquifer, critical-habitat, and OpenStreetMap infrastructure/access
screens.  A resolved coverage state is returned for every requested field;
absence of a public engineering/title record is never converted into a fact.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import requests
from pyproj import Transformer
from shapely.geometry import Point, box, mapping, shape
from shapely.ops import transform as shapely_transform

from app.config.settings import get_settings
from app.regions import states
from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
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

log = logging.getLogger("propdesk.site_check.site_constraints")

SOURCE_ID = "site_constraints"
_RADIUS_MI = 5.0
_FT_PER_M = 3.280839895
_SQFT_PER_SQM = 10.763910417

_GAS_PIPELINES_QUERY = (
    "https://arcgis.netl.doe.gov/server/rest/services/Hosted/"
    "Natural_Gas_Pipelines/FeatureServer/10/query"
)
_EDWARDS_QUERY = (
    "https://services5.arcgis.com/bVphnK8rPe5MHUSr/arcgis/rest/services/"
    "Edwards_Aquifer/FeatureServer/0/query"
)
_CRITICAL_HABITAT_FINAL_QUERY = (
    "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/"
    "USFWS_Critical_Habitat/FeatureServer/0/query"
)
_CRITICAL_HABITAT_PROPOSED_QUERY = (
    "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/"
    "USFWS_Critical_Habitat/FeatureServer/2/query"
)
# Planet-wide public Overpass instances, all anonymous endpoints that
# receive only the subject coordinates. The public mirrors overload
# independently and often simultaneously in pairs (live 2026-07-15:
# kumi + private.coffee hung, overpass-api.de 504, mail.ru answered in
# 2.3s) — a wide ring plus one attempt per mirror is what actually
# keeps this screen alive.
_OVERPASS_URLS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

_NPMS_PUBLIC_VIEWER = "https://www.npms.phmsa.dot.gov/PublicViewer/"
_TCEQ_EDWARDS_VIEWER = "https://www.tceq.texas.gov/permitting/eapp/viewer.html"
_USFWS_IPAC = "https://ipac.ecosphere.fws.gov/"


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates()

    sess = http_session(session)
    results = (config or {}).get("upstream_results") or {}
    parcel_geojson = _parcel_geojson(identity, results)
    errors: list[str] = []

    # The five public screens hit five independent providers. They run
    # concurrently so the resilient per-provider retry budget bounds
    # this adapter's wall time to the slowest provider instead of the
    # sum of all five (the orchestrator already shares one Session
    # across parallel adapter workers, so concurrent use is the
    # established pattern).
    with ThreadPoolExecutor(
        max_workers=5, thread_name_prefix="site-constraints",
    ) as pool:
        gas_future = pool.submit(
            _query_gas_pipelines,
            sess, identity.lat, identity.lng, parcel_geojson,
        )
        # The Edwards Aquifer map (TCEQ) is a Texas layer.
        edwards_future = None if identity.state != "TX" else pool.submit(
            _query_polygon_screen,
            sess,
            _EDWARDS_QUERY,
            identity.lat,
            identity.lng,
            parcel_geojson,
            out_fields="OBJECTID,TYPE",
            kind="edwards_aquifer",
            title_field="TYPE",
            source="TCEQ Edwards Aquifer protection zones",
            source_url=_TCEQ_EDWARDS_VIEWER,
        )
        habitat_futures = [
            pool.submit(
                _query_polygon_screen,
                sess,
                url,
                identity.lat,
                identity.lng,
                parcel_geojson,
                out_fields=(
                    "OBJECTID,comname,sciname,status,pubdate,effectdate,"
                    "unit,subunit,listing_status"
                ),
                kind="critical_habitat",
                title_field="comname",
                source="U.S. Fish and Wildlife Service critical habitat",
                source_url=_USFWS_IPAC,
                extra_properties={"designation": designation},
            )
            for url, designation in (
                (_CRITICAL_HABITAT_FINAL_QUERY, "final"),
                (_CRITICAL_HABITAT_PROPOSED_QUERY, "proposed"),
            )
        ]
        osm_future = pool.submit(
            _query_osm_site_features,
            sess, identity.lat, identity.lng, parcel_geojson,
        )

    gas_pipelines, gas_error = gas_future.result()

    edwards_zones, edwards_error = (
        edwards_future.result() if edwards_future else ([], None)
    )
    if edwards_error:
        errors.append(edwards_error)

    habitat: list[dict[str, Any]] = []
    for future in habitat_futures:
        rows, error = future.result()
        habitat.extend(rows)
        if error:
            errors.append(error)

    osm, osm_error = osm_future.result()

    # ── Degradation vs failure (2026-07-22, Admin) ──────────────
    # The public Overpass mirrors (and the DOE pipeline mirror) fail OFTEN,
    # and a failure there used to flip the WHOLE card into the "did not
    # finish — temporary server error" state, hiding confirmed findings like
    # an Edwards recharge-zone intersection behind a scary banner. Rule:
    #   - Supplementary/community sources (OSM access+utility features, DOE
    #     gas-pipeline mirror) DEGRADE: recorded in facts.degraded_sources +
    #     advisory, card still renders everything else.
    #   - Authoritative regulatory screens (Edwards, critical habitat) still
    #     fail LOUD — silently missing a deal-shaper is worse.
    #   - If literally everything failed, keep the hard failure.
    degraded_sources: list[dict[str, str]] = []
    everything_failed = bool(
        gas_error and edwards_error and osm_error
        and len(habitat) == 0 and not edwards_zones and not gas_pipelines
    )
    if everything_failed:
        if gas_error:
            errors.append(gas_error)
        if osm_error:
            errors.append(osm_error)
    else:
        if gas_error:
            degraded_sources.append({
                "label": "Gas transmission pipelines (DOE map mirror)",
                "reason": gas_error,
                "impact": (
                    "Mapped transmission pipelines are missing from this "
                    "refresh; the PHMSA NPMS viewer link still applies."
                ),
            })
        if osm_error:
            degraded_sources.append({
                "label": "OpenStreetMap access & utility features",
                "reason": osm_error,
                "impact": (
                    "Community-mapped driveways, utility lines, and access "
                    "features are missing from this refresh. Refresh Public "
                    "Data later to add them."
                ),
            })

    buildable = _buildable_envelope(
        parcel_geojson,
        results,
        gas_pipelines=gas_pipelines,
        osm=osm,
    )
    market = _market_context(identity.lat, identity.lng, results)
    coverage = _coverage_ledger(
        results=results,
        gas_pipelines=gas_pipelines,
        edwards_zones=edwards_zones,
        habitat=habitat,
        osm=osm,
        buildable=buildable,
        market=market,
        source_errors=errors + [d["reason"] for d in degraded_sources],
        state=identity.state,
    )

    mapped_count = sum(
        len(rows)
        for rows in (
            gas_pipelines,
            edwards_zones,
            habitat,
            osm["utility_lines"],
            osm["utility_facilities"],
            osm["access_features"],
        )
    )
    access_facts = _access_facts(
        results, osm, parcel_geojson, identity.lat, identity.lng,
    )

    material_findings = _material_findings(
        edwards_zones, habitat, gas_pipelines,
    )
    high_findings = [
        f for f in material_findings if f["severity"] == "high"
    ]

    facts = {
        "result_schema_version": 4,
        "material_findings": material_findings,
        "degraded_sources": degraded_sources,
        "access_facts": access_facts,
        "status": "resolved_with_gaps" if any(
            row["status"] in {
                "authoritative_source_unavailable",
                "provider_verification_required",
                "title_or_survey_required",
            }
            for row in coverage
        ) else "resolved",
        "search_radius_mi": _RADIUS_MI,
        "coverage": coverage,
        "gas_pipelines": gas_pipelines,
        "edwards_aquifer_zones": edwards_zones,
        "critical_habitat": habitat,
        **osm,
        "buildable_envelope": buildable,
        "market_context": market,
        "source_links": [
            {"label": "PHMSA NPMS Public Viewer", "url": _NPMS_PUBLIC_VIEWER},
            *(
                [{"label": "TCEQ Edwards Aquifer Viewer", "url": _TCEQ_EDWARDS_VIEWER}]
                if (identity.state or "TX") == "TX" else []
            ),
            {"label": "USFWS IPaC", "url": _USFWS_IPAC},
        ],
        "mapped_feature_count": mapped_count,
        "advisory": " ".join(filter(None, [
            (
                "MAJOR CONSTRAINT: " + "; ".join(
                    f["headline"] for f in high_findings
                ) + ". Read the flagged finding(s) before anything else — "
                "these are confirmed on official map layers and shape the "
                "deal."
            ) if high_findings else None,
            (
                "Public-source resolution is complete for this refresh. "
                "Mapped screening evidence does not establish utility "
                "capacity, a right to connect, three-phase availability, "
                "pipeline/easement title, approved access, survey location, "
                "or a legally buildable area. Rows marked provider, title, "
                "survey, or agency verification required remain open "
                "diligence items."
            ),
            (
                "Some supplementary map sources were temporarily "
                "unavailable this refresh ("
                + "; ".join(d["label"] for d in degraded_sources)
                + ") — the screens above still resolved; refresh later to "
                "add the missing features."
            ) if degraded_sources else None,
        ])),
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(
            SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE
            if errors else SourceAccessStatus.PROGRAMMATIC_PUBLIC
        ),
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.PARTIAL if errors else CoverageStatus.COVERED,
        normalized_facts=facts,
        identifier_used="parcel_polygon" if parcel_geojson else "lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[facts["advisory"]],
        errors=errors or None,
        manual_upload_request=ManualUploadRequest(
            document_types=[
                "will-serve letter or utility SER",
                "utility map or as-built",
                "title commitment and recorded easement instruments",
                "boundary/topographic survey",
                "access permit or TIA",
                "plat and civil site plan",
            ],
            reason=(
                "Bind non-public capacity, tie-in, easement, access, and "
                "buildable-area evidence to the resolved public screen."
            ),
        ),
    )


# ── Access facts (Task B of the approved feasibility spec §5) ─────────
# Mapped/current facts ONLY, per frontage road, with the governing
# authority identified. Deliberately ABSENT (spec §5 / reviewer round-2):
# no "TIA likely", no "turn lane likely", no future-ROW claims from
# ROW_MIN, no "access looks feasible" — determinations rest with the
# authority. The TxDOT Access Management Manual spacing table is shown
# for REFERENCE and only on TxDOT on-system frontage.

_STATE_SYSTEM_PREFIXES = (
    "IH ", "US ", "SH ", "SL ", "SS ", "FM ", "RM ", "PR ",
    "BI ", "BU ", "BS ", "BF ", "LP ",
)

# TxDOT Access Management Manual, Chapter 2 (connection spacing by
# posted speed, roads other than freeways). Published standard —
# reference only; the district determines each permit.
_AMM_CITATION = (
    "TxDOT Access Management Manual, Ch. 2 connection-spacing criteria "
    "(posted-speed based); the TxDOT district determines each driveway "
    "permit."
)
_AMM_SPACING_FT_BY_SPEED = (
    (30, 200), (35, 250), (40, 305), (45, 360), (999, 425),
)


def _amm_reference_spacing_ft(posted_speed) -> int | None:
    speed = None
    try:
        speed = int(posted_speed)
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    for limit, spacing in _AMM_SPACING_FT_BY_SPEED:
        if speed <= limit:
            return spacing
    return None


def _access_facts(
    results: dict[str, Any],
    osm: dict[str, list[dict[str, Any]]],
    parcel_geojson: dict | None,
    lat: float,
    lng: float,
) -> dict[str, Any]:
    settings = get_settings()
    roadway = _facts(results, "txdot_roadway")
    rows = roadway.get("roadways") or []
    jurisdiction = _facts(results, "jurisdiction_resolver")
    jurisdiction_kind = str(jurisdiction.get("jurisdiction_kind") or "")
    city = str(jurisdiction.get("city") or "").strip().title()
    county = str(jurisdiction.get("county") or "").strip().title()

    frontage_max_mi = settings.access_facts_frontage_max_mi
    frontage_max_ft = settings.access_facts_frontage_max_ft
    # Frontage basis: distance from the PARCEL BOUNDARY to the road
    # centerline (a deep parcel's centroid can sit hundreds of feet
    # from its own frontage road — live Travis/US 79 finding). Falls
    # back to anchor distance_mi only when no parcel polygon exists.
    parcel_utm = None
    forward = None
    if isinstance(parcel_geojson, dict):
        try:
            forward, _inv = _utm_transformers(lat, lng)
            parcel_utm = shapely_transform(
                forward.transform, shape(parcel_geojson),
            )
        except (TypeError, ValueError):
            parcel_utm = None
    connections = [
        row for row in (osm.get("access_features") or [])
        if isinstance(row.get("map_geometry"), dict)
        and row.get("kind") != "access_signal"
    ]

    # Per road NAME, take the NEAREST geometry across BOTH published
    # sources: the roadway inventory's per-road dedup can keep a
    # far-away segment of the same road (live Chandler Rd finding: the
    # inventory row sat 1 mile out while the AADT row carried the
    # frontage segment) — same source-merging the map layer performs.
    aadt_rows = _facts(results, "txdot_aadt").get("traffic_counts") or []
    # Per road name keep BOTH the nearest edge distance AND that
    # nearest GEOMETRY: the same geometry must drive inclusion and the
    # mapped-connection gap stationing (reviewer review finding — using
    # the roadway row's own geometry for gaps reintroduced the
    # Chandler-class defect whenever AADT carried the frontage segment).
    best_by_road: dict[str, tuple[float, dict]] = {}
    if parcel_utm is not None:
        for source_row in [*rows, *aadt_rows]:
            geometry = source_row.get("map_geometry")
            name_key = " ".join(
                str(source_row.get("road_name") or "").casefold().split()
            )
            if not name_key or not isinstance(geometry, dict):
                continue
            try:
                road_utm = shapely_transform(
                    forward.transform, shape(geometry),
                )
                edge_ft = float(parcel_utm.distance(road_utm)) * _FT_PER_M
            except (TypeError, ValueError):
                continue
            current = best_by_road.get(name_key)
            if current is None or edge_ft < current[0]:
                best_by_road[name_key] = (edge_ft, geometry)

    frontages: list[dict[str, Any]] = []
    for row in rows:
        distance_mi = row.get("distance_mi")
        name_key = " ".join(
            str(row.get("road_name") or "").casefold().split()
        )
        best = best_by_road.get(name_key)
        edge_distance_ft = best[0] if best else None
        frontage_geometry = (
            best[1] if best else row.get("map_geometry")
        )
        if edge_distance_ft is not None:
            if edge_distance_ft > frontage_max_ft:
                continue
        else:
            if not isinstance(distance_mi, (int, float)):
                continue
            if distance_mi > frontage_max_mi:
                continue
        name = str(row.get("road_name") or "Unknown")
        on_system = name.upper().startswith(_STATE_SYSTEM_PREFIXES)
        if on_system:
            authority = "TxDOT district"
            status = (
                "TxDOT driveway permit required; published spacing "
                "standard shown for reference"
            )
        elif jurisdiction_kind == "city" and city:
            authority = f"City of {city}"
            status = (
                "Local access standards not encoded; driveway approval "
                f"by the City of {city} is unresolved"
            )
        else:
            authority = f"{county} County" if county else "County"
            status = (
                "Local access standards not encoded; driveway approval "
                f"by {authority} is unresolved"
            )

        facts_row: dict[str, Any] = {
            "road_name": name,
            "authority": authority,
            "status": status,
            "distance_mi": distance_mi,
            "parcel_edge_distance_ft": (
                round(edge_distance_ft)
                if edge_distance_ft is not None else None
            ),
            "posted_speed_mph": row.get("speed_limit") or None,
            "lanes": row.get("num_lanes") or None,
            "median_type": row.get("median_type_label"),
            "median_width_ft": row.get("median_width_ft"),
            "access_control": row.get("access_control_label"),
            "right_turn_lane": row.get("right_turn_lane_label"),
            "left_turn_lane": row.get("left_turn_lane_label"),
            "row_width_ft": row.get("row_width_ft"),
            "row_width_note": (
                "current roadway-inventory attribute; not a planned "
                "widening or future take"
            ),
        }
        if on_system:
            spacing = _amm_reference_spacing_ft(row.get("speed_limit"))
            if spacing is not None:
                facts_row["amm_reference_spacing_ft"] = spacing
                facts_row["amm_reference_citation"] = _AMM_CITATION
        gaps = _mapped_connection_gaps_ft(
            frontage_geometry, connections, lat=lat, lng=lng,
        )
        if gaps:
            facts_row["mapped_connection_gaps_ft"] = gaps
            facts_row["mapped_connection_note"] = (
                "measured between community-mapped connections only — "
                "unmapped driveways may exist"
            )
        frontages.append(facts_row)

    frontages.sort(key=lambda r: (
        r["parcel_edge_distance_ft"]
        if r["parcel_edge_distance_ft"] is not None
        else (r["distance_mi"] or 0) * 5280
    ))
    return {
        "frontages": frontages,
        "status": (
            "mapped access facts available"
            if frontages
            else "no mapped road frontage identified (screening)"
        ),
        "planned_work_source": (
            "see TxDOT projects layer for published planned work"
        ),
        "advisory": (
            "Mapped facts only. Legal access, driveway location, "
            "spacing, median openings, and any traffic study are "
            "determined by the road authority."
        ),
    }


def _mapped_connection_gaps_ft(
    road_geometry: dict | None,
    connections: list[dict[str, Any]],
    *,
    lat: float,
    lng: float,
) -> list[int]:
    """Gap distances (ft) between community-mapped connections
    projected onto the frontage road centerline. Facts only — an
    empty list when fewer than two mapped connections sit within the
    buffer."""
    if not isinstance(road_geometry, dict) or len(connections) < 2:
        return []
    settings = get_settings()
    buffer_m = settings.access_facts_connection_buffer_m
    try:
        forward, _inverse = _utm_transformers(lat, lng)
        road = shapely_transform(forward.transform, shape(road_geometry))
        if road.is_empty or road.geom_type not in {
            "LineString", "MultiLineString",
        }:
            return []
        stations: list[float] = []
        for row in connections:
            geom = shapely_transform(
                forward.transform, shape(row["map_geometry"]),
            )
            point = geom.centroid
            if road.distance(point) > buffer_m:
                continue
            stations.append(float(road.project(point)))
        stations.sort()
        gaps = [
            round((b - a) * _FT_PER_M)
            for a, b in pairwise(stations)
            if (b - a) * _FT_PER_M >= 1
        ]
        return gaps[:20]
    except (TypeError, ValueError):
        return []


def _facts(results: dict[str, Any], source_id: str) -> dict[str, Any]:
    raw = results.get(source_id) if isinstance(results, dict) else None
    value = raw.get("normalized_facts") if isinstance(raw, dict) else None
    return value if isinstance(value, dict) else {}


def _parcel_geojson(
    identity: PropertyIdentity,
    results: dict[str, Any],
) -> dict[str, Any] | None:
    candidates = [identity.parcel_geometry]
    for source_id in ("wcad_parcel", "tcad_parcel"):
        candidates.append(_facts(results, source_id).get("parcel_polygon"))
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
                return mapping(geom)
        except (TypeError, ValueError):
            continue
    return None


def _bbox(lat: float, lng: float, radius_mi: float = _RADIUS_MI) -> list[float]:
    dlat = radius_mi / 69.0
    dlng = radius_mi / (69.0 * max(0.2, math.cos(math.radians(lat))))
    return [lng - dlng, lat - dlat, lng + dlng, lat + dlat]


def _utm_transformers(lat: float, lng: float) -> tuple[Transformer, Transformer]:
    zone = min(60, max(1, int((lng + 180) / 6) + 1))
    epsg = (32600 if lat >= 0 else 32700) + zone
    return (
        Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True),
        Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True),
    )


def _subject_shape(lat: float, lng: float, parcel_geojson: dict | None):
    if parcel_geojson:
        return shape(parcel_geojson)
    return Point(lng, lat)


def _distance_ft(
    geometry: dict[str, Any],
    *,
    lat: float,
    lng: float,
    parcel_geojson: dict | None,
) -> float | None:
    try:
        fwd, _ = _utm_transformers(lat, lng)
        subject = shapely_transform(
            fwd.transform, _subject_shape(lat, lng, parcel_geojson),
        )
        target = shapely_transform(fwd.transform, shape(geometry))
        return round(subject.distance(target) * _FT_PER_M)
    except (TypeError, ValueError):
        return None


def _clip_geometry(
    geometry: dict[str, Any], lat: float, lng: float,
) -> dict[str, Any] | None:
    try:
        geom = shape(geometry)
        clipped = geom.intersection(box(*_bbox(lat, lng)))
        if clipped.is_empty:
            return None
        simplified = clipped.simplify(0.00004, preserve_topology=True)
        return mapping(simplified)
    except (TypeError, ValueError):
        return None


def _arcgis_geojson(
    sess: requests.Session,
    url: str,
    *,
    lat: float,
    lng: float,
    out_fields: str,
    max_records: int = 100,
) -> list[dict[str, Any]]:
    payload = resilient_json(
        sess,
        "GET",
        url,
        params={
            "f": "geojson",
            "where": "1=1",
            "geometry": ",".join(map(str, _bbox(lat, lng))),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "outSR": 4326,
            "outFields": out_fields,
            "returnGeometry": "true",
            "resultRecordCount": max_records,
        },
        json_error=arcgis_json_error,
    )
    features = payload.get("features") or []
    return [row for row in features if isinstance(row, dict)]


def _query_gas_pipelines(
    sess: requests.Session,
    lat: float,
    lng: float,
    parcel_geojson: dict | None,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        features = _arcgis_geojson(
            sess,
            _GAS_PIPELINES_QUERY,
            lat=lat,
            lng=lng,
            out_fields="objectid,typepipe,operator",
            max_records=80,
        )
        rows = []
        for feature in features:
            geometry = _clip_geometry(feature.get("geometry") or {}, lat, lng)
            if not geometry or geometry.get("type") not in {
                "LineString", "MultiLineString",
            }:
                continue
            props = feature.get("properties") or {}
            distance_ft = _distance_ft(
                geometry, lat=lat, lng=lng, parcel_geojson=parcel_geojson,
            )
            rows.append({
                "id": f"hifld-gas:{props.get('objectid') or len(rows)}",
                "kind": "gas_transmission_pipeline",
                "title": str(props.get("operator") or "Natural gas pipeline"),
                "operator": props.get("operator"),
                "pipeline_type": props.get("typepipe"),
                "distance_ft": distance_ft,
                "intersects_subject": distance_ft == 0,
                "map_geometry": geometry,
                "source": "DOE/NETL HIFLD Natural Gas Pipelines",
                "source_url": _NPMS_PUBLIC_VIEWER,
                "currency_note": (
                    "HIFLD screening geometry may be dated. Confirm current "
                    "operator/location in the PHMSA NPMS county viewer and by 811."
                ),
                "easement_status": "title_or_survey_required",
            })
        return sorted(rows, key=lambda row: row["distance_ft"] or 0), None
    except Exception as exc:
        log.warning("gas pipeline screen failed: %s", exc)
        return [], f"Gas pipeline public layer failed: {str(exc)[:220]}"


# ── Material findings (2026-07-22, Admin) ───────────────────────
# A CONFIRMED deal-shaping constraint must be stated forcefully — with its
# consequence — not buried under "specialist review required". This array is
# the general surface: Edwards zones are the first producer; pipeline-through-
# parcel and critical-habitat intersections plug into the same shape, and the
# UI renders severity=high entries as prominent callouts.


def _material_findings(
    edwards_zones: list[dict[str, Any]],
    habitat: list[dict[str, Any]],
    gas_pipelines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    for zone in edwards_zones:
        if not zone.get("intersects_subject"):
            continue
        title = str(zone.get("title") or "Edwards Aquifer protection zone")
        low = title.lower()
        if "recharge" in low:
            severity, plan = "high", "Water Pollution Abatement Plan (WPAP)"
            why = (
                "The recharge zone is the most tightly regulated Edwards "
                "zone — surface water enters the aquifer directly here. "
                f"TCEQ must approve a {plan} BEFORE any construction or "
                "ground disturbance; fuel/chemical storage needs enhanced "
                "containment; impervious cover is capped and permanent "
                "water-quality controls are required. This is a gating "
                "permit that adds real cost and time — not a formality."
            )
        elif "transition" in low:
            severity, plan = "high", "Water Pollution Abatement Plan (WPAP)"
            why = (
                f"The transition zone still requires a TCEQ-approved {plan} "
                "before construction, with geologic assessment and "
                "water-quality controls. Budget permitting cost and time."
            )
        else:  # contributing zone (or unlabeled protection area)
            severity, plan = "medium", "Contributing Zone Plan (CZP)"
            why = (
                f"The contributing zone requires a TCEQ-approved {plan} "
                "before construction on most commercial projects, with "
                "permanent water-quality controls."
            )
        findings.append({
            "id": zone.get("id") or "edwards_zone",
            "severity": severity,
            "kind": "edwards_aquifer",
            "headline": f"This property is ON the {title}",
            "what_we_verified": (
                f"TCEQ's official Edwards Aquifer map places the subject "
                f"property inside the {title} (intersects the parcel, not "
                "merely nearby)."
            ),
            "why_it_matters": why,
            "next_step": (
                "Bring in an Edwards-experienced civil/environmental "
                f"engineer BEFORE committing capital; confirm {plan} scope "
                "and timeline with TCEQ."
            ),
            "source": zone.get("source"),
            "source_url": zone.get("source_url"),
        })

    for row in habitat:
        if not row.get("intersects_subject"):
            continue
        title = str(row.get("title") or "designated critical habitat")
        findings.append({
            "id": row.get("id") or "critical_habitat",
            "severity": "high",
            "kind": "critical_habitat",
            "headline": f"Mapped critical habitat ({title}) intersects this property",
            "what_we_verified": (
                "USFWS-designated critical habitat overlaps the subject "
                "property on the official map layer."
            ),
            "why_it_matters": (
                "Federal Endangered Species Act review can restrict or "
                "condition development where designated habitat overlaps a "
                "site; permits with a federal nexus trigger consultation. "
                "This can reshape the buildable area and timeline."
            ),
            "next_step": (
                "Run a USFWS IPaC screen and engage an environmental "
                "consultant before design; do not assume full-parcel "
                "buildability."
            ),
            "source": row.get("source"),
            "source_url": row.get("source_url"),
        })

    for row in gas_pipelines:
        if not row.get("intersects_subject"):
            continue
        findings.append({
            "id": row.get("id") or "gas_pipeline",
            "severity": "high",
            "kind": "gas_transmission_pipeline",
            "headline": "A mapped gas transmission pipeline crosses this property",
            "what_we_verified": (
                "A transmission pipeline segment intersects the subject "
                "parcel on the public map layer."
            ),
            "why_it_matters": (
                "Pipeline easements carry building setbacks and use "
                "restrictions that can cut directly through the buildable "
                "area, and crossing/encroachment agreements take time."
            ),
            "next_step": (
                "Verify the easement location and width on the title "
                "commitment and the PHMSA NPMS viewer; get the operator's "
                "encroachment requirements before site design."
            ),
            "source": row.get("source"),
            "source_url": row.get("source_url"),
        })

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return findings


def _query_polygon_screen(
    sess: requests.Session,
    url: str,
    lat: float,
    lng: float,
    parcel_geojson: dict | None,
    *,
    out_fields: str,
    kind: str,
    title_field: str,
    source: str,
    source_url: str,
    extra_properties: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        features = _arcgis_geojson(
            sess, url, lat=lat, lng=lng, out_fields=out_fields, max_records=40,
        )
        rows = []
        for feature in features:
            geometry = _clip_geometry(feature.get("geometry") or {}, lat, lng)
            if not geometry or geometry.get("type") not in {
                "Polygon", "MultiPolygon",
            }:
                continue
            props = feature.get("properties") or {}
            distance_ft = _distance_ft(
                geometry, lat=lat, lng=lng, parcel_geojson=parcel_geojson,
            )
            rows.append({
                "id": f"{kind}:{props.get('OBJECTID') or len(rows)}",
                "kind": kind,
                "title": str(props.get(title_field) or kind.replace("_", " ").title()),
                "distance_ft": distance_ft,
                "intersects_subject": distance_ft == 0,
                "map_geometry": geometry,
                "attributes": {
                    key: value for key, value in props.items()
                    if value not in (None, "") and key != "OBJECTID"
                },
                "source": source,
                "source_url": source_url,
                **(extra_properties or {}),
            })
        return sorted(rows, key=lambda row: row["distance_ft"] or 0), None
    except Exception as exc:
        log.warning("%s screen failed: %s", kind, exc)
        return [], f"{source} layer failed: {str(exc)[:220]}"


def _query_osm_site_features(
    sess: requests.Session,
    lat: float,
    lng: float,
    parcel_geojson: dict | None,
) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    radius_m = round(_RADIUS_MI * 1609.344)
    # Server-side Overpass query timeout stays under the client read
    # timeout so the server aborts (and reports) before the client
    # gives up on a still-running query.
    server_timeout_s = max(
        25, get_settings().public_intel_http_timeout_seconds - 5,
    )
    query = f"""
[out:json][timeout:{server_timeout_s}];
(
  way[man_made=pipeline](around:{radius_m},{lat},{lng});
  nwr[man_made~\"^(water_works|wastewater_plant|pumping_station)$\"](around:{radius_m},{lat},{lng});
  node[highway=traffic_signals](around:{radius_m},{lat},{lng});
  way[highway=service][service=driveway](around:3219,{lat},{lng});
);
out center geom tags;
""".strip()
    try:
        # Attempts rotate across the public Overpass mirrors, so a
        # mirror outage and a transient error share one retry budget;
        # the floor below guarantees every mirror gets at least one
        # attempt before the screen gives up.
        payload = resilient_json(
            sess, "POST", _OVERPASS_URLS, data={"data": query},
            attempts=max(
                get_settings().public_intel_http_attempts,
                len(_OVERPASS_URLS),
            ),
        )
        elements = payload.get("elements") or []
        return _normalize_osm_elements(
            elements, lat=lat, lng=lng, parcel_geojson=parcel_geojson,
        ), None
    except Exception as exc:
        return {
            "utility_lines": [], "utility_facilities": [],
            "access_features": [],
        }, (
            "OpenStreetMap infrastructure/access screen failed: "
            f"{str(exc)[:220]}"
        )


def _osm_geometry(element: dict[str, Any]) -> dict[str, Any] | None:
    geometry = element.get("geometry")
    if isinstance(geometry, list):
        coordinates = [
            [point.get("lon"), point.get("lat")]
            for point in geometry
            if isinstance(point, dict)
            and isinstance(point.get("lon"), (int, float))
            and isinstance(point.get("lat"), (int, float))
        ]
        if len(coordinates) >= 2:
            return {"type": "LineString", "coordinates": coordinates}
    if isinstance(element.get("lon"), (int, float)) and isinstance(
        element.get("lat"), (int, float),
    ):
        return {
            "type": "Point",
            "coordinates": [element["lon"], element["lat"]],
        }
    center = element.get("center")
    if isinstance(center, dict) and isinstance(center.get("lon"), (int, float)):
        return {"type": "Point", "coordinates": [center["lon"], center["lat"]]}
    return None


def _normalize_osm_elements(
    elements: list[dict[str, Any]],
    *,
    lat: float,
    lng: float,
    parcel_geojson: dict | None,
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {
        "utility_lines": [], "utility_facilities": [], "access_features": [],
    }
    for element in elements[:500]:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        geometry = _osm_geometry(element)
        if not geometry:
            continue
        distance_ft = _distance_ft(
            geometry, lat=lat, lng=lng, parcel_geojson=parcel_geojson,
        )
        stable = f"osm:{element.get('type')}:{element.get('id')}"
        if tags.get("man_made") == "pipeline":
            substance = str(tags.get("substance") or tags.get("content") or "").lower()
            utility_kind = (
                "water" if "water" in substance
                else "wastewater" if substance in {"sewage", "wastewater"}
                else "gas" if substance in {"gas", "natural_gas"}
                else "other"
            )
            output["utility_lines"].append({
                "id": stable,
                "kind": f"utility_main_{utility_kind}",
                "title": str(tags.get("name") or f"Mapped {utility_kind} pipeline"),
                "utility_kind": utility_kind,
                "operator": tags.get("operator"),
                "pipe_size": tags.get("diameter") or tags.get("diameter:nominal"),
                "pressure": tags.get("pressure"),
                "flow_type": tags.get("flow") or tags.get("sewer"),
                "distance_ft": distance_ft,
                "map_geometry": geometry,
                "source": "OpenStreetMap contributors",
                "confidence": "community_mapped_screening_indicator",
            })
        elif tags.get("man_made") in {
            "water_works", "wastewater_plant", "pumping_station",
        }:
            facility_kind = str(tags["man_made"])
            output["utility_facilities"].append({
                "id": stable,
                "kind": f"utility_facility_{facility_kind}",
                "title": str(tags.get("name") or facility_kind.replace("_", " ").title()),
                "facility_kind": facility_kind,
                "operator": tags.get("operator"),
                "distance_ft": distance_ft,
                "map_geometry": geometry,
                "source": "OpenStreetMap contributors",
                "confidence": "community_mapped_screening_indicator",
            })
        elif tags.get("highway") == "traffic_signals":
            output["access_features"].append({
                "id": stable,
                "kind": "access_signal",
                "title": str(tags.get("name") or "Traffic signal"),
                "distance_ft": distance_ft,
                "map_geometry": geometry,
                "source": "OpenStreetMap contributors",
                "confidence": "community_mapped_screening_indicator",
            })
        elif tags.get("service") == "driveway":
            output["access_features"].append({
                "id": stable,
                "kind": "access_driveway",
                "title": str(tags.get("name") or "Mapped driveway"),
                "access": tags.get("access"),
                "distance_ft": distance_ft,
                "map_geometry": geometry,
                "source": "OpenStreetMap contributors",
                "confidence": "community_mapped_screening_indicator",
            })
    for rows in output.values():
        rows.sort(key=lambda row: row.get("distance_ft") or 0)
    output["utility_lines"] = output["utility_lines"][:80]
    output["utility_facilities"] = output["utility_facilities"][:40]
    output["access_features"] = output["access_features"][:120]
    return output


def _standard_value(standards: dict[str, Any], key: str) -> float | None:
    row = standards.get(key)
    value = row.get("value") if isinstance(row, dict) else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _buildable_envelope(
    parcel_geojson: dict[str, Any] | None,
    results: dict[str, Any],
    *,
    gas_pipelines: list[dict[str, Any]],
    osm: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if not parcel_geojson:
        return {
            "status": "parcel_geometry_required",
            "map_geometry": None,
            "gross_area_sqft": None,
            "screened_area_sqft": None,
            "completeness": "parcel_boundary_missing",
            "applied_constraints": [],
            "missing_constraints": ["verified parcel boundary"],
            "constraints_ledger": [],
        }
    standards_facts = _facts(results, "zoning_standards")
    standards = standards_facts.get("dimensional_standards") or {}
    setbacks = {
        label: value for label, value in (
            ("front setback", _standard_value(standards, "front_setback")),
            ("side setback", _standard_value(standards, "side_setback")),
            ("rear setback", _standard_value(standards, "rear_setback")),
        ) if value is not None
    }
    offset_ft = max(setbacks.values()) if setbacks else 0.0
    max_impervious_pct = _standard_value(standards, "max_impervious_cover")
    fema = _facts(results, "fema_nfhl")
    wetlands = _facts(results, "wetlands_nwi")
    ccn = _facts(results, "twdb_ccn")
    electric = _facts(results, "electric_power_infrastructure")
    roadway = _facts(results, "txdot_extended").get("roadway_context") or {}
    projects = _facts(results, "txdot_projects")
    plats = _facts(results, "plats_intel")
    try:
        parcel = shape(parcel_geojson)
        if not parcel.is_valid:
            parcel = parcel.buffer(0)
        centroid = parcel.centroid
        fwd, inv = _utm_transformers(centroid.y, centroid.x)
        projected = shapely_transform(fwd.transform, parcel)
        screened = projected.buffer(-offset_ft / _FT_PER_M) if offset_ft else projected
        after_setback_area = screened.area * _SQFT_PER_SQM if not screened.is_empty else 0.0

        sfha_excluded = None
        sfha_geojson = fema.get("sfha_map_geometry")
        if isinstance(sfha_geojson, dict):
            try:
                sfha = shape(sfha_geojson)
                if not sfha.is_valid:
                    sfha = sfha.buffer(0)
                sfha_projected = shapely_transform(fwd.transform, sfha)
                sfha_excluded = screened.intersection(sfha_projected)
                screened = screened.difference(sfha_projected)
            except Exception:
                sfha_excluded = None

        wetland_excluded = None
        wetland_geojson = wetlands.get("wetlands_map_geometry")
        if isinstance(wetland_geojson, dict):
            try:
                wetland = shape(wetland_geojson)
                if not wetland.is_valid:
                    wetland = wetland.buffer(0)
                wetland_projected = shapely_transform(fwd.transform, wetland)
                wetland_excluded = screened.intersection(wetland_projected)
                screened = screened.difference(wetland_projected)
            except Exception:
                wetland_excluded = None

        if screened.is_empty:
            screened_geojson = None
            screened_area = 0.0
        else:
            screened_geojson = mapping(shapely_transform(inv.transform, screened))
            screened_area = screened.area * _SQFT_PER_SQM
        applied = ["verified CAD parcel boundary"]
        applied.extend(f"{label}: {value:g} ft" for label, value in setbacks.items())
        sfha_excluded_sqft = (
            sfha_excluded.area * _SQFT_PER_SQM
            if sfha_excluded is not None and not sfha_excluded.is_empty else 0.0
        )
        wetland_excluded_sqft = (
            wetland_excluded.area * _SQFT_PER_SQM
            if wetland_excluded is not None and not wetland_excluded.is_empty else 0.0
        )
        if sfha_excluded_sqft > 1:
            applied.append(
                f"FEMA SFHA conservatively excluded: {sfha_excluded_sqft / 43560:,.2f} ac"
            )
        if wetland_excluded_sqft > 1:
            applied.append(
                f"NWI mapped wetlands conservatively excluded: {wetland_excluded_sqft / 43560:,.2f} ac"
            )

        parcel_wgs84 = parcel

        def line_crossings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            crossings = []
            for row in rows:
                geometry = row.get("map_geometry") if isinstance(row, dict) else None
                if not isinstance(geometry, dict):
                    continue
                try:
                    if shape(geometry).intersects(parcel_wgs84):
                        crossings.append(row)
                except Exception as exc:
                    log.debug("line-crossing geometry test failed: %s", exc)
                    continue
            return crossings

        electric_lines = [
            row for row in electric.get("nearest_transmission_lines") or []
            if isinstance(row, dict)
        ]
        gas_crossings = line_crossings(gas_pipelines)
        electric_crossings = line_crossings(electric_lines)
        public_utility_lines = [
            row for row in osm.get("utility_lines") or [] if isinstance(row, dict)
        ]
        utility_line_crossings = line_crossings(public_utility_lines)
        utility_crossing_count = (
            len(gas_crossings) + len(electric_crossings) + len(utility_line_crossings)
        )

        primary_road = (
            (roadway.get("engineering_details") or {}).get("primary")
            if isinstance(roadway, dict) else None
        ) or {}
        row_width_ft = primary_road.get("row_width_ft")
        road_name = primary_road.get("road_name")
        project_rows = projects.get("items") or projects.get("projects") or []
        water_lines = [
            row for row in public_utility_lines if row.get("utility_kind") == "water"
        ]
        sewer_lines = [
            row for row in public_utility_lines if row.get("utility_kind") == "wastewater"
        ]
        recorded_refs = _recorded_instrument_references(plats)
        floodway_acres = float(fema.get("floodway_intersection_acres") or 0)
        shaded_x_acres = float(fema.get("shaded_x_intersection_acres") or 0)
        gross_sqft = projected.area * _SQFT_PER_SQM
        max_impervious_acres = (
            gross_sqft / 43560 * max_impervious_pct / 100
            if max_impervious_pct is not None else None
        )

        def ledger(
            category: str,
            status: str,
            result: str,
            source: str,
        ) -> dict[str, str]:
            return {
                "category": category,
                "status": status,
                "result": result,
                "source": source,
            }

        constraints_ledger = [
            ledger(
                "FEMA 100-year floodplain (SFHA)",
                "applied" if sfha_excluded_sqft > 1 else (
                    "clear" if fema.get("has_sfha") is False else "not_mapped"
                ),
                (
                    f"{sfha_excluded_sqft / 43560:,.2f} ac excluded from the public screening envelope"
                    if sfha_excluded_sqft > 1 else
                    "No parcel-clipped SFHA geometry was returned"
                ),
                "FEMA NFHL",
            ),
            ledger(
                "Regulatory floodway",
                "applied" if floodway_acres > 0 else "clear_or_not_mapped",
                f"{floodway_acres:,.2f} ac intersects the parcel",
                "FEMA NFHL",
            ),
            ledger(
                "500-year / shaded Zone X",
                "screened_not_excluded" if shaded_x_acres > 0 else "clear_or_not_mapped",
                f"{shaded_x_acres:,.2f} ac intersects; shown as risk context, not automatically excluded",
                "FEMA NFHL",
            ),
            ledger(
                "Zoning setbacks",
                "applied" if setbacks else "unresolved",
                (
                    ", ".join(f"{label} {value:g} ft" for label, value in setbacks.items())
                    if setbacks else "No authoritative parcel setbacks were published"
                ),
                "Governing zoning code / dimensional standards",
            ),
            ledger(
                "Impervious-cover cap",
                "quantified_not_geometric" if max_impervious_pct is not None else "unresolved",
                (
                    f"{max_impervious_pct:g}% screening cap, approximately {max_impervious_acres:,.2f} ac of gross parcel"
                    if max_impervious_acres is not None else
                    "No authoritative cap was published for the resolved jurisdiction/district"
                ),
                "Governing zoning or watershed standard",
            ),
            ledger(
                "Existing / future public ROW",
                "screened_not_geometric" if row_width_ft or project_rows else "unresolved",
                (
                    f"{road_name or 'Frontage road'} inventory ROW minimum {row_width_ft:g} ft; "
                    f"{len(project_rows)} nearby public project record(s); no surveyed parcel take polygon"
                    if isinstance(row_width_ft, (int, float)) else
                    f"{len(project_rows)} nearby public project record(s); surveyed ROW take not published"
                ),
                "TxDOT Roadway Inventory / public project records",
            ),
            ledger(
                "Recorded easements",
                "reference_found_geometry_unresolved" if recorded_refs else "unresolved",
                (
                    f"Recorded reference(s): {', '.join(recorded_refs[:6])}; easement polygons are not georeferenced"
                    if recorded_refs else
                    "No georeferenced public easement polygon was resolved"
                ),
                "County clerk / recorded plat / title records",
            ),
            ledger(
                "Utility corridors crossing parcel",
                "mapped_width_unresolved" if utility_crossing_count else "no_public_crossing_mapped",
                (
                    f"{len(gas_crossings)} gas, {len(electric_crossings)} electric transmission, "
                    f"and {len(utility_line_crossings)} community-mapped utility line crossing(s); "
                    "centerlines do not establish easement width"
                ),
                "HIFLD / DOE-NETL / OpenStreetMap; verify by title, survey, provider, and 811",
            ),
            ledger(
                "Utility serviceability",
                "provider_verification_required",
                (
                    f"{len(ccn.get('service_territories') or [])} CCN/operating territory match(es); "
                    f"{len(water_lines)} mapped water and {len(sewer_lines)} mapped sewer line(s). "
                    "Capacity, main size, tie-in right, gravity/force-main routing, three-phase service, and cost are not established"
                ),
                "TWDB/PUC/TCEQ, HIFLD, community mapping, and serving utility",
            ),
            ledger(
                "Wetlands / waters",
                "applied_screening_exclusion" if wetland_excluded_sqft > 1 else (
                    "screened_clear" if wetlands.get("status") == "clear"
                    else "field_verification_required"
                ),
                (
                    f"{wetland_excluded_sqft / 43560:,.2f} ac of parcel-clipped NWI geometry excluded; field delineation still controls"
                    if wetland_excluded_sqft > 1 else (
                        "No NWI indicator returned; field delineation still controls"
                        if wetlands.get("status") == "clear" else
                        f"NWI status {wetlands.get('status') or 'unresolved'}; no parcel-clipped geometry applied"
                    )
                ),
                "USFWS NWI / field delineation / USACE",
            ),
            ledger(
                "Drainage / detention",
                "civil_design_required",
                "No verified detention footprint or downstream drainage design was available to subtract",
                "Governing drainage criteria and civil study",
            ),
        ]
        missing = [
            "georeferenced recorded easement geometry",
            "surveyed existing/future ROW take",
            "verified detention footprint",
            "field-verified wetlands / waters",
            "site-specific access geometry",
            "utility easement widths and serviceability/capacity",
        ]
        if not setbacks:
            missing.insert(0, "authoritative parcel setbacks")
        return {
            "status": "preliminary_public_screen_calculated",
            "map_geometry": screened_geojson,
            "gross_area_sqft": round(gross_sqft),
            "after_setback_area_acres": round(after_setback_area / 43560, 3),
            "screened_area_sqft": round(screened_area),
            "screened_area_acres": round(screened_area / 43560, 3),
            "sfha_excluded_acres": round(sfha_excluded_sqft / 43560, 3),
            "nwi_wetlands_excluded_acres": round(wetland_excluded_sqft / 43560, 3),
            "floodway_intersection_acres": round(floodway_acres, 3),
            "shaded_x_intersection_acres": round(shaded_x_acres, 3),
            "uniform_setback_offset_ft": offset_ft,
            "max_impervious_cover_pct": max_impervious_pct,
            "max_impervious_area_acres": (
                round(max_impervious_acres, 3)
                if max_impervious_acres is not None else None
            ),
            "utility_crossing_count": utility_crossing_count,
            "utility_serviceability_status": "provider_verification_required",
            "completeness": "public_constraints_partial",
            "applied_constraints": applied,
            "missing_constraints": missing,
            "constraints_ledger": constraints_ledger,
            "method": (
                "Preliminary development screen starts with the verified CAD parcel, "
                "applies a conservative uniform inset when published setbacks exist, "
                "and excludes parcel-clipped FEMA SFHA and NWI wetland geometry. Utility centerline "
                "crossings, serviceability, ROW, easements, impervious cover, wetlands, "
                "and detention are reported below but are not subtracted without a "
                "defensible polygon or governing dimensional rule."
            ),
        }
    except Exception as exc:
        return {
            "status": "calculation_failed",
            "map_geometry": None,
            "gross_area_sqft": None,
            "screened_area_sqft": None,
            "completeness": "not_calculable",
            "applied_constraints": [],
            "missing_constraints": ["valid projected parcel geometry"],
            "constraints_ledger": [],
            "error": str(exc)[:220],
        }


def _recorded_instrument_references(value: Any) -> list[str]:
    """Collect recorded-document identifiers without guessing their meaning."""
    found: list[str] = []
    keys = {
        "document_number",
        "instrument",
        "instrument_number",
        "plat_document_number",
        "recorded_plat_number",
    }

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in keys and child not in (None, ""):
                    text = str(child).strip()
                    if text and text not in found:
                        found.append(text)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _circle_polygon(lat: float, lng: float, radius_mi: float) -> dict[str, Any]:
    coordinates = []
    for degree in range(0, 361, 10):
        angle = math.radians(degree)
        dlat = radius_mi * math.sin(angle) / 69.0
        dlng = radius_mi * math.cos(angle) / (
            69.0 * max(0.2, math.cos(math.radians(lat)))
        )
        coordinates.append([round(lng + dlng, 6), round(lat + dlat, 6)])
    return {"type": "Polygon", "coordinates": [coordinates]}


def _market_context(
    lat: float,
    lng: float,
    results: dict[str, Any],
) -> dict[str, Any]:
    census = _facts(results, "census_acs")
    entitlement = _facts(results, "entitlement_pipeline")
    radius_m = float(entitlement.get("search_radius_m") or 800)
    permit_count = entitlement.get("city_entitlement_record_count")
    if permit_count is None:
        permit_count = len(entitlement.get("subdivision_cases") or [])
    area_sqmi = math.pi * (radius_m / 1609.344) ** 2
    density = round(float(permit_count) / area_sqmi, 2) if area_sqmi else None
    # A public routing SLA is not available for free. These catchments remain
    # transparent speed-based screens and are never called network-routed.
    catchments = []
    for minutes, radius_mi in ((5, 2.1), (10, 4.2), (15, 6.25)):
        catchments.append({
            "minutes": minutes,
            "radius_mi": radius_mi,
            "method": "25 mph radial travel-time screening estimate; not network-routed",
            "map_geometry": _circle_polygon(lat, lng, radius_mi),
        })
    return {
        "demographic_rings": census.get("rings") or {},
        "acs_year": census.get("acs_year"),
        "travel_time_screening_catchments": catchments,
        "published_permit_project_record_density_per_sqmi": density,
        "published_record_count": int(permit_count or 0),
        "published_record_radius_m": round(radius_m),
        "permit_density_scope": (
            "Nearby records published by the resolved city/county entitlement "
            "sources; not a complete inventory of all building permits."
        ),
    }


def _present(rows: list[Any]) -> str:
    return "mapped_screening_evidence" if rows else "no_mapped_feature_found"


def _coverage_ledger(
    *,
    results: dict[str, Any],
    gas_pipelines: list[dict[str, Any]],
    edwards_zones: list[dict[str, Any]],
    habitat: list[dict[str, Any]],
    osm: dict[str, list[dict[str, Any]]],
    buildable: dict[str, Any],
    market: dict[str, Any],
    source_errors: list[str],
    state: str = "TX",
) -> list[dict[str, Any]]:
    ccn = _facts(results, "twdb_ccn")
    electric = _facts(results, "electric_power_infrastructure")
    roadway = _facts(results, "txdot_roadway")
    projects = _facts(results, "txdot_projects")
    zoning = _facts(results, "zoning_standards")
    districts = _facts(results, "district_overlays")
    plats = _facts(results, "plats_intel")
    water_lines = [r for r in osm["utility_lines"] if r["utility_kind"] == "water"]
    sewer_lines = [r for r in osm["utility_lines"] if r["utility_kind"] == "wastewater"]
    lift_stations = [
        r for r in osm["utility_facilities"]
        if r["facility_kind"] == "pumping_station"
    ]
    signals = [r for r in osm["access_features"] if r["kind"] == "access_signal"]
    driveways = [r for r in osm["access_features"] if r["kind"] == "access_driveway"]
    roadways = roadway.get("roadways") or []
    district_count = sum(
        len(districts.get(key) or []) for key in (
            "mud_districts", "wcid_districts", "pid_districts",
            "tirz_districts", "esd_districts", "school_districts",
            "metro_districts",
        )
    )

    def row(
        key: str,
        label: str,
        status: str,
        result: str,
        source: str,
        verification: str,
    ) -> dict[str, str]:
        return {
            "key": key,
            "label": label,
            "status": status,
            "result": result,
            "source": source,
            "verification_action": verification,
        }

    entries = [
        row(
            "water_wastewater_authority", "Water / wastewater service authority",
            "verified_public_fact" if ccn.get("service_territories") else "no_mapped_feature_found",
            ", ".join(ccn.get("water_providers") or ccn.get("wastewater_providers") or [])
            or "No intersecting public CCN/operator territory returned",
            "TWDB / PUC / TCEQ service areas",
            "Confirm current provider and obtain will-serve/SER.",
        ),
        row(
            "water_mains", "Water mains, pipe size, and tie-in distance",
            _present(water_lines),
            f"{len(water_lines)} community-mapped line(s)" if water_lines else "No public mapped water-main geometry found",
            "OpenStreetMap public infrastructure screen",
            "Obtain provider GIS/as-built; verify size, pressure, capacity, tie-in, and extension cost.",
        ),
        row(
            "wastewater_mains", "Sanitary sewer, lift stations, and routing",
            _present([*sewer_lines, *lift_stations]),
            f"{len(sewer_lines)} line(s), {len(lift_stations)} pumping/lift feature(s)",
            "OpenStreetMap public infrastructure screen",
            "Obtain utility as-built and civil route; verify gravity/force-main, invert, capacity, and lift requirements.",
        ),
        row(
            "utility_capacity", "Water / wastewater capacity and will-serve",
            "provider_verification_required",
            "Not established by any general free public geometry source",
            "Serving utility",
            "Obtain a dated will-serve/SER and concept review response.",
        ),
        row(
            "electric_service", "Three-phase service, feeder headroom, and cost",
            "provider_verification_required",
            f"{len(electric.get('nearest_transmission_lines') or [])} transmission line(s), {len(electric.get('nearest_substations') or [])} substation(s) screened",
            "HIFLD electric infrastructure",
            "Obtain serving-utility distribution map, load study, service letter, easement, and cost estimate.",
        ),
        row(
            "gas_pipelines", "Gas transmission pipelines and easements",
            _present(gas_pipelines),
            f"{len(gas_pipelines)} dated HIFLD line(s) within {_RADIUS_MI:g} mi",
            "DOE/NETL HIFLD plus PHMSA NPMS verification",
            "Verify current pipeline/operator in NPMS and 811; obtain title/survey easement geometry.",
        ),
        row(
            "access_inventory", "Driveways, medians, signals, and access control",
            "mapped_screening_evidence" if roadways or signals or driveways else "no_mapped_feature_found",
            f"{len(roadways)} TxDOT segment(s), {len(driveways)} mapped driveway(s), {len(signals)} signal(s)",
            "TxDOT Roadway Inventory and OpenStreetMap",
            "Field-verify and obtain current road plans/access classification and agency spacing approval.",
        ),
        row(
            "future_row", "Planned widening and future ROW impact",
            "mapped_screening_evidence" if (projects.get("items") or projects.get("projects")) else "no_mapped_feature_found",
            f"{len(projects.get('items') or projects.get('projects') or [])} public project record(s)",
            "TxDOT project records and published plans",
            "Overlay surveyed proposed ROW or dedication line from current plans before closing.",
        ),
        row(
            "edwards", "Edwards Aquifer protection zones",
            _present(edwards_zones),
            f"{len(edwards_zones)} mapped zone(s) within {_RADIUS_MI:g} mi",
            "TCEQ Edwards Aquifer public map",
            "Confirm applicability and required EAPP review with TCEQ/civil/environmental counsel.",
        ),
        row(
            "critical_habitat", "Endangered-species critical habitat",
            _present(habitat),
            f"{len(habitat)} final/proposed mapped habitat feature(s)",
            "USFWS critical habitat service",
            "Run parcel-specific USFWS IPaC and species/karst field review where indicated.",
        ),
        row(
            "karst", "Karst / cave habitat",
            "authoritative_source_unavailable",
            "No complete nationwide parcel-level public karst feature service is available",
            "USFWS IPaC, state/local records, and field investigation",
            "Order karst/cave review in susceptible geology; do not infer clearance from no mapped feature.",
        ),
        row(
            "regulatory", "Setbacks, height, coverage, impervious, detention, platting",
            "verified_public_fact" if zoning.get("dimensional_standards") else "agency_verification_required",
            f"{len(zoning.get('dimensional_standards') or {})} curated dimensional rule(s); plat status {plats.get('status') or 'unresolved'}",
            "Official code citations, zoning record, county GIS, and clerk record",
            "Confirm current standards, drainage/detention criteria, and plat/replat path with governing agencies.",
        ),
        row(
            "districts", "ETJ / MUD / PID / TIRZ and special districts",
            "verified_public_fact" if district_count else "no_mapped_feature_found",
            f"{district_count} district record(s) returned",
            "Jurisdiction resolver and official district layers",
            "Confirm current boundary/tax obligations with district and title evidence.",
        ),
        row(
            "recorded_easements", "Recorded utility, drainage, access, and pipeline easements",
            "title_or_survey_required",
            "Public plat/deed discovery is recorded-reference evidence, not georeferenced easement geometry",
            "County clerk, title commitment, recorded instruments, and survey",
            "Retrieve all referenced instruments and have the surveyor plot them on the parcel.",
        ),
        row(
            "market_context", "Demographics and permit/project activity",
            "calculated_screening_result",
            f"{len(market.get('demographic_rings') or {})} ACS ring(s); {market.get('published_record_count')} nearby published record(s)",
            "U.S. Census ACS and resolved jurisdiction permit/project sources",
            "Treat radial travel-time estimates as screening; use a routed study for investment reliance.",
        ),
        row(
            "buildable_envelope", "Provisional buildable envelope",
            "calculated_screening_result" if buildable.get("map_geometry") else "insufficient_geometry",
            f"{buildable.get('screened_area_acres')} screened acre(s); completeness {buildable.get('completeness')}",
            "Verified CAD parcel plus available published constraints",
            "Replace with survey/civil envelope after easements, ROW, drainage, access, flood, and wetlands are located.",
        ),
    ]
    if source_errors:
        entries.append(row(
            "source_health", "Supplemental source health",
            "source_temporarily_unavailable",
            f"{len(source_errors)} supplemental source error(s) during refresh",
            "Public provider responses",
            "Refresh later; retained upstream facts remain available.",
        ))
    if state != "TX":
        _adjust_for_other_state(entries, results, state, driveways, signals)
    return entries


def _adjust_for_other_state(
    entries: list[dict[str, Any]],
    results: dict[str, Any],
    state: str,
    driveways: list[Any],
    signals: list[Any],
) -> None:
    """Outside Texas a row keeps its finding only when the state's own source
    for it answered (spec 2.6); otherwise it says not connected, never "no
    mapped feature" or "0 found". The Edwards Aquifer is in Texas only."""
    name = states.name_of(state)
    not_connected = f"Not connected for {name} yet"

    def answered(slot: str) -> dict[str, Any] | None:
        raw = results.get(slot) if isinstance(results, dict) else None
        facts = _facts(results, slot)
        if not facts.get("source_module"):
            return None
        if (raw or {}).get("coverage_status") not in ("covered", "partial"):
            return {"_failed": True, **facts}
        return facts

    ccn = answered("twdb_ccn")
    roads = answered("txdot_roadway")
    projects = answered("txdot_projects")
    for entry in entries:
        key = entry["key"]
        if key == "edwards":
            entry.update(
                status="not_applicable",
                result=f"The Edwards Aquifer is in Texas; it does not apply in {name}.",
                source="",
            )
        elif key == "water_wastewater_authority":
            if ccn is None:
                entry.update(status="not_connected", result=not_connected, source="")
                continue
            if ccn.get("_failed"):
                entry.update(
                    status="unavailable_this_time",
                    result=f"{ccn.get('provider_name') or 'The utility source'} did not answer this time",
                    source=str(ccn.get("provider_name") or ""),
                )
                continue
            entry["source"] = str(ccn.get("provider_name") or "")
            if not ccn.get("service_territories"):
                entry["result"] = "No service area containing the site was returned"
            sewer = (ccn.get("parts") or {}).get("sewer") or {}
            if sewer.get("coverage") in ("not_connected", "not_applicable"):
                entry["result"] += f"; sewer provider: {sewer.get('note') or not_connected.lower()}"
        elif key == "wastewater_mains" and ccn and ccn.get("sewer_mains") is not None:
            mains = ccn.get("sewer_mains") or []
            agency = str(
                (mains[0] if mains else {}).get("provider") or "the city")
            entry.update(
                status="mapped_screening_evidence" if mains else entry["status"],
                result=f"{len(mains)} sanitary main(s) mapped by {agency} near the site; {entry['result']}",
                source=f"{agency} sanitary mains; OpenStreetMap",
            )
        elif key == "access_inventory":
            if roads is None or roads.get("_failed"):
                entry.update(
                    result=(
                        f"{len(driveways)} mapped driveway(s), {len(signals)} "
                        f"signal(s); road inventory {not_connected.lower()}"
                    ),
                    source="OpenStreetMap",
                )
            else:
                short = str(roads.get("provider_short") or "state")
                entry.update(
                    result=entry["result"].replace("TxDOT", short),
                    source=f"{roads.get('provider_name') or short} and OpenStreetMap",
                )
        elif key == "future_row":
            if projects is not None and projects.get("_failed"):
                entry.update(
                    status="unavailable_this_time",
                    result=f"{projects.get('provider_name') or 'The project source'} did not answer this time",
                    source=str(projects.get("provider_name") or ""),
                )
            elif projects is None:
                entry.update(status="not_connected", result=not_connected, source="")
            else:
                entry.update(
                    result=(
                        f"{entry['result']}; no planned right-of-way layer is "
                        f"published in {name}"
                    ),
                    source=str(projects.get("provider_name") or ""),
                )


def _no_coordinates() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "coordinates_required",
            "coverage": [],
            "advisory": "Coordinates are required for site-constraints resolution.",
        },
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for site constraints"],
    )


__all__ = ["SOURCE_ID", "fetch"]
