"""Denver's landmark districts and landmark structures for the historic card (spec 2.6)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.regions.arcgis import haversine_m, query_near
from app.regions.co.sources.denver import _planning as p
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "historic_intel"
PROVIDER_NAME = p.PROVIDER_NAME
PROVIDER_SHORT = p.PROVIDER_SHORT
VERSION = 1
DISTRICTS = p.layer("ODC_HIST_LANDMARKDISTRICT_A", 68)
STRUCTURES = p.layer("ODC_HIST_LANDMARKSTRUCTURE_P", 69)
PAGE_URL = (
    "https://denvergov.org/Government/Agencies-Departments-Offices/"
    "Agencies-Departments-Offices-Directory/Community-Planning-and-Development/"
    "Landmark-Preservation/Historic-Landmarks-and-Districts"
)
_DISTRICT_RADIUS_M = 200
_LANDMARK_RADIUS_M = 402  # a quarter mile, as the card says


def fetch(identity, session=None, config=None) -> AdapterResult:
    base = p.source_facts(PAGE_URL, "unknown")
    lat, lng = identity.query_point()
    if lat is None or lng is None:
        return p.no_coordinates(SOURCE_ID, base)
    try:
        # Radius queries only count when outSR=4326 is sent (the helper does).
        d_rows = query_near(DISTRICTS, lat, lng, _DISTRICT_RADIUS_M, session=session)
        s_rows = query_near(STRUCTURES, lat, lng, _LANDMARK_RADIUS_M, session=session, max_records=500)
        dates = [d for d in (p.data_as_of(DISTRICTS, session), p.data_as_of(STRUCTURES, session))
                 if d != "unknown"]
        as_of = min(dates) if len(dates) == 2 else "unknown"  # the older of the two layers
    except Exception as exc:
        return p.failed(SOURCE_ID, base, "Denver landmark layers", exc)

    inside = [r for r in d_rows if r.get("_distance_m") == 0.0]
    nearby = [r for r in d_rows if r.get("_distance_m") != 0.0]
    districts = []
    for r in inside:
        year = p.ms_date(r.get("ORD_DATE"))
        districts.append({
            "name": p.text(r.get("DIST_NAME")),
            "case_number": p.text(r.get("DIST_NUM")),
            "ordinance_number": (f"{r['ORD_NUM']} ({year[:4]})" if r.get("ORD_NUM") and year
                                 else p.text(r.get("ORD_NUM"))),
            "ordinance_url": "",
            "design_standards_url": "",
            "period_of_significance": p.text(r.get("PERIOD_SIGNIFICANT")),
        })
    landmarks = []
    for r in s_rows:
        geom = r.get("_geometry") or {}
        dist_m = r.get("_distance_m")
        if dist_m is None and geom.get("x") is not None:
            dist_m = haversine_m(lat, lng, geom["y"], geom["x"])
        landmarks.append({
            "name": p.text(r.get("LDMK_NAME")) or "Unnamed landmark",
            "address": p.text(r.get("ADDRESS_LINE1")),
            "case_number": f"Ord. {r['ORD_NUM']}/{r['ORD_YEAR']}" if r.get("ORD_NUM") else "",
            "landmark_id": r.get("LDMK_NUM"),
            "historic_district": p.text(r.get("HISTORIC_DIST")),
            "year_built": p.text(r.get("YEAR_BUILT")),
            "distance_mi": round(dist_m / 1609.344, 2) if dist_m is not None else None,
        })
    landmarks.sort(key=lambda x: (x["distance_mi"] is None, x["distance_mi"] or 0))

    if districts:
        status = "in_historic_district"
    elif landmarks:
        status = "near_landmark"
    else:
        status = "clear"
    names = ", ".join(d["name"] for d in districts)
    facts = {
        **p.source_facts(PAGE_URL, as_of),
        "status": status,
        "is_in_local_historic_district": bool(districts),
        "districts": districts,
        "nearby_districts": [p.text(r.get("DIST_NAME")) for r in nearby],
        "nearby_landmarks_count": len(landmarks),
        "nearby_landmarks": landmarks[:5],
        "history_colorado_url": "https://www.historycolorado.org/compass",
        "state_atlas_url": "https://www.historycolorado.org/compass",
        "state_atlas_label": "History Colorado Compass (state historic records)",
        "contradiction_flags": [],
        "advisory": (
            f"The site is inside Denver's landmark district: {names}. Exterior changes go through "
            "Landmark Preservation review." if districts else
            f"{len(landmarks)} Denver landmark(s) within a quarter mile; the site is not inside a "
            "landmark district." if landmarks else
            "No Denver landmark district or landmark is mapped at this site. National Register "
            "listings that are not Denver designations are not in this layer."
        ),
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.VERIFIED_PUBLIC_FACT,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
    )
