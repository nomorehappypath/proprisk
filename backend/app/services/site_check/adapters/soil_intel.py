"""soil_intel adapter — NRCS SSURGO soil survey.

Ported verbatim from legacy_services/core/public_intel_adapters/
soil_intel.py (May 2026). Identity / contract import paths
adjusted; SOURCE_ID matches registry; coverage_status field
added.

Single SQL query against the USDA NRCS Soil Data Access
(SDA) Tabular service joins mapunit + component, filtered
to comppct_r >= 5 (drops noise-level minor components).
Returns map units intersecting the parcel point with their
hydric rating, drainage class, slope range, runoff potential,
and prime-farmland status.

Confidence is always SCREENED_INDICATOR — SSURGO is broad-
acre mapping (typical map-unit polygon is hundreds of acres).
Geotech / wetland delineation / OSSF evaluation is the
binding answer.
"""

from __future__ import annotations

import logging
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
    ManualUploadRequest,
    SourceAccessStatus,
)

log = logging.getLogger("propdesk.site_check.soil_intel")

SOURCE_ID = "soil_intel"

_NRCS_SDA_ENDPOINT = (
    "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"
)

_SQL_TEMPLATE = """
SELECT m.mukey, m.muname, m.musym, m.farmlndcl,
       c.compname, c.comppct_r, c.hydricrating, c.drainagecl,
       c.slope_l, c.slope_h, c.runoff
FROM mapunit AS m
INNER JOIN component AS c ON c.mukey = m.mukey
WHERE m.mukey IN (
    SELECT mukey
    FROM SDA_Get_Mukey_from_intersection_with_WktWgs84(
        'point({lng} {lat})'
    )
)
  AND c.comppct_r >= 5
ORDER BY m.mukey, c.comppct_r DESC
"""

_TIMEOUT = 30

_SOIL_UPLOAD_TYPES = [
    "geotechnical investigation",
    "wetland delineation",
    "septic site evaluation (OSSF)",
    "civil drainage study",
]

_WEB_SOIL_SURVEY_URL = "https://websoilsurvey.sc.egov.usda.gov/"


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _no_coordinates_result()

    sess = http_session(session)

    rows, error = _query_sda(sess, identity.lat, identity.lng)
    if error:
        return _fetch_failed_result(error)

    map_units = _summarize(rows)

    any_hydric = any(
        c.get("hydric_rating", "").upper() in ("YES", "PARTIALLY")
        for mu in map_units
        for c in mu.get("components", [])
    )
    any_prime_farmland = any(
        "prime farmland"
        in (mu.get("farmland_class") or "").lower()
        for mu in map_units
    )
    max_slope = max(
        (
            (c.get("slope_high") or 0)
            for mu in map_units
            for c in mu.get("components", [])
        ),
        default=None,
    )

    advisory = _build_advisory(
        map_units, any_hydric, any_prime_farmland, max_slope,
    )

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.SCREENED_INDICATOR,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": (
                "clear" if map_units else "no_soils_at_point"
            ),
            "map_units_count": len(map_units),
            "map_units": map_units,
            "any_hydric_component": any_hydric,
            "any_prime_farmland": any_prime_farmland,
            "max_component_slope_pct": max_slope,
            "web_soil_survey_url": _WEB_SOIL_SURVEY_URL,
            "advisory": advisory,
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[advisory],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_SOIL_UPLOAD_TYPES),
            reason=(
                "SSURGO is broad-acre soil mapping. A site-"
                "specific geotech investigation is required "
                "for any foundation or grading design. For "
                "parcels considering OSSF, an on-site sewage "
                "facility evaluation is required by the "
                "county OSSF office. Hydric components "
                "trigger a wetland delineation."
            ),
        ),
    )


def _query_sda(
    session: requests.Session, lat: float, lng: float,
) -> tuple[list[list[Any]], str | None]:
    sql = _SQL_TEMPLATE.format(lat=lat, lng=lng)
    try:
        resp = session.post(
            _NRCS_SDA_ENDPOINT,
            json={"format": "JSON+COLUMNNAME", "query": sql},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return [], (
                f"non-JSON content-type ({ct[:40]}) — "
                "NRCS SDA may be down or query rejected"
            )
        data = resp.json()
        table = data.get("Table") or []
        if len(table) <= 1:
            return [], None
        return table[1:], None
    except Exception as exc:
        log.warning("NRCS SDA query failed: %s", exc)
        return [], str(exc)[:200]


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _summarize(
    rows: list[list[Any]],
) -> list[dict[str, Any]]:
    by_mukey: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not row or len(row) < 11:
            continue
        (
            mukey, muname, musym, farmland_class,
            compname, comppct_r, hydric_rating,
            drainage_class, slope_l, slope_h, runoff,
        ) = row[:11]
        if mukey not in by_mukey:
            by_mukey[mukey] = {
                "mukey": str(mukey),
                "name": muname,
                "symbol": musym,
                "farmland_class": farmland_class,
                "components": [],
            }
        by_mukey[mukey]["components"].append({
            "component_name": compname,
            "comp_pct": _to_int(comppct_r),
            "hydric_rating": hydric_rating or "",
            "drainage_class": drainage_class or "",
            "slope_low": _to_float(slope_l),
            "slope_high": _to_float(slope_h),
            "runoff": runoff or "",
        })
    return list(by_mukey.values())


def _build_advisory(
    map_units: list[dict[str, Any]],
    any_hydric: bool,
    any_prime_farmland: bool,
    max_slope: float | None,
) -> str:
    if not map_units:
        return (
            "No SSURGO soil map unit returned for this point. "
            "Verify with a site-specific geotech investigation."
        )
    parts: list[str] = []
    names = [
        f"{mu.get('symbol')} ({(mu.get('name') or '?')[:50]})"
        for mu in map_units[:3]
    ]
    parts.append(f"Soil map unit(s): {'; '.join(names)}.")
    flags: list[str] = []
    if any_hydric:
        flags.append(
            "hydric component(s) present — wetland "
            "delineation likely required"
        )
    if any_prime_farmland:
        flags.append(
            "prime-farmland classification — Farmland "
            "Protection Policy Act review applies to any "
            "federally-funded or permitted project"
        )
    if max_slope is not None and max_slope >= 15:
        flags.append(
            f"max component slope {max_slope:.0f}% — hill-"
            "country overlay / grading-cost premium"
        )
    if flags:
        parts.append("Flags: " + "; ".join(flags) + ".")
    parts.append(
        "SSURGO is broad-acre mapping; site-specific geotech, "
        "OSSF evaluation, or wetland delineation is required "
        "for any binding decision."
    )
    return " ".join(parts)


def _no_coordinates_result() -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
        normalized_facts={
            "status": "unknown",
            "map_units": [], "map_units_count": 0,
            "any_hydric_component": False,
            "any_prime_farmland": False,
            "max_component_slope_pct": None,
            "web_soil_survey_url": _WEB_SOIL_SURVEY_URL,
            "advisory": (
                "Coordinates required for SSURGO soil "
                "lookup. Use Web Soil Survey directly."
            ),
            "contradiction_flags": [],
        },
        identifier_used="address",
        fetched_at=datetime.now(UTC),
        errors=["lat/lng required for SSURGO query"],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_SOIL_UPLOAD_TYPES),
            reason=(
                "Coordinates unavailable. Pull a Web Soil "
                "Survey area-of-interest report manually at "
                f"{_WEB_SOIL_SURVEY_URL}."
            ),
        ),
    )


def _fetch_failed_result(error_msg: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "status": "unknown",
            "map_units": [], "map_units_count": 0,
            "any_hydric_component": False,
            "any_prime_farmland": False,
            "max_component_slope_pct": None,
            "web_soil_survey_url": _WEB_SOIL_SURVEY_URL,
            "advisory": (
                "NRCS SDA did not respond. Pull a soil "
                "report manually from Web Soil Survey."
            ),
            "contradiction_flags": [],
        },
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        errors=[f"NRCS SDA query failed: {error_msg}"[:500]],
        manual_upload_request=ManualUploadRequest(
            document_types=list(_SOIL_UPLOAD_TYPES),
            reason=(
                "NRCS SDA service did not respond. Pull a "
                "Web Soil Survey report manually at "
                f"{_WEB_SOIL_SURVEY_URL}."
            ),
        ),
    )


__all__ = ["SOURCE_ID", "fetch"]
