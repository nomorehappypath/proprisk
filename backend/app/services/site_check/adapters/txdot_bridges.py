"""txdot_bridges adapter — TxDOT Bridges within radius_mi.

Ported from legacy_services/core/public_intel.py
::fetch_txdot_bridges. Returns count + nearest distance + per-
bridge condition / posted weight / inventory rating. Used by
the Site Analysis truck_traffic scorer for posted bridge
weight restrictions on the access route.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Any

import requests

from app.services.site_check.adapters._base import (
    AdapterResult,
    PropertyIdentity,
    http_session,
)
from app.services.site_check.adapters._txdot_common import (
    _MILES_TO_METERS,
    _TIMEOUT_S,
    _TXDOT_BRIDGES,
)
from app.services.site_check.contract import (
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

SOURCE_ID = "txdot_bridges"
_EARTH_RADIUS_MI = 3958.8


def _haversine_mi(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return _EARTH_RADIUS_MI * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _cond_val(c: Any) -> int:
    if c and str(c).isdigit():
        return int(c)
    return 9


def fetch(
    identity: PropertyIdentity,
    *,
    session: requests.Session | None = None,
    config=None,
    radius_mi: float = 5.0,
) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return _failed("lat/lng required for txdot_bridges")

    sess = http_session(session)
    params = {
        "where": "1=1",
        "geometry": f"{identity.lng},{identity.lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "distance": int(radius_mi * _MILES_TO_METERS),
        "units": "esriSRUnit_Meter",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "BRDG_ID,FACLTY_CARRD_BY_STRUC,"
            "FEAT_INTSECTD,YR_BLT,"
            "DECK_COND_RTNG,SUPERSTRUC_COND_RTNG,SUBSTRUC_COND_RTNG,"
            "INV_RTNG_GROSS_LDNG_TONS,"
            "LDNG_RSTRCN,LDNG_RSTRCN_THOUS_LBS,"
            "INSPT_DT,OPRTL_STAT,MAINT_RSPNBL,"
            "LAT_DD,LONG_DD,BRDG_DESC,LOCN"
        ),
        "returnGeometry": "false",
        "resultRecordCount": 50,
        "f": "json",
    }
    try:
        resp = sess.get(_TXDOT_BRIDGES, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return _failed(f"txdot bridges query failed: {exc}")

    if "error" in data:
        msg = data["error"].get("message", str(data["error"]))
        return _failed(f"bridges: ArcGIS error: {msg}")
    time.sleep(0.5)

    items: list[dict] = []
    for f in data.get("features", []):
        attr = f.get("attributes", {})
        b_lat = attr.get("LAT_DD")
        b_lon = attr.get("LONG_DD")
        dist = None
        if b_lat and b_lon:
            dist = round(_haversine_mi(
                identity.lat, identity.lng,
                float(b_lat), float(b_lon),
            ), 2)

        posted_raw = str(
            attr.get("LDNG_RSTRCN_THOUS_LBS") or "",
        ).strip()
        posted_wt = None
        if (
            posted_raw and posted_raw != "NNN"
            and posted_raw.isdigit() and int(posted_raw) > 0
        ):
            posted_wt = int(posted_raw) / 2.0

        inv_raw = attr.get("INV_RTNG_GROSS_LDNG_TONS")
        inv_tons = None
        try:
            if inv_raw and float(inv_raw) > 0:
                inv_tons = round(float(inv_raw) * 1.1023, 1)
        except (ValueError, TypeError):
            pass

        deck_c = attr.get("DECK_COND_RTNG")
        super_c = attr.get("SUPERSTRUC_COND_RTNG")
        sub_c = attr.get("SUBSTRUC_COND_RTNG")
        worst = min(_cond_val(deck_c), _cond_val(super_c), _cond_val(sub_c))

        items.append({
            "bridge_id": attr.get("BRDG_ID") or "",
            "name": (
                attr.get("BRDG_DESC")
                or attr.get("FACLTY_CARRD_BY_STRUC")
                or "Unnamed bridge"
            ),
            "carries": attr.get("FACLTY_CARRD_BY_STRUC") or "",
            "intersects": attr.get("FEAT_INTSECTD") or "",
            "year_built": attr.get("YR_BLT"),
            "deck_cond": deck_c,
            "super_cond": super_c,
            "sub_cond": sub_c,
            "worst_cond_rtg": worst,
            "posted_weight_us_tons": posted_wt,
            "inventory_rating_us_tons": inv_tons,
            "load_restriction": attr.get("LDNG_RSTRCN"),
            "distance_mi": dist,
        })

    items.sort(key=lambda x: x.get("distance_mi") or 999)
    nearest = items[0]["distance_mi"] if items else None

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=(
            DataConfidenceStatus.VERIFIED_PUBLIC_FACT
            if items else DataConfidenceStatus.UNKNOWN
        ),
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={
            "count_within_radius": len(items),
            "nearest_distance_mi": nearest,
            "items": items,
            "source": "TxDOT Bridges",
        },
        fetched_at=datetime.now(UTC),
        identifier_used="lat_lng",
    )


def _failed(reason: str) -> AdapterResult:
    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC_BUT_FRAGILE,
        data_confidence_status=DataConfidenceStatus.UNKNOWN,
        coverage_status=CoverageStatus.COVERED,
        normalized_facts={},
        errors=[reason],
        fetched_at=datetime.now(UTC),
    )
