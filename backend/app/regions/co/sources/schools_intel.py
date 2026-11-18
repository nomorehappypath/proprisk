"""Colorado's schools card (slot schools_intel, spec 2.6).

District at the point: Census TIGERweb unified school districts.
Schools nearby: Colorado Department of Education locations (CDPHE open-data copy).
Enrollment and grade range: NCES Common Core of Data through the Urban Institute
Education Data API (keyless). If that API fails, enrollment is simply left out.
Ratings: Colorado publishes a performance framework, not a score; the card
links to CDE (and each school has its own performance report link).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.regions import arcgis
from app.services.site_check.adapters._base import http_session
from app.services.site_check.adapters._resilient_http import resilient_json
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

log = logging.getLogger(__name__)

SOURCE_ID = "schools_intel"
PROVIDER_NAME = "Colorado Department of Education"
PROVIDER_SHORT = "CDE"
VERSION = 1

TIGER_DISTRICTS = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/School/MapServer/0"
)
CDE_SCHOOLS = (
    "https://www.cohealthmaps.dphe.state.co.us/arcgis/rest/services/OPEN_DATA/"
    "cdphe_cdoe_colorado_schools_districts/MapServer/0"
)
CCD_DIRECTORY = "https://educationdata.urban.org/api/v1/schools/ccd/directory/2024/"
CCD_YEAR = "2024-25"
REPORT_CARD_URL = "https://www.cde.state.co.us/accountability/performance"
_SPF_PDF = "https://cedar2.cde.state.co.us/documents/SPF2025/Official/{district}-{school}-1-Year-Official.pdf"
SEARCH_RADIUS_MI = 3.0
_M_PER_MI = 1609.344
_MAX_RETURNED = 10


def _grade_text(value: int) -> str:
    return "PK" if value == -1 else "K" if value == 0 else str(value)


def _band(low: int | None, high: int | None) -> str:
    if low is None or high is None:
        return "unknown"
    if high <= 5:
        return "elementary"
    if low >= 6 and high <= 8:
        return "middle"
    if low >= 9:
        return "high"
    return "mixed"


def _cde_grades(row: dict) -> tuple[int | None, int | None]:
    """CDE grade codes are grade x 10; codes under 10 (004, 006, 007) are
    pre-K / kindergarten style, treated as K here."""
    try:
        low, high = int(row.get("Lowest_Grade")), int(row.get("Highest_Grade"))
    except (TypeError, ValueError):
        return None, None
    return (0 if low < 10 else low // 10), (0 if high < 10 else high // 10)


def _enrollment(sess, district_codes: set[str]) -> dict[str, dict]:
    """{ 'dddd-ssss': ccd row } for the given CDE district codes; {} on failure."""
    out: dict[str, dict] = {}
    for code in sorted(district_codes):
        try:
            payload = resilient_json(
                sess, "GET", CCD_DIRECTORY,
                params={"state_leaid": f"CO-{code}", "limit": 1000},
            )
        except Exception:  # enrollment is optional; the card leaves it out
            log.warning("Urban Institute enrollment lookup failed for %s", code)
            continue
        for item in payload.get("results") or []:
            if item.get("seasch"):
                out[str(item["seasch"])] = item
    return out


def _no_coordinates() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown", "schools": [], "schools_count": 0,
            "nearest_by_band": {"elementary": None, "middle": None, "high": None},
            "report_card_lookup_url": REPORT_CARD_URL,
            "advisory": "Coordinates are required for the school lookup.",
            "provider_name": PROVIDER_NAME, "provider_short": PROVIDER_SHORT,
            "source_url": CDE_SCHOOLS, "data_as_of": "unknown",
        },
        errors=["lat/lng required"], fetched_at=datetime.now(UTC),
    )


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates()
    lat, lng = identity.lat, identity.lng
    errors: list[str] = []

    district = None
    try:
        rows = arcgis.query_point(
            TIGER_DISTRICTS, lat, lng, fields="NAME,GEOID,LOGRADE,HIGRADE",
            session=session)
        if rows:
            district = {"name": rows[0].get("NAME"), "geoid": rows[0].get("GEOID"),
                        "lowest_grade": rows[0].get("LOGRADE"),
                        "highest_grade": rows[0].get("HIGRADE")}
    except Exception as exc:
        errors.append(f"School district boundary: {str(exc)[:200]}")

    rows: list[dict] = []
    cde_failed = False
    try:
        rows = arcgis.query_near(
            CDE_SCHOOLS, lat, lng, SEARCH_RADIUS_MI * _M_PER_MI,
            where="Type_ = 'Public School Physical Address'", session=session)
    except Exception as exc:
        cde_failed = True
        errors.append(f"CDE school locations: {str(exc)[:200]}")
    if cde_failed:
        facts = {
            "status": "unknown", "schools": [], "schools_count": 0,
            "nearest_by_band": {"elementary": None, "middle": None, "high": None},
            "school_district": district, "report_card_lookup_url": REPORT_CARD_URL,
            "advisory": "The Colorado school locations service did not respond. "
                        "Look up schools at the Colorado Department of Education.",
            "contradiction_flags": [], "provider_name": PROVIDER_NAME,
            "provider_short": PROVIDER_SHORT, "source_url": CDE_SCHOOLS, "data_as_of": "unknown",
        }
        return AdapterResult(
            source_id=SOURCE_ID, source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.UNKNOWN, coverage_status=CoverageStatus.COVERED,
            normalized_facts=facts, errors=errors, fetched_at=datetime.now(UTC))

    rows.sort(key=lambda r: (r.get("_distance_m") is None, r.get("_distance_m") or 0))
    shown = rows[:_MAX_RETURNED]
    ccd = _enrollment(http_session(session), {
        str(r.get("District_Code")) for r in shown if r.get("District_Code")})

    schools: list[dict[str, Any]] = []
    for row in shown:
        key = f"{row.get('District_Code')}-{row.get('School_Code')}"
        extra = ccd.get(key) or {}
        low, high = extra.get("lowest_grade_offered"), extra.get("highest_grade_offered")
        if isinstance(low, int) and isinstance(high, int):
            grade_range = f"{_grade_text(low)}-{_grade_text(high)}"
        else:
            low, high = _cde_grades(row)
            grade_range = ""
        enrollment = extra.get("enrollment")
        schools.append({
            "name": str(row.get("School_Name") or "Unnamed").strip(),
            "district": str(row.get("District_Name") or "").strip(),
            "grade_range": grade_range,
            "band": _band(low, high),
            "address": str(row.get("Address") or "").strip(),
            "city": str(row.get("City") or "").strip(),
            "phone": str(row.get("Phone") or "").strip(),
            "enrollment": enrollment if isinstance(enrollment, int) and enrollment >= 0 else None,
            "school_number": key,
            "website": "",
            "charter": row.get("Charter_YN") == "Y",
            "distance_mi": round((row.get("_distance_m") or 0) / _M_PER_MI, 2),
            "lat": row.get("LATITUDE"), "lng": row.get("LONGITUDE"),
            "performance_report_url": _SPF_PDF.format(
                district=row.get("District_Code"), school=row.get("School_Code")),
        })

    by_band: dict[str, dict | None] = {"elementary": None, "middle": None, "high": None}
    for s in schools:
        if s["band"] in by_band and by_band[s["band"]] is None:
            by_band[s["band"]] = s
        elif s["band"] == "mixed":
            for k, v in by_band.items():
                if v is None:
                    by_band[k] = s
    district_name = (district or {}).get("name")
    advisory = (
        f"{len(rows)} public schools within {SEARCH_RADIUS_MI:g} mi"
        + (f"; the site is in {district_name}" if district_name else "")
        + ". Distance is not school assignment: districts assign by attendance zone "
          "or school choice, so confirm with the district. Colorado publishes school "
          "performance plans, not scores; see the Colorado Department of Education."
        if rows else
        f"No public schools within {SEARCH_RADIUS_MI:g} mi of the site in the Colorado "
        "Department of Education list."
    )
    facts = {
        "status": "clear" if rows else "no_schools_in_buffer",
        "search_radius_mi": SEARCH_RADIUS_MI,
        "schools_count": len(rows),
        "schools": schools,
        "nearest_by_band": by_band,
        "school_district": district,
        "report_card_lookup_url": REPORT_CARD_URL,
        "enrollment_year": CCD_YEAR if ccd else None,
        "advisory": advisory,
        "contradiction_flags": [],
        "provider_name": PROVIDER_NAME,
        "provider_short": PROVIDER_SHORT,
        "source_url": CDE_SCHOOLS,
        "data_as_of": "2026",
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        errors=errors or None,
    )
