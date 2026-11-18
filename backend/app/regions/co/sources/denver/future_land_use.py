"""Denver's Blueprint Denver future place for the future-land-use card (spec 2.6)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.regions.arcgis import query_point
from app.regions.co.sources.denver import _planning as p
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "future_land_use"
PROVIDER_NAME = p.PROVIDER_NAME
PROVIDER_SHORT = p.PROVIDER_SHORT
VERSION = 1
LAYER = p.layer("ODC_PLAN_BPFUTUREPLACES_A", 101)
PLAN_NAME = "Blueprint Denver (adopted 2019)"
PLAN_URL = "https://denvergov.org/Government/Citywide-Programs-and-Initiatives/Blueprint-Denver"


def _designation(row: dict) -> dict:
    place = p.text(row.get("PLCTYPE_FUT"))
    context = p.text(row.get("FUT_NBHD_CONTEXT")).title()
    growth = p.text(row.get("GROWTH_FUTURE"))
    return {
        # A bare "Low" reads oddly on the card, so the context is part of the label.
        "designation": f"{place} ({context} context)" if place and context else place,
        "place_type": place,
        "neighborhood_context": context,
        "neighborhood_planning_area": "",
        "overlays": [f"Growth strategy: {growth}"] if growth else [],
        "raw_attributes": row,
    }


def fetch(identity, session=None, config=None) -> AdapterResult:
    base = p.source_facts(PLAN_URL, "unknown")
    lat, lng = identity.query_point()
    if lat is None or lng is None:
        return p.no_coordinates(SOURCE_ID, {**base, "jurisdiction": "denver"})
    try:
        rows = query_point(LAYER, lat, lng, session=session)
        as_of = p.data_as_of(LAYER, session)
    except Exception as exc:
        return p.failed(SOURCE_ID, {**base, "jurisdiction": "denver"}, "Blueprint Denver map", exc)
    designations = [d for d in (_designation(r) for r in rows) if d["designation"]]
    status = "unknown" if not designations else ("clear" if len(designations) == 1 else "mixed")
    facts = {
        **p.source_facts(LAYER, as_of),
        "status": status,
        "jurisdiction": "denver",
        "designations": designations,
        "plan_name": PLAN_NAME,
        "plan_url": PLAN_URL,
        "advisory": (
            "Blueprint Denver future place: " + ", ".join(d["designation"] for d in designations)
            + ". It guides future rezonings; it does not change today's zoning."
            if designations else
            "Blueprint Denver's map shows no future place at this point (streets are not mapped)."
        ),
        "contradiction_flags": [],
    }
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(DataConfidenceStatus.VERIFIED_PUBLIC_FACT if status == "clear"
                                else DataConfidenceStatus.SCREENED_INDICATOR if designations
                                else DataConfidenceStatus.UNKNOWN),
        coverage_status=CoverageStatus.COVERED if designations else CoverageStatus.PARTIAL,
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
    )
