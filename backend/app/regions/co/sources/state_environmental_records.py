"""Colorado's state environmental records (slot state_environmental_records, spec 2.6).

This is the state part of environmental_screening (EPA ECHO stays national and
is not here). It returns the same keys the Texas (TCEQ) part adds, so the
environmental card reads them unchanged:
  pst_records, tceq_screen, tceq_address_registry, registry_street_queried,
  manual_records_links, search_radius_m, broad_search_radius_m,
  source_statuses, source_cooldowns, unavailable_sources, advisory.
The key names keep their Texas spelling ("tceq_*") because the card reads them;
every value is Colorado data.

pst_records holds only OPEN tank releases (OPS Regulated Storage Tank
Facilities with a release whose status is not Closed). Closed releases are
counted in closed_release_count, never shown as "none". RCRA active handlers
and PFAS lists are not contamination records and are not used.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.regions import arcgis
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "state_environmental_records"
PROVIDER_NAME = "Colorado Division of Oil and Public Safety and CDPHE"
PROVIDER_SHORT = "OPS / CDPHE"
VERSION = 1

OPS_TANKS = (
    "https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/arcgis/rest/services/"
    "Regulated_Storage_Tank_Facilities_in_Colorado/FeatureServer/0"
)
_CDPHE = "https://services3.arcgis.com/66aUo8zsujfVXRIT/arcgis/rest/services/"
SUPERFUND = _CDPHE + "CDPHE_Superfund/FeatureServer/0"
RCRA_CA = _CDPHE + "RCRA_corrective_action/FeatureServer/0"
INST_CONTROLS = _CDPHE + "Institutional_Controls_PROD_local/FeatureServer/0"
SOLID_WASTE = _CDPHE + "Solid_Waste_Facilities/FeatureServer/0"
CLOSED_LANDFILLS = _CDPHE + "ClosedLandfillsColorado/FeatureServer/0"
BROWNFIELD = _CDPHE + "CDPHE_Brownfield/FeatureServer/0"

TANK_RADIUS_M = 500
BROAD_RADIUS_M = 1609
_SOURCE_URL = "https://cdphe.colorado.gov/environmental-records-search"

_CATEGORIES: tuple[dict[str, Any], ...] = (
    {"category": "superfund", "label": "Superfund / National Priorities List site",
     "layers": ((SUPERFUND, "super"),)},
    {"category": "corrective_action", "label": "Hazardous-waste corrective-action cleanup (RCRA)",
     "layers": ((RCRA_CA, "rcra"),)},
    {"category": "brownfield", "label": "Brownfield site", "layers": ((BROWNFIELD, "brownfield"),)},
    {"category": "landfill", "label": "Solid-waste facility or closed landfill",
     "layers": ((SOLID_WASTE, "solid_waste"), (CLOSED_LANDFILLS, "closed_landfill"))},
    {"category": "groundwater_msd", "label": "Land-use restriction (institutional control)",
     "layers": ((INST_CONTROLS, "inst"),)},
)

_MANUAL_LINKS = (
    {"label": "CDPHE Environmental Records Search",
     "url": "https://cdphe.colorado.gov/environmental-records-search"},
    {"label": "Colorado storage tank release records (OPS)",
     "url": "https://oilandpublicsafety.colorado.gov/"},
    {"label": "EPA Cleanups in My Community map",
     "url": "https://www.epa.gov/cleanups/cleanups-my-community"},
)


def _first(attrs: dict, *keys: str) -> str:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, "", " ", "NULL"):
            return str(value).strip()
    return ""


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "_geometry"}


def _site_record(kind: str, row: dict) -> dict[str, Any]:
    """The card's record shape: facility_name, address, city, county, tceq_rn
    (the facility's state or EPA id), program (status), detail_url, raw."""
    if kind == "super":
        name, address, city = _first(row, "NAME", "Site"), "", _first(row, "CITY")
        rid, program = _first(row, "ID"), _first(row, "SiteStatus")
        url = _first(row, "EPA_Links", "CDPHE_LINK")
    elif kind == "rcra":
        name, address, city = _first(row, "facility_names"), _first(row, "address"), ""
        rid, program, url = _first(row, "Handler_ID"), _first(row, "status"), ""
    elif kind == "brownfield":
        name, address, city = _first(row, "Site"), _first(row, "Address"), _first(row, "City")
        rid, program, url = _first(row, "Prcl_ID"), _first(row, "SitStts"), ""
    elif kind == "solid_waste":
        name, address, city = _first(row, "Facility"), _first(row, "Address"), _first(row, "City")
        rid = _first(row, "LocationID")
        program = " / ".join(x for x in (_first(row, "Type"), _first(row, "Status")) if x)
        url = ""
    elif kind == "closed_landfill":
        name, address, city = _first(row, "Facility_Name"), "", _first(row, "City")
        rid, url = "", ""
        suspect = _first(row, "Known_Suspect_Release")
        program = "Closed landfill" + (f"; known or suspected release: {suspect}" if suspect else "")
    else:  # institutional controls
        name, address, city = _first(row, "Site"), _first(row, "Desc_"), ""
        rid, program, url = _first(row, "PGM_ID"), _first(row, "IC_Type"), ""
    return {
        "facility_name": name or "Unknown site", "address": address, "city": city,
        "county": _first(row, "County", "COUNTY"), "tceq_rn": rid, "program": program,
        "detail_url": url, "distance_m": _round(row.get("_distance_m")), "raw": _clean(row),
    }


def _round(value: Any) -> float | None:
    return round(value, 1) if isinstance(value, (int, float)) else None


def _is_open(status: str) -> bool:
    return bool(status) and not status.strip().lower().startswith("closed")


def _release_record(row: dict) -> dict[str, Any]:
    return {
        "facility_name": _first(row, "Facility_Name", "Site_Name") or "Unknown facility",
        "address": _first(row, "Address"), "city": _first(row, "City"),
        "county": _first(row, "County"), "tceq_rn": "",
        "pst_id": str(row.get("Facility_ID") or ""),
        "lpst_status": _first(row, "Status"),
        "release_number": _first(row, "Release_Number"),
        "release_type": _first(row, "Release_Record_Type"),
        "release_date": _first(row, "Release_Date"),
        "closure_date": _first(row, "Closure_Date"),
        "release_record_url": _first(row, "Release_Record_Link"),
        "active_tanks": row.get("Number_of_Active_Tanks"),
        "distance_m": _round(row.get("_distance_m")), "raw": _clean(row),
    }


def _status(error: str | None) -> str:
    if not error:
        return "queried"
    low = error.lower()
    return ("temporary_rate_limited" if "429" in low or "rate limit" in low
            else "temporary_timeout" if "timeout" in low or "timed out" in low
            else "temporary_unavailable")


def fetch(identity, session=None, config=None) -> AdapterResult:
    cfg = config if isinstance(config, dict) else {}
    tank_radius = int(cfg.get("search_radius_m") or TANK_RADIUS_M)
    broad_radius = int(cfg.get("broad_search_radius_m") or BROAD_RADIUS_M)
    if identity.lat is None or identity.lng is None:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            normalized_facts={"status": "unknown", "advisory": "Coordinates are required.",
                              "provider_name": PROVIDER_NAME, "provider_short": PROVIDER_SHORT,
                              "source_url": _SOURCE_URL, "data_as_of": "unknown"},
            errors=["lat/lng required"], fetched_at=datetime.now(UTC),
        )
    lat, lng = identity.lat, identity.lng
    errors: list[str] = []

    pst_records: list[dict] = []
    tank_facilities = closed_releases = 0
    pst_error = None
    try:
        rows = arcgis.query_near(OPS_TANKS, lat, lng, tank_radius, session=session)
        tank_facilities = len({r.get("Facility_ID") for r in rows})
        for row in rows:
            if not _first(row, "Release_Number"):
                continue
            if _is_open(_first(row, "Status")):
                pst_records.append(_release_record(row))
            else:
                closed_releases += 1
    except Exception as exc:
        pst_error = str(exc)[:200] or exc.__class__.__name__
        errors.append(f"Colorado storage tanks: {pst_error}")

    screen: dict[str, dict[str, Any]] = {}
    statuses = {"tceq_pst": _status(pst_error)}
    for spec in _CATEGORIES:
        records: list[dict] = []
        failures: list[str] = []
        for url, kind in spec["layers"]:
            try:
                rows = arcgis.query_near(url, lat, lng, broad_radius, session=session)
                records.extend(_site_record(kind, r) for r in rows)
            except Exception as exc:
                failures.append(str(exc)[:200] or exc.__class__.__name__)
        records.sort(key=lambda r: (r["distance_m"] is None, r["distance_m"] or 0))
        error = "; ".join(failures) or None
        screen[spec["category"]] = {
            "label": spec["label"], "count": len(records), "records": records, "error": error}
        statuses[f"tceq_{spec['category']}"] = _status(error)
        if error:
            errors.append(f"{spec['label']}: {error}")

    cat_hits = sum(c["count"] for c in screen.values())
    any_failed = bool(errors)
    all_failed = bool(pst_error) and all(c["error"] for c in screen.values())
    unavailable = (["Colorado storage tanks"] if pst_error else []) + [
        c["label"] for c in screen.values() if c["error"]]

    if pst_error:
        tanks = "Storage tank records could not be checked; do not treat that as a clean result."
    elif pst_records:
        tanks = (f"{len(pst_records)} open storage-tank release(s) within {tank_radius} m "
                 f"(Colorado Division of Oil and Public Safety).")
    else:
        tanks = (f"No open storage-tank releases within {tank_radius} m"
                 + (f"; {closed_releases} closed release record(s) are on file." if closed_releases
                    else "."))
    advisory = " ".join([
        tanks,
        f"{cat_hits} state cleanup, waste or land-use record(s) within {broad_radius} m "
        "(CDPHE)." if cat_hits else f"No CDPHE cleanup records within {broad_radius} m.",
        "State enforcement history and dry-cleaner records are not connected for Colorado.",
        "This is a limited screen, not a Phase I environmental report.",
    ])

    facts: dict[str, Any] = {
        "status": ("unknown" if all_failed else
                   "mixed" if (pst_records or cat_hits) else "partial" if any_failed else "clear"),
        "pst_records": pst_records,
        "closed_release_count": closed_releases,
        "tank_facility_count": tank_facilities,
        "tceq_screen": screen,
        "tceq_address_registry": {},
        "registry_street_queried": None,
        "manual_records_links": [dict(x) for x in _MANUAL_LINKS],
        "search_radius_m": tank_radius,
        "broad_search_radius_m": broad_radius,
        "source_statuses": statuses,
        "source_cooldowns": {},
        "unavailable_sources": unavailable,
        "parts": {
            "enforcement": {"coverage": "not_connected",
                            "note": "No Colorado enforcement-history source is connected."},
            "dry_cleaner": {"coverage": "not_connected",
                            "note": "Colorado has no dry-cleaner remediation list; none is connected."},
        },
        "advisory": advisory,
        "contradiction_flags": [],
        "provider_name": PROVIDER_NAME,
        "provider_short": PROVIDER_SHORT,
        "source_url": _SOURCE_URL,
        "data_as_of": "2026",
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=(SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE if any_failed
                              else SourceAccessStatus.PROGRAMMATIC_PUBLIC),
        data_confidence_status=(DataConfidenceStatus.UNKNOWN if all_failed
                                else DataConfidenceStatus.SCREENED_INDICATOR),
        coverage_status=CoverageStatus.PARTIAL,
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors or None,
    )
