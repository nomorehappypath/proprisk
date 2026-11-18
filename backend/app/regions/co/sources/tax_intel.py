"""Colorado's tax card (slot tax_intel, spec 2.6).

DOLA's property tax map: every taxing unit whose area contains the site, with
its certified mill levy and the part of it that is bond levy. A bond levy is
what voters approved to repay bonds; it is not the amount of debt outstanding.

New keys (Texas has none of them): levies [{name, type, total_mills, bond_mills,
tax_year, url}], total_mills, tax_year. Texas debt-report keys (results,
districts_matched, comptroller_portal_url) are not filled.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.regions import arcgis
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "tax_intel"
PROVIDER_NAME = "Colorado Department of Local Affairs"
PROVIDER_SHORT = "DOLA"
VERSION = 1

PROPERTY_TAX_MAP = (
    "https://services3.arcgis.com/DgjqnJA1rgO92Soi/arcgis/rest/services/"
    "PropTaxMap2025/FeatureServer/22"
)
_FIELDS = ("Local_Government_Name,Local_Government_Type,lgid,TAX_YEAR,"
           "TOTAL_LEVY,GENERAL_LEVY,BOND_LEVY,OVERRIDE_LEVY,URL,SUBDISTRICT_NUM")
_SOURCE_URL = "https://dola.colorado.gov/propertytax/"


def _mills(value) -> float:
    return round(float(value), 3) if isinstance(value, (int, float)) else 0.0


def fetch(identity, session=None, config=None) -> AdapterResult:
    def result(facts, confidence, coverage, errors=None):
        facts.update(provider_name=PROVIDER_NAME, provider_short=PROVIDER_SHORT,
                     source_url=_SOURCE_URL, contradiction_flags=[])
        return AdapterResult(
            source_id=SOURCE_ID, source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=confidence, coverage_status=coverage,
            normalized_facts=facts, identifier_used="lat_lng",
            fetched_at=datetime.now(UTC), advisories=[facts["advisory"]], errors=errors)

    if identity.lat is None or identity.lng is None:
        return result({"status": "unknown", "levies": [], "data_as_of": "unknown",
                       "advisory": "Coordinates are required for the tax lookup."},
                      DataConfidenceStatus.UNKNOWN, CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
                      ["lat/lng required"])
    try:
        rows = arcgis.query_point(PROPERTY_TAX_MAP, identity.lat, identity.lng,
                                  fields=_FIELDS, session=session)
    except Exception as exc:
        return result({"status": "unknown", "levies": [], "data_as_of": "unknown",
                       "advisory": "The Colorado property tax map did not respond. "
                                   "Ask the county assessor for the tax district levies."},
                      DataConfidenceStatus.UNKNOWN, CoverageStatus.COVERED,
                      [f"DOLA property tax map: {str(exc)[:200]}"])

    levies, seen = [], set()
    for row in rows:
        if not isinstance(row.get("TOTAL_LEVY"), (int, float)):
            continue  # placeholder row with no certified levy
        key = (row.get("lgid"), row.get("SUBDISTRICT_NUM"))
        if key in seen:
            continue
        seen.add(key)
        levies.append({
            "name": str(row.get("Local_Government_Name") or "").strip(),
            "type": str(row.get("Local_Government_Type") or "").strip(),
            "total_mills": _mills(row["TOTAL_LEVY"]),
            "bond_mills": _mills(row.get("BOND_LEVY")),
            "tax_year": row.get("TAX_YEAR"),
            "url": str(row.get("URL") or "").strip() or None,
        })
    levies.sort(key=lambda x: -x["total_mills"])
    years = {x["tax_year"] for x in levies if x["tax_year"]}
    tax_year = str(max(years)) if years else None
    total = round(sum(x["total_mills"] for x in levies), 3)
    advisory = (
        f"{len(levies)} taxing unit(s) levy {total:g} mills in total for tax year {tax_year}. "
        "Bond mills repay voter-approved bonds; they are not the amount of debt outstanding. "
        "A parcel's actual bill depends on its assessed value; ask the county assessor."
        if levies else
        "No taxing unit with a certified levy was found for this site in the DOLA property tax map."
    )
    return result({
        "status": "clear" if levies else "no_levies",
        "levies": levies, "total_mills": total,
        "tax_year": tax_year, "fiscal_year": tax_year,
        "taxing_units": len(levies),
        "advisory": advisory, "data_as_of": tax_year or "unknown",
    }, DataConfidenceStatus.VERIFIED_PUBLIC_FACT, CoverageStatus.COVERED)
