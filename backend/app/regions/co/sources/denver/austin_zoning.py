"""Denver's own zoning for the zoning card (spec 2.6): the zone district at the site.

Source: Denver Open Data ZONE_ZONING_A (the official zoning map). The district
code ("D-C", "U-SU-C") goes in both `zoning_type` and `base_zone`, because the
orchestrator hands it on to the zoning-standards source.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.regions.arcgis import query_near, query_point
from app.regions.co.sources.denver import _planning as p
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "austin_zoning"
PROVIDER_NAME = p.PROVIDER_NAME
PROVIDER_SHORT = p.PROVIDER_SHORT
VERSION = 1
LAYER = p.layer("ODC_ZONE_ZONING_A", 209)
MAP_URL = "https://www.denvergov.org/maps/map/zoning"
_FIELDS = (
    "ZONE_DISTRICT,ZONE_DESCRIPTION,ZONE_DIST_TYPE,NBHD_CONTEXT,OVERLAY_DISTRICT,"
    "PUD_NUM,ORD_NUM,ORD_YEAR,ZCODE_VERSION,HEIGHT_STORIES,ADU,WAIVERS,CONDITIONS,NOTES"
)
_ROW_ENVELOPE_M = 30  # a point in the street still gets the nearest district


def _describe(row: dict) -> str:
    parts = [" ".join(p.text(row.get("ZONE_DESCRIPTION")).split())]
    if p.text(row.get("NBHD_CONTEXT")):
        parts.append(f"{p.text(row['NBHD_CONTEXT']).title()} neighborhood context")
    if p.text(row.get("ZONE_DIST_TYPE")):
        parts.append(p.text(row["ZONE_DIST_TYPE"]))
    if p.text(row.get("OVERLAY_DISTRICT")):
        parts.append(f"Overlay district {p.text(row['OVERLAY_DISTRICT'])}")
    if p.text(row.get("PUD_NUM")):
        parts.append(f"Planned unit development {p.text(row['PUD_NUM'])}: its own standards apply")
    if p.text(row.get("ADU")):
        parts.append(f"Accessory dwelling units: {p.text(row['ADU'])}")
    return ". ".join(x for x in parts if x)


def fetch(identity, session=None, config=None) -> AdapterResult:
    base = p.source_facts(MAP_URL, "unknown")
    lat, lng = identity.query_point()
    if lat is None or lng is None:
        return p.no_coordinates(SOURCE_ID, base)
    try:
        rows = query_point(LAYER, lat, lng, fields=_FIELDS, session=session)
        anchor = "point"
        if not rows:
            rows = query_near(LAYER, lat, lng, _ROW_ENVELOPE_M, fields=_FIELDS, session=session)
            anchor = f"nearest_within_{_ROW_ENVELOPE_M}m"
        as_of = p.data_as_of(LAYER, session)
    except Exception as exc:
        return p.failed(SOURCE_ID, base, "Denver zoning map", exc)

    zones = [p.text(r.get("ZONE_DISTRICT")) for r in rows if p.text(r.get("ZONE_DISTRICT"))]
    found = list(dict.fromkeys(zones))
    facts = {
        **p.source_facts(LAYER, as_of),
        "jurisdiction": "Denver",
        "jurisdiction_kind": "city",
        "zones_found": found,
        "source": "Denver Open Data, zoning map (ZONE_ZONING_A)",
        "manual_lookup_url": MAP_URL,
        "zoning_source_url": MAP_URL,
        "contradiction_flags": [],
        "query_anchor": anchor,
        "raw_records": [
            {"zoning_type": r.get("ZONE_DISTRICT"), "base_zone": r.get("ZONE_DISTRICT"),
             "description": _describe(r), "jurisdiction": "Denver", "raw": r}
            for r in rows
        ],
    }
    if not rows or not found:
        facts.update(status="unknown", advisory=(
            "Denver's zoning map shows no district at this point. That does not mean the site "
            "is unzoned; Denver's zoning map or a zoning letter settles it."))
        return _result(facts, DataConfidenceStatus.UNKNOWN, CoverageStatus.PARTIAL)

    first = rows[0]
    overlays = sorted({p.text(r.get("OVERLAY_DISTRICT")) for r in rows if p.text(r.get("OVERLAY_DISTRICT"))})
    facts.update(
        status="clear" if len(found) == 1 else "mixed",
        zoning_type=found[0], base_zone=found[0],
        description=_describe(first),
        overlay_districts=overlays,
        accessory_dwelling_units=p.text(first.get("ADU")) or None,
        height_stories=first.get("HEIGHT_STORIES"),
        ordinance=(f"{first['ORD_NUM']}/{first['ORD_YEAR']}" if first.get("ORD_NUM") else None),
        zoning_code_edition=first.get("ZCODE_VERSION"),
        advisory=(
            f"Denver zoning map: {found[0]}."
            + (" The point is not inside a mapped district; the nearest one within "
               f"{_ROW_ENVELOPE_M} m is shown." if anchor != "point" else "")
            + (f" Overlay: {', '.join(overlays)}." if overlays else "")
        ),
    )
    confidence = (DataConfidenceStatus.VERIFIED_PUBLIC_FACT if len(found) == 1 and anchor == "point"
                  else DataConfidenceStatus.SCREENED_INDICATOR)
    return _result(facts, confidence, CoverageStatus.COVERED)


def _result(facts, confidence, coverage) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=confidence,
        coverage_status=coverage,
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
    )
