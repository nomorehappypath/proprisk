"""Colorado's utilities card (slot twdb_ccn, spec 2.6).

Colorado has no certificate (CCN) regime, so nothing here is a legal
certificate. What is shown:
  water     EPA community water system boundaries (Denver Water's own service
            area is a second look at the same question);
  sewer     Metro Water Recovery member areas; outside them the sewer part is
            "not connected" (no statewide sewer map exists);
  districts State DOLA water and sanitation districts and metropolitan districts;
  mains     Denver's sanitary sewer mains (Denver sites only).
Every service_territories entry says whether the area CONTAINS the site
(authority "service_area", contains_site True) or is only nearby
(authority "nearest_ccn", contains_site False).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.regions import arcgis
from app.services.site_check.adapters.twdb_ccn import _clipped_arcgis_polygon
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "twdb_ccn"
PROVIDER_NAME = "EPA, Metro Water Recovery and Colorado Department of Local Affairs"
PROVIDER_SHORT = "EPA / MWR / DOLA"
VERSION = 1

EPA_WATER = (
    "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/"
    "Water_System_Boundaries/FeatureServer/0"
)
DENVER_WATER = (
    "https://services.arcgis.com/czCXlxn6hYtK4vdg/arcgis/rest/services/"
    "Denver_Water_Service_Area1/FeatureServer/1"
)
MWR_MEMBERS = (
    "https://services3.arcgis.com/DScgTtNUAFi9XJi8/arcgis/rest/services/"
    "MetroServiceArea_GPWebApp/FeatureServer/2"
)
_DOLA = "https://services3.arcgis.com/DgjqnJA1rgO92Soi/arcgis/rest/services/"
DOLA_WATER_SAN = _DOLA + "Water_and_Sanitation_Districts/FeatureServer/0"
DOLA_METRO = _DOLA + "Metropolitan_Districts/FeatureServer/0"
DENVER_SEWER_MAINS = (
    "https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/arcgis/rest/services/"
    "ODC_UTIL_SANMAIN_L/FeatureServer/353"
)
NEARBY_WATER_RADIUS_M = 3000
MAINS_RADIUS_M = 150
_DISTRICT_KINDS = {
    "6": "Metropolitan District", "10": "Sanitation District",
    "11": "Water District", "12": "Water & Sanitation District",
}
_MAIN_OWNERS = {"DPUB": "City & County of Denver", "MWRD": "Metro Water Recovery",
                "PRIV": "Private"}
_SIMPLIFY = {"maxAllowableOffset": 0.0002, "geometryPrecision": 5}
_SOURCE_URL = "https://www.epa.gov/ground-water-and-drinking-water"

_NO_CERTIFICATE_NOTE = (
    "Colorado has no certificate of convenience and necessity: a mapped area "
    "shows who is mapped as serving, not a legal right to serve. Only the "
    "provider's written will-serve letter settles service."
)


def _polygons(url: str, lat: float, lng: float, session, radius_m: int = 0):
    """Polygon features that contain the site (or lie within radius_m)."""
    params: dict[str, Any] = {
        "f": "json", "where": "1=1", "outFields": "*",
        "geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint",
        "inSR": 4326, "outSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "returnGeometry": "true", **_SIMPLIFY,
    }
    if radius_m:
        params.update(distance=radius_m, units="esriSRUnit_Meter")
    return arcgis._query(url, params, session, 20)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:  # a failing layer never stops the card
        return [], str(exc)[:200] or exc.__class__.__name__


def _attrs(feature: dict) -> dict:
    return feature.get("attributes") or {}


def _territories(features, *, service, authority, provider_of, source, contains, lat, lng):
    out = []
    for feat in features[:10]:
        rings = (feat.get("geometry") or {}).get("rings")
        geometry = _clipped_arcgis_polygon(rings, center_lat=lat, center_lng=lng) if rings else None
        if geometry is None:
            continue
        out.append({
            "service": service, "authority": authority,
            "provider": provider_of(_attrs(feat)), "source": source,
            "contains_site": contains,
            "geometry_scope": "display_clip_6mi", "geometry": geometry,
        })
    return out


def _line_geometry(geometry: dict | None) -> dict | None:
    paths = [p for p in (geometry or {}).get("paths") or [] if len(p) >= 2]
    if not paths:
        return None
    if len(paths) == 1:
        return {"type": "LineString", "coordinates": [[p[0], p[1]] for p in paths[0]]}
    return {"type": "MultiLineString",
            "coordinates": [[[p[0], p[1]] for p in path] for path in paths]}


def _sewer_mains(lat: float, lng: float, session) -> list[dict[str, Any]]:
    rows = arcgis.query_near(
        DENVER_SEWER_MAINS, lat, lng, MAINS_RADIUS_M, keep_geometry=True,
        session=session, max_records=60,
    )
    mains = []
    for row in rows:
        if str(row.get("lifecyclestatus")) == "5":  # abandoned
            continue
        owner = str(row.get("ownedby") or "").strip()
        mains.append({
            "provider": "Denver Wastewater Management",
            "diameter_in": row.get("diameter"),
            "material": row.get("material"),
            "owner": _MAIN_OWNERS.get(owner, owner or None),
            "unit_type": row.get("unittype"),
            "pipe_info": row.get("pipe_info"),
            "distance_m": round(row["_distance_m"], 1) if row.get("_distance_m") is not None else None,
            "geometry": _line_geometry(row.get("_geometry")),
        })
    return mains[:12]


def _no_coordinates() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown", "water_providers": [], "wastewater_providers": [],
            "service_territories": [], "sewer_district_candidates": [],
            "advisory": "Coordinates are required for a utility lookup.",
            "provider_name": PROVIDER_NAME, "provider_short": PROVIDER_SHORT,
            "source_url": _SOURCE_URL, "data_as_of": "unknown",
        },
        errors=["lat/lng required"],
        fetched_at=datetime.now(UTC),
    )


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates()
    lat, lng = identity.lat, identity.lng
    errors: list[str] = []

    epa, epa_err = _safe(_polygons, EPA_WATER, lat, lng, session)
    denver_water, dw_err = _safe(_polygons, DENVER_WATER, lat, lng, session)
    mwr, mwr_err = _safe(_polygons, MWR_MEMBERS, lat, lng, session)
    ws, ws_err = _safe(_polygons, DOLA_WATER_SAN, lat, lng, session)
    metro, metro_err = _safe(_polygons, DOLA_METRO, lat, lng, session)
    for label, err in (("EPA water boundaries", epa_err), ("Denver Water service area", dw_err),
                       ("Metro Water Recovery areas", mwr_err),
                       ("DOLA water and sanitation districts", ws_err),
                       ("DOLA metropolitan districts", metro_err)):
        if err:
            errors.append(f"{label}: {err}")

    water_names = []
    for feat in epa:
        name = str(_attrs(feat).get("PWS_Name") or "").strip()
        if name and name not in water_names:
            water_names.append(name)
    denver_water_confirms = bool(denver_water)
    if denver_water_confirms and not any("DENVER WATER" in n.upper() for n in water_names):
        water_names.append("Denver Water")
    nearby_water: list[dict] = []
    if not epa and not epa_err and not denver_water_confirms:
        nearby_water, _ = _safe(_polygons, EPA_WATER, lat, lng, session, NEARBY_WATER_RADIUS_M)

    sewer_names = []
    for feat in mwr:
        label = str(_attrs(feat).get("LABEL") or "").strip()
        if label:
            sewer_names.append(f"{label} (member of Metro Water Recovery)")

    districts = []
    for feat, _layer in [(f, "ws") for f in ws] + [(f, "metro") for f in metro]:
        a = _attrs(feat)
        name = str(a.get("lgname") or "").strip()
        if name and str(a.get("lgstatusid") or "1") == "1":
            districts.append((feat, {
                "name": name,
                "kind": _DISTRICT_KINDS.get(str(a.get("lgtypeid")), "Special District"),
            }))

    territories = [
        *_territories(epa, service="water", authority="service_area",
                      provider_of=lambda a: str(a.get("PWS_Name") or "Provider not named"),
                      source="EPA Community Water System Boundaries", contains=True, lat=lat, lng=lng),
        *_territories(mwr, service="wastewater", authority="service_area",
                      provider_of=lambda a: f"{a.get('LABEL')} (Metro Water Recovery member)",
                      source="Metro Water Recovery member service areas", contains=True, lat=lat, lng=lng),
        *_territories([f for f, _ in districts], service="district",
                      authority="dola_special_district",
                      provider_of=lambda a: str(a.get("lgname") or "District"),
                      source="Colorado DOLA special districts", contains=True, lat=lat, lng=lng),
        *_territories(nearby_water, service="water", authority="nearest_ccn",
                      provider_of=lambda a: str(a.get("PWS_Name") or "Provider not named"),
                      source="EPA Community Water System Boundaries (nearby, does NOT contain this site)",
                      contains=False, lat=lat, lng=lng),
    ]

    parts: dict[str, dict[str, str]] = {}
    if mwr_err:
        parts["sewer"] = {"coverage": "not_connected",
                          "note": "The Metro Water Recovery service did not answer; sewer provider unknown."}
    elif not mwr:
        parts["sewer"] = {"coverage": "not_connected",
                          "note": "No statewide sewer service map exists. This site is outside Metro Water "
                                  "Recovery's service area; ask the city or district for sewer service."}

    facts: dict[str, Any] = {
        "water_provider": water_names[0] if water_names else None,
        "wastewater_provider": sewer_names[0] if sewer_names else None,
        "water_providers": water_names,
        "wastewater_providers": sewer_names,
        "water_provider_legal_ccn": [],
        "water_provider_operating": list(water_names),
        "wastewater_nearest": None,
        "municipal_wastewater_candidate": None,
        "sewer_district_candidates": [d for _, d in districts],
        "service_territories": territories,
        "providers_disagree": False,
        "contradiction_flags": [],
        "water_confirmed_by_denver_water": denver_water_confirms,
        "nearby_water_providers": [
            str(_attrs(f).get("PWS_Name")) for f in nearby_water if _attrs(f).get("PWS_Name")],
        "parts": parts,
        "provider_name": PROVIDER_NAME,
        "provider_short": PROVIDER_SHORT,
        "source_url": _SOURCE_URL,
        "data_as_of": "2022–2026",
    }
    jurisdiction = (config or {}).get("jurisdiction") if isinstance(config, dict) else None
    if isinstance(jurisdiction, dict) and jurisdiction.get("city") and not sewer_names and \
            str(jurisdiction.get("jurisdiction_kind") or "").casefold() in {"city", "full_purpose_city"}:
        facts["municipal_wastewater_candidate"] = f"City of {str(jurisdiction['city']).title()}"

    if str(getattr(identity, "county", "") or "").casefold() == "denver":
        mains, mains_err = _safe(_sewer_mains, lat, lng, session)
        if mains_err:
            errors.append(f"Denver sewer mains: {mains_err}")
        else:
            facts["sewer_mains"] = mains

    all_failed = bool(epa_err and mwr_err and ws_err and metro_err)
    if all_failed:
        status, confidence = "unknown", DataConfidenceStatus.UNKNOWN
    elif errors:
        status, confidence = "mixed", DataConfidenceStatus.SCREENED_INDICATOR
    else:
        status = "clear" if (water_names or sewer_names) else "no_ccn_coverage"
        confidence = DataConfidenceStatus.SCREENED_INDICATOR

    advice = []
    advice.append(f"Water: {', '.join(water_names)}." if water_names else (
        "No water system boundary contains this site; the nearest is "
        f"{facts['nearby_water_providers'][0]}." if facts["nearby_water_providers"]
        else "No water system boundary was mapped here."))
    advice.append(f"Sewer: {sewer_names[0]}." if sewer_names else
                  "Sewer provider not mapped for this site (no statewide sewer map).")
    if districts:
        advice.append("Inside " + ", ".join(d["name"] for _, d in districts[:3]) + ".")
    advice.append(_NO_CERTIFICATE_NOTE)
    facts["status"] = status
    facts["advisory"] = " ".join(advice)

    letter_to = (water_names[0] if not sewer_names and water_names else
                 (sewer_names[0].split(" (")[0] if sewer_names else
                  districts[0][1]["name"] if districts else None))
    where = getattr(identity, "address", None) or f"{lat:.5f}, {lng:.5f}"
    facts["availability_request_text"] = None if not letter_to else (
        f"To {letter_to}:\n\nPlease confirm in writing whether water and sewer service "
        f"is available to the property at {where}, including the nearest main, capacity, "
        "the connection point, extension requirements, and all tap and impact fees. "
        "A written will-serve answer is requested.")

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=confidence,
        coverage_status=CoverageStatus.PARTIAL if (parts or errors) else CoverageStatus.COVERED,
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[facts["advisory"]],
        errors=errors or None,
    )
