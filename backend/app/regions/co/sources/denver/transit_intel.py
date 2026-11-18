"""RTD (Denver) bus stops and rail stations for the Transit card (spec 2.6).

Both layers are RTD's own data, hosted by CDOT on ArcGIS Online. They are
old: the bus-stop layer was last edited 2022-03-18 and the rail layer
2021-09-14, so `data_as_of` says so and the advisory tells the reader to
confirm current service with RTD.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.regions.arcgis import query_near
from app.services.site_check.contract import (
    AdapterResult,
    CoverageStatus,
    DataConfidenceStatus,
    SourceAccessStatus,
)

log = logging.getLogger("propdesk.co.transit_intel")

SOURCE_ID = "transit_intel"
PROVIDER_NAME = "Regional Transportation District (RTD)"
PROVIDER_SHORT = "RTD"
VERSION = 1

_BASE = "https://services.arcgis.com/yzB9WM8W0BO3Ql7d/ArcGIS/rest/services"
BUS_LAYER = f"{_BASE}/RTD%20Bus%20Stops/FeatureServer/0"
RAIL_LAYER = f"{_BASE}/Light%20Rail%20Stations/FeatureServer/0"
DATA_AS_OF = "2021–2022"  # rail layer edited 2021-09-14, bus layer 2022-03-18

_BUS_BUFFER_MI = 1.0
_RAIL_BUFFER_MI = 3.0
_M_PER_MI = 1609.34

_ADVISORY = (
    "Distances are straight-line, not walking. RTD's published stop and "
    "station layers were last updated in 2021-2022, so confirm current "
    "routes and frequency with RTD."
)


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def _point(row: dict) -> tuple[float | None, float | None]:
    geom = row.get("_geometry") or {}
    return geom.get("y"), geom.get("x")


def _bus_item(row: dict) -> dict | None:
    lat, lng = _point(row)
    if lat is None or lng is None or row.get("_distance_m") is None:
        return None
    return {
        "stop_id": row.get("BSID"),
        "name": _clean(row.get("STOPNAME")) or "Unnamed stop",
        "on_street": _clean(row.get("ON_STREET")),
        "at_street": _clean(row.get("AT_STREET")),
        "routes": _clean(row.get("ROUTES")),
        "distance_mi": round(row["_distance_m"] / _M_PER_MI, 2),
        "lat": lat, "lng": lng,
    }


def _rail_item(row: dict) -> dict | None:
    lat, lng = _point(row)
    if lat is None or lng is None or row.get("_distance_m") is None:
        return None
    return {
        "stop_id": row.get("PID"),
        "name": _clean(row.get("NAME")) or "Unnamed station",
        "city": _clean(row.get("CITY")),
        "zip": _clean(row.get("ZIPCODE")) or None,
        "lines": _clean(row.get("SPECIAL_RT")),
        "distance_mi": round(row["_distance_m"] / _M_PER_MI, 2),
        "lat": lat, "lng": lng,
    }


def fetch(identity, session=None, config=None) -> AdapterResult:
    if identity.lat is None or identity.lng is None:
        return AdapterResult(
            source_id=SOURCE_ID,
            source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
            data_confidence_status=DataConfidenceStatus.UNKNOWN,
            coverage_status=CoverageStatus.MANUAL_VERIFICATION_REQUIRED,
            normalized_facts={
                "status": "unknown",
                "provider_name": PROVIDER_NAME, "provider_short": PROVIDER_SHORT,
                "source_url": BUS_LAYER, "data_as_of": DATA_AS_OF,
                "advisory": "Coordinates are required for the RTD stop lookup.",
            },
            fetched_at=datetime.now(UTC),
            errors=["lat/lng required for RTD query"],
        )

    errors: list[str] = []
    facts: dict = {
        "provider_name": PROVIDER_NAME, "provider_short": PROVIDER_SHORT,
        "source_url": BUS_LAYER, "data_as_of": DATA_AS_OF,
        "advisory": _ADVISORY, "contradiction_flags": [],
    }

    bus = rail = None
    try:
        rows = query_near(
            BUS_LAYER, identity.lat, identity.lng, _BUS_BUFFER_MI * _M_PER_MI,
            where="STATUS='active'", keep_geometry=True, session=session, max_records=2000,
        )
        bus = [i for i in map(_bus_item, rows) if i]
        facts.update(
            bus_stops_count=len(bus), nearest_bus_stop=bus[0] if bus else None,
            bus_stops=bus[:10], bus_buffer_mi=_BUS_BUFFER_MI,
        )
    except Exception as exc:  # provider down: say so, never a false zero
        log.warning("RTD bus stop query failed: %s", exc)
        errors.append(f"RTD bus stops: {str(exc)[:200]}")
        facts.setdefault("parts", {})["bus_stops"] = {
            "coverage": "not_connected",
            "note": "RTD's bus stop layer did not answer; stops were not checked.",
        }
    try:
        rows = query_near(
            RAIL_LAYER, identity.lat, identity.lng, _RAIL_BUFFER_MI * _M_PER_MI,
            keep_geometry=True, session=session, max_records=500,
        )
        rail = [i for i in map(_rail_item, rows) if i]
        facts.update(
            rail_stations_count=len(rail),
            nearest_rail_station=rail[0] if rail else None,
            rail_stations=rail[:5], rail_buffer_mi=_RAIL_BUFFER_MI,
        )
    except Exception as exc:
        log.warning("RTD rail station query failed: %s", exc)
        errors.append(f"RTD rail stations: {str(exc)[:200]}")
        facts.setdefault("parts", {})["rail_stations"] = {
            "coverage": "not_connected",
            "note": "RTD's rail station layer did not answer; stations were not checked.",
        }

    if bus is None and rail is None:
        facts["status"] = "unknown"
        coverage = CoverageStatus.MANUAL_VERIFICATION_REQUIRED
        confidence = DataConfidenceStatus.UNKNOWN
    else:
        facts["status"] = "clear" if (bus or rail) else "no_transit_in_buffer"
        coverage = (CoverageStatus.COVERED if not errors else CoverageStatus.PARTIAL)
        confidence = DataConfidenceStatus.SCREENED_INDICATOR

    return AdapterResult(
        source_id=SOURCE_ID,
        source_access_status=SourceAccessStatus.PROGRAMMATIC_PUBLIC,
        data_confidence_status=confidence,
        coverage_status=coverage,
        normalized_facts=facts,
        identifier_used="lat_lng",
        fetched_at=datetime.now(UTC),
        advisories=[_ADVISORY],
        errors=errors or None,
    )
