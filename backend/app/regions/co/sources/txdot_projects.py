"""Colorado's road projects for the Road Projects card (spec 2.6).

Sources: CDOT GoSheet_Projects and TenYearPlan_Approved_Lines, both within
five miles. CDOT publishes cost only as a range ("$1,000,000 to
$5,000,000"), so it goes in `cost_range` as published and
`estimated_cost_usd` is never filled. No award or contractor data exists
in either layer. GoSheet's "ad" date (when bidding opens) fills the bid date.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.regions import arcgis
from app.regions.co.sources import _roads as r
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "txdot_projects"
PROVIDER_NAME = "Colorado Department of Transportation"
PROVIDER_SHORT = "CDOT"
VERSION = 1
GOSHEET = f"{r.CDOT_ROOT}/GoSheet_Projects/FeatureServer/0"
TEN_YEAR = f"{r.CDOT_ROOT}/TenYearPlan_Approved_Lines/FeatureServer/0"
RADIUS_M = 8047  # five miles


def _place(row: dict, lat: float, lng: float) -> dict:
    geometry = row.get("_geometry")
    dist = r.distance_m(lat, lng, geometry)
    point = r.nearest_point(lat, lng, geometry)
    return {
        "distance_mi": round(dist / r.MILE_M, 2) if dist is not None else None,
        "lat": point[0] if point else None,
        "lng": point[1] if point else None,
        "geometry_basis": "public_project_geometry_center" if point else None,
        "_d": dist if dist is not None else 1e12,
    }


def _gosheet_item(row: dict, lat: float, lng: float) -> dict:
    where = r.clean_text(row.get("ProjectLocation"))
    work = r.clean_text(row.get("ProposalOfWork"))
    description = " - ".join(x for x in (where, work) if x) or "CDOT project"
    ad_sched = r.epoch_date(row.get("ScheduledAd_Date"))
    ad_actual = r.epoch_date(row.get("ActualAd_Date"))
    letting = r.epoch_date(row.get("Letting_Date"))
    cost_range = r.clean_text(row.get("BudgetRange"))
    region = r.clean_text(row.get("Region"))
    return {
        "csj": r.clean_text(row.get("ProjectNumber") or row.get("PROJECT_ID")),
        "project_id": r.clean_text(row.get("PROJECT_ID")),
        "description": description,
        "phase": "",
        "work_type": work or None,
        "limits_from": None,
        "limits_to": None,
        "estimated_let_date": letting or ad_actual or ad_sched,
        "scheduled_let_date": ad_sched,
        "actual_let_date": ad_actual or letting,
        "commission_award_date": None,
        "estimated_cost_usd": None,
        "cost_range": cost_range or None,
        "district": f"Region {region}" if region else "",
        "district_number": r.int_or_none(region),
        "county": r.clean_text(row.get("County")),
        "project_stage": None,
        "project_status": None,
        "let_type": r.clean_text(row.get("DeliveryMethod")) or None,
        "project_length_mi": None,
        "mpo": None,
        "program": "CDOT GoSheet",
        **_place(row, lat, lng),
    }


def _ten_year_item(row: dict, lat: float, lng: float) -> dict:
    name = r.clean_text(row.get("tenyearprojectname")) or "CDOT ten-year plan project"
    corridor = r.clean_text(row.get("corridor"))
    link = r.clean_text(row.get("adaptiveformtablewebsite"))
    item = {
        "csj": r.clean_text(row.get("stip_id") or row.get("planningprojectid")),
        "project_id": r.clean_text(row.get("planningprojectid")),
        "description": f"{corridor} - {name}" if corridor and corridor not in name else name,
        "phase": r.clean_text(row.get("planningprojectidstatus")),
        "work_type": r.clean_text(row.get("projecttype")) or None,
        "limits_from": None,
        "limits_to": None,
        "estimated_let_date": None,
        "scheduled_let_date": None,
        "actual_let_date": None,
        "commission_award_date": None,
        "estimated_cost_usd": None,
        "cost_range": None,
        "district": f"Region {row['cdotregion']}" if row.get("cdotregion") else "",
        "district_number": r.int_or_none(row.get("cdotregion")),
        "county": r.clean_text(row.get("county")).replace(";", ", "),
        "project_stage": None,
        "project_status": None,
        "let_type": None,
        "project_length_mi": None,
        "mpo": r.clean_text(row.get("mpos")) or None,
        "program": "CDOT Ten-Year Plan",
        **_place(row, lat, lng),
    }
    if link:
        item["official_lookup_links"] = [{"label": "CDOT project page", "url": link}]
    return item


def _nearest_per_project(items: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for item in items:
        key = f"{item['program']}|{item['project_id'] or item['csj'] or item['description']}"
        if key not in best or item["_d"] < best[key]["_d"]:
            best[key] = item
    out = sorted(best.values(), key=lambda i: i["_d"])
    for item in out:
        item.pop("_d")
    return out


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return r.failed(SOURCE_ID, "lat/lng required for txdot_projects")
    lat, lng = identity.lat, identity.lng
    errors: list[str] = []
    items: list[dict] = []
    refreshed: list[str] = []
    for url, build, label in (
        (GOSHEET, _gosheet_item, "GoSheet projects"),
        (TEN_YEAR, _ten_year_item, "ten-year plan projects"),
    ):
        try:
            rows = arcgis.query_near(
                url, lat, lng, RADIUS_M, keep_geometry=True,
                session=session, max_records=1000,
            )
        except Exception as exc:
            errors.append(f"CDOT {label} unavailable: {str(exc)[:160]}")
            continue
        refreshed += [r.epoch_date(x.get("Refresh_Date")) or "" for x in rows]
        items += [build(x, lat, lng) for x in rows]
    if errors and not items:
        return r.failed(SOURCE_ID, "; ".join(errors))
    items = _nearest_per_project(items)
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT if items and not errors
            else DataConfidenceStatus.SCREENED_INDICATOR if items
            else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.PARTIAL if errors else CoverageStatus.COVERED,
        normalized_facts={
            "count_within_radius": len(items),
            "items": items,
            "projects": items,
            "source": PROVIDER_SHORT,
            "provider_name": PROVIDER_NAME,
            "provider_short": PROVIDER_SHORT,
            "source_url": GOSHEET,
            "data_as_of": max((d for d in refreshed if d), default="unknown"),
            "coverage_note": (
                "CDOT publishes project cost only as a range and publishes no "
                "award or contractor data. Bid dates are CDOT's advertisement "
                "dates. Projects led by the city of Denver may not appear."
            ),
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
        errors=errors or None,
        advisories=errors or None,
    )
