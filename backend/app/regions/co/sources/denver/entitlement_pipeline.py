"""Denver development activity for the entitlement card (spec 2.6).

Rezonings (zone map amendments), site development plans and new-building,
addition and demolition permits within 800 m of the site, all from Denver
Open Data. Rezonings have no street address; their status "Pending" can be
years old, and the status text says so.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from app.regions.arcgis import query_near
from app.regions.co.sources.denver import _planning as p
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "entitlement_pipeline"
PROVIDER_NAME = p.PROVIDER_NAME
PROVIDER_SHORT = p.PROVIDER_SHORT
VERSION = 1
REZONINGS = p.layer("ODC_ZONE_MAPAMENDMENTS_A", 356)
SITE_PLANS = p.layer("ODC_PLAN_SITEDEVPLANS_A", 105)
PERMITS = {
    "Residential": p.layer("ODC_DEV_RESIDENTIALCONSTPERMIT_P", 316),
    "Commercial": p.layer("ODC_DEV_COMMERCIALCONSTPERMIT_P", 317),
    "Demolition": p.layer("ODC_DEV_DEMOLITIONPERMIT_P", 318),
}
PORTAL_URL = (
    "https://denvergov.org/Government/Agencies-Departments-Offices/"
    "Agencies-Departments-Offices-Directory/Community-Planning-and-Development/"
    "Denver-Zoning-Code/Map-Amendments"
)
RADIUS_M = 800
_RECENT_YEARS = 10      # passed rezonings and recorded plans older than this are history
_PERMIT_YEARS = 3
_MAX_LISTED = 25
_PERMIT_WHERE = {
    "Residential": "UPPER(CLASS) LIKE '%NEW BUILDING%' OR UPPER(CLASS) LIKE '%ADDITION%'",
    "Commercial": "UPPER(CLASS) LIKE '%NEW BUILDING%' OR UPPER(CLASS) LIKE '%ADDITION%'",
    "Demolition": "UPPER(CLASS) LIKE '%WRECK%'",
}
_STALE = "the city's status may be out of date"


def _today() -> datetime:
    return datetime.now(UTC)


def _iso(ms) -> str:
    return p.ms_date(ms) or ""


def _permit_class(value) -> str:
    """Denver writes the class in several cases and with number prefixes."""
    cleaned = re.sub(r"^\s*\d+\s*-\s*", "", p.text(value)).strip()
    return {"Wreck": "Wreck / demolition"}.get(cleaned.title(), cleaned.title())


def _rezonings(rows, cutoff_ms) -> list[dict]:
    cases: dict[str, dict] = {}
    for r in rows:
        key = p.text(r.get("APP_NUM")) or p.text(r.get("ZMA_ID")) or f"object-{r.get('OBJECTID')}"
        case = cases.setdefault(key, {"rows": [], "distance": None})
        case["rows"].append(r)
        d = r.get("_distance_m")
        if d is not None and (case["distance"] is None or d < case["distance"]):
            case["distance"] = d
    out = []
    for key, case in cases.items():
        r0 = case["rows"][0]
        pending = any(p.text(x.get("STATUS")).lower() == "pending" for x in case["rows"])
        passed = r0.get("PASSED_DATE")
        applied = _iso(r0.get("APP_DATE"))
        if not pending and (passed or r0.get("APP_DATE") or 0) < cutoff_ms:
            continue
        to_zones = sorted({p.text(x.get("ZONE_DISTRICT")) for x in case["rows"] if p.text(x.get("ZONE_DISTRICT"))})
        from_zones = sorted({re.sub(r"^From\s+", "", p.text(x.get("FROM_ZONE_DESC"))) for x in case["rows"]
                             if p.text(x.get("FROM_ZONE_DESC"))})
        if pending:
            status = f"Pending (applied {applied or 'date not published'}; {_STALE})"
        else:
            status = f"Passed {_iso(passed)}" + (f" (Ord. {r0['ORD_NUM']}/{r0['ORD_YEAR']})" if r0.get("ORD_NUM") else "")
        out.append({
            "case_number": key,
            "case_name": f"Rezoning {', '.join(from_zones) or '?'} to {', '.join(to_zones) or '?'}",
            "case_type": "Zone map amendment (rezoning)",
            "address": (f"{p.text(r0.get('NBHD'))} neighborhood; no street address published"
                        if p.text(r0.get("NBHD")) else "No street address published"),
            "status": status,
            "status_raw": "Pending" if pending else "Passed",
            "filed_date": applied,
            "passed_date": _iso(passed),
            "acres": r0.get("ACRES"),
            "distance_m": round(case["distance"]) if case["distance"] is not None else None,
            "_sort": (0 if pending else 1, -(passed or r0.get("APP_DATE") or 0)),
        })
    out.sort(key=lambda c: c["_sort"])
    for c in out:
        c.pop("_sort")
    return out


def _site_plans(rows, cutoff_ms) -> list[dict]:
    out = []
    for r in rows:
        under_review = p.text(r.get("STATUS")).lower() == "under review"
        when = r.get("STATUS_DATE") if r.get("STATUS_DATE") else r.get("RECEPTION_DATE")
        if not under_review and (when or 0) < cutoff_ms:
            continue
        number = p.text(r.get("RECORD_ID")) or p.text(r.get("LOG_NUM")) or p.text(r.get("RECEPTION_NUM"))
        status = p.text(r.get("STATUS")) or "Status not published"
        if _iso(when):
            status += f" {_iso(when)}"
        out.append({
            "case_number": number,
            "case_name": p.text(r.get("PLAN_NAME")),
            "case_type": p.text(r.get("RECORD_TYPE")) or p.text(r.get("PROP_BLDG_FORM_1")) or "Site development plan",
            "address": p.text(r.get("ADDRESS_LINE1")),
            "status": status,
            "filed_date": _iso(when),
            "dwelling_units": r.get("PROP_DWELLING_UNITS"),
            "stories": r.get("PROP_STORIES"),
            "distance_m": round(r["_distance_m"]) if r.get("_distance_m") is not None else None,
            "_sort": (0 if under_review else 1, -(when or 0)),
        })
    out.sort(key=lambda c: c["_sort"])
    for c in out:
        c.pop("_sort")
    return out


def _permit_records(kind: str, rows) -> list[dict]:
    out = []
    for r in rows:
        klass = _permit_class(r.get("CLASS"))
        if r.get("CANCEL"):
            status = "Cancelled"
        elif r.get("DATE_CO_ISSUED"):
            status = "Certificate of occupancy issued"
        elif r.get("FINAL_DATE"):
            status = "Finaled"
        else:
            status = "Issued"
        issued = r.get("DATE_ISSUED")
        value = r.get("VALUATION")
        signal = f"{klass} permit"
        if p.text(r.get("UNITS")):
            signal += f", {p.text(r['UNITS'])} unit(s)"
        if isinstance(value, (int, float)) and value > 0:
            signal += f", ${value:,.0f} valuation"
        out.append({
            "project_number": p.text(r.get("PERMIT_NUM")),
            "project_name": p.text(r.get("ADDRESS")),
            "project_status": status,
            "project_year": int(_iso(issued)[:4]) if _iso(issued) else None,
            "primary_signal": signal,
            "address": p.text(r.get("ADDRESS")),
            "zoning": None,
            "permit_type": klass,
            "land_use": None,
            "permit_layer": kind,
            "date_issued": _iso(issued),
            "valuation": value,
            "distance_m": round(r["_distance_m"]) if r.get("_distance_m") is not None else None,
            "_issued": issued or 0,
        })
    return out


def fetch(identity, session=None, config=None) -> AdapterResult:
    base = p.source_facts(PORTAL_URL, "unknown")
    lat, lng = identity.query_point()
    if identity.lat is not None and identity.lng is not None:
        lat, lng = identity.lat, identity.lng  # development nearby is measured from the site
    if lat is None or lng is None:
        return p.no_coordinates(SOURCE_ID, base)
    now = _today()
    cutoff_ms = (now - timedelta(days=365 * _RECENT_YEARS)).timestamp() * 1000
    permit_from = (now - timedelta(days=365 * _PERMIT_YEARS)).strftime("%Y-%m-%d")
    errors: list[str] = []
    statuses: dict[str, str] = {}

    def run(name, url, fields, where="1=1"):
        try:
            rows = query_near(url, lat, lng, RADIUS_M, fields=fields, where=where, session=session)
            statuses[name] = "queried"
            return rows
        except Exception as exc:
            statuses[name] = "query_failed"
            errors.append(f"Denver {name} unavailable: {str(exc)[:160]}")
            return None

    zma_rows = run("rezonings", REZONINGS,
                   "APP_NUM,ZMA_ID,APP_DATE,STATUS,FROM_ZONE_DESC,ZONE_DISTRICT,NBHD,PASSED_DATE,"
                   "ORD_NUM,ORD_YEAR,ACRES")
    sdp_rows = run("site development plans", SITE_PLANS,
                   "RECORD_ID,RECORD_TYPE,STATUS,STATUS_DATE,PLAN_NAME,LOG_NUM,RECEPTION_NUM,"
                   "RECEPTION_DATE,ADDRESS_LINE1,PROP_DWELLING_UNITS,PROP_STORIES,PROP_BLDG_FORM_1")
    permits: list[dict] = []
    for kind, url in PERMITS.items():
        rows = run(f"{kind.lower()} permits", url,
                   "PERMIT_NUM,DATE_ISSUED,ADDRESS,CLASS,UNITS,VALUATION,FINAL_DATE,CANCEL,DATE_CO_ISSUED",
                   f"({_PERMIT_WHERE[kind]}) AND DATE_ISSUED >= timestamp '{permit_from} 00:00:00'")
        if rows is not None:
            permits += _permit_records(kind, rows)
    if len(errors) == 5:
        return p.failed(SOURCE_ID, base, "Denver development layers", RuntimeError("; ".join(errors)))

    zoning_cases = _rezonings(zma_rows, cutoff_ms) if zma_rows is not None else []
    site_plans = _site_plans(sdp_rows, cutoff_ms) if sdp_rows is not None else []
    permits.sort(key=lambda r: -r["_issued"])
    for r in permits:
        r.pop("_issued")
    listed = permits[:_MAX_LISTED]
    pending = [c for c in zoning_cases if c["status_raw"] == "Pending"]
    as_of_dates = [d for d in (p.data_as_of(u, session) for u in (REZONINGS, SITE_PLANS, *PERMITS.values()))]
    known = [d for d in as_of_dates if d != "unknown"]
    as_of = min(known) if known and len(known) == len(as_of_dates) else "unknown"

    headline = (f"{len(zoning_cases)} rezoning(s), {len(site_plans)} site plan(s) and "
                f"{len(permits)} new-building, addition or demolition permit(s) within {RADIUS_M} m")
    facts = {
        **p.source_facts(PORTAL_URL, as_of),
        "status": "queried" if not errors else "partial",
        "source_status": "queried" if not errors else "query_failed",
        "source_statuses": statuses,
        "county": "denver", "jurisdiction": "denver", "jurisdiction_kind": "city",
        "search_radius_m": RADIUS_M,
        "zoning_cases": zoning_cases[:_MAX_LISTED],
        "zoning_case_count": len(zoning_cases),
        "pending_rezoning_count": len(pending),
        "site_plan_cases": site_plans[:_MAX_LISTED],
        "site_plan_case_count": len(site_plans),
        "city_entitlement_records": listed,
        "city_entitlement_record_count": len(permits),
        "city_entitlement_source": "Denver building permits (new building, addition, wreck), last "
                                   f"{_PERMIT_YEARS} years",
        "city_entitlement_source_url": PORTAL_URL,
        "city_entitlement_radius_mi": round(RADIUS_M / 1609.344, 2),
        "case_portal_url": PORTAL_URL,
        "case_portal_name": "Denver rezonings (map amendments)",
        "entitlement_read": {
            "headline": headline,
            "broker_read": headline + ". Passed rezonings and recorded plans cover the last "
                           f"{_RECENT_YEARS} years.",
            "why_it_matters": "Rezonings, site plans and new construction nearby show where Denver "
                              "expects change around this site.",
            "next_step": "Rezonings filed in the last months are on Denver's rezoning pages, not in "
                         "this data; check them before relying on this list.",
        },
        "parts": {
            "subdivision_plats": {"coverage": "not_connected",
                                  "note": "Denver publishes no recorded-subdivision case layer with lot counts here."},
            "current_rezoning_applications": {
                "coverage": "not_connected",
                "note": "Current rezoning applications are published only as web pages. The city's "
                        "map layer lists them as Pending, and that status can be years old."},
        },
        "advisory": headline + (f". {len(pending)} rezoning(s) marked Pending: {_STALE}." if pending else "."),
        "contradiction_flags": [],
    }
    if errors:
        facts["part_errors"] = errors
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED if not errors else CoverageStatus.PARTIAL,
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=errors or None,
    )
