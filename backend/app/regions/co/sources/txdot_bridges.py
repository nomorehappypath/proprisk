"""Colorado's bridges for the Bridges card (spec 2.6).

Sources: the National Bridge Inventory (FHWA, geo.dot.gov) for what each
bridge carries and crosses, year built and ratings; CDOT's
BridgeConditionforDashboard for newer condition ratings; and CDOT's
Bridge_Weight_Limit layer for postings.

A bridge is "posted" ONLY when the weight-limit layer has a record for it.
No record means posting is unknown (the layer covers state-highway
structures only), never "not posted": `load_restriction` is "Y" or
"unknown", never "N". The inventory's own posting evaluation field is not
a posting and is not used.
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

SOURCE_ID = "txdot_bridges"
PROVIDER_NAME = "FHWA National Bridge Inventory and Colorado Department of Transportation"
PROVIDER_SHORT = "NBI / CDOT"
VERSION = 1
NBI = "https://geo.dot.gov/server/rest/services/Hosted/National_Bridge_Inventory/FeatureServer/0"
CONDITION = f"{r.CDOT_ROOT}/BridgeConditionforDashboard/FeatureServer/0"
WEIGHT_LIMIT = f"{r.CDOT_ROOT}/Bridge_Weight_Limit/FeatureServer/0"
RADIUS_M = 8047  # five miles
MAX_ITEMS = 50
NBI_AS_OF = "2023-06-15"  # the layer's own description: "as of June 15, 2023"
_NBI_FIELDS = (
    "structure_,facility_c,features_d,location_0,year_built,deck_cond_,superstruc,"
    "substructu,culvert_co,inventory_,operating_,bridge_con"
)
_COND_FIELDS = (
    "BRKEY,YEAR,DKRATING,SUPRATING,SUBRATING,CULVRATING,OVERALL_COND,INSPDATE"
)


def _rating(value) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else None  # "N" = not applicable


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return r.failed(SOURCE_ID, "lat/lng required for txdot_bridges")
    lat, lng = identity.lat, identity.lng
    try:
        nbi_rows = arcgis.query_near(
            NBI, lat, lng, RADIUS_M, where="state_code='08'", fields=_NBI_FIELDS,
            session=session, max_records=2000,
        )
    except Exception as exc:
        return r.failed(SOURCE_ID, f"National Bridge Inventory query failed: {exc}")
    warnings: list[str] = []

    weight: dict[str, dict] = {}
    posting_known_layer = True
    try:
        for row in arcgis.query_near(
            WEIGHT_LIMIT, lat, lng, RADIUS_M + 500, session=session, max_records=500,
        ):
            weight.setdefault(r.clean_text(row.get("BRKEY")), row)
    except Exception as exc:
        posting_known_layer = False
        warnings.append(f"CDOT weight-limit layer unavailable: {str(exc)[:160]}")

    # One base record per bridge: the nearest NBI structures, plus any posted
    # structure CDOT lists that the (2023) inventory does not carry under the
    # same key, so a posting is never hidden.
    bases = []
    for row in nbi_rows[:MAX_ITEMS]:
        bases.append({
            "key": r.clean_text(row.get("structure_")),
            "carries": r.clean_text(row.get("facility_c")),
            "intersects": r.clean_text(row.get("features_d")),
            "name": r.clean_text(row.get("location_0") or row.get("facility_c")),
            "year_built": row.get("year_built") or None,
            "nbi": [row.get("deck_cond_"), row.get("superstruc"),
                    row.get("substructu"), row.get("culvert_co")],
            "nbi_overall": {"G": "Good", "F": "Fair", "P": "Poor"}.get(row.get("bridge_con")),
            "inventory": row.get("inventory_"),
            "dist": row.get("_distance_m"),
        })
    known = {b["key"] for b in bases}
    extra = 0
    for key, rec in weight.items():
        if key and key not in known and (rec.get("_distance_m") or 0) <= RADIUS_M:
            extra += 1
            bases.append({
                "key": key,
                "carries": r.clean_text(rec.get("FACILITY")),
                "intersects": r.clean_text(rec.get("FEATINT")),
                "name": r.clean_text(rec.get("FACILITY")),
                "year_built": None, "nbi": [None] * 4, "nbi_overall": None,
                "inventory": None, "dist": rec.get("_distance_m"),
            })
    total = len(nbi_rows) + extra

    latest: dict[str, dict] = {}
    keys = [b["key"] for b in bases if b["key"]]
    if keys:
        quoted = ",".join("'" + k.replace("'", "''") + "'" for k in keys)
        try:
            for row in arcgis.query_near(
                CONDITION, lat, lng, RADIUS_M + 500, where=f"BRKEY IN ({quoted})",
                fields=_COND_FIELDS, session=session, max_records=2000,
            ):
                key = r.clean_text(row.get("BRKEY"))
                if key not in latest or (row.get("YEAR") or 0) > (latest[key].get("YEAR") or 0):
                    latest[key] = row
        except Exception as exc:
            warnings.append(f"CDOT bridge ratings unavailable: {str(exc)[:160]}")

    items = []
    condition_years: list[str] = []
    for base in bases:
        key = base["key"]
        cdot = latest.get(key)
        if cdot:
            ratings = [cdot.get("DKRATING"), cdot.get("SUPRATING"),
                       cdot.get("SUBRATING"), cdot.get("CULVRATING")]
            condition_years.append(str(int(cdot["YEAR"])) if cdot.get("YEAR") else "")
        else:
            ratings = base["nbi"]
        numeric = [n for n in (_rating(x) for x in ratings) if n is not None]
        inventory = base["inventory"]
        record = weight.get(key)
        dist = base["dist"]
        items.append({
            "bridge_id": key,
            "name": base["name"] or "Unnamed bridge",
            "carries": base["carries"],
            "intersects": base["intersects"],
            "year_built": base["year_built"],
            "deck_cond": ratings[0], "super_cond": ratings[1], "sub_cond": ratings[2],
            "culvert_cond": ratings[3],
            "worst_cond_rtg": min(numeric) if numeric else None,
            "overall_condition": (cdot or {}).get("OVERALL_COND") or base["nbi_overall"],
            "condition_source": "CDOT" if cdot else "NBI",
            "inspection_date": r.epoch_date((cdot or {}).get("INSPDATE")),
            "posted_weight_us_tons": None,
            "inventory_rating_us_tons": (
                round(float(inventory) * 1.1023, 1)
                if isinstance(inventory, (int, float)) and inventory > 0 else None
            ),
            "load_restriction": "Y" if record else "unknown",
            "posting_status": "posted" if record else "unknown",
            "weight_limit_class": (record or {}).get("ColorValue"),
            "distance_mi": round(dist / r.MILE_M, 2) if dist is not None else None,
        })
    items.sort(key=lambda x: x["distance_mi"] if x["distance_mi"] is not None else 999)

    posted = sum(1 for i in items if i["posting_status"] == "posted")
    years = condition_years or []
    as_of = NBI_AS_OF[:4]
    latest_year = max((y for y in years if y), default="")
    if latest_year and latest_year != as_of:
        as_of = f"{as_of}–{latest_year}"
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT if items and not warnings
            else DataConfidenceStatus.SCREENED_INDICATOR if items
            else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.PARTIAL if warnings else CoverageStatus.COVERED,
        normalized_facts={
            "count_within_radius": total,
            "nearest_distance_mi": items[0]["distance_mi"] if items else None,
            "items": items,
            "source": PROVIDER_SHORT,
            "posted_count": posted,
            "posting_data": (
                "CDOT weight-limit layer (state-highway structures)"
                if posting_known_layer else "unavailable"
            ),
            "posting_note": (
                "A bridge is shown as posted only when CDOT's weight-limit "
                "layer lists it. Any other bridge's posting is unknown, not "
                "'not posted'."
            ),
            "provider_name": PROVIDER_NAME,
            "provider_short": PROVIDER_SHORT,
            "source_url": NBI,
            "data_as_of": as_of,
            "source_warnings": warnings,
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
        advisories=warnings or None,
    )
